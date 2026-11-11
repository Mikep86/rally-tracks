"""
Mock embedding service for the msmarco-v2-bayesian-eval track.

Serves the precomputed Cohere embed-english-v3.0 query embeddings from a queries file so that Elasticsearch's `custom`
inference service can embed query text without calling Cohere. Only exact query texts from the file are known; any other
input is rejected so that a query never silently receives a wrong vector.

Request:  POST /embed  {"input": ["query text", ...]}
Response: {"embeddings": [{"embedding": [float, ...]}, ...]}
"""

import argparse
import bz2
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("mock_embedding_server")


def load_embeddings(queries_file):
    embeddings = {}
    with bz2.open(queries_file, "rt") as f:
        for line in f:
            query = json.loads(line)
            embeddings[query["text"]] = query["emb"]
    return embeddings


def make_handler(embeddings):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "queries": len(embeddings)})
            else:
                self._send(404, {"error": f"unknown path [{self.path}]"})

        def do_POST(self):
            if self.path != "/embed":
                self._send(404, {"error": f"unknown path [{self.path}]"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                inputs = json.loads(self.rfile.read(length))["input"]
            except (ValueError, KeyError) as e:
                self._send(400, {"error": f"malformed request: {e}"})
                return
            if isinstance(inputs, str):
                inputs = [inputs]
            unknown = [text for text in inputs if text not in embeddings]
            if unknown:
                logger.warning("No embedding for input(s): %s", unknown)
                self._send(400, {"error": f"no embedding for input(s): {unknown}"})
                return
            logger.info("Embedding input(s): %s", inputs)
            self._send(200, {"embeddings": [{"embedding": embeddings[text]} for text in inputs]})

        def _send(self, status, body):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            # Request outcomes are logged by the handler itself
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries-file", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--log-file")
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_file, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    embeddings = load_embeddings(args.queries_file)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(embeddings))
    logger.info("Serving %d query embeddings on %s:%d", len(embeddings), args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
