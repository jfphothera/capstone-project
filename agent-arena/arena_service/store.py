"""Durable registry, assignment state machine, event log and credentials.

One SQLite file owned by the service. Every public method takes the store lock,
so the threaded HTTP server can share a single connection safely.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid

from . import crypto
from .appearance import appearance
from .contract import TERMINAL, ContractError

EXPIRED = "expired"            # service-only terminal state for abandoned leases
DEFAULT_LEASE = 600            # executing/waiting/arrived without an explicit lease
TERMINAL_GRACE = 30            # seconds a finished assignment stays in snapshots
ABANDON_AFTER = 3600           # stale this long after lease expiry -> expired
STALE_UNLEASED = 86400         # queued (no lease) and not updated for a day -> expired (Arena 2.5.1)
RESULT_WINDOWS = (("1h", 3600), ("8h", 8 * 3600), ("24h", 86400))   # rolling pass/fail totals
REGISTRY_TYPES = ("application.updated", "orchestrator.heartbeat", "agent.registered",
                  "agent.updated", "agent.retired")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications(app_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orchestrators(app_id TEXT NOT NULL, orchestrator_id TEXT NOT NULL,
  display_name TEXT NOT NULL, location_id TEXT NOT NULL UNIQUE, last_seen REAL,
  heartbeat_seconds INTEGER NOT NULL DEFAULT 30, PRIMARY KEY(app_id, orchestrator_id));
CREATE TABLE IF NOT EXISTS heads(node_id TEXT PRIMARY KEY, location_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL, address TEXT, allowed_apps TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS displays(display_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  location_id TEXT NOT NULL, address TEXT, machine_hash TEXT, status TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(credential_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  key_b64 TEXT NOT NULL, app_id TEXT, orchestrators TEXT, display_id TEXT,
  created_at REAL NOT NULL, expires_at REAL, revoked_at REAL, used_at REAL, replaced_by TEXT,
  sealed_for_predecessor TEXT);
CREATE TABLE IF NOT EXISTS agents(app_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  display_name TEXT NOT NULL, role TEXT NOT NULL, capabilities TEXT NOT NULL,
  orchestrator_id TEXT NOT NULL, color TEXT NOT NULL UNIQUE, retired INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL, PRIMARY KEY(app_id, agent_id));
CREATE TABLE IF NOT EXISTS assignments(app_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
  agent_id TEXT NOT NULL, orchestrator_id TEXT NOT NULL, workflow_id TEXT, task_id TEXT,
  kind TEXT NOT NULL, location_id TEXT NOT NULL, state TEXT NOT NULL, wait_reason TEXT,
  visual_action TEXT, model_id TEXT, label TEXT, lease_expires_at REAL,
  occurred_at REAL NOT NULL, updated_at REAL NOT NULL, terminal_at REAL, created_at REAL NOT NULL,
  PRIMARY KEY(app_id, assignment_id));
CREATE INDEX IF NOT EXISTS ix_assign_loc ON assignments(location_id, state);
CREATE TABLE IF NOT EXISTS handoffs(app_id TEXT NOT NULL, correlation_id TEXT NOT NULL,
  sender_agent_id TEXT NOT NULL, receiver_agent_id TEXT NOT NULL, status TEXT NOT NULL,
  occurred_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(app_id, correlation_id));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT NOT NULL,
  event_id TEXT NOT NULL, type TEXT NOT NULL, received_at REAL NOT NULL, occurred_at REAL NOT NULL,
  orchestrator_id TEXT, agent_id TEXT, assignment_id TEXT, loc_from TEXT, loc_to TEXT,
  applied INTEGER NOT NULL, note TEXT, payload TEXT NOT NULL, result TEXT,
  UNIQUE(app_id, event_id));
CREATE INDEX IF NOT EXISTS ix_events_loc ON events(loc_to, seq);
CREATE INDEX IF NOT EXISTS ix_events_assignment ON events(app_id, assignment_id, seq);
CREATE TABLE IF NOT EXISTS telemetry(node_id TEXT NOT NULL, received_at REAL NOT NULL,
  payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS addresses(address TEXT PRIMARY KEY, role TEXT NOT NULL, app_id TEXT,
  orchestrator_id TEXT, label TEXT, status TEXT NOT NULL, reserved_at REAL NOT NULL, claimed_at REAL,
  computer TEXT, machine_hash TEXT);
CREATE INDEX IF NOT EXISTS ix_telemetry ON telemetry(node_id, received_at);
CREATE INDEX IF NOT EXISTS ix_telemetry_time ON telemetry(received_at);
CREATE INDEX IF NOT EXISTS ix_events_time ON events(received_at);
CREATE TABLE IF NOT EXISTS results(seq INTEGER PRIMARY KEY, received_at REAL NOT NULL, app_id TEXT NOT NULL,
  orchestrator_id TEXT, head TEXT, outcome TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_results_time ON results(received_at);
"""
# Arena 2.5.1: the hub dropped out for seconds at a time (2026-11-19, DESKTOP-REDACT10). Every snapshot recounted 24 h of
# results from the event log (a LIKE scan plus a subquery per finished task: about 5 s on the hub with 250k events),
# holding the store lock, so heads' telemetry, the adapter's events and the admin console's 2 s health check all
# waited, and ten displays resnapshot every minute. Results are now kept in their own table as they arrive (the event
# that finished the task, by its seq), and the counts are one indexed GROUP BY.
RESULTS_SQL = ("SELECT e.seq,e.received_at,e.app_id,e.orchestrator_id,e.result,(SELECT h.loc_to FROM events h WHERE "
               "h.app_id=e.app_id AND h.assignment_id=e.assignment_id AND h.seq<=e.seq AND h.loc_to LIKE 'head:%' "
               "ORDER BY h.seq DESC LIMIT 1) AS head FROM events e WHERE e.applied=1 AND e.assignment_id IS NOT NULL "
               "AND e.received_at>? AND (e.result LIKE '%\"state\":\"completed\"%' OR e.result LIKE '%\"state\":\"failed\"%')")


class Resnapshot(Exception):
    """The requested sequence is older than retention; the viewer must resnapshot."""


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path: str, *, retention_days: float = 7, max_events: int = 1_000_000,
                 max_events_per_app: int = 250_000):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "address" not in {r[1] for r in self.db.execute("PRAGMA table_info(credentials)")}:
            self.db.execute("ALTER TABLE credentials ADD COLUMN address TEXT")    # claim codes are bound to an address
        self.retention_seconds = retention_days * 86400
        self.max_events = max_events
        self.max_events_per_app = max_events_per_app
        self.changed = threading.Condition(self.lock)
        if not self.meta("service_id"):
            self.set_meta("service_id", "aes-" + uuid.uuid4().hex[:12])
        # Arena 2.5.1: read by /v2/health without the store lock (the identity never changes; the sequence is kept as
        # events are recorded), so a long read never makes the service look down.
        self.service_id = self.meta("service_id")
        self._latest = self._one("SELECT COALESCE(MAX(seq),0) AS s FROM events")["s"]
        if not self.meta("results_backfilled"):
            self._backfill_results(time.time())

    def _backfill_results(self, now):
        """Once, on the first start of 2.5.1: the last 24 h of results from the event log (the 2.5.0 rule)."""
        with self.lock:
            for row in self._all(RESULTS_SQL, (now - RESULT_WINDOWS[-1][1],)):
                state = json.loads(row["result"]).get("state")
                if state in ("completed", "failed"):
                    self.db.execute("INSERT OR IGNORE INTO results VALUES(?,?,?,?,?,?)",
                                    (row["seq"], row["received_at"], row["app_id"], row["orchestrator_id"], row["head"],
                                     "pass" if state == "completed" else "fail"))
            self.set_meta("results_backfilled", now)

    # ------------------------------------------------------------------ basics
    def _one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def meta(self, key, default=None):
        with self.lock:
            row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))
            if key == "service_id":
                self.service_id = value

    # ---------------------------------------------------------------- registry
    def add_application(self, app_id, display_name):
        with self.lock:
            self.db.execute("INSERT INTO applications(app_id,display_name,created_at) VALUES(?,?,?) "
                            "ON CONFLICT(app_id) DO UPDATE SET display_name=excluded.display_name",
                            (app_id, display_name, time.time()))

    def add_orchestrator(self, app_id, orchestrator_id, display_name):
        with self.lock:
            if not self._one("SELECT 1 FROM applications WHERE app_id=?", (app_id,)):
                raise ValueError("unknown application " + app_id)
            location = f"orch:{app_id}/{orchestrator_id}"
            self.db.execute("INSERT INTO orchestrators(app_id,orchestrator_id,display_name,location_id) "
                            "VALUES(?,?,?,?) ON CONFLICT(app_id,orchestrator_id) DO UPDATE SET "
                            "display_name=excluded.display_name", (app_id, orchestrator_id, display_name, location))
            return location

    def add_head(self, node_id, display_name, address=None, allowed_apps=("*",)):
        with self.lock:
            location = "head:" + node_id
            self.db.execute("INSERT INTO heads(node_id,location_id,display_name,address,allowed_apps,created_at) "
                            "VALUES(?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET display_name=excluded.display_name,"
                            "address=excluded.address,allowed_apps=excluded.allowed_apps,status='active'",
                            (node_id, location, display_name, address, _dump(list(allowed_apps)), time.time()))
            return location

    def add_display(self, display_id, kind, location_id, address=None):
        if kind not in ("head", "orchestrator"):
            raise ValueError("display kind must be head or orchestrator")
        with self.lock:
            table = "heads" if kind == "head" else "orchestrators"
            if not self._one(f"SELECT 1 FROM {table} WHERE location_id=?", (location_id,)):
                raise ValueError("unknown location " + location_id)
            self.db.execute("INSERT INTO displays(display_id,kind,location_id,address,status,created_at) "
                            "VALUES(?,?,?,?,'active',?) ON CONFLICT(display_id) DO UPDATE SET "
                            "location_id=excluded.location_id,address=excluded.address,status='active'",
                            (display_id, kind, location_id, address, time.time()))

    def display(self, display_id):
        with self.lock:
            return self._one("SELECT * FROM displays WHERE display_id=?", (display_id,))

    def displays_at(self, address):
        """Active displays registered at one address: the hint an installer uses to propose its identity."""
        with self.lock:
            return self._all("SELECT display_id,kind,location_id FROM displays WHERE address=? AND status='active' "
                             "ORDER BY display_id", (address,))

    def list_displays(self):
        with self.lock:
            return self._all("SELECT display_id,kind,location_id,address,status FROM displays ORDER BY display_id")

    def _disable_display(self, display_id, now):
        self.db.execute("UPDATE displays SET status='disabled' WHERE display_id=?", (display_id,))
        self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind IN ('viewer','enrollment') AND display_id=? "
                        "AND revoked_at IS NULL", (now, display_id))

    def remove_display(self, display_id):
        """Disable a display and revoke its viewer credential and any unused code. Re-adding restores it."""
        with self.lock:
            if not self.display(display_id):
                raise ValueError("unknown display " + display_id)
            self._disable_display(display_id, time.time())

    def remove_head(self, node_id):
        """Retire a head: no new work may be placed there and its displays are disabled.

        Returns (disabled display ids, open assignments still located there). Those
        assignments are not rewritten; they end through their producer or lease expiry.
        """
        with self.lock:
            head = self._one("SELECT * FROM heads WHERE node_id=?", (node_id,))
            if not head:
                raise ValueError("unknown head " + node_id)
            now = time.time()
            self.db.execute("UPDATE heads SET status='retired' WHERE node_id=?", (node_id,))
            displays = [d["display_id"] for d in self._all("SELECT display_id FROM displays WHERE location_id=?",
                                                           (head["location_id"],))]
            for display_id in displays:
                self._disable_display(display_id, now)
            open_count = self._one("SELECT COUNT(*) AS n FROM assignments WHERE location_id=? AND terminal_at IS NULL",
                                   (head["location_id"],))["n"]
            self.changed.notify_all()
            return displays, open_count

    # ------------------------------------------------------------- credentials
    def _store_key(self, credential_id, kind, secret, **binding):
        self.db.execute("INSERT INTO credentials(credential_id,kind,key_b64,app_id,orchestrators,display_id,"
                        "created_at,expires_at,address) VALUES(?,?,?,?,?,?,?,?,?)",
                        (credential_id, kind, crypto.b64u(crypto.derive_key(secret)), binding.get("app_id"),
                         _dump(binding["orchestrators"]) if binding.get("orchestrators") else None,
                         binding.get("display_id"), time.time(), binding.get("expires_at"), binding.get("address")))

    def issue_producer(self, app_id, orchestrators):
        with self.lock:
            for orch in orchestrators:
                if not self._one("SELECT 1 FROM orchestrators WHERE app_id=? AND orchestrator_id=?", (app_id, orch)):
                    raise ValueError(f"unknown orchestrator {app_id}/{orch}")
            credential_id, secret = "prd-" + uuid.uuid4().hex[:16], crypto.new_secret()
            self._store_key(credential_id, "producer", secret, app_id=app_id, orchestrators=list(orchestrators))
            return credential_id, secret

    def issue_viewer(self, display_id, machine_hash=None):
        with self.lock:
            if not self.display(display_id):
                raise ValueError("unknown display " + display_id)
            credential_id, secret = "vwr-" + uuid.uuid4().hex[:16], crypto.new_secret()
            self._store_key(credential_id, "viewer", secret, display_id=display_id)
            if machine_hash:
                self.db.execute("UPDATE displays SET machine_hash=? WHERE display_id=?", (machine_hash, display_id))
            return credential_id, secret

    def issue_enrollment(self, display_id, hours=24):
        """One-time code typed by the operator; replaces any unused earlier code."""
        with self.lock:
            if not self.display(display_id):
                raise ValueError("unknown display " + display_id)
            now = time.time()
            self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind='enrollment' AND display_id=? "
                            "AND revoked_at IS NULL AND used_at IS NULL", (now, display_id))
            code = crypto.new_code()
            self._store_key("enr-" + uuid.uuid4().hex[:16], "enrollment", crypto.normalize_code(code),
                            display_id=display_id, expires_at=now + hours * 3600)
            return code

    def enrollment_for(self, display_id):
        with self.lock:
            return self._one("SELECT * FROM credentials WHERE kind='enrollment' AND display_id=? AND revoked_at IS NULL "
                             "AND used_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                             (display_id, time.time()))

    def consume_enrollment(self, credential_id, machine_hash):
        """Mark the code used, revoke the display's earlier viewer credentials, issue a new one."""
        with self.lock:
            row = self._one("SELECT * FROM credentials WHERE credential_id=?", (credential_id,))
            if not row or row["used_at"] or row["revoked_at"]:
                raise PermissionError("enrollment code already used or revoked")
            now = time.time()
            self.db.execute("UPDATE credentials SET used_at=? WHERE credential_id=?", (now, credential_id))
            self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind='viewer' AND display_id=? "
                            "AND revoked_at IS NULL", (now, row["display_id"]))
            return self.issue_viewer(row["display_id"], machine_hash)

    # -------------------------------------------------- addresses and claims
    # A reservation says what an address is for (a head, or an application's orchestrator). Its
    # one-time claim code is used by the provisioner on the new machine, from that address, and
    # marks it taken: the hub then knows which machine holds it. App codes give an application's
    # adapter or publisher its producer credential the same way, so the secret never travels.
    ROLE_RANGES = {"head": (2, 199), "app": (200, 249)}

    @staticmethod
    def _host(address):
        parts = address.split(".")
        if len(parts) != 4 or parts[:3] != ["192", "0", "2"] or not parts[3].isdigit():
            raise ValueError(f"{address} is not on 192.0.2.0/24")
        return int(parts[3])

    def _used_addresses(self):
        used = {r["address"] for r in self._all("SELECT address FROM addresses")}
        used |= {r["address"] for r in self._all("SELECT address FROM displays WHERE address IS NOT NULL AND status='active'")}
        used |= {r["address"] for r in self._all("SELECT address FROM heads WHERE address IS NOT NULL AND status='active'")}
        return used

    def next_free(self, role):
        low, high = self.ROLE_RANGES[role]
        with self.lock:
            used = self._used_addresses()
        return next((f"192.0.2.{n}" for n in range(low, high + 1) if f"192.0.2.{n}" not in used), None)

    def reserve_address(self, address, role, *, app_id=None, orchestrator_id=None, label=None, hours=72):
        """Reserve an address for a role and return its one-time claim code (replacing any unused one)."""
        if role not in self.ROLE_RANGES:
            raise ValueError("role must be head or app")
        low, high = self.ROLE_RANGES[role]
        if not low <= self._host(address) <= high:
            raise ValueError(f"{address} is outside the {role} range .{low}-.{high}")
        with self.lock:
            if role == "app" and not self._one("SELECT 1 FROM orchestrators WHERE app_id=? AND orchestrator_id=?",
                                               (app_id, orchestrator_id)):
                raise ValueError(f"unknown orchestrator {app_id}/{orchestrator_id}")
            prior = self._one("SELECT * FROM addresses WHERE address=?", (address,))
            if prior and prior["status"] == "claimed":
                raise ValueError(f"{address} is already claimed by {prior['computer'] or 'a machine'}; release it first")
            if not prior and address in self._used_addresses():
                raise ValueError(f"{address} is already registered to a machine")
            now = time.time()
            self.db.execute("INSERT INTO addresses(address,role,app_id,orchestrator_id,label,status,reserved_at) "
                            "VALUES(?,?,?,?,?,'reserved',?) ON CONFLICT(address) DO UPDATE SET role=excluded.role,"
                            "app_id=excluded.app_id,orchestrator_id=excluded.orchestrator_id,label=excluded.label,"
                            "reserved_at=excluded.reserved_at", (address, role, app_id, orchestrator_id, label, now))
            self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind='claim' AND address=? AND revoked_at IS NULL "
                            "AND used_at IS NULL", (now, address))
            code = crypto.new_code()
            self._store_key("clm-" + uuid.uuid4().hex[:16], "claim", crypto.normalize_code(code), address=address,
                            app_id=app_id, expires_at=now + hours * 3600)
            return code

    def claim_for(self, address):
        with self.lock:
            return self._one("SELECT * FROM credentials WHERE kind='claim' AND address=? AND revoked_at IS NULL "
                             "AND used_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1", (address, time.time()))

    def consume_claim(self, credential_id, computer, machine_hash):
        """Mark the address taken by this machine; a head is registered with its display."""
        with self.lock:
            row = self._one("SELECT * FROM credentials WHERE credential_id=?", (credential_id,))
            if not row or row["used_at"] or row["revoked_at"]:
                raise PermissionError("claim code already used or revoked")
            reservation = self._one("SELECT * FROM addresses WHERE address=?", (row["address"],))
            now = time.time()
            self.db.execute("UPDATE credentials SET used_at=? WHERE credential_id=?", (now, credential_id))
            self.db.execute("UPDATE addresses SET status='claimed',claimed_at=?,computer=?,machine_hash=? WHERE address=?",
                            (now, computer, machine_hash, row["address"]))
            out = {"address": row["address"], "role": reservation["role"]}
            if reservation["role"] == "head":
                from .admin import head_identity
                node, name = head_identity(row["address"])
                self.add_display(node, "head", self.add_head(node, reservation["label"] or name, row["address"]), row["address"])
                out.update(node_id=node, display_id=node)
            else:
                out.update(app_id=reservation["app_id"], orchestrator_id=reservation["orchestrator_id"],
                           location_id=f"orch:{reservation['app_id']}/{reservation['orchestrator_id']}")
            return out

    def release_address(self, address):
        """Free an address: its reservation and any unused claim code go; registered displays are untouched."""
        with self.lock:
            if not self._one("SELECT 1 FROM addresses WHERE address=?", (address,)):
                raise ValueError(f"{address} has no reservation")
            self.db.execute("DELETE FROM addresses WHERE address=?", (address,))
            self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind='claim' AND address=? AND revoked_at IS NULL "
                            "AND used_at IS NULL", (time.time(), address))

    def list_addresses(self):
        with self.lock:
            rows = self._all("SELECT * FROM addresses ORDER BY address")
            waiting = {r["address"]: r["expires_at"] for r in self._all(
                "SELECT address,expires_at FROM credentials WHERE kind='claim' AND revoked_at IS NULL AND used_at IS NULL "
                "AND expires_at>?", (time.time(),))}
        for r in rows:
            r.pop("machine_hash", None)
            r["code_expires_at"] = waiting.get(r["address"])
        return sorted(rows, key=lambda r: self._host(r["address"]))

    def issue_app_code(self, app_id, orchestrator_id, hours=24):
        """One-time code that gives an application's adapter or publisher its producer credential."""
        with self.lock:
            if not self._one("SELECT 1 FROM orchestrators WHERE app_id=? AND orchestrator_id=?", (app_id, orchestrator_id)):
                raise ValueError(f"unknown orchestrator {app_id}/{orchestrator_id}")
            now = time.time()
            self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind='app_enrollment' AND app_id=? AND orchestrators=? "
                            "AND revoked_at IS NULL AND used_at IS NULL", (now, app_id, _dump([orchestrator_id])))
            code = crypto.new_code()
            self._store_key("apc-" + uuid.uuid4().hex[:16], "app_enrollment", crypto.normalize_code(code), app_id=app_id,
                            orchestrators=[orchestrator_id], expires_at=now + hours * 3600)
            return code

    def app_code_for(self, app_id, orchestrator_id):
        with self.lock:
            return self._one("SELECT * FROM credentials WHERE kind='app_enrollment' AND app_id=? AND orchestrators=? "
                             "AND revoked_at IS NULL AND used_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                             (app_id, _dump([orchestrator_id]), time.time()))

    def claimed_address(self, app_id, orchestrator_id):
        with self.lock:
            row = self._one("SELECT address FROM addresses WHERE role='app' AND app_id=? AND orchestrator_id=? "
                            "AND status='claimed'", (app_id, orchestrator_id))
        return row and row["address"]

    def consume_app_code(self, credential_id):
        """Mark the code used, revoke this orchestrator's earlier producer credentials, issue a new one."""
        with self.lock:
            row = self._one("SELECT * FROM credentials WHERE credential_id=?", (credential_id,))
            if not row or row["used_at"] or row["revoked_at"]:
                raise PermissionError("app code already used or revoked")
            now = time.time()
            self.db.execute("UPDATE credentials SET used_at=? WHERE credential_id=?", (now, credential_id))
            self.db.execute("UPDATE credentials SET revoked_at=? WHERE kind='producer' AND app_id=? AND orchestrators=? "
                            "AND revoked_at IS NULL", (now, row["app_id"], row["orchestrators"]))
            return self.issue_producer(row["app_id"], json.loads(row["orchestrators"]))

    def credential(self, credential_id):
        """Active credential row with its derived key, or None."""
        with self.lock:
            row = self._one("SELECT * FROM credentials WHERE credential_id=?", (credential_id,))
        if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] < time.time()):
            return None
        if row["kind"] == "enrollment" and row["used_at"]:
            return None
        row["key"] = crypto.unb64u(row["key_b64"])
        row["orchestrators"] = json.loads(row["orchestrators"]) if row["orchestrators"] else []
        return row

    def revoke(self, credential_id):
        with self.lock:
            return self.db.execute("UPDATE credentials SET revoked_at=? WHERE credential_id=? AND revoked_at IS NULL",
                                   (time.time(), credential_id)).rowcount == 1

    def rotate(self, credential_id, overlap_hours=24):
        """Issue a replacement with the same binding; the old one expires after the overlap."""
        with self.lock:
            old = self.credential(credential_id)
            if not old or old["kind"] not in ("producer", "viewer"):
                raise ValueError("only active producer or viewer credentials rotate")
            if old["kind"] == "producer":
                new_id, secret = self.issue_producer(old["app_id"], old["orchestrators"])
            else:
                # A desktop picks up its new secret through /v2/config, sealed under the old key.
                new_id, secret = self.issue_viewer(old["display_id"])
                self.db.execute("UPDATE credentials SET sealed_for_predecessor=? WHERE credential_id=?",
                                (crypto.seal(old["key"], secret.encode()), new_id))
            self.db.execute("UPDATE credentials SET replaced_by=?, expires_at=? WHERE credential_id=?",
                            (new_id, time.time() + overlap_hours * 3600, credential_id))
            return new_id, secret

    def rotation_for(self, credential):
        """The sealed replacement secret waiting for this credential's holder, if any."""
        if not credential.get("replaced_by"):
            return None
        with self.lock:
            row = self._one("SELECT credential_id,sealed_for_predecessor FROM credentials WHERE credential_id=?",
                            (credential["replaced_by"],))
        if not row or not row["sealed_for_predecessor"]:
            return None
        return {"credential_id": row["credential_id"], "sealed_secret": row["sealed_for_predecessor"]}

    def list_credentials(self):
        with self.lock:
            return self._all("SELECT credential_id,kind,app_id,orchestrators,display_id,created_at,expires_at,"
                             "revoked_at,used_at,replaced_by FROM credentials ORDER BY created_at")

    # ------------------------------------------------------------------ ingest
    def ingest(self, credential, events):
        """Apply a validated batch for the credential's application; one result per event."""
        app_id, permitted = credential["app_id"], set(credential["orchestrators"])
        results = []
        with self.lock:
            now = time.time()
            for event in events:
                try:
                    if event["orchestrator_id"] not in permitted:
                        raise ContractError("orchestrator:not_permitted")
                    if self._one("SELECT 1 FROM events WHERE app_id=? AND event_id=?", (app_id, event["event_id"])):
                        results.append({"event_id": event["event_id"], "status": "duplicate"})
                        continue
                    self.db.execute("BEGIN IMMEDIATE")
                    try:
                        outcome = self._apply(app_id, event, now)
                        seq = self._record(app_id, event, now, outcome)
                        self.db.execute("UPDATE orchestrators SET last_seen=? WHERE app_id=? AND orchestrator_id=?",
                                        (now, app_id, event["orchestrator_id"]))
                        self.db.execute("COMMIT")
                    except BaseException:
                        self.db.execute("ROLLBACK")
                        raise
                    result = {"event_id": event["event_id"], "status": "accepted" if outcome["applied"] else "recorded",
                              "seq": seq}
                    if outcome.get("note"):
                        result["note"] = outcome["note"]
                    results.append(result)
                except ContractError as exc:
                    results.append({"event_id": event.get("event_id"), "status": "rejected", "reason": str(exc)})
            self.changed.notify_all()
        return results

    def _record(self, app_id, event, now, outcome):
        payload = {k: v for k, v in event.items() if k != "occurred_at_epoch"}
        cursor = self.db.execute(
            "INSERT INTO events(app_id,event_id,type,received_at,occurred_at,orchestrator_id,agent_id,assignment_id,"
            "loc_from,loc_to,applied,note,payload,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (app_id, event["event_id"], event["type"], now, event["occurred_at_epoch"], event["orchestrator_id"],
             event.get("agent_id") or event.get("sender_agent_id"), event.get("assignment_id"),
             outcome.get("loc_from"), outcome.get("loc_to"), int(outcome["applied"]), outcome.get("note"),
             _dump(payload), _dump(outcome["result"]) if outcome.get("result") is not None else None))
        seq = cursor.lastrowid
        result = outcome.get("result")
        if outcome["applied"] and event.get("assignment_id") and isinstance(result, dict) and result.get("state") in ("completed", "failed"):
            # The head the work ran at: the last head this assignment was at, this event included (as before 2.5.1).
            head = self._one("SELECT loc_to FROM events WHERE app_id=? AND assignment_id=? AND seq<=? AND loc_to LIKE 'head:%' "
                             "ORDER BY seq DESC LIMIT 1", (app_id, event["assignment_id"], seq))
            self.db.execute("INSERT OR IGNORE INTO results VALUES(?,?,?,?,?,?)",
                            (seq, now, app_id, event["orchestrator_id"], head["loc_to"] if head else None,
                             "pass" if result["state"] == "completed" else "fail"))
        self._latest = max(self._latest, seq)
        return seq

    def _location_allowed(self, app_id, location):
        if location.startswith("orch:"):
            row = self._one("SELECT app_id FROM orchestrators WHERE location_id=?", (location,))
            if not row:
                raise ContractError("location:unknown")
            if row["app_id"] != app_id:
                raise ContractError("location:other_application")
            return
        head = self._one("SELECT * FROM heads WHERE location_id=?", (location,))
        if not head or head["status"] != "active":
            raise ContractError("location:unknown_head")
        allowed = json.loads(head["allowed_apps"])
        if "*" not in allowed and app_id not in allowed:
            raise ContractError("location:head_not_permitted")

    def _color(self, app_id, agent_id):
        salt = 0
        while True:
            digest = hashlib.sha256(f"{app_id}/{agent_id}:{salt}".encode()).hexdigest()
            color = f"hsl({int(digest[:8], 16) % 360} 65% 67%)"
            if not self._one("SELECT 1 FROM agents WHERE color=?", (color,)):
                return color
            salt += 1

    def _agent(self, app_id, agent_id):
        return self._one("SELECT * FROM agents WHERE app_id=? AND agent_id=?", (app_id, agent_id))

    def _apply(self, app_id, ev, now):
        kind, orch = ev["type"], ev["orchestrator_id"]
        if kind == "application.updated":
            self.db.execute("UPDATE applications SET display_name=? WHERE app_id=?", (ev["display_name"], app_id))
            return {"applied": True}
        if kind == "orchestrator.heartbeat":
            changes = {k: ev[k] for k in ("display_name", "heartbeat_seconds") if k in ev}
            if changes:
                sets = ",".join(f"{k}=?" for k in changes)
                self.db.execute(f"UPDATE orchestrators SET {sets} WHERE app_id=? AND orchestrator_id=?",
                                (*changes.values(), app_id, orch))
            return {"applied": True}
        if kind.startswith("agent."):
            return self._apply_agent(app_id, ev, now)
        if kind.startswith("handoff."):
            for field in ("sender_agent_id", "receiver_agent_id"):
                if not self._agent(app_id, ev[field]):
                    raise ContractError(f"{field}:unknown_agent")
            status = "sent" if kind == "handoff.sent" else "received"
            prior = self._one("SELECT * FROM handoffs WHERE app_id=? AND correlation_id=?", (app_id, ev["correlation_id"]))
            if prior and prior["occurred_at"] > ev["occurred_at_epoch"]:
                return {"applied": False, "note": "late"}
            self.db.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?) ON CONFLICT(app_id,correlation_id) DO UPDATE SET "
                            "status=excluded.status,occurred_at=excluded.occurred_at,updated_at=excluded.updated_at",
                            (app_id, ev["correlation_id"], ev["sender_agent_id"], ev["receiver_agent_id"], status,
                             ev["occurred_at_epoch"], now))
            location = self._one("SELECT location_id FROM orchestrators WHERE app_id=? AND orchestrator_id=?", (app_id, orch))
            return {"applied": True, "loc_from": location["location_id"], "loc_to": location["location_id"]}
        return self._apply_assignment(app_id, ev, now)

    def _apply_agent(self, app_id, ev, now):
        kind, orch = ev["type"], ev["orchestrator_id"]
        existing = self._agent(app_id, ev["agent_id"])
        if existing and existing["orchestrator_id"] != orch:
            raise ContractError("agent:owned_by_other_orchestrator")
        if kind == "agent.registered":
            if existing:
                self.db.execute("UPDATE agents SET display_name=?,role=?,capabilities=?,retired=0,updated_at=? "
                                "WHERE app_id=? AND agent_id=?",
                                (ev["display_name"], ev["role"], _dump(ev.get("capabilities", [])), now, app_id, ev["agent_id"]))
            else:
                self.db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,0,?)",
                                (app_id, ev["agent_id"], ev["display_name"], ev["role"], _dump(ev.get("capabilities", [])),
                                 orch, self._color(app_id, ev["agent_id"]), now))
        elif not existing:
            raise ContractError("agent_id:unknown_agent")
        elif kind == "agent.updated":
            changes = {k: ev[k] for k in ("display_name", "role") if k in ev}
            if "capabilities" in ev:
                changes["capabilities"] = _dump(ev["capabilities"])
            changes["updated_at"] = now
            sets = ",".join(f"{k}=?" for k in changes)
            self.db.execute(f"UPDATE agents SET {sets} WHERE app_id=? AND agent_id=?", (*changes.values(), app_id, ev["agent_id"]))
        else:
            self.db.execute("UPDATE agents SET retired=1,updated_at=? WHERE app_id=? AND agent_id=?", (now, app_id, ev["agent_id"]))
        return {"applied": True, "result": self._agent_view(self._agent(app_id, ev["agent_id"]))}

    def _apply_assignment(self, app_id, ev, now):
        kind, orch = ev["type"], ev["orchestrator_id"]
        row = self._one("SELECT * FROM assignments WHERE app_id=? AND assignment_id=?", (app_id, ev["assignment_id"]))
        home = f"orch:{app_id}/{orch}"
        if kind == "assignment.created":
            if row:
                raise ContractError("assignment:exists")
            agent = self._agent(app_id, ev["agent_id"])
            if not agent or agent["retired"]:
                raise ContractError("agent_id:unknown_agent")
            if agent["orchestrator_id"] != orch:
                raise ContractError("agent:owned_by_other_orchestrator")
            location = ev.get("location", home)
            self._location_allowed(app_id, location)
            state = ev.get("state", "queued")
            if state in TERMINAL:
                raise ContractError("state:terminal_on_create")
            row = {"app_id": app_id, "assignment_id": ev["assignment_id"], "agent_id": ev["agent_id"],
                   "orchestrator_id": orch, "workflow_id": ev.get("workflow_id"), "task_id": ev.get("task_id"),
                   "kind": ev["kind"], "location_id": location, "state": state,
                   "wait_reason": ev.get("wait_reason"), "visual_action": ev.get("visual_action"),
                   "model_id": ev.get("model_id"), "label": ev.get("label"),
                   "lease_expires_at": self._lease(state, ev, now), "occurred_at": ev["occurred_at_epoch"],
                   "updated_at": now, "terminal_at": None, "created_at": now}
            self.db.execute(f"INSERT INTO assignments({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
            return {"applied": True, "loc_from": None, "loc_to": location, "result": self._assignment_view(row, now)}
        if not row:
            raise ContractError("assignment_id:unknown_assignment")
        if row["orchestrator_id"] != orch:
            raise ContractError("assignment:owned_by_other_orchestrator")
        current = row["location_id"]
        if row["state"] in TERMINAL or row["state"] == EXPIRED:
            return {"applied": False, "note": "terminal", "loc_from": current, "loc_to": current}
        if ev["occurred_at_epoch"] < row["occurred_at"]:
            return {"applied": False, "note": "late", "loc_from": current, "loc_to": current}
        note = None
        if kind == "assignment.heartbeat":
            row["lease_expires_at"] = now + ev["lease_seconds"]
        else:
            if kind == "assignment.moved":
                self._location_allowed(app_id, ev["to_location"])
                if "from_location" in ev and ev["from_location"] != current:
                    note = "from_location_mismatch"
                row["location_id"] = ev["to_location"]
                default_state = "arrived" if ev["to_location"].startswith("head:") else row["state"]
                row["state"] = ev.get("state", default_state)
            else:
                row["state"] = ev["state"]
            for field in ("workflow_id", "task_id", "wait_reason", "visual_action", "model_id", "label", "kind"):
                if field in ev:
                    row[field] = ev[field]
            if row["state"] != "waiting":
                row["wait_reason"] = None
            if row["state"] in TERMINAL:
                row["terminal_at"], row["lease_expires_at"] = now, None
            else:
                row["lease_expires_at"] = self._lease(row["state"], ev, now)
        row["occurred_at"], row["updated_at"] = ev["occurred_at_epoch"], now
        sets = ",".join(f"{k}=?" for k in row if k not in ("app_id", "assignment_id"))
        self.db.execute(f"UPDATE assignments SET {sets} WHERE app_id=? AND assignment_id=?",
                        (*(v for k, v in row.items() if k not in ("app_id", "assignment_id")), app_id, row["assignment_id"]))
        return {"applied": True, "note": note, "loc_from": current, "loc_to": row["location_id"],
                "result": self._assignment_view(row, now)}

    @staticmethod
    def _lease(state, ev, now):
        if "lease_seconds" in ev:
            return now + ev["lease_seconds"]
        return now + DEFAULT_LEASE if state in ("executing", "waiting", "arrived") else None

    # ------------------------------------------------------------- read model
    @staticmethod
    def _look(app_id, agent_id):
        """The agent's figure (Arena 2.4.0): its body, palette and color come from appearance.py, not the stored
        hash color (the agents table keeps that, unique, for older records)."""
        look = appearance(app_id, agent_id)
        return {"color": look["color"], "body": look["body"], "palette": look["palette"],
                "appearance_group": look["group"]}

    def _agent_view(self, row):
        view = {"agent_key": f"{row['app_id']}/{row['agent_id']}", "app_id": row["app_id"], "agent_id": row["agent_id"],
                "display_name": row["display_name"], "role": row["role"],
                "capabilities": json.loads(row["capabilities"]), "orchestrator_id": row["orchestrator_id"],
                "home_location": f"orch:{row['app_id']}/{row['orchestrator_id']}", "color": row["color"],
                "retired": bool(row["retired"])}
        view.update(self._look(row["app_id"], row["agent_id"]))
        return view

    def _producer_offline(self, app_id, orchestrator_id, now):
        orch = self._one("SELECT last_seen,heartbeat_seconds FROM orchestrators WHERE app_id=? AND orchestrator_id=?",
                         (app_id, orchestrator_id))
        return not orch or orch["last_seen"] is None or now - orch["last_seen"] > 3 * orch["heartbeat_seconds"]

    def _assignment_view(self, row, now, offline=None):
        render = row["state"]
        if render not in TERMINAL and render != EXPIRED:
            # offline: a per-call memo of producer state (the snapshot asks once per orchestrator, not per assignment)
            key = (row["app_id"], row["orchestrator_id"])
            if offline is None or key not in offline:
                down = self._producer_offline(*key, now)
                if offline is not None:
                    offline[key] = down
            else:
                down = offline[key]
            if down:
                render = "producer_offline"
            elif row["lease_expires_at"] and now > row["lease_expires_at"]:
                render = "stale"
        view = {k: row[k] for k in ("app_id", "assignment_id", "agent_id", "orchestrator_id", "workflow_id", "task_id",
                                    "kind", "location_id", "state", "wait_reason", "visual_action", "model_id", "label",
                                    "lease_expires_at", "occurred_at", "updated_at", "terminal_at")}
        view.update(assignment_key=f"{row['app_id']}/{row['assignment_id']}",
                    agent_key=f"{row['app_id']}/{row['agent_id']}", render_state=render)
        return view

    def registry(self):
        with self.lock:
            now = time.time()
            orchestrators = self._all("SELECT * FROM orchestrators ORDER BY app_id,orchestrator_id")
            for orch in orchestrators:
                orch["producer_state"] = "offline" if self._producer_offline(orch["app_id"], orch["orchestrator_id"], now) else "online"
            heads = self._all("SELECT node_id,location_id,display_name,address,status FROM heads WHERE status='active' "
                              "ORDER BY node_id")
            for head in heads:
                sample = self._one("SELECT payload,received_at FROM telemetry WHERE node_id=? ORDER BY received_at DESC LIMIT 1",
                                   (head["node_id"],))
                head["telemetry"] = dict(json.loads(sample["payload"]), received_at=sample["received_at"]) if sample else None
            return {"applications": self._all("SELECT app_id,display_name,status FROM applications ORDER BY app_id"),
                    "orchestrators": orchestrators, "heads": heads,
                    "agents": [self._agent_view(r) for r in self._all("SELECT * FROM agents ORDER BY app_id,agent_id")]}

    def snapshot(self, view="cluster"):
        with self.lock:
            now = time.time()
            data = self.registry()
            rows = self._all("SELECT * FROM assignments WHERE terminal_at IS NULL OR terminal_at>?", (now - TERMINAL_GRACE,))
            memo = {}
            assignments = [self._assignment_view(r, now, memo) for r in rows if r["state"] != EXPIRED]
            handoffs = self._all("SELECT * FROM handoffs WHERE updated_at>?", (now - 120,))
            seq = self._one("SELECT COALESCE(MAX(seq),0) AS s FROM events")["s"]
            kind, _, value = view.partition(":")
            agents = data["agents"]
            if kind == "location":
                assignments = [a for a in assignments if a["location_id"] == value]
                if value.startswith("orch:"):
                    agents = [a for a in agents if a["home_location"] == value]
                else:
                    present = {a["agent_key"] for a in assignments}
                    agents = [a for a in agents if a["agent_key"] in present]
            elif kind == "app":
                assignments = [a for a in assignments if a["app_id"] == value]
                agents = [a for a in agents if a["app_id"] == value]
            elif kind == "workflow":
                app_id, _, workflow = value.partition("/")
                assignments = [a for a in assignments if a["app_id"] == app_id and a["workflow_id"] == workflow]
                present = {a["agent_key"] for a in assignments}
                agents = [a for a in agents if a["agent_key"] in present]
            elif kind == "role":
                agents = [a for a in agents if a["role"] == value]
                keys = {a["agent_key"] for a in agents}
                assignments = [a for a in assignments if a["agent_key"] in keys]
            elif view != "cluster":
                raise ValueError("unknown view")
            data.update(view=view, agents=agents, assignments=assignments, handoffs=handoffs,
                        as_of_seq=seq, server_time=now, min_seq=self._min_seq(), result_counts=self.result_counts(now))
            return data

    def result_counts(self, now=None):
        """Rolling pass/fail totals over 1 h, 8 h and 24 h, counted from the event history so a
        restart never resets them and every display agrees. A finished task counts for the whole
        cluster, for its orchestrator, and for the last head it was at (where its work ran).
        Canceled and expired tasks count as neither."""
        now = now or time.time()
        windows_sql = ",".join(f"SUM(received_at>=?) AS w{i}" for i in range(len(RESULT_WINDOWS)))
        with self.lock:
            rows = self._all(f"SELECT app_id,orchestrator_id,head,outcome,{windows_sql} FROM results WHERE received_at>? "
                             "GROUP BY app_id,orchestrator_id,head,outcome",
                             tuple(now - span for _, span in RESULT_WINDOWS) + (now - RESULT_WINDOWS[-1][1],))
        counts = {}
        for row in rows:
            for place in {"cluster", f"orch:{row['app_id']}/{row['orchestrator_id']}", row["head"]} - {None}:
                windows = counts.setdefault(place, {w: {"pass": 0, "fail": 0} for w, _ in RESULT_WINDOWS})
                for i, (name, _) in enumerate(RESULT_WINDOWS):
                    windows[name][row["outcome"]] += row[f"w{i}"] or 0
        return counts

    def _min_seq(self):
        return self._one("SELECT COALESCE(MIN(seq),0) AS s FROM events")["s"]

    def events_after(self, after, view="cluster", limit=500):
        with self.lock:
            low = self._min_seq()
            if after and low and after < low - 1:
                raise Resnapshot()
            rows = self._all("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, max(1, min(limit, 1000))))
            kind, _, value = view.partition(":")
            out = []
            for row in rows:
                if kind == "location" and row["type"] not in REGISTRY_TYPES and value not in (row["loc_from"], row["loc_to"]):
                    continue
                if kind == "app" and row["app_id"] != value:
                    continue
                result = json.loads(row["result"]) if row["result"] else None
                if row["type"].startswith("agent.") and isinstance(result, dict) and "agent_id" in result:
                    # The rooms replace state.agents[key] with this result: give records from before 2.4.0 a figure too.
                    result.update(self._look(result.get("app_id") or row["app_id"], result["agent_id"]))
                out.append({"seq": row["seq"], "app_id": row["app_id"], "event_id": row["event_id"], "type": row["type"],
                            "received_at": row["received_at"], "occurred_at": row["occurred_at"],
                            "applied": bool(row["applied"]), "note": row["note"], "from_location": row["loc_from"],
                            "to_location": row["loc_to"], "payload": json.loads(row["payload"]),
                            "result": result})
            last = rows[-1]["seq"] if rows else after
            return {"events": out, "next": last, "server_time": time.time()}

    def latest_seq(self):
        return self._latest      # kept by _record and maintain (Arena 2.5.1): no lock, no query

    # --------------------------------------------------------------- telemetry
    def put_telemetry(self, node_id, payload):
        with self.lock:
            self.db.execute("INSERT INTO telemetry VALUES(?,?,?)", (node_id, time.time(), _dump(payload)))

    # ------------------------------------------------------------- maintenance
    def maintain(self, now=None):
        """Expire abandoned leases, prune terminal rows and enforce event retention."""
        now = now or time.time()
        with self.lock:
            # Arena 2.5.1: also lease-less work (queued) not updated for STALE_UNLEASED. Before, only leased work could
            # expire, and on the hub 106 queued tasks of stopped Testing Lab workers stayed for up to 157 h.
            abandoned = self._all("SELECT * FROM assignments WHERE terminal_at IS NULL AND ((lease_expires_at IS NOT NULL "
                                  "AND lease_expires_at<?) OR (lease_expires_at IS NULL AND updated_at<?))",
                                  (now - ABANDON_AFTER, now - STALE_UNLEASED))
            for row in abandoned:
                self.db.execute("UPDATE assignments SET state=?,terminal_at=?,lease_expires_at=NULL,updated_at=? "
                                "WHERE app_id=? AND assignment_id=?", (EXPIRED, now, now, row["app_id"], row["assignment_id"]))
                row.update(state=EXPIRED, terminal_at=now, lease_expires_at=None)
                payload = {"type": "assignment.state", "event_id": "aes-expire-" + uuid.uuid4().hex[:12],
                           "orchestrator_id": row["orchestrator_id"], "assignment_id": row["assignment_id"],
                           "state": EXPIRED, "source_ref": "service:lease_abandoned"}
                self.db.execute("INSERT INTO events(app_id,event_id,type,received_at,occurred_at,orchestrator_id,agent_id,"
                                "assignment_id,loc_from,loc_to,applied,note,payload,result) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                                (row["app_id"], payload["event_id"], "assignment.state", now, now, row["orchestrator_id"],
                                 row["agent_id"], row["assignment_id"], row["location_id"], row["location_id"],
                                 "service:lease_abandoned", _dump(payload), _dump(self._assignment_view(row, now))))
            if abandoned:
                self._latest = self._one("SELECT COALESCE(MAX(seq),0) AS s FROM events")["s"]
            self.db.execute("DELETE FROM results WHERE received_at<?", (now - RESULT_WINDOWS[-1][1] - 3600,))
            self.db.execute("DELETE FROM assignments WHERE terminal_at IS NOT NULL AND terminal_at<?", (now - 86400,))
            self.db.execute("DELETE FROM handoffs WHERE updated_at<?", (now - 86400,))
            self.db.execute("DELETE FROM telemetry WHERE received_at<?", (now - self.retention_seconds,))
            self.db.execute("DELETE FROM events WHERE received_at<?", (now - self.retention_seconds,))
            for app in self._all("SELECT app_id, COUNT(*) AS n FROM events GROUP BY app_id"):
                if app["n"] > self.max_events_per_app:
                    self.db.execute("DELETE FROM events WHERE seq IN (SELECT seq FROM events WHERE app_id=? ORDER BY seq LIMIT ?)",
                                    (app["app_id"], app["n"] - self.max_events_per_app))
            total = self._one("SELECT COUNT(*) AS n FROM events")["n"]
            if total > self.max_events:
                self.db.execute("DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?)",
                                (total - self.max_events,))
            if abandoned:
                self.changed.notify_all()
            return len(abandoned)
