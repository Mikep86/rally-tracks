## DBPedia-entity bayesian multi_match relevance track

This track compares the relevance of the `bayesian` `multi_match` type against the `rrf` and `linear` retrievers when searching across `semantic_text` and `text` fields.
Each retrieval strategy runs all 400 [BEIR DBPedia-entity](https://huggingface.co/datasets/BeIR/dbpedia-entity) test queries and reports nDCG@10 and nDCG@100 against `qrels.tsv` (graded 0, 1, 2).
It is the DBPedia counterpart of [`msmarco-v2-bayesian-eval`](../msmarco-v2-bayesian-eval) and runs the same operations and strategies.

The corpus is the full DBPedia-entity collection (4,635,922 entities, `title` + `text` abstract), the same documents and ids as the [`search/mteb/dbpedia`](../search/mteb/dbpedia) track.
Its corpus files (`jina-v5-documents-*.json.zst`) are hosted next to that track's `documents.json.bz2` at `https://rally-tracks.elastic.co/search/mteb/dbpedia/`.
Unlike the msmarco 10M slice, every judged document is indexed, and the queries are entity-centric with strong lexical matches.

### Precomputed embeddings

The corpus and queries carry [Jina `jina-embeddings-v5-text-small`](https://huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval) embeddings (retrieval adapter, MLX port `jinaai/jina-embeddings-v5-text-small-retrieval-mlx` at revision `781222631dc5`), computed locally:

* Documents: `"Document: " + title + " " + text`, 1024 dimensions, L2-normalized, last-token pooling.
* Queries: `"Query: " + text`.
* `emb` holds the vector as base64 of big-endian float32.

Exact kNN over these vectors scores nDCG@10 0.4453 and nDCG@100 0.5012 on the test queries.
`body_semantic` uses DiskBBQ (`bbq_disk`), which is approximate, so the `semantic` strategy can score somewhat lower.

At index time, a bulk param source moves each document's `emb` into the `_inference_fields` metadata field, so Elasticsearch skips inference for `body_semantic`.
Each document becomes a single chunk attributed to the `text` field.
`bbq_disk` requires an Enterprise or trial license.

### Query embeddings

The track starts a mock embedding server (`mock_embedding_server.py`) on the load driver, which returns the stored query embedding for each test query text and rejects any other input.
Elasticsearch reaches it through an inference endpoint using the `custom` service.
The Elasticsearch nodes must be able to reach the load driver on `mock_embedding_port`.
By default (`mock_embedding_host: auto`), the endpoint URL uses the load driver's address on the route to the first Elasticsearch node.

The track sets the `xpack.inference.skip_validate_and_start` cluster setting so that creating the endpoint does not send a validation request.

### Retrieval strategies

| Strategy | Request |
|---|---|
| `lexical` | `multi_match` (`best_fields`) on `title`, `text` |
| `semantic` | `semantic` query on `body_semantic` |
| `bayesian` | `multi_match` (`bayesian`) on `title`, `text`, `body_semantic` |
| `rrf` | `rrf` retriever with `fields` `title`, `text`, `body_semantic` |
| `linear` | `linear` retriever with `fields` `title`, `text`, `body_semantic` |

Bayesian BM25 parameters are estimated per shard after a merge, so the schedule force-merges the index and waits until a bayesian query succeeds before evaluating.

### Smoke corpus

`dbpedia-entity-jina-v5-1k` holds the 823 positively judged documents of the first test queries in `qrels.tsv`, so a smoke run scores non-zero nDCG.
Its numbers are not comparable to a full run.
`--test-mode` is not supported; use `corpora: ["dbpedia-entity-jina-v5-1k"]` instead.

### Parameters

This track accepts the following parameters with Rally 0.8.0+ using `--track-params`:

* `base_url` (default: `https://rally-tracks.elastic.co/search/mteb/dbpedia`): Bucket path to download the corpus from.
* `number_of_shards` (default: 1)
* `number_of_replicas` (default: 0)
* `inference_id` (default: `mock-jina-v5`): ID of the inference endpoint used by `body_semantic`.
* `corpora` (default: `["dbpedia-entity-jina-v5"]`): Corpora to index; `dbpedia-entity-jina-v5-1k` is meant for smoke runs.
* `mock_embedding_host` (default: `auto`): Address at which Elasticsearch reaches the mock embedding server.
* `mock_embedding_port` (default: 18080): Port of the mock embedding server.
* `initial_indexing_bulk_size` (default: 200)
* `initial_indexing_bulk_indexing_clients` (default: 5)
* `initial_indexing_bulk_warmup` (default: 40)
* `initial_indexing_ingest_percentage` (default: 100)
* `force_merge_max_num_segments` (default: 1)
* `bayesian_estimate_wait_retries` (default: 60): Number of 5 second retries while waiting for bayesian BM25 parameters.
* `bayesian_estimate_settle_seconds` (default: 0): Seconds to sleep after the bayesian wait step succeeds, so that the estimate for the final force-merged segment can finish (for example 300).
* `relevance_size` (default: 100): Number of hits retrieved per query.
* `relevance_ops`: List of strategy configurations to evaluate.
  Each entry needs `strategy` and accepts `fields`, `rank_window_size`, `rank_constant` (`rrf`), `normalizer` (`linear`) and `name`.
  Default:
  ```json
  [
    {"strategy": "lexical"},
    {"strategy": "semantic"},
    {"strategy": "bayesian"},
    {"strategy": "rrf", "rank_constant": 60, "rank_window_size": 100},
    {"strategy": "linear", "normalizer": "minmax", "rank_window_size": 100}
  ]
  ```

### License

DBPedia-entity is distributed under CC BY-SA 4.0 (see [BEIR](https://github.com/beir-cellar/beir)).
The Jina embeddings-v5 model weights used to compute the vectors are licensed CC BY-NC 4.0.
