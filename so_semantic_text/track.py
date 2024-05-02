import bz2
from os.path import dirname
import random

from esrally.track.params import ParamSource

QUERIES_DIRNAME: str = dirname(__file__)
QUERIES_FILENAME: str = f"{QUERIES_DIRNAME}/queries.txt.bz2"


class QueryParamSource(ParamSource):
    def __init__(self, track, params, **kwargs):
        super().__init__(track, params, **kwargs)
        self._queries = self._read_queries()

    @staticmethod
    def _read_queries():
        queries = []
        with bz2.open(QUERIES_FILENAME, mode="r") as queries_file:
            for query in queries_file:
                query = query.decode("utf-8")
                escaped_query = query.replace("\"", "\\\"")
                queries.append(escaped_query)

        return queries

    def params(self):
        query = random.choice(self._queries)
        if self._params["use_pipelines"]:
            es_query = {
                "text_expansion": {
                    "ml.title_embedding.tokens": {
                        "model_id": self._params["model_id"],
                        "model_text": query
                    }
                }
            }
        else:
            es_query = {
                "semantic": {
                    "field": "title_semantic",
                    "query": query
                }
            }

        return {
            "body": {
                "query": es_query
            },
            "size": self._params["size"],
            "index": "so-semantic-text"
        }


def register(registry):
    registry.register_param_source("semantic-search", QueryParamSource)
