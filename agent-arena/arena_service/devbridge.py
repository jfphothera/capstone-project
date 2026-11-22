"""Development bridge: the same page-facing contract as the Windows desktop bridge.

Serves the Arena frontend from a folder on 127.0.0.1 and forwards an allow-list of
read-only /api/v2 GETs to the event service with signed requests. Used for
rendering tests on a workstation; heads use AgentArena.exe instead.

Page-facing contract (also implemented by AgentArena.exe):
  GET /api/local/identity      -> {display_id, role, location_id, service:{url, service_id}, version}
  GET /api/v2/snapshot?view=…  -> service snapshot
  GET /api/v2/events?after=&view=&wait=
Errors: JSON {error, message, origin:"bridge"|"service"}; error codes service_unreachable,
service_identity, not_enrolled, not_authorized, resnapshot.
"""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .client import ArenaError, Client, ServiceIdentityError

ALLOWED = ("/api/v2/snapshot", "/api/v2/events")
TYPES = {".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css", ".html": "text/html"}


def make_handler(web_root: Path, get_client, identity_extra=None):
    class Bridge(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload).encode())

        def do_GET(self):
            parts = urlsplit(self.path)
            client = get_client()
            if parts.path == "/api/local/identity":
                if not client:
                    return self._json(403, {"error": "not_enrolled", "message": "No viewer credential", "origin": "bridge"})
                try:
                    config = client.request("GET", "/v2/config", timeout=5)
                except ServiceIdentityError:
                    return self._json(502, {"error": "service_identity", "message": "authenticity check failed", "origin": "bridge"})
                except ArenaError as exc:
                    code = "not_authorized" if exc.status == 403 else "service_error"
                    return self._json(exc.status, {"error": code, "message": exc.message, "origin": "service"})
                except OSError as exc:
                    return self._json(503, {"error": "service_unreachable", "message": client.base, "origin": "bridge"})
                return self._json(200, dict(display_id=config["display_id"], role=config["role"], location_id=config["location_id"],
                                            service={"url": client.base, "service_id": config["service_id"]}, version=__version__,
                                            **(identity_extra or {})))
            if parts.path.startswith("/api/"):
                if parts.path not in ALLOWED:
                    return self._json(404, {"error": "not_allowed", "message": "Not a permitted bridge route", "origin": "bridge"})
                if not client:
                    return self._json(403, {"error": "not_enrolled", "message": "No viewer credential", "origin": "bridge"})
                try:
                    data = client.request("GET", "/v2" + self.path[len("/api/v2"):], timeout=35)
                except ServiceIdentityError:
                    return self._json(502, {"error": "service_identity", "message": "authenticity check failed", "origin": "bridge"})
                except ArenaError as exc:
                    code = {410: "resnapshot", 403: "not_authorized"}.get(exc.status, exc.code)
                    return self._json(exc.status, {"error": code, "message": exc.message, "origin": "service"})
                except OSError:
                    return self._json(503, {"error": "service_unreachable", "message": client.base, "origin": "bridge"})
                return self._json(200, data)
            rel = "agent_arena_desktop.html" if parts.path in ("", "/") else parts.path.lstrip("/")
            target = (web_root / rel).resolve()
            if web_root.resolve() not in target.parents or not target.is_file():
                return self._json(404, {"error": "not_found", "message": rel, "origin": "bridge"})
            ctype = TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    return Bridge


def serve(web_root, get_client, port, identity_extra=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Path(web_root), get_client, identity_extra))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
