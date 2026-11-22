"""Persistent local storage for the simulated customer-service operation.

SQLite, one file, created on first use. A restart must not lose case history,
agent findings, approvals or simulated transactions, so everything
operational lives here rather than in memory.

Three kinds of record are kept deliberately separate:

**Source records** - the imported synthetic ERP and ticket pool. Read-mostly
reference data. Importing again is idempotent.

**Operational records** - cases, messages, checklist items, tool calls,
approvals, ERP calls, RMAs, shipments, notifications. These carry a `run_id`
which is NULL for live operation and set for an evaluation replay, so a
replay can never inflate the business counts in the trends view.

**Audit events** - append-only. Nothing updates or deletes a row in
`audit_events`, including failed and rejected actions. Editing a case cannot
erase what was decided earlier.

Identifier formats are fictional and fixed: cases CASE-######, ERP calls
CA######, RMAs R######, shipments SHP-########.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from . import config

SCHEMA_VERSION = 11

# Columns added after the first schema was in use. `CREATE TABLE IF NOT
# EXISTS` will not add a column to a database that already exists, so these
# are applied with ALTER TABLE on every open. Adding a nullable column is the
# only migration shape used here, which keeps an existing operations.db -
# including its case history and audit trail - usable without a rebuild.
ADDED_COLUMNS = (
    ('cases','case_version','INTEGER NOT NULL DEFAULT 0'),
    ('cases','safety_hold','INTEGER NOT NULL DEFAULT 0'),
    ('erp_calls', 'service_status', 'TEXT'),
    ('rmas', 'idempotency_key', 'TEXT'),
    ('rmas', 'prerequisite_rma_id', 'TEXT'),
    ('rmas', 'return_method', "TEXT DEFAULT 'drop_off_label'"),
    ('rmas', 'received_at', 'TEXT'),
    ('rmas', 'received_by', 'TEXT'),
    ('rmas', 'receipt_note', 'TEXT'),
    ('sim_shipments', 'return_method', 'TEXT'),
    ('sim_shipments', 'pickup_attempts', 'INTEGER DEFAULT 0'),
    ('messages', 'metadata', 'TEXT'),
    ('messages', 'parent_message_id', 'TEXT'),
    ('messages', 'idempotency_key', 'TEXT'),
    ('qa_notifications', 'determination_note', 'TEXT'),
    # (table, column, declaration)
    ("cases", "rma_outcome", "TEXT"),
    ("cases", "revision_confirmation", "TEXT"),
    ("cases", "revision_confirmed_by", "TEXT"),
    ("cases", "revision_confirmed_at", "TEXT"),
    ("checklist_items", "consequence", "TEXT"),
    ("checklist_items", "decision_class", "TEXT"),
    ("conversations", "handed_off", "INTEGER"),
    ("arena_events", "tool_node", "TEXT"),
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS activity_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    investigation_id TEXT NOT NULL,
    conversation_id TEXT,
    case_id TEXT,
    turn_id TEXT,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    duration_ms REAL,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS activity_conversation ON activity_events(conversation_id, event_id);
CREATE INDEX IF NOT EXISTS activity_case ON activity_events(case_id, event_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT );

-- ---------------------------------------------------------------- source
CREATE TABLE IF NOT EXISTS customers (
    account_id      TEXT PRIMARY KEY,
    customer_name   TEXT NOT NULL,
    customer_kind   TEXT,
    date_of_birth   TEXT,
    phone           TEXT,
    mobile_phone    TEXT,
    email           TEXT,
    street          TEXT,
    city            TEXT,
    state           TEXT,
    preferred_contact TEXT,
    source_identity_id INTEGER,
    source_system   TEXT );

CREATE TABLE IF NOT EXISTS devices (
    serial_number   TEXT PRIMARY KEY,
    model           TEXT NOT NULL,
    revision        TEXT,
    revision_status TEXT NOT NULL DEFAULT 'inferred',
    revision_basis  TEXT,
    product_family  TEXT,
    status          TEXT,
    install_date    TEXT,
    last_meter_recal TEXT,
    account_id      TEXT REFERENCES customers(account_id),
    prescription_id TEXT );

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    account_id      TEXT REFERENCES customers(account_id),
    product_prescribed TEXT,
    device_serial_number TEXT,
    order_date      TEXT,
    phase           TEXT,
    distributor_status TEXT,
    integration_status TEXT );

CREATE TABLE IF NOT EXISTS source_shipments (
    tracking_number TEXT PRIMARY KEY,
    carrier         TEXT,
    order_id        TEXT,
    account_id      TEXT,
    device_serial_number TEXT,
    status          TEXT,
    last_scan       TEXT,
    shipped_date    TEXT );

-- The imported ticket pool. NOT processed automatically; the arrival
-- simulator draws from here.
CREATE TABLE IF NOT EXISTS ticket_pool (
    pool_id         TEXT PRIMARY KEY,
    source_ticket_id INTEGER,
    archetype       TEXT,
    subject         TEXT,
    body            TEXT,
    product         TEXT,
    source_identity_id INTEGER,
    account_id      TEXT,
    device_serial_number TEXT,
    created_at      TEXT,
    tags            TEXT,
    released        INTEGER NOT NULL DEFAULT 0 );

-- ----------------------------------------------------------- operational
CREATE TABLE IF NOT EXISTS cases (
    case_id         TEXT PRIMARY KEY,
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT,
    channel         TEXT NOT NULL DEFAULT 'email',
    status          TEXT NOT NULL,
    priority        TEXT,
    priority_reason TEXT,
    priority_overridden_by TEXT,
    subject         TEXT,
    account_id      TEXT,
    customer_name   TEXT,
    serial_number   TEXT,
    model           TEXT,
    revision        TEXT,
    identity_verified INTEGER NOT NULL DEFAULT 0,
    verification_points TEXT,
    is_complaint    INTEGER,
    complaint_reason TEXT,
    is_adverse_event INTEGER,
    ae_certainty    TEXT,
    classification  TEXT,
    rma_needed      INTEGER NOT NULL DEFAULT 0,
    tracking_number TEXT,
    tracking_status TEXT,
    ai_state        TEXT NOT NULL DEFAULT 'queued',
    assigned_to     TEXT,
    resolution      TEXT,
    resolved        INTEGER,
    pool_id         TEXT );

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    direction       TEXT NOT NULL,          -- inbound | outbound | internal
    channel         TEXT NOT NULL,          -- email | chat | qa_notification
    author          TEXT,
    subject         TEXT,
    body            TEXT,
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft|approved|sent|rejected
    approved_by     TEXT,
    approved_at     TEXT );

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    account_id      TEXT,
    channel         TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    state_json      TEXT NOT NULL );

CREATE TABLE IF NOT EXISTS checklist_items (
    item_id         TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    run_id          TEXT,
    seq             INTEGER NOT NULL,
    action          TEXT NOT NULL,
    description     TEXT,
    actor           TEXT NOT NULL,          -- ai | representative
    status          TEXT NOT NULL,          -- completed|failed|pending|
                                            -- requires_approval|not_applicable
    tool_result     TEXT,
    evidence        TEXT,
    missing_info    TEXT,
    approval_decision TEXT,
    authorizer      TEXT,
    decided_at      TEXT,
    feedback        TEXT,
    executed_at     TEXT NOT NULL );

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    at              TEXT NOT NULL,
    tool            TEXT NOT NULL,
    productive      INTEGER,
    summary         TEXT,
    detail          TEXT );

CREATE TABLE IF NOT EXISTS retrieved_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    at              TEXT NOT NULL,
    chunk_id        TEXT,
    document_id     TEXT,
    procedure_id    TEXT,
    similarity      REAL,
    rank            INTEGER,
    selected_by     TEXT,
    status          TEXT,
    reason          TEXT );

CREATE TABLE IF NOT EXISTS reasoning_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    at              TEXT NOT NULL,
    seq             INTEGER,
    kind            TEXT,
    action          TEXT,
    thought         TEXT,
    observation     TEXT,
    productive      INTEGER );

CREATE TABLE IF NOT EXISTS recommendations (
    rec_id          TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- response|product_movement|priority
    status          TEXT NOT NULL,          -- proposed|approved|rejected|revised
    payload         TEXT,
    original_payload TEXT,
    feedback        TEXT,
    revision_count  INTEGER NOT NULL DEFAULT 0,
    decided_by      TEXT,
    decided_at      TEXT );

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    at              TEXT NOT NULL,
    subject_type    TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    decision        TEXT NOT NULL,
    authorizer      TEXT NOT NULL,
    note            TEXT );

-- -------------------------------------------------- simulated ERP / logistics
CREATE TABLE IF NOT EXISTS erp_calls (
    call_id         TEXT PRIMARY KEY,       -- CA######
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    call_type       TEXT NOT NULL,
    status          TEXT NOT NULL,          -- proposed|approved|executed|
                                            -- failed|canceled|rejected
    account_id      TEXT,
    serial_number   TEXT,
    payload         TEXT,
    result          TEXT,
    executed_at     TEXT,
    idempotency_key TEXT UNIQUE );

CREATE TABLE IF NOT EXISTS rmas (
    rma_id          TEXT PRIMARY KEY,       -- R######
    call_id         TEXT REFERENCES erp_calls(call_id),
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    movement_type   TEXT NOT NULL,          -- device_return|part_outbound|...
    direction       TEXT NOT NULL,          -- inbound|outbound
    items           TEXT,
    reason          TEXT,
    status          TEXT NOT NULL,
    approved_by     TEXT,
    approved_at     TEXT );

CREATE TABLE IF NOT EXISTS sim_shipments (
    shipment_id     TEXT PRIMARY KEY,       -- SHP-########
    rma_id          TEXT REFERENCES rmas(rma_id),
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    direction       TEXT NOT NULL,
    carrier         TEXT,
    service         TEXT,
    tracking_number TEXT UNIQUE,
    label_path      TEXT,
    contents        TEXT,
    ship_to         TEXT,
    ship_from       TEXT,
    status          TEXT NOT NULL,
    failure_reason  TEXT,
    idempotency_key TEXT UNIQUE );

CREATE TABLE IF NOT EXISTS shipment_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id     TEXT NOT NULL REFERENCES sim_shipments(shipment_id),
    at              TEXT NOT NULL,
    status          TEXT NOT NULL,
    location        TEXT,
    note            TEXT );

CREATE TABLE IF NOT EXISTS qa_notifications (
    notification_id TEXT PRIMARY KEY,
    case_id         TEXT REFERENCES cases(case_id),
    run_id          TEXT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,     -- adverse_event|possible_adverse_event|
                                       -- qc_discrepancy|qa_document_concern
    certainty       TEXT,
    body            TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    determination   TEXT,
    determined_by   TEXT,
    determined_at   TEXT );

-- ------------------------------------------------------------- learning
CREATE TABLE IF NOT EXISTS improvement_proposals (
    proposal_id     TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    trigger_summary TEXT,
    body            TEXT,
    evidence        TEXT,
    review_status   TEXT NOT NULL DEFAULT 'pending_review',
    reviewed_by     TEXT,
    reviewed_at     TEXT );

-- --------------------------------------------------------------- audit
CREATE TABLE IF NOT EXISTS audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    run_id          TEXT,
    case_id         TEXT,
    event_type      TEXT NOT NULL,
    actor           TEXT,
    summary         TEXT,
    detail          TEXT );

-- -------------------------------------------------------- evaluation runs
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    label           TEXT,
    mode            TEXT,
    report          TEXT );

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY, value INTEGER NOT NULL );

CREATE INDEX IF NOT EXISTS ix_cases_run       ON cases(run_id);
CREATE INDEX IF NOT EXISTS ix_cases_status    ON cases(status);
CREATE INDEX IF NOT EXISTS ix_msg_case        ON messages(case_id);
CREATE INDEX IF NOT EXISTS ix_check_case      ON checklist_items(case_id);
CREATE INDEX IF NOT EXISTS ix_tool_case       ON tool_calls(case_id);
CREATE INDEX IF NOT EXISTS ix_src_case        ON retrieved_sources(case_id);
CREATE INDEX IF NOT EXISTS ix_audit_case      ON audit_events(case_id);
CREATE INDEX IF NOT EXISTS ix_pool_released   ON ticket_pool(released);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _unj(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


class Store:
    """Thread-safe-enough SQLite wrapper for a single-process demonstration."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or config.DB_PATH
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                     timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            from .coordination import SCHEMA as COORDINATION_SCHEMA
            self._conn.executescript(COORDINATION_SCHEMA)
            from .erp_research import SCHEMA as CONTACT_SCHEMA
            self._conn.executescript(CONTACT_SCHEMA)
            from .planning import SCHEMA as PLANNING_SCHEMA
            self._conn.executescript(PLANNING_SCHEMA)
            from .collaboration import SCHEMA as COLLABORATION_SCHEMA
            self._conn.executescript(COLLABORATION_SCHEMA)
            from .independent_review import SCHEMA as INDEPENDENT_REVIEW_SCHEMA
            self._conn.executescript(INDEPENDENT_REVIEW_SCHEMA)
            from .revision import SCHEMA as REVISION_SCHEMA
            self._conn.executescript(REVISION_SCHEMA)
            from .telemetry import SCHEMA as TELEMETRY_SCHEMA
            self._conn.executescript(TELEMETRY_SCHEMA)
            from .inference import SCHEMA as INFERENCE_SCHEMA
            self._conn.executescript(INFERENCE_SCHEMA)
            from .scheduler import SCHEMA as SCHEDULER_SCHEMA
            self._conn.executescript(SCHEDULER_SCHEMA)
            from .cluster import SCHEMA as CLUSTER_SCHEMA
            self._conn.executescript(CLUSTER_SCHEMA)
            from .execution import SCHEMA as EXECUTION_SCHEMA
            self._conn.executescript(EXECUTION_SCHEMA)
            from .mcp_services import SCHEMA as MCP_SCHEMA
            self._conn.executescript(MCP_SCHEMA)
            for table in ('case_events','case_notes','case_tag_events','specialist_findings','conversation_findings','contact_observations','contact_decisions','approved_contacts','inference_snapshots','inference_calls','response_reviews','validation_packages','validation_verdicts','correction_cycles','artifact_versions','artifact_validation_records','execution_task_transitions','execution_results','execution_recovery_records','worker_health_snapshots','scheduler_decisions'):
                for operation in ('UPDATE','DELETE'):
                    self._conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Append-only record; append a correction instead'); END")
            # Audit, approval and activity records are append-only for the
            # application. (A database owner can still drop triggers or edit the
            # file directly; this protects against ordinary application code,
            # not privileged access. Deployment needs file ACLs and backups.)
            for table in ('audit_events', 'approvals'):
                for operation in ('UPDATE', 'DELETE'):
                    self._conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Append-only record; append a correction instead'); END")
            self._conn.execute("CREATE TRIGGER IF NOT EXISTS immutable_activity_events_delete BEFORE DELETE ON activity_events BEGIN SELECT RAISE(ABORT,'Append-only record; append a correction instead'); END")
            # The only permitted activity update is the one-time link of an
            # early conversation event to the case it created.
            self._conn.execute("CREATE TRIGGER IF NOT EXISTS immutable_activity_events_update BEFORE UPDATE ON activity_events WHEN OLD.case_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'Append-only record; append a correction instead'); END")
            self._migrate()
            self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_rma_intent ON rmas(idempotency_key)')
            self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_message_intent ON messages(idempotency_key)')
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),))
            self._conn.commit()

    def _migrate(self):
        """Add any column introduced after this database was created."""
        for table, column, decl in ADDED_COLUMNS:
            cols = {r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- plumbing --------------------------------------------------------

    @contextmanager
    def transaction(self):
        with self._lock:
            level=self._transaction_depth
            self._conn.execute('BEGIN IMMEDIATE' if level==0 else f'SAVEPOINT nested_{level}')
            self._transaction_depth+=1
            try:
                yield
                self._conn.execute('COMMIT' if level==0 else f'RELEASE nested_{level}')
            except BaseException:
                if level==0: self._conn.rollback()
                else:
                    self._conn.execute(f'ROLLBACK TO nested_{level}')
                    self._conn.execute(f'RELEASE nested_{level}')
                raise
            finally: self._transaction_depth-=1

    def close(self):
        with self._lock:
            self._conn.close()

    def execute(self, sql, params=()):
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
            except BaseException:
                if not self._transaction_depth:self._conn.rollback()
                raise
            if not self._transaction_depth: self._conn.commit()
            return cur

    def query(self, sql, params=()) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def one(self, sql, params=()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: dict):
        cols = [k for k, v in row.items() if v is not None or True]
        placeholders = ",".join("?" for _ in cols)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) "
               f"VALUES ({placeholders})")
        return self.execute(sql, tuple(row[c] for c in cols))

    def upsert(self, table: str, row: dict, key: str):
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) "
               f"VALUES ({placeholders}) "
               f"ON CONFLICT({key}) DO UPDATE SET {updates}")
        return self.execute(sql, tuple(row[c] for c in cols))

    def update(self, table: str, key_col: str, key_val, changes: dict):
        with self.transaction():
            return self._update(table,key_col,key_val,changes)

    def _update(self, table: str, key_col: str, key_val, changes: dict):
        if not changes:
            return
        changes=dict(changes)
        if table=='cases':
            current=self.one('SELECT safety_hold,is_adverse_event FROM cases WHERE case_id=?',(key_val,))
            if current and current['safety_hold'] and changes.get('safety_hold')!=0:
                if 'priority' in changes:changes['priority']='Critical'
                if 'is_adverse_event' in changes:changes['is_adverse_event']=max(current['is_adverse_event'] or 0,changes['is_adverse_event'] or 0)
        sets = ",".join(f"{c}=?" for c in changes)
        if table == 'cases' and 'case_version' not in changes:
            sets += ',case_version=case_version+1'
        self.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?",
                     tuple(changes.values()) + (key_val,))

    # -- identifiers -----------------------------------------------------

    def next_id(self, name: str, fmt: str) -> str:
        """Monotonic fictional identifier, persisted so restarts do not reuse."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO counters(name, value) VALUES(?, 0)",
                (name,))
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
            n = self._conn.execute(
                "SELECT value FROM counters WHERE name = ?", (name,)
            ).fetchone()[0]
            if not self._transaction_depth: self._conn.commit()
        return fmt.format(n)

    def new_case_id(self) -> str:
        return self.next_id("case", "CASE-{:06d}")

    def new_call_id(self) -> str:
        return self.next_id("erp_call", "CA{:06d}")

    def new_rma_id(self) -> str:
        return self.next_id("rma", "R{:06d}")

    def new_shipment_id(self) -> str:
        return self.next_id("shipment", "SHP-{:08d}")

    def new_tracking_number(self) -> str:
        # Clearly fictional: no real carrier uses this prefix/check format.
        return self.next_id("tracking", "SIMFX{:010d}")

    # -- audit -----------------------------------------------------------

    def audit(self, event_type: str, actor: str = "system",
              case_id: Optional[str] = None, summary: str = "",
              detail: Any = None, run_id: Optional[str] = None):
        """Append-only. Never updated, never deleted, including failures."""
        self.insert("audit_events", {
            "at": now(), "run_id": run_id, "case_id": case_id,
            "event_type": event_type, "actor": actor, "summary": summary,
            "detail": _j(detail),
        })

    def audit_trail(self, case_id: str) -> list:
        rows = self.query(
            "SELECT * FROM audit_events WHERE case_id=? ORDER BY id", (case_id,))
        for r in rows:
            r["detail"] = _unj(r["detail"])
        return rows

    # -- helpers used widely ---------------------------------------------

    def customer(self, account_id) -> Optional[dict]:
        return self.one("SELECT * FROM customers WHERE account_id=?",
                        (account_id,))

    def device(self, serial) -> Optional[dict]:
        return self.one("SELECT * FROM devices WHERE serial_number=?", (serial,))

    def devices_for_account(self, account_id) -> list:
        return self.query("SELECT * FROM devices WHERE account_id=? "
                          "ORDER BY serial_number", (account_id,))

    def case(self, case_id) -> Optional[dict]:
        return self.one("SELECT * FROM cases WHERE case_id=?", (case_id,))

    def cases(self, run_id=None, include_eval=False, status=None) -> list:
        sql = "SELECT * FROM cases WHERE 1=1"
        params = []
        if not include_eval:
            if run_id is None:
                sql += " AND run_id IS NULL"
            else:
                sql += " AND run_id=?"
                params.append(run_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return self.query(sql, tuple(params))

    def messages(self, case_id) -> list:
        return self.query(
            "SELECT * FROM messages WHERE case_id=? ORDER BY created_at",
            (case_id,))

    def checklist(self, case_id) -> list:
        return self.query(
            "SELECT * FROM checklist_items WHERE case_id=? ORDER BY seq",
            (case_id,))

    def stats(self) -> dict:
        def n(table, where="1=1", params=()):
            return self.one(f"SELECT COUNT(*) c FROM {table} WHERE {where}",
                            params)["c"]
        return {
            "customers": n("customers"),
            "devices": n("devices"),
            "orders": n("orders"),
            "ticket_pool": n("ticket_pool"),
            "ticket_pool_released": n("ticket_pool", "released=1"),
            "cases_live": n("cases", "run_id IS NULL"),
            "cases_eval": n("cases", "run_id IS NOT NULL"),
            "erp_calls": n("erp_calls", "run_id IS NULL"),
            "rmas": n("rmas", "run_id IS NULL"),
            "shipments": n("sim_shipments", "run_id IS NULL"),
            "qa_notifications": n("qa_notifications", "run_id IS NULL"),
            "audit_events": n("audit_events"),
        }


_DEFAULT: Optional[Store] = None


def get_store(path: Optional[str] = None) -> Store:
    """Process-wide default store, so the portal and the agent share one DB."""
    global _DEFAULT
    if path is not None:
        return Store(path)
    if _DEFAULT is None:
        _DEFAULT = Store()
    return _DEFAULT


def reset_default():
    global _DEFAULT
    if _DEFAULT is not None:
        _DEFAULT.close()
    _DEFAULT = None
