"""Serve frontend/ on 127.0.0.1 for previews, telling browsers never to cache, so every refresh shows
the files as they are now (modules included). Development only.
    python tools/serve_frontend.py [port]"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class NoCache(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript", ".json": "application/json"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18794
    root = Path(__file__).resolve().parents[1] / "frontend"
    ThreadingHTTPServer(("127.0.0.1", port), partial(NoCache, directory=str(root))).serve_forever()
