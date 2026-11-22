"""HTTP front end of the Arena Event Service (arena.v2).

Producers POST events; viewers read snapshots and long-poll deltas. Every
authenticated request is HMAC-signed and every authenticated response is signed
back, so a display can tell the real service from anything else at that address.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import CONTRACT, SUPPORTED_CONTRACTS, __version__, crypto
from .contract import ContractError, NODE_ID, validate
from .store import Resnapshot, Store

MAX_BODY = 256 * 1024
MAX_BATCH = 100
CLOCK_SKEW = 120
NONCE_WINDOW = 600
MAX_WAITERS = 64
TELEMETRY_KEYS = {"sampled_at", "cpu_percent", "ram_total_bytes", "ram_available_bytes", "gpus", "ollama", "validity"}

log = logging.getLogger("arena_service")


class Bucket:
    """Token bucket: `rate` tokens per second up to `burst`."""

    def __init__(self, rate, burst):
        self.rate, self.burst, self.tokens, self.at = rate, burst, float(burst), time.monotonic()

    def take(self, n=1):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class Service:
    def __init__(self, config):
        self.config = config
        self.store = Store(config["db_path"], retention_days=config.get("retention_days", 7),
                           max_events=config.get("max_events", 1_000_000),
                           max_events_per_app=config.get("max_events_per_app", 250_000))
        self.nonces = {}
        self.buckets = {}
        self.guard = threading.Lock()
        self.waiters = threading.BoundedSemaphore(MAX_WAITERS)
        self.event_rate = config.get("producer_events_per_second", 50)
        self.event_burst = config.get("producer_event_burst", 500)

    def bucket(self, key, rate, burst):
        with self.guard:
            if key not in self.buckets:
                self.buckets[key] = Bucket(rate, burst)
            return self.buckets[key]

    def fresh_nonce(self, credential_id, nonce):
        now = time.time()
        with self.guard:
            if len(self.nonces) > 100_000:
                self.nonces = {k: t for k, t in self.nonces.items() if now - t < NONCE_WINDOW}
            key = (credential_id, nonce)
            if key in self.nonces and now - self.nonces[key] < NONCE_WINDOW:
                return False
            self.nonces[key] = now
            return True

    def endpoints(self):
        return self.store.meta("endpoints") or self.config.get("public_endpoints") or []


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentArenaEventService/" + __version__
    sys_version = ""
    service: Service = None
    protocol_version = "HTTP/1.1"

    # ----------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def _reply(self, status, payload, auth=None, extra=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Arena-Service", self.service.store.service_id)
        if auth:
            key, nonce = auth
            self.send_header("X-Arena-Response-Signature", crypto.sign(key, crypto.canonical_response(status, nonce, body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message, auth=None, extra=None):
        self._reply(status, {"error": code, "message": message}, auth, extra)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body_too_large")
        return self.rfile.read(length) if length else b""

    def _client_bucket(self):
        return self.service.bucket(("ip", self.client_address[0]), 10, 40)

    def _authenticate(self, body, kinds):
        """Return (credential, auth) or send an error and return (None, None)."""
        credential_id = self.headers.get("X-Arena-Credential", "")
        stamp = self.headers.get("X-Arena-Timestamp", "")
        nonce = self.headers.get("X-Arena-Nonce", "")
        signature = self.headers.get("X-Arena-Signature", "")
        if not (credential_id and stamp and nonce and signature):
            self._error(401, "authentication_required", "Signed request headers are required")
            return None, None
        if not self._client_bucket().take():
            self._error(429, "rate_limited", "Too many requests from this address", extra={"Retry-After": "2"})
            return None, None
        store = self.service.store
        one_time = {"enroll:": store.enrollment_for, "claim:": store.claim_for,
                    "app:": lambda rest: store.app_code_for(*rest.partition("/")[::2])}
        prefix = next((p for p in one_time if credential_id.startswith(p)), None)
        if prefix:
            credential = one_time[prefix](credential_id[len(prefix):])
            if credential:
                credential["key"] = crypto.unb64u(credential["key_b64"])
        else:
            credential = self.service.store.credential(credential_id)
        try:
            skew = abs(time.time() - int(stamp))
        except ValueError:
            skew = CLOCK_SKEW + 1
        message = crypto.canonical_request(self.command, self.path, stamp, nonce, body)
        if not credential or credential["kind"] not in kinds or not crypto.verify(credential["key"], message, signature):
            log.warning("auth failure from %s credential=%s", self.client_address[0], credential_id[:24])
            self._error(403, "not_authorized", "Credential is unknown, revoked, expired or not permitted here")
            return None, None
        if skew > CLOCK_SKEW:
            self._error(401, "clock_skew", "Request timestamp is more than 120 s from service time",
                        (credential["key"], nonce), {"X-Arena-Time": str(int(time.time()))})
            return None, None
        if not self.service.fresh_nonce(credential["credential_id"], nonce):
            self._error(401, "replayed", "Request nonce was already used", (credential["key"], nonce))
            return None, None
        return credential, (credential["key"], nonce)

    def _optional_auth(self):
        """Health checks may be signed by any active credential to prove service identity."""
        if not self.headers.get("X-Arena-Credential"):
            return None            # Arena 2.5.1: an unsigned check (the admin console's) never waits for the store lock
        credential = self.service.store.credential(self.headers.get("X-Arena-Credential", ""))
        if not credential:
            return None
        message = crypto.canonical_request(self.command, self.path, self.headers.get("X-Arena-Timestamp", ""),
                                           self.headers.get("X-Arena-Nonce", ""), b"")
        if crypto.verify(credential["key"], message, self.headers.get("X-Arena-Signature", "")):
            return credential["key"], self.headers.get("X-Arena-Nonce", "")
        return None

    # ------------------------------------------------------------- routes
    def do_GET(self):
        try:
            self._route_get()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass                # the viewer closed while waiting; nothing to answer
        except Exception:
            log.exception("GET %s failed", self.path)
            self._error(500, "internal_error", "The event service hit an internal error; see its log")

    def do_POST(self):
        try:
            self._route_post()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except ValueError as exc:
            self._error(413 if str(exc) == "body_too_large" else 400, "bad_request", str(exc)[:120])
        except Exception:
            log.exception("POST %s failed", self.path)
            self._error(500, "internal_error", "The event service hit an internal error; see its log")

    def _route_get(self):
        parts = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        store = self.service.store
        if parts.path == "/v2/health":
            if not self._client_bucket().take():
                return self._error(429, "rate_limited", "Too many requests", extra={"Retry-After": "2"})
            return self._reply(200, {"service": "agent-arena-event-service", "version": __version__,
                                     "service_id": store.service_id, "contracts": list(SUPPORTED_CONTRACTS),
                                     "time": time.time(), "latest_seq": store.latest_seq()}, self._optional_auth())
        if parts.path == "/v2/whoami":
            # The registry, not the package, says what a machine at this address is. It is only a
            # proposal for the installer: the one-time, address-bound code still authorizes it.
            if not self._client_bucket().take():
                return self._error(429, "rate_limited", "Too many requests", extra={"Retry-After": "2"})
            address = self.client_address[0]
            return self._reply(200, {"service_id": store.service_id, "address": address,
                                     "displays": store.displays_at(address)}, self._optional_auth())
        credential, auth = self._authenticate(b"", {"viewer"})
        if not credential:
            return
        display = store.display(credential["display_id"])
        if not display or display["status"] != "active":
            return self._error(403, "display_disabled", "This display is disabled", auth)
        if parts.path == "/v2/config":
            return self._reply(200, {"contract": CONTRACT, "service_id": store.service_id,
                                     "display_id": display["display_id"], "role": display["kind"],
                                     "location_id": display["location_id"], "endpoints": self.service.endpoints(),
                                     "credential_id": credential["credential_id"],
                                     "rotation": store.rotation_for(credential)}, auth)
        if parts.path == "/v2/registry":
            return self._reply(200, dict(store.registry(), contract=CONTRACT), auth)
        if parts.path == "/v2/snapshot":
            try:
                return self._reply(200, dict(store.snapshot(query.get("view", "cluster")), contract=CONTRACT), auth)
            except ValueError:
                return self._error(400, "bad_view", "Unknown view", auth)
        if parts.path == "/v2/events":
            return self._events(query, auth)
        self._error(404, "not_found", "Unknown endpoint", auth)

    def _events(self, query, auth):
        store = self.service.store
        try:
            after = max(0, int(query.get("after", "0")))
            wait = max(0, min(25, int(query.get("wait", "0"))))
        except ValueError:
            return self._error(400, "bad_request", "after and wait must be integers", auth)
        view = query.get("view", "cluster")
        if wait and store.latest_seq() <= after and self.service.waiters.acquire(blocking=False):
            try:
                with store.changed:
                    store.changed.wait_for(lambda: store.latest_seq() > after, timeout=wait)
            finally:
                self.service.waiters.release()
        try:
            result = store.events_after(after, view)
        except Resnapshot:
            return self._error(410, "resnapshot", "Requested sequence is older than retention; take a new snapshot", auth)
        self._reply(200, dict(result, contract=CONTRACT), auth)

    def _route_post(self):
        path = urlsplit(self.path).path
        body = self._body()
        if path == "/v2/events":
            credential, auth = self._authenticate(body, {"producer"})
            if credential:
                self._ingest(credential, auth, body)
        elif path == "/v2/enroll":
            credential, auth = self._authenticate(body, {"enrollment"})
            if credential:
                self._enroll(credential, auth, body)
        elif path == "/v2/telemetry":
            credential, auth = self._authenticate(body, {"viewer"})
            if credential:
                self._telemetry(credential, auth, body)
        elif path == "/v2/claim":
            credential, auth = self._authenticate(body, {"claim"})
            if credential:
                self._claim(credential, auth, body)
        elif path == "/v2/app/enroll":
            credential, auth = self._authenticate(body, {"app_enrollment"})
            if credential:
                self._app_enroll(credential, auth)
        else:
            self._error(404, "not_found", "Unknown endpoint")

    def _ingest(self, credential, auth, body):
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            return self._error(400, "bad_json", "Body is not JSON", auth)
        if not isinstance(request, dict) or request.get("contract") not in SUPPORTED_CONTRACTS:
            return self._error(426, "contract_unsupported", "Supported contracts: " + ",".join(SUPPORTED_CONTRACTS), auth)
        events = request.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH:
            return self._error(400, "bad_batch", f"events must be a list of 1-{MAX_BATCH}", auth)
        if not self.service.bucket(("cred", credential["credential_id"]), self.service.event_rate,
                                   self.service.event_burst).take(len(events)):
            return self._error(429, "rate_limited", "Producer event rate exceeded", auth, {"Retry-After": "1"})
        valid, results = [], [None] * len(events)
        for index, event in enumerate(events):
            try:
                valid.append((index, validate(event)))
            except ContractError as exc:
                results[index] = {"event_id": event.get("event_id") if isinstance(event, dict) else None,
                                  "status": "rejected", "reason": str(exc)}
        for (index, _), outcome in zip(valid, self.service.store.ingest(credential, [e for _, e in valid])):
            results[index] = outcome
        rejected = sum(1 for r in results if r["status"] == "rejected")
        if rejected:
            log.info("producer %s: %d of %d events rejected", credential["app_id"], rejected, len(events))
        self._reply(200, {"contract": CONTRACT, "app_id": credential["app_id"], "results": results}, auth)

    def _enroll(self, credential, auth, body):
        store = self.service.store
        try:
            request = json.loads(body)
            machine_hash = request["machine_hash"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._error(400, "bad_request", "machine_hash is required", auth)
        if not isinstance(machine_hash, str) or not 16 <= len(machine_hash) <= 128:
            return self._error(400, "bad_request", "machine_hash is invalid", auth)
        display = store.display(credential["display_id"])
        if display["address"] and display["address"] != self.client_address[0]:
            log.warning("enrollment for %s refused from %s", display["display_id"], self.client_address[0])
            return self._error(403, "address_mismatch",
                               f"This code is for {display['display_id']}, which is registered at a different address", auth)
        try:
            credential_id, secret = store.consume_enrollment(credential["credential_id"], machine_hash)
        except PermissionError as exc:
            return self._error(403, "enrollment_used", str(exc), auth)
        log.info("display %s enrolled as %s", display["display_id"], credential_id)
        self._reply(200, {"contract": CONTRACT, "service_id": store.service_id, "display_id": display["display_id"],
                          "role": display["kind"], "location_id": display["location_id"], "credential_id": credential_id,
                          "sealed_secret": crypto.seal(credential["key"], secret.encode()),
                          "endpoints": self.service.endpoints()}, auth)

    def _peer(self):
        return self.client_address[0]

    def _claim(self, credential, auth, body):
        """A provisioned machine takes its reserved address: only from that address, once."""
        try:
            request = json.loads(body)
            computer, machine_hash = str(request["computer"])[:64], request["machine_hash"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._error(400, "bad_request", "computer and machine_hash are required", auth)
        if not isinstance(machine_hash, str) or not 16 <= len(machine_hash) <= 128:
            return self._error(400, "bad_request", "machine_hash is invalid", auth)
        if credential["address"] != self._peer():
            log.warning("claim for %s refused from %s", credential["address"], self._peer())
            return self._error(403, "address_mismatch", f"This code is for {credential['address']}; give this machine "
                               "that address first", auth)
        try:
            claimed = self.service.store.consume_claim(credential["credential_id"], computer, machine_hash)
        except PermissionError as exc:
            return self._error(403, "claim_used", str(exc), auth)
        log.info("address %s claimed by %s as %s", claimed["address"], computer, claimed["role"])
        self._reply(200, dict(claimed, contract=CONTRACT, service_id=self.service.store.service_id,
                              endpoints=self.service.endpoints(), time=time.time()), auth)

    def _app_enroll(self, credential, auth):
        """An application's adapter or publisher gets its producer credential, sealed to the code."""
        store = self.service.store
        app_id, orchestrator = credential["app_id"], json.loads(credential["orchestrators"])[0]
        bound = store.claimed_address(app_id, orchestrator)
        if bound and bound != self._peer():
            log.warning("app code for %s/%s refused from %s (claimed by %s)", app_id, orchestrator, self._peer(), bound)
            return self._error(403, "address_mismatch", f"{app_id}/{orchestrator} is claimed by {bound}; use the code there", auth)
        try:
            credential_id, secret = store.consume_app_code(credential["credential_id"])
        except PermissionError as exc:
            return self._error(403, "code_used", str(exc), auth)
        log.info("application %s/%s enrolled as %s from %s", app_id, orchestrator, credential_id, self._peer())
        self._reply(200, {"contract": CONTRACT, "service_id": store.service_id, "app_id": app_id,
                          "orchestrator_id": orchestrator, "credential_id": credential_id,
                          "sealed_secret": crypto.seal(credential["key"], secret.encode()),
                          "endpoints": self.service.endpoints()}, auth)

    def _telemetry(self, credential, auth, body):
        display = self.service.store.display(credential["display_id"])
        if display["kind"] != "head":
            return self._error(403, "not_a_head", "Only head displays report hardware telemetry", auth)
        try:
            sample = json.loads(body)
        except json.JSONDecodeError:
            return self._error(400, "bad_json", "Body is not JSON", auth)
        if not isinstance(sample, dict) or set(sample) - TELEMETRY_KEYS or "sampled_at" not in sample:
            return self._error(400, "bad_telemetry", "Unknown or missing telemetry fields", auth)
        if abs(time.time() - float(sample["sampled_at"])) > 300:
            return self._error(400, "stale_telemetry", "sampled_at is more than 5 minutes from service time", auth)
        if not self.service.bucket(("tel", credential["credential_id"]), 0.5, 5).take():
            return self._error(429, "rate_limited", "Telemetry rate exceeded", auth, {"Retry-After": "10"})
        node_id = display["location_id"].split(":", 1)[1]
        if not NODE_ID.match(node_id):
            return self._error(400, "bad_node", "Display location is not a head", auth)
        self.service.store.put_telemetry(node_id, sample)
        self._reply(200, {"accepted": True, "node_id": node_id}, auth)


def configure_logging(path):
    log.setLevel(logging.INFO)
    if path:
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return          # a client went away (reachability probe, viewer closed); not a service fault
        log.exception("request from %s failed", client_address[0])


def build(config):
    """Create (server, service) without starting; used by tests and the CLI."""
    service = Service(config)
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = QuietServer((config.get("host", "127.0.0.1"), int(config.get("port", 8790))), handler)
    server.daemon_threads = True
    return server, service


def report_results(store, memory, now=None, threshold=10):
    """Log a once-a-day results line per location, and warn (at most hourly) when a head's
    failures in the last hour reach the threshold. `memory` keeps what was already logged."""
    now = now or time.time()
    counts = store.result_counts(now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if memory.setdefault("day", day) != day:
        memory["day"] = day
        for place in sorted(counts):
            c = counts[place]["24h"]
            log.info("daily results %s: %d passed, %d failed in the last 24 h", place, c["pass"], c["fail"])
    for place, windows in counts.items():
        failed = windows["1h"]["fail"]
        if place.startswith("head:") and failed >= threshold and now - memory.get(place, 0) >= 3600:
            memory[place] = now
            log.warning("%s: %d failed tasks in the last hour (warning threshold %d)", place, failed, threshold)


def maintenance_loop(service, stop, interval=60):
    memory = {}
    while not stop.wait(interval):
        try:
            expired = service.store.maintain()
            if expired:
                log.info("expired %d abandoned assignments", expired)
            report_results(service.store, memory, threshold=int(service.config.get("failure_warning_per_hour", 10)))
        except Exception:
            log.exception("maintenance failed")


def serve(config):
    configure_logging(config.get("log_path"))
    server, service = build(config)
    stop = threading.Event()
    threading.Thread(target=maintenance_loop, args=(service, stop), daemon=True).start()
    log.info("Agent Arena Event Service %s listening on %s:%s (service_id %s)", __version__,
             *server.server_address[:2], service.store.service_id)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        log.info("service stopped")
