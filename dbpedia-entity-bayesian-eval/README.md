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
* `bayesian_calibration_dump` (default: `true`): Whether to run `dump-bayesian-calibration` before the relevance operations.
* `bayesian_calibration_fields` (default: `["title", "text", "body_semantic"]`): Fields whose bayesian calibration is recorded, one at a time.
* `bayesian_candidate_scores` (default: `true`): Whether `dump-bayesian-calibration` also records every field's probability for each query's candidates.
* `bayesian_candidate_depths` (default: `{"title": 1000, "text": 1000, "body_semantic": 100}`): Number of each field's top hits that become candidates.
  Fields not listed use `relevance_size`.
* `bayesian_estimator_debug_logging` (default: `true`): Whether to set the `org.elasticsearch.index.shard.BayesianBM25ShardEstimator` logger to `DEBUG` before indexing, so that the Elasticsearch logs show which estimates fell back to a default.

### Per-query results and calibration data

Every relevance operation stores, next to the mean nDCG, `query_ids` and the matching `ndcg_10_per_query_json` and `ndcg_100_per_query_json` in the `meta` of its metrics documents, so strategies can be compared query by query.
The per-query nDCG lists are JSON-encoded strings, because the metrics store would map an array of 128 or more floats as a `dense_vector` and then reject runs with a different number of queries.

The data for reliability plots goes to `rally.log`, one JSON object per line after the prefix `bayesian-eval-dump`:

* `dump-bayesian-calibration` writes one `"kind": "parameters"` record per field and shard, with the `alpha`, `beta` and `baseRate` read from a `_validate/query` explanation of a single-field bayesian `multi_match`.
  The parameters of the first shard are also stored in the operation's `meta.parameters`.
* It then writes one `"kind": "hits"` record per judged query and field, with `"op": "calibration-<field>"` and the top `relevance_size` hits of a single-field bayesian `multi_match` as `[id, score]` pairs.
  These scores are the field's calibrated probabilities.
* With `bayesian_candidate_scores`, it then writes one `"kind": "scores"` record per judged query, with `fields` and, for each candidate, `[id, probability per field]`.
  The candidates are the union of each field's top `bayesian_candidate_depths` hits and the top `relevance_size` hits of every `bayesian` strategy in `relevance_ops`.
  A field that does not match a candidate has `null` for it, which contributes nothing to the fusion.
  These records let fusion be replayed offline with other parameters.
* Relevance operations with the `bayesian` strategy write a `"kind": "hits"` record per query for the fused probabilities, with the operation name in `op`.

Join the hits with `qrels.tsv` to compute reliability curves; unjudged documents count as not relevant.
`dump-bayesian-calibration` runs one search per field and judged query, so it adds about as much time as one relevance operation per field.
Candidate scores add one search per field and one per `bayesian` strategy for each query, so the operation takes about as long as running every `bayesian` relevance operation once more.

### License

DBPedia-entity is distributed under CC BY-SA 4.0 (see [BEIR](https://github.com/beir-cellar/beir)).
The Jina embeddings-v5 model weights used to compute the vectors are licensed CC BY-NC 4.0.
