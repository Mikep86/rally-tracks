import asyncio
import base64
import bz2
import csv
import http.client
import json
import logging
import os
import signal
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict
from typing import Dict

import elasticsearch
from esrally.track.params import BulkIndexParamSource

logger = logging.getLogger(__name__)

Qrels = Dict[str, Dict[str, int]]
Results = Dict[str, Dict[str, float]]

TRACK_DIR: str = os.path.dirname(__file__)
QUERIES_FILENAME: str = "queries-recall-10m.json.bz2"
QRELS_FILENAME: str = "qrels.tsv"
MOCK_SERVER_SCRIPT: str = "mock_embedding_server.py"

DIMS: int = 1024
SEMANTIC_FIELD: str = "body_semantic"
# The corpus embeddings were generated from title + " " + text, but semantic_text needs offsets into a single source field.
# The chunk is attributed to "text"; offsets only affect highlighting.
CHUNK_SOURCE_FIELD: str = "text"
MODEL_SETTINGS = {"task_type": "text_embedding", "dimensions": DIMS, "similarity": "dot_product", "element_type": "float"}
DEFAULT_QUERY_FIELDS = ["title", "text", SEMANTIC_FIELD]
DEFAULT_LEXICAL_FIELDS = ["title", "text"]


def calc_ndcg(qrels: Qrels, results: Results, k_list: list, query_ids: list):
    """
    Mean nDCG@k over all query_ids. A query with no hits scores 0 instead of being dropped, which pytrec_eval would do.
    """
    import pytrec_eval as pe

    evaluator = pe.RelevanceEvaluator(qrels, {f"ndcg_cut.{','.join(str(k) for k in k_list)}"})
    per_query = evaluator.evaluate({qid: results.get(qid, {}) for qid in query_ids})
    return {
        k: sum(per_query.get(qid, {}).get(f"ndcg_cut_{k}", 0.0) for qid in query_ids) / len(query_ids) if query_ids else 0.0 for k in k_list
    }


def read_qrels(qrels_input_file):
    qrels = defaultdict(dict)
    with open(qrels_input_file, "r") as input_file:
        tsv_reader = csv.reader(input_file, delimiter="\t")
        for query_id, doc_id, score, _ in tsv_reader:
            qrels[query_id][doc_id] = int(score)
    return qrels


def read_queries(queries_input_file):
    with bz2.open(queries_input_file, "rt") as input_file:
        return [{"query_id": q["query_id"], "text": q["text"]} for q in map(json.loads, input_file)]


def utf16_length(text: str) -> int:
    # semantic_text offsets index into Java strings, which count UTF-16 code units
    return len(text.encode("utf-16-le")) // 2


class SemanticBulkParamSource:
    """
    Wraps Rally's standard bulk param source and rewrites each document so its precomputed embedding reaches the
    semantic_text field through _inference_fields, which makes Elasticsearch skip inference at index time:

    - "emb" (base64 big-endian float32) is removed and decoded into a float array.
    - _inference_fields.<semantic field> carries one chunk with that embedding, spanning the whole "text" field.
    - The bulk action line gets _id=<docid>.
    """

    def __init__(self, track, params, **kwargs):
        self._inner = BulkIndexParamSource(track, params, **kwargs)
        self._inference_id = params["inference-id"]
        self.infinite = self._inner.infinite

    @property
    def corpora(self):
        # Rally discovers which corpora to download and prepare through this attribute
        return self._inner.corpora

    def partition(self, partition_index, total_partitions):
        return _SemanticBulkPartition(self._inner.partition(partition_index, total_partitions), self._inference_id)


class _SemanticBulkPartition:
    def __init__(self, inner, inference_id):
        self._inner = inner
        self._inference_id = inference_id
        self.infinite = inner.infinite

    @property
    def percent_completed(self):
        return self._inner.percent_completed

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        p = self._inner.params()
        body = p["body"]
        body_was_str = isinstance(body, str)
        body_bytes = body.encode("utf-8") if body_was_str else body
        new_body = self._rewrite_body(body_bytes)
        p["body"] = new_body.decode("utf-8") if body_was_str else new_body
        return p

    def _rewrite_body(self, body_bytes):
        lines = [line for line in body_bytes.split(b"\n") if line]
        if len(lines) % 2 != 0:
            raise ValueError(f"Expected action/document line pairs in bulk body but got [{len(lines)}] lines")
        out = []
        for i in range(0, len(lines), 2):
            action = json.loads(lines[i])
            doc = json.loads(lines[i + 1])
            verb = next(iter(action))
            action[verb]["_id"] = doc["docid"]
            out.append(json.dumps(action, separators=(",", ":")).encode("utf-8"))
            out.append(json.dumps(self._rewrite_doc(doc), separators=(",", ":")).encode("utf-8"))
        out.append(b"")
        return b"\n".join(out)

    def _rewrite_doc(self, doc):
        embedding = struct.unpack(f">{DIMS}f", base64.b64decode(doc.pop("emb")))
        doc["_inference_fields"] = {
            SEMANTIC_FIELD: {
                "inference": {
                    "inference_id": self._inference_id,
                    "model_settings": MODEL_SETTINGS,
                    "chunks": {
                        CHUNK_SOURCE_FIELD: [
                            {
                                "start_offset": 0,
                                "end_offset": utf16_length(doc[CHUNK_SOURCE_FIELD]),
                                "embeddings": embedding,
                            }
                        ]
                    },
                }
            }
        }
        return doc


def _mock_server_paths(port):
    base = os.path.join(tempfile.gettempdir(), f"msmarco-v2-bayesian-eval-mock-{port}")
    return f"{base}.pid", f"{base}.log"


def _mock_server_healthy(port):
    # http.client rather than urllib: urllib looks up system proxy settings, which crashes forked Rally workers on macOS
    connection = http.client.HTTPConnection("localhost", port, timeout=2)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


class StartMockEmbeddingServerRunner:
    """
    Starts the mock embedding server as a detached process on the load driver so it outlives the Rally worker that
    started it. It is stopped by the stop-mock-embedding-server operation.
    """

    async def __call__(self, es, params):
        # No request goes to Elasticsearch, so mark the timing boundaries Rally needs (as its own sleep runner does)
        es.on_request_start()
        try:
            await self._start(params["port"])
        finally:
            es.on_request_end()

    async def _start(self, port):
        pid_file, log_file = _mock_server_paths(port)
        if _mock_server_healthy(port):
            logger.info("Mock embedding server already running on port [%s]", port)
            return
        process = subprocess.Popen(
            [
                sys.executable,
                os.path.join(TRACK_DIR, MOCK_SERVER_SCRIPT),
                "--queries-file",
                os.path.join(TRACK_DIR, QUERIES_FILENAME),
                "--port",
                str(port),
                "--log-file",
                log_file,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with open(pid_file, "w") as f:
            f.write(str(process.pid))
        for _ in range(60):
            if _mock_server_healthy(port):
                logger.info("Started mock embedding server on port [%s] with pid [%s], logging to [%s]", port, process.pid, log_file)
                return
            if process.poll() is not None:
                break
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Mock embedding server did not become healthy on port [{port}]; see [{log_file}]")

    def __repr__(self, *args, **kwargs):
        return "start-mock-embedding-server"


class StopMockEmbeddingServerRunner:
    async def __call__(self, es, params):
        es.on_request_start()
        try:
            self._stop(params["port"])
        finally:
            es.on_request_end()

    def _stop(self, port):
        pid_file, _ = _mock_server_paths(port)
        if not os.path.exists(pid_file):
            logger.info("No mock embedding server pid file at [%s]", pid_file)
            return
        with open(pid_file) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Stopped mock embedding server with pid [%s]", pid)
        except ProcessLookupError:
            logger.info("Mock embedding server with pid [%s] was not running", pid)
        os.remove(pid_file)

    def __repr__(self, *args, **kwargs):
        return "stop-mock-embedding-server"


class PutMockInferenceEndpointRunner:
    """
    (Re)creates a custom-service text_embedding endpoint pointing at the mock embedding server. Its dimensions and
    similarity match the model_settings written into _inference_fields at index time.
    """

    async def __call__(self, es, params):
        inference_id = params["inference-id"]
        path = f"/_inference/text_embedding/{inference_id}"
        try:
            await es.perform_request(method="DELETE", path=path, params={"force": "true"})
        except elasticsearch.NotFoundError:
            pass
        await es.perform_request(
            method="PUT",
            path=path,
            headers={"Content-Type": "application/json"},
            body={
                "service": "custom",
                "service_settings": {
                    "url": f"http://{params['host']}:{params['port']}/embed",
                    "request": '{"input": ${input}}',
                    "response": {"json_parser": {"text_embeddings": "$.embeddings[*].embedding"}},
                    "dimensions": DIMS,
                    "similarity": MODEL_SETTINGS["similarity"],
                },
            },
        )

    def __repr__(self, *args, **kwargs):
        return "put-mock-inference-endpoint"


def build_search_body(strategy_params, text, size):
    strategy = strategy_params["strategy"]
    fields = strategy_params.get("fields", DEFAULT_QUERY_FIELDS)
    if strategy == "lexical":
        body = {
            "query": {
                "multi_match": {"query": text, "type": "best_fields", "fields": strategy_params.get("fields", DEFAULT_LEXICAL_FIELDS)}
            }
        }
    elif strategy == "semantic":
        body = {"query": {"semantic": {"field": strategy_params.get("field", SEMANTIC_FIELD), "query": text}}}
    elif strategy == "bayesian":
        body = {"query": {"multi_match": {"query": text, "type": "bayesian", "fields": fields}}}
    elif strategy == "rrf":
        rrf = {"fields": fields, "query": text, "rank_window_size": strategy_params.get("rank_window_size", size)}
        if "rank_constant" in strategy_params:
            rrf["rank_constant"] = strategy_params["rank_constant"]
        body = {"retriever": {"rrf": rrf}}
    elif strategy == "linear":
        linear = {"fields": fields, "query": text, "rank_window_size": strategy_params.get("rank_window_size", size)}
        if "normalizer" in strategy_params:
            linear["normalizer"] = strategy_params["normalizer"]
        body = {"retriever": {"linear": linear}}
    else:
        raise ValueError(f"Unknown strategy [{strategy}]; expected one of lexical, semantic, bayesian, rrf, linear")
    body["size"] = size
    body["_source"] = False
    return body


class RelevanceParamSource:
    def __init__(self, track, params, **kwargs):
        if len(track.indices) == 1:
            default_index = track.indices[0].name
        else:
            default_index = "_all"
        self._index_name = params.get("index", default_index)
        self._size = params.get("size", 100)
        self._strategy_params = params["strategy-params"]
        self._queries = read_queries(os.path.join(TRACK_DIR, QUERIES_FILENAME))
        self._qrels = read_qrels(os.path.join(TRACK_DIR, QRELS_FILENAME))
        self.infinite = True

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        return {
            "index": self._index_name,
            "size": self._size,
            "strategy-params": self._strategy_params,
            "queries": self._queries,
            "qrels": self._qrels,
        }


class RelevanceRunner:
    """
    Runs every judged query with one retrieval strategy and reports mean nDCG@10 and nDCG@100 against the qrels.
    """

    async def __call__(self, es, params):
        strategy_params = params["strategy-params"]
        size = params["size"]
        results = defaultdict(dict)
        query_ids = []
        for query in params["queries"]:
            query_ids.append(query["query_id"])
            # perform_request rather than es.search: the client bundled with Rally rejects body keys it doesn't know, like retriever
            response = await es.perform_request(
                method="POST",
                path=f"/{params['index']}/_search",
                params={"request_cache": "false"},
                headers={"Content-Type": "application/json"},
                body=build_search_body(strategy_params, query["text"], size),
            )
            for hit in response["hits"]["hits"]:
                results[query["query_id"]][hit["_id"]] = hit["_score"]

        ndcg = calc_ndcg(params["qrels"], results, [10, 100], query_ids)
        result = {
            **strategy_params,
            "size": size,
            "queries": len(query_ids),
            "queries_with_hits": len(results),
            "ndcg_10": ndcg[10],
            "ndcg_100": ndcg[100],
        }
        logger.info("Relevance results: %s", result)
        return result

    def __repr__(self, *args, **kwargs):
        return "relevance"


def register(registry):
    registry.register_param_source("semantic-bulk-param-source", SemanticBulkParamSource)
    registry.register_param_source("relevance-param-source", RelevanceParamSource)
    registry.register_runner("start-mock-embedding-server", StartMockEmbeddingServerRunner(), async_runner=True)
    registry.register_runner("stop-mock-embedding-server", StopMockEmbeddingServerRunner(), async_runner=True)
    registry.register_runner("put-mock-inference-endpoint", PutMockInferenceEndpointRunner(), async_runner=True)
    registry.register_runner("relevance", RelevanceRunner(), async_runner=True)
