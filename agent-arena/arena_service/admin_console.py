"""arena-admin-console: the admin hub's screen, served to a browser on the hub itself.

    python -I -m arena_service.admin_console --config <service config> [--port 8791]

The same administration as arena-admin (registry, enrollment codes, endpoints), plus status and
backups, behind the admin room. It listens on 127.0.0.1 only and answers only requests from this
computer that carry the token printed at start (in the URL it opens), with this console's own
Host and Origin, so a web page elsewhere cannot drive it. Codes are shown once and never stored
in clear; nothing here reaches the network except the event service's public health check.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .adapters.gateway.observation import Observer, issues as gateway_issues
from .admin import head_identity
from .contract import NODE_ID, TERMINAL
from .fleet import Fleet
from .store import EXPIRED, Store

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
STATIC_TYPES = {".html", ".css", ".js", ".mjs", ".json", ".png", ".svg", ".gif"}
FLEET = ipaddress.IPv4Network("192.0.2.0/24")
TELEMETRY_STALE = 120          # heads report every 30 s
MAX_BODY = 16 * 1024
ARRIVED_KEEP = 60              # a landed or canceled trip stays on the board this long
TROUBLE_KEEP = 300             # a failed or lost one stays longer, so a glance still catches it
HEAD_OCTET = re.compile(r"^head:head-192-0-2-(\d{1,3})$")

log = logging.getLogger("arena_service.admin_console")


def role_for(address):
    """The fleet's address plan: heads .2-.199, orchestrators .200-.249, admin machines .250-.254."""
    ip = ipaddress.IPv4Address(address)
    if ip not in FLEET:
        raise ValueError(f"{address} is outside {FLEET}")
    n = int(str(ip).rsplit(".", 1)[1])
    return "head" if 2 <= n <= 199 else "orchestrator" if 200 <= n <= 249 else "admin" if 250 <= n <= 254 else None


def _ago(seconds):
    return f"{int(seconds // 60)} min" if seconds < 5400 else f"{seconds / 3600:.0f} h" if seconds < 172800 else f"{seconds / 86400:.0f} days"


def _sort_key(address):
    try:
        return int(ipaddress.IPv4Address(address or "0.0.0.0"))
    except ValueError:
        return 0


class Console:
    def __init__(self, config, token=None):
        self.config = config
        self.store = Store(config["db_path"])
        self.token = token or secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        # The Inference Gateway, when its operator publishes an observation file for Arena: read only.
        gateway = config.get("gateway") or {}
        self.gateway = Observer(gateway["observation_file"], gateway_id=gateway.get("gateway_id", "gateway"),
                                stale_seconds=gateway.get("stale_seconds", 30),
                                resource_heads=gateway.get("resource_heads")) if gateway.get("observation_file") else None
        self.gateway_lock = threading.Lock()
        # Fleet: heads, tiers, models, projects and changes for one Commit (arena_service.fleet).
        self.fleet = Fleet(config, self.store, self.gateway_view)

    # ------------------------------------------------------------------ status
    def service_health(self):
        url = f"http://{self.config.get('host', '127.0.0.1')}:{int(self.config.get('port', 8790))}/v2/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=3) as response:
                data = json.loads(response.read())
            self._health_misses, self._last_version = 0, data.get("version")
            return {"up": True, "version": data.get("version"), "url": url}
        except Exception as exc:          # down, refused, slow or not ours
            # Arena 2.5.1: one missed check is "slow to answer" (it was shown as down, so a busy moment flashed
            # "Service not answering"); two in a row is "not answering".
            self._health_misses = getattr(self, "_health_misses", 0) + 1
            if self._health_misses < 2:
                return {"up": True, "slow": True, "version": getattr(self, "_last_version", None), "url": url,
                        "error": type(exc).__name__}
            return {"up": False, "version": None, "url": url, "error": type(exc).__name__}

    def machines(self, now=None):
        now = now or time.time()
        store = self.store
        registry = store.registry()
        heads = {h["location_id"]: h for h in registry["heads"]}
        orchs = {o["location_id"]: o for o in registry["orchestrators"]}
        names = {h["location_id"]: h["display_name"] for h in store._all("SELECT location_id,display_name FROM heads")}
        creds = store.list_credentials()
        enrolled = {c["display_id"] for c in creds if c["kind"] == "viewer" and not c["revoked_at"]
                    and (not c["expires_at"] or c["expires_at"] > now)}
        waiting = {c["display_id"]: c["expires_at"] for c in creds if c["kind"] == "enrollment" and not c["revoked_at"]
                   and not c["used_at"] and c["expires_at"] and c["expires_at"] > now}
        out = []
        for d in store.list_displays():
            m = {"display_id": d["display_id"], "kind": d["kind"], "location_id": d["location_id"], "address": d["address"],
                 "name": names.get(d["location_id"]) or orchs.get(d["location_id"], {}).get("display_name") or d["location_id"],
                 "enrolled": d["display_id"] in enrolled, "code_expires_at": waiting.get(d["display_id"]), "detail": ""}
            if d["status"] != "active":
                m["state"] = "retired"
            elif d["kind"] == "head":
                t = (heads.get(d["location_id"]) or {}).get("telemetry")
                age = now - t["received_at"] if t else None
                m["telemetry_age"] = age
                if age is None or age > TELEMETRY_STALE:
                    m["state"], m["detail"] = ("no-telemetry", "no report yet" if age is None else f"last report {_ago(age)} ago")
                elif not (t.get("ollama") or {}).get("reachable"):
                    m["state"], m["detail"] = "warn", "Ollama not reachable"
                else:
                    m["state"], m["detail"] = "up", "Ollama " + str((t.get("ollama") or {}).get("version") or "")
            else:
                producer = (orchs.get(d["location_id"]) or {}).get("producer_state", "offline")
                m["state"], m["detail"] = ("up", "producer online") if producer == "online" else ("warn", "producer offline")
            if m["state"] not in ("retired",) and not m["enrolled"]:
                m["state"], m["detail"] = "not-enrolled", "code waiting" if m["code_expires_at"] else "needs a code"
            out.append(m)
        return sorted(out, key=lambda m: (_sort_key(m["address"]), m["display_id"]))

    def status(self):
        now = time.time()
        health, machines = self.service_health(), self.machines(now)
        counts = self.store.result_counts(now)
        threshold = int(self.config.get("failure_warning_per_hour", 10))
        issues = []
        if not health["up"]:
            issues.append({"who": "SERVICE", "what": f"not answering on :{self.config.get('port', 8790)}", "level": "down"})
        elif health.get("slow"):
            issues.append({"who": "SERVICE", "what": "slow to answer (one health check missed)", "level": "warn"})
        for m in machines:
            if m["state"] in ("no-telemetry", "warn") and m["kind"] == "head":
                issues.append({"who": m["name"].split(" (")[0].upper(), "what": m["detail"], "level": "warn"})
            fails = counts.get(m["location_id"], {}).get("1h", {}).get("fail", 0)
            if m["kind"] == "head" and m["state"] != "retired" and fails >= threshold:
                issues.append({"who": m["name"].split(" (")[0].upper(), "what": f"{fails} failed in the last hour", "level": "warn"})
        gateway = None
        if self.gateway:
            view = self.gateway_view(now)
            issues += gateway_issues(view)
            gateway = {k: view.get(k) for k in ("state", "admission_open", "queued", "active", "executing",
                                                 "heads_up", "heads_total", "utilization")}
        heads = [m for m in machines if m["kind"] == "head" and m["state"] != "retired"]
        hour = counts.get("cluster", {}).get("1h", {"pass": 0, "fail": 0})
        registry = self.store.registry()
        producers = {(c["app_id"], o) for c in self.store.list_credentials() if c["kind"] == "producer" and not c["revoked_at"]
                     for o in (json.loads(c["orchestrators"]) if isinstance(c["orchestrators"], str) else c["orchestrators"] or [])}
        applications = [{"app_id": a["app_id"], "name": a["display_name"],
                         "orchestrators": [{"orchestrator_id": o["orchestrator_id"], "location_id": o["location_id"],
                                            "producer_state": o["producer_state"],
                                            "has_credential": (a["app_id"], o["orchestrator_id"]) in producers,
                                            "address": self.store.claimed_address(a["app_id"], o["orchestrator_id"]),
                                            "code_expires_at": (self.store.app_code_for(a["app_id"], o["orchestrator_id"]) or {}).get("expires_at")}
                                           for o in registry["orchestrators"] if o["app_id"] == a["app_id"]]}
                        for a in registry["applications"]]
        return {"console": __version__, "time": now, "service": health, "machines": machines, "issues": issues,
                "gateway": gateway, "addresses": self.store.list_addresses(), "applications": applications,
                "next_free": {role: self.store.next_free(role) for role in ("head", "app")},
                "heads": {"up": sum(m["state"] == "up" for m in heads), "total": len(heads)},
                "codes": sum(1 for m in machines if m["code_expires_at"]), "hour": hour,
                "endpoints": self.store.meta("endpoints") or self.config.get("public_endpoints") or [],
                "orchestrators": [{"location_id": o["location_id"], "name": o["display_name"]} for o in self.store.registry()["orchestrators"]]}

    # ----------------------------------------------------------------- actions
    def add_machine(self, address, location_id=None):
        address = str(ipaddress.IPv4Address(address))
        role = role_for(address)
        if role == "head":
            node_id, name = head_identity(address)
            location = self.store.add_head(node_id, name, address, ["*"])
            self.store.add_display(node_id, "head", location, address)
            return {"display_id": node_id, "kind": "head", "location_id": location}
        if role == "orchestrator":
            if not location_id or not location_id.startswith("orch:"):
                raise ValueError("choose which orchestrator this display shows")
            app, _, orch = location_id[5:].partition("/")
            display_id = f"orch-{app}-{orch}-{address.replace('.', '-')}"
            if not NODE_ID.match(display_id):
                raise ValueError("invalid orchestrator name")
            self.store.add_display(display_id, "orchestrator", location_id, address)
            return {"display_id": display_id, "kind": "orchestrator", "location_id": location_id}
        raise ValueError(f"{address} is an admin address (.250-.254); admin machines have no display to register")

    def retire(self, display_id):
        display = self.store.display(display_id)
        if not display:
            raise ValueError("unknown display " + display_id)
        if display["kind"] == "head":
            node = self.store._one("SELECT node_id FROM heads WHERE location_id=?", (display["location_id"],))
            displays, open_count = self.store.remove_head(node["node_id"])
            return {"retired": display_id, "open_assignments": open_count}
        self.store.remove_display(display_id)
        return {"retired": display_id, "open_assignments": 0}

    def restore(self, display_id):
        display = self.store.display(display_id)
        if not display:
            raise ValueError("unknown display " + display_id)
        if display["kind"] == "head":
            head = self.store._one("SELECT * FROM heads WHERE location_id=?", (display["location_id"],))
            self.store.add_head(head["node_id"], head["display_name"], head["address"], json.loads(head["allowed_apps"]))
        self.store.add_display(display_id, display["kind"], display["location_id"], display["address"])
        return {"restored": display_id}

    def issue_code(self, display_id, hours=24):
        hours = float(hours)
        if not 1 <= hours <= 72:
            raise ValueError("a code lasts 1 to 72 hours")
        display = self.store.display(display_id)
        if not display or display["status"] != "active":
            raise ValueError("codes are only for active displays")
        code = self.store.issue_enrollment(display_id, hours)
        log.info("enrollment code issued for %s (%g h)", display_id, hours)
        return {"display_id": display_id, "code": code, "hours": hours, "expires_at": time.time() + hours * 3600}

    def reserve(self, address, role, app_id=None, orchestrator_id=None, label=None, hours=72):
        """Reserve an address for a head or an application's laptop; the claim code is shown once."""
        address = str(ipaddress.IPv4Address(address))
        hours = float(hours)
        if not 1 <= hours <= 168:
            raise ValueError("a claim code lasts 1 to 168 hours")
        code = self.store.reserve_address(address, role, app_id=app_id or None, orchestrator_id=orchestrator_id or None,
                                          label=(label or "").strip()[:60] or None, hours=hours)
        log.info("address %s reserved for %s (%g h claim code)", address, role if role == "head" else f"{app_id}/{orchestrator_id}", hours)
        return {"address": address, "role": role, "code": code, "hours": hours, "expires_at": time.time() + hours * 3600}

    def release(self, address):
        self.store.release_address(str(ipaddress.IPv4Address(address)))
        return {"released": address}

    def add_application(self, app_id, name):
        """A new application with its main orchestrator, ready for an address and an app code."""
        app_id, name = (app_id or "").strip(), (name or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", app_id):
            raise ValueError("application ID: lowercase letters, digits and dashes, starting with a letter")
        if not name or len(name) > 40:
            raise ValueError("give the application a display name (up to 40 characters)")
        if any(a["app_id"] == app_id for a in self.store.registry()["applications"]):
            raise ValueError(f"application {app_id} already exists")
        self.store.add_application(app_id, name)
        location = self.store.add_orchestrator(app_id, "main", f"{name} coordinator")
        return {"app_id": app_id, "location_id": location}

    def app_code(self, app_id, orchestrator_id="main", hours=24):
        hours = float(hours)
        if not 1 <= hours <= 72:
            raise ValueError("a code lasts 1 to 72 hours")
        code = self.store.issue_app_code(app_id, orchestrator_id, hours)
        log.info("app code issued for %s/%s (%g h)", app_id, orchestrator_id, hours)
        return {"app_id": app_id, "orchestrator_id": orchestrator_id, "code": code, "hours": hours,
                "address": self.store.claimed_address(app_id, orchestrator_id)}

    def set_endpoints(self, urls):
        clean = []
        for url in urls:
            parts = urlsplit(url.strip())
            if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/") or parts.query:
                raise ValueError(f"not an event service address: {url}")
            ipaddress.IPv4Address(parts.hostname)
            clean.append(f"http://{parts.hostname}:{parts.port or 8790}")
        if not clean:
            raise ValueError("at least one address is required")
        self.store.set_meta("endpoints", clean)
        log.info("endpoints set: %s", ", ".join(clean))
        return {"endpoints": clean}

    def backup(self):
        db = Path(self.config["db_path"])
        folder = db.parent / "backups"
        folder.mkdir(exist_ok=True)
        target = folder / f"arena-events-{time.strftime('%Y%m%d-%H%M%S')}.db"
        source, copy = sqlite3.connect(db), sqlite3.connect(target)
        try:
            source.backup(copy)     # consistent while the service keeps writing
        finally:
            copy.close()
            source.close()
        return {"path": str(target), "bytes": target.stat().st_size}

    def warnings(self, lines=40):
        path = self.config.get("log_path")
        if not path or not Path(path).exists():
            return {"lines": []}
        with open(path, encoding="utf-8", errors="replace") as handle:
            tail = deque((line.rstrip() for line in handle if " WARNING " in line or " ERROR " in line), maxlen=lines)
        return {"lines": list(tail)}

    # ----------------------------------------------------------------- gateway
    def gateway_view(self, now=None):
        """The gateway's published observation, summarized; applications it shares with Arena's
        registry carry their Arena name. Nothing here writes to the gateway."""
        if not self.gateway:
            return {"state": "not_configured"}
        with self.gateway_lock:
            view = self.gateway.view(now)
        names = {a["app_id"]: a["display_name"] for a in self.store.registry()["applications"]}
        if "applications" in view:
            view["applications"] = [dict(a, arena_name=names.get(a["app_id"])) for a in view["applications"]]
        return view

    # ----------------------------------------------------------------- traffic
    def traffic(self, now=None):
        """The traffic board: work out at a head now (departures) and work that just came back
        (arrivals), from the service's own assignments and event history. A trip is one stay of
        one assignment at one head; it departs when the assignment moves there and arrives when
        it moves away or finishes there. Only facts the applications published, nothing guessed."""
        now = now or time.time()
        store = self.store
        with store.lock:
            apps = {a["app_id"]: a["display_name"] for a in store._all("SELECT app_id,display_name FROM applications")}
            heads = {h["location_id"]: h["display_name"] for h in store._all("SELECT location_id,display_name FROM heads")}
            departures = []
            for row in store._all("SELECT * FROM assignments WHERE location_id LIKE 'head:%' AND terminal_at IS NULL "
                                  "AND state!=?", (EXPIRED,)):
                moved = store._one("SELECT MAX(received_at) AS t FROM events WHERE app_id=? AND assignment_id=? AND applied=1 "
                                   "AND loc_to=? AND (loc_from IS NULL OR loc_from!=loc_to)",
                                   (row["app_id"], row["assignment_id"], row["location_id"]))["t"]
                view = store._assignment_view(row, now)
                departures.append(self._trip(apps, heads, row["app_id"], row["location_id"], moved or row["created_at"],
                                             _out_status(view)))
            arrivals = []
            for e in store._all("SELECT app_id,received_at,loc_from,loc_to,result FROM events WHERE applied=1 "
                                "AND assignment_id IS NOT NULL AND received_at>? AND loc_from LIKE 'head:%' ORDER BY seq",
                                (now - TROUBLE_KEEP,)):
                result = json.loads(e["result"]) if e["result"] else {}
                left = e["loc_to"] != e["loc_from"]
                if not left and result.get("state") not in TERMINAL | {EXPIRED}:
                    continue                    # still at the head: a state change, not a trip's end
                status = _back_status(result)
                if now - e["received_at"] > (TROUBLE_KEEP if status in ("failed", "unknown") else ARRIVED_KEEP):
                    continue
                arrivals.append(self._trip(apps, heads, e["app_id"], e["loc_from"], e["received_at"], status))
        departures.sort(key=lambda t: t["at"])
        return {"time": now, "departures": departures, "arrivals": arrivals}

    @staticmethod
    def _trip(apps, heads, app_id, head, at, status):
        octet = HEAD_OCTET.match(head)
        name = f"Head {octet.group(1)}" if octet else (heads.get(head) or head[5:]).split(" (")[0]
        return {"at": at, "app_id": app_id, "app_name": apps.get(app_id, app_id), "head": head, "head_name": name,
                "status": status}


def _out_status(view):
    """Where a trip out at a head stands: waiting for capacity, waiting for the model, running,
    or not heard from (lease lapsed or producer offline: never shown as failed)."""
    if view["render_state"] in ("stale", "producer_offline"):
        return "unknown"
    if view["state"] == "executing":
        return "running"
    return "gate" if view["state"] == "waiting" and view["wait_reason"] == "capacity" else "boarding"


def _back_status(result):
    """How a trip ended: the work came home (or finished) fine, failed, was canceled, or was lost."""
    state = result.get("state")
    if state == EXPIRED:
        return "unknown"
    if state == "canceled":
        return "canceled"
    return "failed" if state == "failed" or result.get("visual_action") == "error_handling" else "landed"


class Handler(BaseHTTPRequestHandler):
    console: Console
    server_version = "ArenaAdminConsole"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _send(self, status, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        """Only this computer, through this console's own name: no other host, no rebinding."""
        port = self.server.server_address[1]
        own = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.client_address[0] not in ("127.0.0.1", "::1") or self.headers.get("Host") not in own:
            self._send(403, {"error": "local_only"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in own}:
            self._send(403, {"error": "foreign_origin"})
            return False
        return True

    def _authorized(self):
        given = self.headers.get("X-Arena-Admin-Token", "")
        if not hmac.compare_digest(given.encode(), self.console.token.encode()):
            self._send(401, {"error": "token_required"})
            return False
        return True

    def do_GET(self):
        if not self._local():
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if not self._authorized():
                return
            if path == "/api/status":
                return self._send(200, self.console.status())
            if path == "/api/warnings":
                return self._send(200, self.console.warnings())
            if path == "/api/traffic":
                return self._send(200, self.console.traffic())
            if path == "/api/fleet":
                return self._send(200, self.console.fleet.view())
            if path == "/api/gateway":
                return self._send(200, self.console.gateway_view())
            return self._send(404, {"error": "not_found"})
        self._static(path)

    def do_POST(self):
        if not self._local() or not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": "too_large"})
        # Read the body before any answer: closing with it unread makes Windows reset the connection, and the
        # client can lose the answer (a refusal then looked like a network error).
        raw = self.rfile.read(length) if length else b""
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "json_required"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad_json"})
        c, path = self.console, urlsplit(self.path).path
        actions = {"/api/machines": lambda: c.add_machine(body.get("address", ""), body.get("location_id")),
                   "/api/machines/retire": lambda: c.retire(body.get("display_id", "")),
                   "/api/machines/restore": lambda: c.restore(body.get("display_id", "")),
                   "/api/codes": lambda: c.issue_code(body.get("display_id", ""), body.get("hours", 24)),
                   "/api/endpoints": lambda: c.set_endpoints(body.get("urls") or []),
                   "/api/addresses": lambda: c.reserve(body.get("address", ""), body.get("role", ""), body.get("app_id"),
                                                       body.get("orchestrator_id"), body.get("label"), body.get("hours", 72)),
                   "/api/addresses/release": lambda: c.release(body.get("address", "")),
                   "/api/apps": lambda: c.add_application(body.get("app_id"), body.get("name")),
                   "/api/app-codes": lambda: c.app_code(body.get("app_id", ""), body.get("orchestrator_id", "main"),
                                                        body.get("hours", 24)),
                   "/api/fleet/pending": lambda: c.fleet.set_pending(body.get("changes")),
                   "/api/fleet/commit": lambda: c.fleet.commit(os.environ.get("USERNAME", "Arena")),
                   "/api/fleet/scan": lambda: {"started": c.fleet.scan_now()},
                   "/api/fleet/tier": lambda: c.fleet.set_tier(body.get("address", ""), body.get("tier")),
                   "/api/backup": c.backup}
        if path not in actions:
            return self._send(404, {"error": "not_found"})
        try:
            with c.lock:
                result = actions[path]()
        except (ValueError, KeyError, TypeError) as exc:
            return self._send(400, {"error": "rejected", "message": str(exc)})
        log.info("admin %s %s", path, {k: v for k, v in body.items() if k != "code"})
        self._send(200, result)

    def _static(self, path):
        rel = "admin.html" if path in ("/", "") else path.lstrip("/")
        target = (FRONTEND / rel).resolve()
        if FRONTEND not in target.parents or target.suffix not in STATIC_TYPES or not target.is_file():
            return self._send(404, {"error": "not_found"})
        ctype = {".mjs": "text/javascript", ".js": "text/javascript"}.get(target.suffix) or mimetypes.guess_type(target.name)[0]
        self._send(200, target.read_bytes(), ctype or "application/octet-stream")


def build(config, port=8791, token=None):
    console = Console(config, token)
    handler = type("BoundConsoleHandler", (Handler,), {"console": console})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server, console


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arena-admin-console", description=__doc__)
    parser.add_argument("--config", required=True, help="service configuration JSON")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server, console = build(config, args.port)
    console.fleet.start_background()      # the head scan, once a minute
    url = f"http://127.0.0.1:{server.server_address[1]}/#token={console.token}"
    print(f"Admin console on this computer only: {url}")
    print("Keep this window open while you use it; close it to stop the console.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
