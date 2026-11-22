"""A stand-in for Capstone's web process in the update rehearsal (Rehearse-CapstoneUpdate.ps1).

Capstone cannot start with the rehearsal's dummy Gateway tokens (its startup asks the Gateway, which refuses them).
This answers /api/health exactly where Capstone would: ONLY on appsettings.json's listen_host:listen_port (like
run_app.py --host), with the version current.json names, after a start delay like Capstone's. It never touches the
network beyond that one socket.
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--install", required=True)
p.add_argument("--data", required=True)
p.add_argument("--delay", type=float, default=20.0)
a = p.parse_args()
settings = json.loads(Path(a.data, "config", "appsettings.json").read_text(encoding="utf-8-sig"))
version = json.loads(Path(a.install, "current.json").read_text(encoding="utf-8-sig"))["version"]
journal = Path(a.data, "logs", "standin.log")


def note(text):
    with open(journal, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {text}\n")


note(f"started {version}")
time.sleep(a.delay)                      # Capstone needs tens of seconds to start
note(f"listening {version} on {settings['listen_host']}:{settings['listen_port']}")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            note(f"health {version}")
        body = json.dumps({"ok": True, "version": version, "stand_in": True}).encode() if self.path == "/api/health" else b"{}"
        self.send_response(200 if self.path == "/api/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer((settings["listen_host"], int(settings["listen_port"])), H).serve_forever()
