## MS MARCO v2 bayesian multi_match relevance track

This track compares the relevance of the `bayesian` `multi_match` type against the `rrf` and `linear` retrievers when searching across `semantic_text` and `text` fields.
Each retrieval strategy runs every judged [TREC-DL 2022](https://ir-datasets.com/msmarco-passage-v2.html#msmarco-passage-v2/trec-dl-2022/judged) query and reports nDCG@10 and nDCG@100 against `qrels.tsv`.

The track reuses the `msmarco-v2_base64-10-million` corpus and the query files from the [`msmarco-v2-vector`](../msmarco-v2-vector) track.

### Precomputed embeddings

The corpus contains [Cohere `embed-english-v3.0`](https://huggingface.co/datasets/Cohere/msmarco-v2-embed-english-v3) embeddings, generated from `title + " " + text`.
At index time, a bulk param source moves each document's `emb` into the `_inference_fields` metadata field, so Elasticsearch skips inference for `body_semantic`.
Each passage becomes a single chunk attributed to the `text` field.
`body_semantic` uses DiskBBQ (`bbq_disk`) index options, which requires an Enterprise or trial license.

### Query embeddings

Query text still needs embedding at search time.
The track starts a mock embedding server (`mock_embedding_server.py`) on the load driver, which returns the stored Cohere query embedding for each judged query text and rejects any other input.
Elasticsearch reaches it through an inference endpoint using the `custom` service.
The Elasticsearch nodes must be able to reach the load driver on `mock_embedding_port`.
By default (`mock_embedding_host: auto`), the endpoint URL uses the load driver's address on the route to the first Elasticsearch node, which works both for a local cluster and for a cluster on other hosts.

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

### Limitations

* The 10M-document corpus contains about 5.7% of the judged passages, so nDCG is much lower than on the full 138M corpus.
  Exact kNN reaches nDCG@10 of 0.234 on this corpus versus 0.556 on the full corpus.
  Compare strategies with each other, not with published results.
* There are only 76 judged queries.
* `--test-mode` does not work, because `cohere-documents-04_base64_1M-1k.json.zst` is not published.
  For a smoke run, use `corpora: ["msmarco-v2_base64-1k"]` instead.
  Its 1,000 documents contain no relevant passages, so every strategy scores nDCG 0.

### Parameters

This track accepts the following parameters with Rally 0.8.0+ using `--track-params`:

* `base_url` (default: `https://rally-tracks.elastic.co/cohere-msmarco-v2-embed-english-v3`): Bucket path to download the corpus from.
* `number_of_shards` (default: 1)
* `number_of_replicas` (default: 0)
* `inference_id` (default: `mock-cohere-embed-v3`): ID of the inference endpoint used by `body_semantic`.
* `corpora` (default: `["msmarco-v2_base64-10-million"]`): Corpora to index.
  `msmarco-v2_base64-1k` holds the first 1,000 documents of the 10M corpus and is meant for smoke runs.
* `mock_embedding_host` (default: `auto`): Address at which Elasticsearch reaches the mock embedding server.
  `auto` uses the load driver's address on the route to the first Elasticsearch node.
* `mock_embedding_port` (default: 18080): Port of the mock embedding server.
* `initial_indexing_bulk_size` (default: 200)
* `initial_indexing_bulk_indexing_clients` (default: 5)
* `initial_indexing_bulk_warmup` (default: 40)
* `initial_indexing_ingest_percentage` (default: 100)
* `force_merge_max_num_segments` (default: 1)
* `bayesian_estimate_wait_retries` (default: 60): Number of 5 second retries while waiting for bayesian BM25 parameters.
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

Terms and Conditions for using the MS MARCO datasets can be found at https://microsoft.github.io/msmarco/
