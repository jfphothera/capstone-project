"""Neutral shared admission foundation. No case, repository or MCP dependencies.

SQLite is private to the broker process/host; independent applications use the
versioned API, not this database. Direct Ollama bypass must be restricted by the
operator before claiming collision-free deployment enforcement.
"""
import collections
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
import re
from contextlib import contextmanager

SCHEMA='''
CREATE TABLE IF NOT EXISTS capacity_clients (
 app_id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,enabled INTEGER NOT NULL,
 max_concurrency INTEGER NOT NULL,queue_limit INTEGER NOT NULL,priority INTEGER NOT NULL,
 models TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_resources (
 resource_id TEXT PRIMARY KEY,endpoint TEXT UNIQUE,resource_type TEXT NOT NULL,
 owner TEXT NOT NULL,applications TEXT NOT NULL,permitted_uses TEXT NOT NULL,
 qualified_models TEXT NOT NULL,health TEXT NOT NULL,last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_requests (
 request_id TEXT PRIMARY KEY,app_id TEXT NOT NULL,client_key TEXT NOT NULL,
 model TEXT NOT NULL,nodes TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,
 deadline REAL NOT NULL,lease_id TEXT,resource_id TEXT,expires REAL,executing INTEGER NOT NULL DEFAULT 0,
 UNIQUE(app_id,client_key));
CREATE TABLE IF NOT EXISTS capacity_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,app_id TEXT NOT NULL,request_id TEXT,
 event_type TEXT NOT NULL,payload TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_client_limits (app_id TEXT PRIMARY KEY,operations TEXT,max_context INTEGER);
CREATE TABLE IF NOT EXISTS capacity_service_policy (key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT OR IGNORE INTO capacity_service_policy VALUES('admission_open','1');
INSERT OR IGNORE INTO capacity_service_policy VALUES('ledger_id','LEDGER-'||lower(hex(randomblob(16))));
CREATE TABLE IF NOT EXISTS capacity_inventory (
 resource_id TEXT PRIMARY KEY,endpoint TEXT NOT NULL,observed_at REAL NOT NULL,runtime_version TEXT,models TEXT NOT NULL);
'''
# 0.4.0, additive: new tables only, so 0.3.0 (positional INSERTs into existing tables) keeps working after rollback.
SCHEMA_0_4='''
CREATE TABLE IF NOT EXISTS capacity_resource_capacity (
 resource_id TEXT PRIMARY KEY,slots INTEGER NOT NULL DEFAULT 1,memory_gb REAL,last_assigned REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS capacity_inventory_runtime (
 resource_id TEXT PRIMARY KEY,loaded TEXT,loaded_at REAL,sizes TEXT,observed_at REAL);
CREATE TABLE IF NOT EXISTS capacity_suspensions (
 suspension_id TEXT PRIMARY KEY,scope TEXT NOT NULL,resource_id TEXT,model TEXT,digest TEXT,operation TEXT,
 failure_class TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL,since REAL NOT NULL,hold_until REAL,next_probe REAL,
 probes_failed INTEGER NOT NULL DEFAULT 0,request_id TEXT,waiting TEXT,manual_probe INTEGER NOT NULL DEFAULT 0,heads TEXT,
 resolved_at REAL,resolved_by TEXT,resolution TEXT);
CREATE TABLE IF NOT EXISTS capacity_faults (
 fault_id INTEGER PRIMARY KEY AUTOINCREMENT,resource_id TEXT NOT NULL,model TEXT NOT NULL,digest TEXT,operation TEXT NOT NULL,
 failure_class TEXT NOT NULL,request_id TEXT,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_probes (
 probe_id TEXT PRIMARY KEY,suspension_id TEXT NOT NULL,resource_id TEXT NOT NULL,model TEXT,operation TEXT,
 passed INTEGER NOT NULL,detail TEXT,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_request_routing (request_id TEXT PRIMARY KEY,operation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_request_attempts (
 request_id TEXT NOT NULL,attempt INTEGER NOT NULL,resource_id TEXT,failure_class TEXT,at REAL NOT NULL,PRIMARY KEY(request_id,attempt));
CREATE TRIGGER IF NOT EXISTS capacity_slot_guard_update BEFORE UPDATE OF status,resource_id ON capacity_requests
 WHEN NEW.status IN ('leased','cancel_requested') AND NEW.resource_id IS NOT NULL
  AND NOT (OLD.status IN ('leased','cancel_requested') AND OLD.resource_id IS NEW.resource_id)
 BEGIN SELECT RAISE(ABORT,'Resource slots exhausted') WHERE
  (SELECT count(*) FROM capacity_requests WHERE resource_id=NEW.resource_id AND status IN ('leased','cancel_requested') AND request_id!=NEW.request_id)
  >=coalesce((SELECT slots FROM capacity_resource_capacity WHERE resource_id=NEW.resource_id),1); END;
CREATE TRIGGER IF NOT EXISTS capacity_slot_guard_insert BEFORE INSERT ON capacity_requests
 WHEN NEW.status IN ('leased','cancel_requested') AND NEW.resource_id IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'Resource slots exhausted') WHERE
  (SELECT count(*) FROM capacity_requests WHERE resource_id=NEW.resource_id AND status IN ('leased','cancel_requested'))
  >=coalesce((SELECT slots FROM capacity_resource_capacity WHERE resource_id=NEW.resource_id),1); END;
'''
UPGRADE_REPORT_KEY='upgrade_report:0.4.0'
# 0.4.1, additive (a new table only; 0.4.0 after a rollback ignores it): lifecycle times for status replies.
SCHEMA_0_4_1='''
CREATE TABLE IF NOT EXISTS capacity_request_times (request_id TEXT PRIMARY KEY,admitted_at REAL,completed_at REAL);
'''
# 0.4.4, additive (indexes and one new table; older releases ignore both, so a rollback keeps working).
# Every status/lease query and the slot-guard triggers filtered capacity_requests by status or
# resource_id without an index: each admission pass, expiry pass and lease grant scanned the whole
# request history inside the write lock, so write transactions grew with the ledger's age.
SCHEMA_0_4_4='''
CREATE INDEX IF NOT EXISTS capacity_requests_status ON capacity_requests(status);
CREATE INDEX IF NOT EXISTS capacity_requests_resource_status ON capacity_requests(resource_id,status);
CREATE TABLE IF NOT EXISTS capacity_model_capacity (
 resource_id TEXT NOT NULL,model TEXT NOT NULL,source TEXT NOT NULL,parallel INTEGER NOT NULL,
 digest TEXT,runtime_version TEXT,num_ctx INTEGER,evidence TEXT,detail TEXT,recorded_at REAL NOT NULL,
 PRIMARY KEY(resource_id,model,source));
'''
LEASE_SECONDS=60
TEMPORARY_PREFIX='lab-'   # 0.4.6: only identities with this prefix may be issued, granted and withdrawn as temporary
# 0.4.4 ledger write budgets (seconds a writer may wait for the write lock before giving up).
REQUEST_WRITE_WAIT=5.0     # HTTP handler writes: below the 0.4.1 client's 10 s attempt, so the client gets a 503, not silence
BACKGROUND_WRITE_WAIT=30.0 # scheduler, renewal, health, recovery
SLOW_HOLD_SECONDS=1.0      # a write transaction held this long is logged (label, wait, hold)
SLOW_WAIT_SECONDS=2.0      # a writer that waited this long is logged, with the holder at the time


class LedgerBusy(Exception):
    """The ledger write lock was not obtained within the caller's budget (0.4.4).

    Nothing was written. `holder` names the transaction that held the lock (label, seconds
    held), or 'external' when another process held it. HTTP answers 503 with Retry-After.
    """
    def __init__(self,label,waited,holder):
        super().__init__('Ledger busy: %s waited %.1f s (holder: %s)'%(label,waited,holder))
        self.label=label;self.waited=waited;self.holder=holder;self.retry_after=1


class _Gate:
    """One per ledger file in this process: FIFO hand-off of the right to write (0.4.4).

    SQLite's busy handler is not a queue. A waiting connection polls with sleeps up to 100 ms
    and loses to every writer that arrives between its polls; under load a waiter can miss
    every free moment until its timeout. Here writers queue in arrival order and the lock is
    handed straight to the next one on release, with no idle gap. SQLite's busy wait then only
    ever applies to another process (operator CLI).
    """
    def __init__(self):
        self.mutex=threading.Lock();self.queue=collections.deque();self.held=False
        self.holder=None # (label, perf_counter start, thread name)
        self.stats={};self.slow=collections.deque(maxlen=50)

    def acquire(self,timeout):
        with self.mutex:
            if not self.held and not self.queue:self.held=True;return True
            turn=threading.Event();self.queue.append(turn)
        if turn.wait(max(0,timeout)):return True
        with self.mutex:
            if turn.is_set():return True # handed over just as the wait ended
            self.queue.remove(turn);return False

    def release(self):
        with self.mutex:
            self.holder=None
            if self.queue:self.queue.popleft().set() # stays held: ownership passes directly
            else:self.held=False

    def describe_holder(self):
        holder=self.holder
        if not holder:return None
        return {'label':holder[0],'held_ms':round((time.perf_counter()-holder[1])*1000),'thread':holder[2]}

    def record(self,label,wait,hold,*,timeout=False,external=0):
        with self.mutex:
            s=self.stats.setdefault(label,{'count':0,'timeouts':0,'external_busy':0,'wait_ms_total':0.0,'wait_ms_max':0.0,'hold_ms_total':0.0,'hold_ms_max':0.0})
            s['external_busy']+=external
            if timeout:s['timeouts']+=1;return
            s['count']+=1;s['wait_ms_total']+=wait*1000;s['hold_ms_total']+=hold*1000
            s['wait_ms_max']=max(s['wait_ms_max'],wait*1000);s['hold_ms_max']=max(s['hold_ms_max'],hold*1000)


_GATES={};_GATES_LOCK=threading.Lock()
_THREAD=threading.local()


def ledger_wait_seconds(reset=False):
    """Seconds this thread spent waiting for the ledger write lock (0.4.4 request timing)."""
    value=getattr(_THREAD,'wait',0.0)
    if reset:_THREAD.wait=0.0
    return value


def _gate(path):
    key=os.path.normcase(os.path.abspath(path))
    with _GATES_LOCK:return _GATES.setdefault(key,_Gate())


class CapacityBroker:
    def __init__(self,path):
        self.path=str(path)
        self.gate=_gate(self.path)
        self.log=None # 0.4.4: callable(event, **fields) for slow or refused ledger writes
        # 0.4.1: request IDs whose execution this process owns and is still running. Expiry
        # extends these leases instead of expiring them: a late renewal never holds a head.
        self.local_leases=lambda:()
        # 0.4.6: placement pass counters (process-local, published with the ledger statistics).
        self.place_stats={'passes':0,'read_only_passes':0,'write_passes':0,'granted':0,'choices':0,'memo_skips':0}
        # 0.4.1: write-ahead log. Status reads no longer block a committing writer (rollback journal:
        # the commit waits for every reader, and waiting threads sleep in the busy handler). Persistent
        # in the file; 0.3.x/0.4.0 open it unchanged after a rollback. synchronous stays FULL (durable results).
        raw=sqlite3.connect(self.path,timeout=30)
        try:raw.execute('PRAGMA journal_mode=WAL')
        finally:raw.close()
        with self.connect() as db:
            db.executescript(SCHEMA)
            if 'node_policy' not in {r['name'] for r in db.execute('PRAGMA table_info(capacity_requests)')}:
                db.execute("ALTER TABLE capacity_requests ADD COLUMN node_policy TEXT NOT NULL DEFAULT 'explicit'")
        with self.connect() as db:
            db.executescript(SCHEMA_0_4);db.executescript(SCHEMA_0_4_1)
            # 0.4.3, additive: each model's families from the head's /api/tags (per-model placement limits).
            # Older releases ignore the column, so a rollback keeps working.
            if 'families' not in {r['name'] for r in db.execute('PRAGMA table_info(capacity_inventory_runtime)')}:
                db.execute('ALTER TABLE capacity_inventory_runtime ADD COLUMN families TEXT')
        with self.connect() as db:db.executescript(SCHEMA_0_4_4)
        self._migrate_0_4()

    def _migrate_0_4(self):
        """One head lease became N slots: the unique index gives way to the slot triggers.

        A ledger written by 0.3.x still has the index; its quarantined heads become held
        heads on the probe schedule, recorded once under UPGRADE_REPORT_KEY. After a
        rollback to 0.3.0 (which recreates the index) and a new update, this runs again.
        """
        from . import recovery
        unheld=("SELECT 1 FROM capacity_resources r WHERE health='quarantined' AND NOT EXISTS (SELECT 1 FROM capacity_suspensions s "
                "WHERE s.scope='resource' AND s.resource_id=r.resource_id AND s.state IN ('suspended','held','needs_attention'))")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='one_active_resource'").fetchone() and not db.execute(unheld).fetchone():return
        with self.write('migrate') as db:
            legacy=db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='one_active_resource'").fetchone()
            db.execute('DROP INDEX IF EXISTS one_active_resource')
            held=recovery.adopt_quarantined(self,db,source='upgrade' if legacy else 'quarantine_without_hold')
            if legacy:
                ledger=db.execute("SELECT value FROM capacity_service_policy WHERE key='ledger_id'").fetchone()[0]
                report={'release':'0.4.0','at':time.time(),'ledger_id':ledger,'held':held}
                db.execute('INSERT INTO capacity_service_policy VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(UPGRADE_REPORT_KEY,json.dumps(report)))
                self._event(db,'infrastructure',None,'service.schema_upgraded',{'release':'0.4.0','held':[h['resource_id'] for h in held]})

    @contextmanager
    def connect(self):
        """Reads (and schema setup). Every ledger write goes through write() since 0.4.4."""
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def _log(self,event,**fields):
        if self.log:
            try:self.log(event,**fields)
            except Exception:pass

    @contextmanager
    def write(self,label,*,wait=BACKGROUND_WRITE_WAIT):
        """One write transaction (0.4.4): FIFO in this process, then BEGIN IMMEDIATE, timed and labelled.

        Wait at most `wait` seconds in total; otherwise raise LedgerBusy (nothing written) and log
        the holder. Only BEGIN IMMEDIATE can meet another process's lock (WAL: once it succeeds
        no later statement or COMMIT waits for a lock), so that one step is retried with a short
        backoff inside the budget. The body must not do network calls, sleeps or inference.
        """
        gate=self.gate;start=time.perf_counter();external=0;began=None;waited=0.0
        if not gate.acquire(wait):
            waited=time.perf_counter()-start;holder=gate.describe_holder() or 'unknown'
            gate.record(label,waited,0,timeout=True)
            self._log('ledger.wait_timeout',label=label,waited_ms=round(waited*1000),holder=holder)
            _THREAD.wait=getattr(_THREAD,'wait',0.0)+waited
            raise LedgerBusy(label,waited,holder)
        try:
            # A short busy timeout per attempt, so every wait for another process is counted and bounded here.
            db=sqlite3.connect(self.path,timeout=min(.25,max(.05,wait)));db.row_factory=sqlite3.Row
            try:
                pause=.02
                while True:
                    try:db.execute('BEGIN IMMEDIATE');break
                    except sqlite3.OperationalError as exc:
                        if 'locked' not in str(exc) and 'busy' not in str(exc):raise
                        external+=1;left=wait-(time.perf_counter()-start)
                        if left<=0:
                            waited=time.perf_counter()-start;gate.record(label,waited,0,timeout=True,external=external)
                            self._log('ledger.wait_timeout',label=label,waited_ms=round(waited*1000),holder='external')
                            _THREAD.wait=getattr(_THREAD,'wait',0.0)+waited
                            raise LedgerBusy(label,waited,'external') from None
                        time.sleep(min(pause,left));pause=min(.5,pause*2)
                began=time.perf_counter();gate.holder=(label,began,threading.current_thread().name)
                waited=began-start;_THREAD.wait=getattr(_THREAD,'wait',0.0)+waited
                with db:yield db
            finally:db.close()
        finally:
            held=time.perf_counter()-began if began is not None else 0
            gate.release()
            if began is not None:
                gate.record(label,waited,held,external=external)
                if held>=SLOW_HOLD_SECONDS or waited>=SLOW_WAIT_SECONDS:
                    item={'label':label,'wait_ms':round(waited*1000),'hold_ms':round(held*1000),'external_busy':external,'at':time.time()}
                    gate.slow.append(item);self._log('ledger.slow_transaction',**{k:v for k,v in item.items() if k!='at'})

    def ledger_stats(self):
        """Process-local write-lock statistics per transaction label (0.4.4), for the observation."""
        gate=self.gate
        with gate.mutex:
            labels={k:{**v,'wait_ms_total':round(v['wait_ms_total']),'hold_ms_total':round(v['hold_ms_total']),
                       'wait_ms_max':round(v['wait_ms_max']),'hold_ms_max':round(v['hold_ms_max'])} for k,v in gate.stats.items()}
            slow=list(gate.slow)[-10:]
        return {'labels':labels,'waiting':len(gate.queue),'holder':gate.describe_holder(),'recent_slow':slow,
                'placement':dict(self.place_stats)} # 0.4.6, additive

    def ledger_size(self):
        """Row counts and file sizes, logged at service start (0.4.4): the scan cost grows with these."""
        out={'file_bytes':os.path.getsize(self.path) if os.path.exists(self.path) else 0,
             'wal_bytes':os.path.getsize(self.path+'-wal') if os.path.exists(self.path+'-wal') else 0,'rows':{}}
        with self.connect() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('capacity_requests','capacity_invocations','capacity_events','capacity_qualification_evidence'):
                if table in tables:out['rows'][table]=db.execute('SELECT count(*) FROM '+table).fetchone()[0]
        return out

    @staticmethod
    def _event(db,app,request,event,payload):
        db.execute('INSERT INTO capacity_events(app_id,request_id,event_type,payload,at) VALUES(?,?,?,?,?)',
                   (app,request,event,json.dumps(payload),time.time()))

    def register_client(self,app_id,token,*,models,max_concurrency=1,queue_limit=16,priority=5):
        if not token:raise ValueError('Explicit identity, models and bounded client policy required')
        self._client_policy(app_id,models,max_concurrency,queue_limit,priority)
        with self.write('register_client') as db:
            db.execute('INSERT INTO capacity_clients VALUES(?,?,1,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET token_hash=excluded.token_hash,enabled=1,max_concurrency=excluded.max_concurrency,queue_limit=excluded.queue_limit,priority=excluded.priority,models=excluded.models',
                (app_id,hashlib.sha256(token.encode()).hexdigest(),max_concurrency,queue_limit,priority,json.dumps(models)))
            self.clear_temporary(db,app_id) # 0.4.6: an ordinary issue (credentials issue, bootstrap) is never temporary

    @staticmethod
    def _client_policy(app_id,models,max_concurrency,queue_limit,priority):
        if not isinstance(app_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',app_id) or not models or not 1<=max_concurrency<=1024 or not 0<=queue_limit<=10000 or not 0<=priority<=10:
            raise ValueError('Explicit identity, models and bounded client policy required')

    def update_client_policy(self,app_id,*,models,max_concurrency=1,queue_limit=16,priority=5):
        """Policy change for an already provisioned client; token and enabled state are untouched."""
        self._client_policy(app_id,models,max_concurrency,queue_limit,priority)
        with self.write('update_client_policy') as db:
            if not db.execute('UPDATE capacity_clients SET max_concurrency=?,queue_limit=?,priority=?,models=? WHERE app_id=?',
                    (max_concurrency,queue_limit,priority,json.dumps(models),app_id)).rowcount:
                raise PermissionError('Provision a credential for this application before applying its policy')
            self.clear_temporary(db,app_id) # 0.4.6: a bootstrap policy entry makes it a real application

    def set_token(self,app_id,token,*,enable=False):
        """Credential rotation: replaces only the token hash (and optionally re-enables)."""
        if not token or len(token)<32:raise ValueError('A high-entropy token is required')
        with self.write('set_token') as db:
            if not db.execute('UPDATE capacity_clients SET token_hash=?,enabled=CASE WHEN ? THEN 1 ELSE enabled END WHERE app_id=?',
                    (hashlib.sha256(token.encode()).hexdigest(),int(enable),app_id)).rowcount:raise PermissionError('Unknown application')
            self.clear_temporary(db,app_id) # 0.4.6: an ordinary rotation makes it a real application
            self._event(db,app_id,None,'authorization.rotated',{})

    def set_client_limits(self,app_id,*,operations=None,max_context=None):
        """Optional per-application operation and context restrictions (None means the service bounds)."""
        if operations is not None and (not isinstance(operations,list) or not operations or set(operations)-{'/api/generate','/api/chat','/api/embed'}):
            raise ValueError('Operations must be a nonempty subset of the inference operations')
        if max_context is not None and (isinstance(max_context,bool) or not isinstance(max_context,int) or not 512<=max_context<=65536):
            raise ValueError('Application context limit must be 512–65536')
        with self.write('set_client_limits') as db:
            if not db.execute('SELECT 1 FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone():raise PermissionError('Unknown application')
            db.execute('INSERT INTO capacity_client_limits VALUES(?,?,?) ON CONFLICT(app_id) DO UPDATE SET operations=excluded.operations,max_context=excluded.max_context',
                (app_id,json.dumps(sorted(operations)) if operations else None,max_context))

    def client_limits(self,app_id):
        with self.connect() as db:row=db.execute('SELECT * FROM capacity_client_limits WHERE app_id=?',(app_id,)).fetchone()
        return {'operations':json.loads(row['operations']) if row and row['operations'] else ['/api/chat','/api/embed','/api/generate'],
                'max_context':row['max_context'] if row else None}

    def authenticate(self,app_id,token):
        with self.connect() as db:
            row=db.execute('SELECT * FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone()
            temporary=self.temporary_clients(db).get(app_id) if row and str(app_id).startswith(TEMPORARY_PREFIX) else None
        if not row or not row['enabled'] or not token or not hmac.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest()):
            raise PermissionError('Application service authorization required')
        # 0.4.6: a temporary lab identity stops working at its expiry even if its tool never revoked it (a hard kill).
        if temporary and temporary.get('expires_at') and time.time()>=temporary['expires_at']:
            raise PermissionError('Temporary application credential expired')
        return dict(row)

    # 0.4.6: temporary lab identities (credentials reissue). Listed under one service-policy key, which older releases
    # ignore: {app_id: {issued_at, expires_at, by}}. Only names starting with TEMPORARY_PREFIX can be temporary.
    TEMPORARY_KEY='temporary_clients'

    def temporary_clients(self,db=None):
        if db is None:
            with self.connect() as db:return self.temporary_clients(db)
        row=db.execute('SELECT value FROM capacity_service_policy WHERE key=?',(self.TEMPORARY_KEY,)).fetchone()
        try:value=json.loads(row[0]) if row else {}
        except ValueError:value={}
        return {k:v for k,v in value.items() if isinstance(k,str) and k.startswith(TEMPORARY_PREFIX) and isinstance(v,dict)}

    def mark_temporary(self,db,app_id,*,expires_at,by):
        if not str(app_id).startswith(TEMPORARY_PREFIX):raise PermissionError('Only %s applications can be temporary'%TEMPORARY_PREFIX)
        value=self.temporary_clients(db);value[app_id]={'issued_at':time.time(),'expires_at':expires_at,'by':by}
        self._store_temporary(db,value)

    def _store_temporary(self,db,value):
        db.execute('INSERT INTO capacity_service_policy VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(self.TEMPORARY_KEY,json.dumps(value,sort_keys=True)))

    def clear_temporary(self,db,app_id):
        """0.4.6: any ordinary credential or policy action on an application (issue, rotate, bootstrap) makes it a real
        application again: its temporary mark, if any, is removed in that same transaction."""
        value=self.temporary_clients(db)
        if app_id in value:
            del value[app_id];self._store_temporary(db,value)
            self._event(db,'infrastructure',None,'temporary_client.cleared',{'app_id':app_id})

    def _revoke_in(self,db,app_id):
        """Disable the application and cancel its queued requests, inside the caller's write (as revoke)."""
        if not db.execute('UPDATE capacity_clients SET enabled=0 WHERE app_id=?',(app_id,)).rowcount:return False
        for row in db.execute("SELECT request_id FROM capacity_requests WHERE app_id=? AND status='queued'",(app_id,)).fetchall():
            db.execute("UPDATE capacity_requests SET status='cancelled' WHERE request_id=?",(row['request_id'],))
            self._event(db,app_id,row['request_id'],'cancelled',{'reason':'authorization.revoked'})
        self._event(db,app_id,None,'authorization.revoked',{})
        return True

    def issue_temporary(self,app_id,token,*,models,max_concurrency=1,queue_limit=16,priority=5,operations=None,max_context=None,expires_at,by):
        """0.4.6: issue or re-key a temporary identity in ONE write transaction: an earlier row of it is revoked (its
        queued requests cancelled), the client row and limits are written, and the temporary mark is set last. A
        failure leaves nothing changed (no mark on an application that was never issued). Inside the transaction it is
        checked again that an existing row is temporary, so a real application is never taken over.
        Returns 'enabled', 'disabled' (the earlier row's state) or None (new)."""
        if not str(app_id).startswith(TEMPORARY_PREFIX):raise PermissionError('Only %s applications can be temporary'%TEMPORARY_PREFIX)
        if not token or len(token)<32:raise ValueError('A high-entropy token is required')
        self._client_policy(app_id,models,max_concurrency,queue_limit,priority)
        if operations is not None and (not isinstance(operations,list) or not operations or set(operations)-{'/api/generate','/api/chat','/api/embed'}):
            raise ValueError('Operations must be a nonempty subset of the inference operations')
        if max_context is not None and (isinstance(max_context,bool) or not isinstance(max_context,int) or not 512<=max_context<=65536):
            raise ValueError('Application context limit must be 512–65536')
        with self.write('issue_temporary') as db:
            prior=db.execute('SELECT enabled FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone()
            if prior and app_id not in self.temporary_clients(db):
                raise PermissionError('%s holds a credential that was not issued as temporary; it is never changed here'%app_id)
            if prior:self._revoke_in(db,app_id)
            db.execute('INSERT INTO capacity_clients VALUES(?,?,1,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET token_hash=excluded.token_hash,enabled=1,'
                       'max_concurrency=excluded.max_concurrency,queue_limit=excluded.queue_limit,priority=excluded.priority,models=excluded.models',
                       (app_id,hashlib.sha256(token.encode()).hexdigest(),max_concurrency,queue_limit,priority,json.dumps(models)))
            db.execute('INSERT INTO capacity_client_limits VALUES(?,?,?) ON CONFLICT(app_id) DO UPDATE SET operations=excluded.operations,max_context=excluded.max_context',
                       (app_id,json.dumps(sorted(operations)) if operations else None,max_context))
            self.mark_temporary(db,app_id,expires_at=expires_at,by=by)
            self._event(db,'infrastructure',None,'temporary_client.issued',{'app_id':app_id,'expires_at':expires_at,'previous':(('enabled' if prior[0] else 'disabled') if prior else None)})
        return ('enabled' if prior[0] else 'disabled') if prior else None

    def withdraw_application(self,db,app_id):
        """Remove app_id from every head's applications inside the caller's write; evented per head. Nothing else changes."""
        withdrawn=[]
        for r in db.execute('SELECT resource_id,applications FROM capacity_resources ORDER BY resource_id').fetchall():
            apps=json.loads(r['applications'])
            if app_id not in apps:continue
            db.execute('UPDATE capacity_resources SET applications=? WHERE resource_id=?',(json.dumps([a for a in apps if a!=app_id]),r['resource_id']))
            self._event(db,'infrastructure',None,'resource.application_withdrawn',{'resource_id':r['resource_id'],'app_id':app_id})
            withdrawn.append(r['resource_id'])
        return withdrawn

    def expire_temporary_clients(self,now=None):
        """0.4.6: revoke every temporary identity past its expiry and withdraw it from the heads (the Gateway's own
        maintenance, so a tool that was killed cannot leave a usable identity behind). Returns the app IDs handled."""
        now=time.time() if now is None else now
        with self.connect() as db: # a cheap read first: usually nothing is due and no write is taken
            due=[a for a,v in self.temporary_clients(db).items() if v.get('expires_at') and now>=v['expires_at'] and not v.get('cleaned_at')]
        handled=[]
        for app_id in sorted(due):
            with self.write('temporary_expiry') as db:
                # Checked again inside the write: still temporary, still past this expiry, not yet cleaned (a reissue or
                # an ordinary issue/rotate/bootstrap in between wins, and a fresh expiry is never cut short).
                entries=self.temporary_clients(db);entry=entries.get(app_id)
                if not entry or not entry.get('expires_at') or now<entry['expires_at'] or entry.get('cleaned_at'):continue
                revoked=self._revoke_in(db,app_id)
                withdrawn=self.withdraw_application(db,app_id)
                # Kept (so the tool may reissue it), marked cleaned: this expiry is never handled twice.
                entry['cleaned_at']=time.time();self._store_temporary(db,entries)
                self._event(db,'infrastructure',None,'temporary_client.expired',{'app_id':app_id,'revoked':revoked,'withdrawn':withdrawn})
            handled.append(app_id)
        return handled

    def _expire(self,db,now):
        # Every expiry is evented so observers see a timeline, not only totals.
        from . import recovery
        changed=[]
        rows=db.execute("SELECT * FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND expires<?",(now,)).fetchall()
        live=set(self.local_leases()) if rows else set()
        for row in rows:
            if row['request_id'] in live:
                # This process is still running the call and will answer it: a renewal that came
                # late (busy ledger) extends the lease; it is not uncertain execution.
                if row['deadline']>now:
                    db.execute('UPDATE capacity_requests SET expires=min(deadline,?) WHERE request_id=?',(now+LEASE_SECONDS,row['request_id']))
                    self._event(db,row['app_id'],row['request_id'],'lease.extended',{'resource_id':row['resource_id'],'reason':'renewal_late'})
                # 0.4.3: at or past its deadline the owning call settles it itself (its transport timeout is the
                # deadline, and _settle checks whether the head still answers); expiring it here first would
                # hold the head before that check. A lease that stops being live is expired on a later pass.
                continue
            changed.append(row['request_id'])
            if row['executing']:
                # Uncertain execution: an executing lease ran out with no answer. Only this
                # class holds the whole head; an existing hold is extended, never re-raised.
                operation=db.execute('SELECT operation FROM capacity_request_routing WHERE request_id=?',(row['request_id'],)).fetchone()
                recovery.hold_resource(self,db,row['resource_id'],request_id=row['request_id'],model=row['model'],
                    operation=operation[0] if operation else None,hold_until=row['deadline'],source='lease_expired',now=now)
            db.execute("UPDATE capacity_requests SET status='expired' WHERE request_id=?",(row['request_id'],))
            self._event(db,row['app_id'],row['request_id'],'lease.expired',{'resource_id':row['resource_id']})
        for row in db.execute("SELECT request_id,app_id FROM capacity_requests WHERE status='queued' AND deadline<?",(now,)).fetchall():
            db.execute("UPDATE capacity_requests SET status='expired' WHERE request_id=?",(row['request_id'],))
            self._event(db,row['app_id'],row['request_id'],'request.expired',{})
            changed.append(row['request_id'])
        return changed

    def reconcile_expired(self,*,wait=BACKGROUND_WRITE_WAIT):
        with self.write('expire',wait=wait) as db:return self._expire(db,time.time())

    def has_expired(self):
        """Read-only: would an expiry pass change anything? (Status reads stay out of the write lock.)

        0.4.4: a lease this process is still running past its deadline is not counted: the expiry
        pass leaves it to its own settlement, so 0.4.3 ran an empty write transaction for it on every
        status poll and submission until the call settled.
        """
        with self.connect() as db:
            now=time.time()
            if db.execute("SELECT 1 FROM capacity_requests WHERE status='queued' AND deadline<? LIMIT 1",(now,)).fetchone():return True
            rows=db.execute("SELECT request_id,deadline FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND expires<?",(now,)).fetchall()
        if not rows:return False
        live=set(self.local_leases())
        return any(r['request_id'] not in live or r['deadline']>now for r in rows)

    @staticmethod
    def validate_model_capacity(value):
        """agent-arena.model-capacity/1 entries as the bootstrap policy carries them (0.4.4).

        {model name: {"parallel": 1-64, "digest": str, "runtime_version": str, "num_ctx": int, "evidence": str}}
        """
        if not isinstance(value,dict) or len(value)>256:raise ValueError('model_capacity must map up to 256 model names to capacity entries')
        out={}
        for model,entry in value.items():
            if not isinstance(model,str) or not 0<len(model)<=200:raise ValueError('model_capacity keys must be model names')
            if not isinstance(entry,dict) or set(entry)-{'parallel','digest','runtime_version','num_ctx','evidence'} or {'parallel','digest','runtime_version'}-set(entry):
                raise ValueError('model_capacity[%s] needs parallel, digest and runtime_version (and optional num_ctx, evidence) only'%model)
            parallel=entry['parallel']
            if isinstance(parallel,bool) or not isinstance(parallel,int) or not 1<=parallel<=64:raise ValueError('model_capacity[%s].parallel must be 1-64'%model)
            for key in ('digest','runtime_version'):
                if not isinstance(entry[key],str) or not 0<len(entry[key])<=200:raise ValueError('model_capacity[%s].%s must be a non-empty string'%(model,key))
            num_ctx=entry.get('num_ctx')
            if num_ctx is not None and (isinstance(num_ctx,bool) or not isinstance(num_ctx,int) or not 1<=num_ctx<=1048576):raise ValueError('model_capacity[%s].num_ctx must be a positive integer'%model)
            evidence=entry.get('evidence')
            if evidence is not None and (not isinstance(evidence,str) or len(evidence)>400):raise ValueError('model_capacity[%s].evidence must be a string up to 400 characters'%model)
            out[model]={'parallel':parallel,'digest':entry['digest'],'runtime_version':entry['runtime_version'],'num_ctx':num_ctx,'evidence':evidence}
        return out

    def authorize_resource(self,resource_id,*,endpoint=None,resource_type,owner,applications,permitted_uses,slots=1,memory_gb=None,model_capacity=None):
        """Explicit enrollment. slots: concurrent leases the head takes (match OLLAMA_NUM_PARALLEL);
        memory_gb: GiB the head's models may occupy (None: unknown, placement never assumes room).
        model_capacity (0.4.4): Arena's declared per-model parallelism for this head; absent keeps the
        declarations already stored, {} clears them. Used only while the head's live runtime version
        and the model's installed digest equal the declared ones (model_capacity.py)."""
        if isinstance(slots,bool) or not isinstance(slots,int) or not 1<=slots<=64:raise ValueError('slots must be an integer 1-64')
        declared=self.validate_model_capacity(model_capacity) if model_capacity is not None else None
        if memory_gb is not None and (isinstance(memory_gb,bool) or not isinstance(memory_gb,(int,float)) or not 0.5<=memory_gb<=4096):
            raise ValueError('memory_gb must be 0.5-4096 (GiB) or absent')
        if resource_type not in {'shared_inference','application_exclusive','exclusive_development_device','exclusive_test_device','shared_test_device','infrastructure_service','unknown'} or not owner:
            raise ValueError('Explicit resource type and owner required')
        if resource_type not in {'shared_inference','shared_test_device'} and applications:
            raise ValueError('Exclusive/test and infrastructure devices cannot join the shared inference pool')
        with self.write('authorize_resource') as db:
            prior=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(resource_id,)).fetchone()
            moved=bool(prior and prior['endpoint']!=endpoint)
            if moved and db.execute("SELECT 1 FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(resource_id,)).fetchone():
                raise ValueError('Resource endpoint cannot move while capacity is allocated')
            db.execute('INSERT INTO capacity_resources VALUES(?,?,?,?,?,?,?, ?,?) ON CONFLICT(resource_id) DO UPDATE SET endpoint=excluded.endpoint,resource_type=excluded.resource_type,owner=excluded.owner,applications=excluded.applications,permitted_uses=excluded.permitted_uses',
                (resource_id,endpoint,resource_type,owner,json.dumps(applications),json.dumps(permitted_uses),'[]','unqualified',0))
            if moved:db.execute("UPDATE capacity_resources SET health='unqualified',qualified_models='[]',last_seen=0 WHERE resource_id=?",(resource_id,))
            db.execute('INSERT INTO capacity_resource_capacity(resource_id,slots,memory_gb) VALUES(?,?,?) ON CONFLICT(resource_id) DO UPDATE SET slots=excluded.slots,memory_gb=excluded.memory_gb',
                       (resource_id,slots,memory_gb))
            if declared is not None:
                # Replaces this head's declarations as a whole; the event keeps the history.
                db.execute("DELETE FROM capacity_model_capacity WHERE resource_id=? AND source='declared'",(resource_id,))
                for model,entry in sorted(declared.items()):
                    db.execute("INSERT INTO capacity_model_capacity VALUES(?,?,'declared',?,?,?,?,?,NULL,?)",
                               (resource_id,model,entry['parallel'],entry['digest'],entry['runtime_version'],entry['num_ctx'],entry['evidence'],time.time()))
            self._event(db,'infrastructure',None,'resource.authorized',{'resource_id':resource_id,'resource_type':resource_type,'location_changed':moved,
                                                                         'slots':slots,'memory_gb':memory_gb,
                                                                         **({'model_capacity':declared} if declared is not None else {})})

    def publish(self,resource_id,models,health):
        with self.write('publish') as db:
            # Inventory/heartbeat cannot enroll a resource or change ownership.
            row=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(resource_id,)).fetchone()
            if not row:raise PermissionError('Resource must be explicitly authorized before qualification')
            db.execute("UPDATE capacity_resources SET qualified_models=?,health=CASE WHEN health='quarantined' THEN health ELSE ? END,last_seen=? WHERE resource_id=?",
                (json.dumps(models),health,time.time(),resource_id))

    def submit(self,app_id,token,client_key,model,nodes,deadline,*,operation=None,wait=REQUEST_WRITE_WAIT):
        """0.4.4: `operation` (completion/embedding) is recorded in the same transaction (0.4.3: a second write)."""
        client=self.authenticate(app_id,token)
        dynamic=nodes==['@authorized']
        if model not in json.loads(client['models']) or not nodes or not time.time()<deadline<=time.time()+3600:
            raise PermissionError('Model, explicit node set or deadline outside client policy')
        with self.write('submit',wait=wait) as db:
            old=db.execute('SELECT * FROM capacity_requests WHERE app_id=? AND client_key=?',(app_id,client_key)).fetchone()
            if old:
                if old['model']!=model or (old['node_policy']=='authorized_dynamic')!=dynamic or not dynamic and json.loads(old['nodes'])!=nodes:raise ValueError('Idempotency key reused with different request')
                return dict(old)
            for node in ([] if dynamic else nodes):
                resource=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(node,)).fetchone()
                if not resource or resource['resource_type']!='shared_inference' or app_id not in json.loads(resource['applications']) or 'inference' not in json.loads(resource['permitted_uses']):
                    raise PermissionError('Resource ownership/permitted-use policy excludes this application')
            count=db.execute("SELECT count(*) FROM capacity_requests WHERE app_id=? AND status='queued'",(app_id,)).fetchone()[0]
            if count>=client['queue_limit']:raise ValueError('Application queue capacity exhausted')
            request='REQ-'+uuid.uuid4().hex
            db.execute('INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,node_policy) VALUES(?,?,?,?,?,?,?,?,?)',
                (request,app_id,client_key,model,json.dumps([] if dynamic else nodes),'queued',time.time(),deadline,'authorized_dynamic' if dynamic else 'explicit'))
            if operation:db.execute('INSERT OR IGNORE INTO capacity_request_routing VALUES(?,?)',(request,operation))
            self._event(db,app_id,request,'queued',{'model':model})
            return dict(db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request,)).fetchone())

    def get(self,app_id,token,request_id):
        self.authenticate(app_id,token)
        with self.connect() as db:row=db.execute('SELECT * FROM capacity_requests WHERE request_id=? AND app_id=?',(request_id,app_id)).fetchone()
        if not row:raise PermissionError('Request belongs to another namespace or does not exist')
        return dict(row)

    def admit(self,app_id,token,request_id):
        """One admission pass, then this request's row (kept for direct broker callers and tests)."""
        self.get(app_id,token,request_id)
        self.place()
        with self.connect() as db:return dict(db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request_id,)).fetchone())

    def place(self):
        """One admission pass over every queued request, in one write transaction.

        0.4.1: the Gateway's scheduler calls this once per event (submit, release, health
        change, and a 1 s tick while anything waits), instead of every waiting request
        calling admit() every 50 ms. Returns the request IDs whose state it changed.

        0.4.6: the pass is first planned on a read snapshot, outside the write lock. When that plan
        grants nothing, withdraws nothing and nothing has expired, no write transaction is taken at
        all (the common pass while every eligible head is full). Otherwise the plan is made again
        inside the write transaction, on the state as it is then (the safe recheck), and applied.
        Within one plan, a (model, candidate heads, operation) that found no head is not tried again:
        later grants in the same pass only add leases, so it cannot fit later in that pass.
        Fair order (priority with aging), per-application limits and per-model limits are unchanged.
        """
        stats=self.place_stats;stats['passes']+=1
        if not self._worth_writing():
            stats['read_only_passes']+=1;return []
        stats['write_passes']+=1
        with self.write('place') as db:
            now=time.time()
            changed=self._expire(db,now)
            actions=self._plan(db,now)
            if not actions:return changed
            from . import placement
            for action,request,*grant in actions:
                if action=='withdraw':
                    db.execute("UPDATE capacity_requests SET status='cancelled' WHERE request_id=?",(request['request_id'],))
                    self._event(db,request['app_id'],request['request_id'],'authorization.withdrawn',{})
                    changed.append(request['request_id']);continue
                rid,tier=grant;lease='LEASE-'+uuid.uuid4().hex
                db.execute("UPDATE capacity_requests SET status='leased',lease_id=?,resource_id=?,expires=? WHERE request_id=?",
                    (lease,rid,min(request['deadline'],now+LEASE_SECONDS),request['request_id']))
                db.execute('INSERT INTO capacity_resource_capacity(resource_id,last_assigned) VALUES(?,?) ON CONFLICT(resource_id) DO UPDATE SET last_assigned=excluded.last_assigned',(rid,now))
                db.execute('INSERT INTO capacity_request_times(request_id,admitted_at) VALUES(?,?) ON CONFLICT(request_id) DO UPDATE SET admitted_at=excluded.admitted_at',(request['request_id'],now))
                self._event(db,request['app_id'],request['request_id'],'admitted',{'resource_id':rid,'placement':placement.TIERS[tier]})
                changed.append(request['request_id'])
            stats['granted']+=sum(1 for a in actions if a[0]=='lease')
            return changed

    def _worth_writing(self):
        """0.4.6: read-only: would a placement pass change anything now (grant, withdraw or expire)?"""
        if self.has_expired():return True
        with self.connect() as db:
            db.execute('BEGIN') # one snapshot for the whole plan
            return bool(self._plan(db,time.time()))

    def _plan(self,db,now):
        """The pass's decisions, in order: ('withdraw', request) and ('lease', request, resource_id, tier).

        Pure over `db` (reads only). Called on a read snapshot to decide whether a write is needed, and
        again inside the write transaction, whose plan is the one applied.
        """
        if db.execute("SELECT value FROM capacity_service_policy WHERE key='admission_open'").fetchone()[0]!='1':return []
        waiting=db.execute("SELECT r.*,c.priority,c.max_concurrency,c.enabled,c.models AS permitted_models,g.operation FROM capacity_requests r JOIN capacity_clients c ON c.app_id=r.app_id "
                           "LEFT JOIN capacity_request_routing g ON g.request_id=r.request_id WHERE r.status='queued'").fetchall()
        if not waiting:return []
        from . import placement,recovery
        resources=[dict(r) for r in db.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference' AND health='healthy' AND last_seen>? ORDER BY resource_id",(now-120,))]
        # 0.4.6: each head's JSON columns are parsed once per pass (0.4.5: three json.loads per head per waiting request).
        heads=[(r['resource_id'],set(json.loads(r['applications'])),'inference' in json.loads(r['permitted_uses']),set(json.loads(r['qualified_models']))) for r in resources]
        view=placement.View(db,now,resources,recovery.load_policy(db)['placement'],recovery.active(db))
        active={r[0]:r[1] for r in db.execute("SELECT app_id,count(*) FROM capacity_requests WHERE status IN ('leased','cancel_requested') GROUP BY app_id")}
        # A request re-placed after a model-on-head fault or an unreachable head never returns to that head.
        # 0.4.6: one query for every waiting request (0.4.5: one per request inside the write lock).
        tried={}
        for r in db.execute("SELECT a.request_id,a.resource_id FROM capacity_request_attempts a JOIN capacity_requests q ON q.request_id=a.request_id WHERE q.status='queued'"):
            tried.setdefault(r[0],set()).add(r[1])
        permitted={};node_sets={};unplaceable=set();actions=[];stats=self.place_stats
        # Aging makes every bounded priority class eventually outrank newer work.
        # 0.4.6: a temporary lab identity never ages and always comes after every real application's waiting work.
        temporary=set(self.temporary_clients(db))
        waiting=sorted(waiting,key=lambda r:(r['app_id'] in temporary,
                                             -(r['priority']+(0 if r['app_id'] in temporary else int((now-r['created'])/5))),r['created'],r['request_id']))
        for request in waiting:
            if not request['enabled']:continue
            app=request['app_id']
            models=permitted.get(app)
            if models is None:models=permitted[app]=set(json.loads(request['permitted_models']))
            if request['model'] not in models:actions.append(('withdraw',request));continue
            if active.get(app,0)>=request['max_concurrency']:continue
            nodes=node_sets.get(request['nodes'])
            if nodes is None:nodes=node_sets[request['nodes']]=frozenset(json.loads(request['nodes']))
            skip=tried.get(request['request_id'],())
            candidates=tuple(rid for rid,apps,inference,qualified in heads
                             if rid in nodes and rid not in skip and app in apps and inference and request['model'] in qualified)
            signature=(request['model'],candidates,request['operation'])
            if signature in unplaceable:stats['memo_skips']+=1;continue
            stats['choices']+=1
            choice=view.choose(request,candidates,request['operation'])
            if not choice:unplaceable.add(signature);continue
            rid,tier=choice
            view.assign(rid,request['model']);active[app]=active.get(app,0)+1
            actions.append(('lease',request,rid,tier))
        return actions

    def finish(self,app_id,token,request_id,*,cancel=False,execution_owner=None,wait=BACKGROUND_WRITE_WAIT):
        self.get(app_id,token,request_id)
        with self.write('cancel' if cancel else 'finish',wait=wait) as db:
            row=db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request_id,)).fetchone()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_invocations'").fetchone():
                invocation=db.execute('SELECT execution_owner,state FROM capacity_invocations WHERE request_id=?',(request_id,)).fetchone()
                if invocation and not cancel and invocation['execution_owner']!=execution_owner:
                    raise PermissionError('Only the executing gateway can release this allocation')
            if row['status'] in {'completed','cancelled','expired'}:return dict(row)
            if not cancel and row['status']=='cancel_requested':raise ValueError('Cancelled allocation cannot publish completion')
            if not cancel and row['status'] not in {'leased','cancel_requested'}:raise ValueError('Only admitted work can complete')
            state='cancel_requested' if cancel and row['executing'] else 'cancelled' if cancel else 'completed'
            db.execute('UPDATE capacity_requests SET status=? WHERE request_id=?',(state,request_id))
            self._event(db,app_id,request_id,state,{})
        return self.get(app_id,token,request_id)

    def release_revoked(self,request_id):
        """Gateway-internal: release an allocation whose client can no longer authenticate.

        Never exposed over HTTP. Dispatched work stays cancel_requested so expiry
        still quarantines uncertain execution.
        """
        with self.write('release_revoked') as db:
            row=db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request_id,)).fetchone()
            if not row or row['status'] in {'completed','cancelled','expired'}:return
            state='cancel_requested' if row['executing'] and row['status']!='queued' else 'cancelled'
            db.execute('UPDATE capacity_requests SET status=? WHERE request_id=?',(state,request_id))
            self._event(db,row['app_id'],request_id,state,{'reason':'authorization.revoked'})

    def reconcile_resource(self,resource_id,confirmed_idle):
        if confirmed_idle is not True:raise ValueError('Explicit operator confirmation of idle execution is required')
        with self.write('reconcile_resource') as db:
            if db.execute("SELECT 1 FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(resource_id,)).fetchone():
                raise ValueError('Resource still has an active reservation')
            db.execute("UPDATE capacity_resources SET health='unqualified' WHERE resource_id=?",(resource_id,))
            from . import recovery
            hold=recovery.active_hold(db,resource_id)
            if hold:recovery.resolve(self,db,hold,state='released',by='operator',resolution='reconciled',event='suspension.released')

    def record_operation(self,request_id,operation):
        """Which qualification purpose (completion or embedding) a request needs; read by placement."""
        with self.write('record_operation') as db:db.execute('INSERT OR IGNORE INTO capacity_request_routing VALUES(?,?)',(request_id,operation))

    def requeue(self,request_id,resource_id,attempt,failure_class):
        """Nothing ran on the failed head: the same request (same ID, key and deadline) waits for another head.

        Returns the queued request, or None when it was cancelled, ended or is past its deadline.
        """
        with self.write('requeue') as db:
            now=time.time()
            row=db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request_id,)).fetchone()
            if row['status']!='leased' or row['deadline']<=now:return None
            db.execute("UPDATE capacity_requests SET status='queued',lease_id=NULL,resource_id=NULL,expires=NULL,executing=0 WHERE request_id=?",(request_id,))
            self._event(db,row['app_id'],request_id,'request.replaced',{'from_resource_id':resource_id,'failure_class':failure_class,'attempt':attempt+1})
            return dict(db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request_id,)).fetchone())

    def record_attempt(self,db,request_id,attempt,resource_id,failure_class):
        db.execute('INSERT OR REPLACE INTO capacity_request_attempts VALUES(?,?,?,?,?)',(request_id,attempt,resource_id,failure_class,time.time()))

    def revoke(self,app_id):
        with self.write('revoke') as db:
            if not db.execute('UPDATE capacity_clients SET enabled=0 WHERE app_id=?',(app_id,)).rowcount:return
            for row in db.execute("SELECT request_id FROM capacity_requests WHERE app_id=? AND status='queued'",(app_id,)).fetchall():
                db.execute("UPDATE capacity_requests SET status='cancelled' WHERE request_id=?",(row['request_id'],))
                self._event(db,app_id,row['request_id'],'cancelled',{'reason':'authorization.revoked'})
            self._event(db,app_id,None,'authorization.revoked',{})

    def executing(self,app_id,token,request_id):
        self.get(app_id,token,request_id)
        with self.write('executing') as db:
            changed=db.execute("UPDATE capacity_requests SET executing=1 WHERE request_id=? AND status='leased' AND expires>?",(request_id,time.time())).rowcount
            if not changed:raise ValueError('Lease is not active')

    def renew(self,app_id,token,request_id):
        """Extend an active lease. 0.4.1: a lease still in state leased (nobody has expired it) may be
        renewed after its expires time; expiry itself sets the state, so an expired lease is never revived."""
        self.get(app_id,token,request_id)
        with self.write('renew',wait=10.0) as db:
            now=time.time()
            changed=db.execute("UPDATE capacity_requests SET expires=min(deadline,?) WHERE request_id=? AND status IN ('leased','cancel_requested') AND deadline>?",
                (now+LEASE_SECONDS,request_id,now)).rowcount
            if not changed:raise ValueError('Expired lease cannot be renewed')
        return self.get(app_id,token,request_id)


def renew_until_stopped(broker,app_id,token,request_id,stopped,*,interval=10,log=None,count=None):
    """Lease renewal for one executing call (0.4.1).

    0.4.0 stopped on its first error, so one 'database is locked' let the 60 s lease run
    out mid-generation. Now every failure is logged and retried with backoff (0.5 s doubling
    to the interval) until stopped. Only a definite answer that the lease has ended
    (ValueError from renew) stops it early.
    """
    delay=interval;failures=0
    while not stopped.wait(delay):
        try:broker.renew(app_id,token,request_id)
        except ValueError as exc:
            if not stopped.is_set() and log:log('lease.renewal_ended',request_id=request_id,error=str(exc)[:200])
            return
        except Exception as exc:
            failures+=1;delay=min(interval,0.5*2**(failures-1))
            if log:log('lease.renewal_failed',request_id=request_id,attempt=failures,error=(type(exc).__name__+': '+str(exc))[:200])
            if count:count('lease_renewal_failures')
        else:
            if failures and log:log('lease.renewal_recovered',request_id=request_id,failed_attempts=failures)
            failures=0;delay=interval


@contextmanager
def shared_slot(path,app_id,token,endpoint,model,deadline):
    """Coordinator adapter to the neutral broker; no model unloading or preemption."""
    import threading
    service=CapacityBroker(path)
    with service.connect() as db:resource=db.execute('SELECT resource_id FROM capacity_resources WHERE endpoint=?',(endpoint,)).fetchone()
    if not resource:raise PermissionError('Endpoint has no explicit shared resource authorization')
    remaining=max(0,deadline-time.monotonic())
    request=service.submit(app_id,token,uuid.uuid4().hex,model,[resource['resource_id']],time.time()+remaining)
    stopped=threading.Event();thread=None;successful=False
    try:
        pause=.1
        while time.monotonic()<deadline:
            request=service.admit(app_id,token,request['request_id'])
            if request['status']=='leased':break
            if request['status']!='queued':raise TimeoutError('Shared capacity request ended before admission')
            # 0.4.1: back off (0.1 s up to 1 s), so waiting coordinators do not hold the write lock.
            time.sleep(min(pause,max(0,deadline-time.monotonic())));pause=min(1.0,pause*1.5)
        else:raise TimeoutError('Shared admission deadline exceeded')
        service.executing(app_id,token,request['request_id'])
        rid=request['request_id']
        service.local_leases=lambda:() if stopped.is_set() else (rid,)
        thread=threading.Thread(target=renew_until_stopped,args=(service,app_id,token,rid,stopped),daemon=True);thread.start()
        yield request
        live=service.get(app_id,token,request['request_id'])
        # Still leased means nobody expired it: this caller owns the execution (0.4.1, a late renewal is not a loss).
        if live['status']!='leased' or live['deadline']<=time.time():raise TimeoutError('Stale or cancelled shared inference result rejected')
        successful=True
    finally:
        stopped.set()
        if not successful and request.get('executing') or not successful and request.get('status')=='leased':
            # A timed-out Ollama call may still execute remotely. Hold the head,
            # rather than reusing or unloading another application's work.
            from . import recovery
            with service.write('coordinator_hold') as db:
                recovery.hold_resource(service,db,request.get('resource_id'),request_id=request['request_id'],model=model,operation=None,
                                       hold_until=request.get('deadline'),source='coordinator')
        service.finish(app_id,token,request['request_id'],cancel=not successful)
