"""Neutral execution gateway over the existing authoritative capacity ledger.

No medical-device, repository, visualization or MCP imports. Results are private
to the authenticated application. Upstream requests run only inside a lease.
"""
import collections
import copy
import hashlib
import ipaddress
import json
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import ssl
import math
import re
from .capacity_broker import CapacityBroker,renew_until_stopped,LedgerBusy,REQUEST_WRITE_WAIT,BACKGROUND_WRITE_WAIT,ledger_wait_seconds
from . import model_capacity
from . import qualification_fixtures
from . import recovery
from .recovery import InvalidReply

CONTRACT='shared.inference/1'
# Operation purposes gate dispatch exactly as in 0.2.0. Capability purposes are
# additional technical evidence on an already completion-qualified model.
OPERATION_PURPOSES=('completion','embedding')
# 0.4.6: structured_chat is /api/chat with a JSON-schema `format` (the shape of Capstone's structured roles and of a
# mediated tool strategy). structured_output stays /api/generate with a schema; tools stays native tool calling.
CAPABILITY_PURPOSES=('chat','tools','structured_output','vision','context','structured_chat')
DECLARED={'completion':'completion','embedding':'embedding','chat':'completion','tools':'tools',
          'structured_output':'completion','vision':'vision','context':'completion','structured_chat':'completion'}
FRESH_SECONDS=120
# Completion test: "does the model produce output for the prompt" (0.4.2), not "does it follow the instruction".
# 128 tokens keep a CPU head inside the 90 s qualification deadline; a model need not finish within them.
COMPLETION_TEST_PROMPT='Return exactly the word READY.'
COMPLETION_TEST_TOKENS=128
# Failures raised before any test call is sent (0.4.0 recorded these as failed tests).
PRECHECK_ERRORS=('Metadata does not support qualification operation','Qualification model not installed',
                 'Qualification requires an observed model digest','Qualification deadline expired',
                 'Capability qualification requires a current completion qualification of this digest and runtime')


class ClientPolicyError(PermissionError):
    """The request, not the resource, is outside policy: the head stays in service."""


class AllocationEnded(ClientPolicyError):
    """0.4.4: the lease ended (client cancellation, expiry) before dispatch. Nothing ran; no head is at fault.

    0.4.0-0.4.3 raised TimeoutError here, which classify() counts as head_unreachable in the checks phase:
    a client's cancel marked a healthy head offline, and the other calls being checked on it then failed.
    """


class GatewayOverload(Exception):
    """The Gateway's own budget is full right now (HTTP 503 with Retry-After); retry with the same client_key."""
    def __init__(self,message,retry_after=1):super().__init__(message);self.retry_after=retry_after


def completion_test(response):
    """Pass when the model produced output for the prompt: done true and eval_count > 0, whether it
    stopped ('stop') or ran out of tokens ('length'), and whether that output is still inside a reasoning
    block. The operator's rule (0.4.2): a model that is reasoning is a working model.

    Recorded, never required: answered (text after the reasoning), finished_reasoning, answer_matches_ready.
    A runtime that reports no eval_count passes on non-empty output text.
    """
    if not isinstance(response,dict):return False,{'done_reason':None,'eval_count':None,'output':False}
    text=response.get('response') if isinstance(response.get('response'),str) else ''
    thinking_field=response.get('thinking') if isinstance(response.get('thinking'),str) else ''
    reasoning='<think>' in text or '</think>' in text or bool(thinking_field.strip())
    visible=re.sub(r'<think>.*?</think>','',text,flags=re.S)
    if '</think>' in visible:visible=visible.rsplit('</think>',1)[1] # reasoning printed without its opening tag
    still_reasoning='<think>' in visible                              # reasoning still open when generation ended
    if still_reasoning:visible=visible.split('<think>',1)[0]
    visible=visible.strip()
    count=response.get('eval_count') if isinstance(response.get('eval_count'),int) and not isinstance(response.get('eval_count'),bool) else None
    output=count>0 if count is not None else bool(text.strip() or thinking_field.strip())
    passed=bool(response.get('done') is True and output)
    return passed,{'done_reason':response.get('done_reason'),'eval_count':count,'output':output,
                   'answered':bool(visible),'reasoning':reasoning,'finished_reasoning':reasoning and not still_reasoning and bool(visible),
                   'answer_matches_ready':re.sub(r'[^a-z0-9]','',visible.lower())=='ready'}


def refused_evidence(evidence):
    """True for a qualification that never sent its test call: it says nothing about the model."""
    return bool(evidence.get('refused') or (not evidence.get('passed') and 'response' not in evidence and evidence.get('error') in PRECHECK_ERRORS))


def _proof_from(rows,resource,purpose):
    """The newest test of this purpose that ran on this endpoint, if it passed; else None.

    rows: (row, parsed evidence) of one head and model, newest first (0.4.6: shared by the ledger read and the cache).
    """
    for q,e in rows:
        if e.get('purpose')!=purpose or e.get('endpoint')!=resource['endpoint'] or refused_evidence(e):continue
        return q if q['passed'] else None
    return None


def _view_from(rows,resource):
    """Newest evidence per purpose, judged against the current operation qualification (rows newest first, at most 200)."""
    # 0.4.1: a test that was never sent (refused by a pre-check) is not evidence about the model.
    rows=[(q,e) for q,e in rows if e.get('endpoint')==resource['endpoint'] and not refused_evidence(e)]
    base=next(((q,e) for q,e in rows if q['passed'] and e.get('purpose') in OPERATION_PURPOSES),None)
    completion=next(((q,e) for q,e in rows if base and e.get('purpose')=='completion' and q['digest']==base[0]['digest'] and q['version']==base[0]['version']),None)
    if completion and not completion[0]['passed']:completion=None # the newest completion test failed
    states={}
    for purpose in OPERATION_PURPOSES+CAPABILITY_PURPOSES:
        mine=[(q,e) for q,e in rows if e.get('purpose')==purpose]
        if not mine:states[purpose]={'source':'measured','state':'not_tested'};continue
        current=next(((q,e) for q,e in mine if base and q['digest']==base[0]['digest'] and q['version']==base[0]['version']),None)
        if not current:
            states[purpose]={'source':'measured','state':'invalidated','reason':'digest_or_runtime_changed','evidence_id':mine[0][0]['evidence_id'],'at':mine[0][0]['at']};continue
        q,e=current;state={'source':'measured','state':'passed' if q['passed'] else 'failed','evidence_id':q['evidence_id'],'at':q['at']}
        limit=(e.get('execution_policy') or {}).get('max_context')
        if purpose in ('completion','context'):state['max_context']=limit
        if purpose=='embedding' and e.get('embedding_dimensions'):state['embedding_dimensions']=e['embedding_dimensions']
        if purpose in CAPABILITY_PURPOSES:
            state['fixture']=(e.get('fixture') or {}).get('id')
            current_limit=(completion[1].get('execution_policy') or {}).get('max_context') if completion else None
            if q['passed'] and (not completion or limit!=current_limit):state.update(state='invalidated',reason='context_policy_changed' if completion else 'completion_not_current')
        states[purpose]=state
    return {'states':states,'qualified':sorted(p for p,s in states.items() if s['state']=='passed')}


_SECRET_PATTERN=re.compile(r'(?i)(authorization|bearer|x-infrastructure-token|token|api[_-]?key|password|secret)(["\'\s:=]+)([^\s"\',;]+)')
_CONTROL=re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def redact(text,secrets=()):
    """0.4.6: bounded diagnostic text with credentials removed (exact known secrets and key=value / Bearer forms)."""
    for secret in secrets:
        if secret and len(secret)>=8:text=text.replace(secret,'[REDACTED]')
    text=re.sub(r'(?i)\bbearer\s+[^\s"\',;]+','Bearer [REDACTED]',text)
    text=_SECRET_PATTERN.sub(lambda m:m.group(1)+m.group(2)+'[REDACTED]',text)
    return _CONTROL.sub(' ',text)


def http_error_body(exc,limit=None):
    """0.4.6: (text, truncated, decode_errors) of an HTTPError's reply body, read once and at most `limit` bytes.

    The body is read when the error is raised (Gateway.upstream) and kept on the exception, so classification
    (recovery.classify sees the same HTTPError) and later reads are unaffected. Never raises: an unreadable body
    is '' and undecodable bytes are replaced.
    """
    limit=UPSTREAM_BODY_LIMIT if limit is None else limit
    cached=getattr(exc,'gateway_error_body',None)
    if cached is not None:return cached
    raw=b''
    try:
        fp=getattr(exc,'fp',None)
        if fp is not None:raw=exc.read(limit+1) or b''
    except Exception:raw=b''
    if not isinstance(raw,bytes):raw=str(raw).encode('utf-8','replace')
    truncated=len(raw)>limit;raw=raw[:limit]
    text=raw.decode('utf-8','replace');decode_errors=text.count('�')
    value=(text,truncated,decode_errors)
    try:exc.gateway_error_body=value
    except Exception:pass
    return value


def upstream_message(text):
    """The useful part of an Ollama error reply: its JSON 'error' string when present, else the text."""
    try:
        value=json.loads(text)
        if isinstance(value,dict) and isinstance(value.get('error'),str):return value['error']
    except (TypeError,ValueError):pass
    return text


def request_shape(operation,payload):
    """0.4.6: what a request looked like, without its content: fields, counts, sizes, format kind and options.

    Never raises: it runs while a failure is being settled, and an exception there would leave the lease executing
    (the head is then held as uncertain execution). On any malformed input the shape says so instead.
    """
    try:return _request_shape(operation,payload)
    except Exception as exc:
        return {'operation':operation if isinstance(operation,str) else None,'capture_error':type(exc).__name__}


def _request_shape(operation,payload):
    payload=payload if isinstance(payload,dict) else {}
    messages=payload.get('messages') if isinstance(payload.get('messages'),list) else []
    roles={}
    for m in messages:
        role=m.get('role') if isinstance(m,dict) and isinstance(m.get('role'),str) else '?'
        roles[role[:20]]=roles.get(role[:20],0)+1
    fmt=payload.get('format')
    shape={'operation':operation,'fields':sorted(k for k in payload if k not in ('model','keep_alive','stream')),
           'messages':len(messages),'roles':roles,
           'message_chars':sum(len(m.get('content')) for m in messages if isinstance(m,dict) and isinstance(m.get('content'),str)),
           'prompt_chars':len(payload['prompt']) if isinstance(payload.get('prompt'),str) else None,
           'format':None if fmt is None else 'json' if fmt=='json' else 'schema' if isinstance(fmt,dict) else 'other',
           'tools':len(payload['tools']) if isinstance(payload.get('tools'),list) else 0,
           'images':len(payload['images']) if isinstance(payload.get('images'),list) else 0,
           'think':payload.get('think') if isinstance(payload.get('think'),(bool,str)) else None}
    if isinstance(fmt,dict):
        shape['format_sha256']=hashlib.sha256(json.dumps(fmt,sort_keys=True).encode()).hexdigest()
        props=fmt.get('properties') if isinstance(fmt.get('properties'),dict) else {}
        shape['format_properties']=sorted(str(k)[:60] for k in props)[:20]
        shape['format_additional_properties']=fmt.get('additionalProperties') if isinstance(fmt.get('additionalProperties'),bool) else None
    if shape['tools']:
        def tool_name(t):
            function=t.get('function') if isinstance(t,dict) else None
            name=function.get('name') if isinstance(function,dict) else None
            return name[:60] if isinstance(name,str) else '<%s>'%type(function).__name__
        shape['tool_names']=[tool_name(t) for t in payload['tools'][:20]]
    options=payload.get('options') if isinstance(payload.get('options'),dict) else {}
    shape['options']={k:v for k,v in options.items() if k!='stop' and isinstance(v,(int,float)) and not isinstance(v,bool)}
    if isinstance(options.get('stop'),list):shape['options']['stop_count']=len(options['stop'])
    return shape


class Scheduler:
    """One admission pass per event (0.4.1, replaces each waiting request's 50 ms admit() spin).

    Events: a submission, a release, a health or qualification change; plus a TICK while any
    request waits (deadlines, other processes). Passes coalesce: many events during one pass
    cause one more pass. Waiting requests block on their own Event and read their row, which
    is a plain read outside the write lock. The thread exits after IDLE seconds with nothing waiting.
    """
    TICK=1.0
    IDLE=5.0
    MIN_GAP=0.05

    def __init__(self,gateway):
        self.gateway=gateway;self.cond=threading.Condition();self.waiters={};self.pending=False;self.thread=None
        self.generation=0;self.passes=0;self.errors=0

    def wake(self,*,health=False):
        with self.cond:
            self.pending=True
            if health:self.generation+=1
            if self.waiters:self._ensure() # nothing in this process waits: no pass needed
            self.cond.notify_all()

    def _ensure(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread=threading.Thread(target=self._loop,daemon=True,name='gateway-scheduler');self.thread.start()

    def register(self,request_id):
        with self.cond:
            event=self.waiters.setdefault(request_id,threading.Event())
            self.pending=True;self._ensure();self.cond.notify_all()
        return event

    def unregister(self,request_id):
        with self.cond:self.waiters.pop(request_id,None)

    def _loop(self):
        idle=time.monotonic()
        while True:
            with self.cond:
                if not self.pending:self.cond.wait(self.TICK)
                if not self.pending and not self.waiters:
                    if time.monotonic()-idle>=self.IDLE:self.thread=None;return
                    continue
                self.pending=False
            idle=time.monotonic();began=idle
            try:changed=self.gateway.broker.place()
            except Exception as exc:
                self.errors+=1;self.gateway.count('scheduler_pass_failures')
                self.gateway.emit('scheduler.pass_failed',error=(type(exc).__name__+': '+str(exc))[:200])
                time.sleep(.2)
                with self.cond:self.pending=True
                continue
            self.passes+=1
            with self.cond:
                for rid in changed:
                    event=self.waiters.get(rid)
                    if event:event.set()
            # 0.4.4: events that arrive during a pass are coalesced for MIN_GAP before the next one. 0.4.1-0.4.3
            # started the next pass at once, so under steady events the scheduler took the write lock back to
            # back and SQLite's polling busy handler starved renewals and submissions (database is locked).
            # 0.4.6: the gap is at least as long as the pass just took (at most MAX_GAP), so a slow pass can occupy
            # at most about half of the ledger's time; events arriving meanwhile coalesce into the next pass.
            time.sleep(min(self.MAX_GAP,max(self.MIN_GAP,time.monotonic()-began)))
    MAX_GAP=0.5
SCHEMA='''CREATE TABLE IF NOT EXISTS capacity_inventory (
 resource_id TEXT PRIMARY KEY,endpoint TEXT NOT NULL,observed_at REAL NOT NULL,runtime_version TEXT,models TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_invocations (
 request_id TEXT PRIMARY KEY,app_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
 execution_owner TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_qualification_evidence (
 evidence_id TEXT PRIMARY KEY,resource_id TEXT NOT NULL,model TEXT NOT NULL,
 digest TEXT,version TEXT NOT NULL,passed INTEGER NOT NULL,evidence TEXT NOT NULL,at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_capacity_qualification_update BEFORE UPDATE ON capacity_qualification_evidence BEGIN SELECT RAISE(ABORT,'Immutable qualification'); END;
CREATE TRIGGER IF NOT EXISTS immutable_capacity_qualification_delete BEFORE DELETE ON capacity_qualification_evidence BEGIN SELECT RAISE(ABORT,'Immutable qualification'); END;'''
# 0.4.6, additive (one index, one table; older releases ignore both, so a rollback keeps working).
# The index serves per-head qualification reads (0.4.5 scanned the whole evidence table for each head and model).
# capacity_upstream_errors keeps what a head answered when it refused a call: protected (ledger only, never in the
# observation file); the application sees only its own request's bounded message.
SCHEMA_0_4_6='''
CREATE INDEX IF NOT EXISTS capacity_qualification_evidence_resource ON capacity_qualification_evidence(resource_id,model,at);
CREATE TABLE IF NOT EXISTS capacity_upstream_errors (
 error_id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,attempt INTEGER,app_id TEXT NOT NULL,resource_id TEXT,
 model TEXT,digest TEXT,runtime_version TEXT,operation TEXT,phase TEXT,failure_class TEXT,status INTEGER,
 message TEXT,body TEXT,body_truncated INTEGER NOT NULL DEFAULT 0,decode_errors INTEGER NOT NULL DEFAULT 0,shape TEXT,at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS capacity_upstream_errors_request ON capacity_upstream_errors(request_id);
CREATE INDEX IF NOT EXISTS capacity_upstream_errors_at ON capacity_upstream_errors(at);
'''
FLEET_SNAPSHOT_SECONDS=5   # 0.4.6: an application's fleet snapshot is served again for at most this long while its inputs are unchanged
UPSTREAM_BODY_LIMIT=4096   # 0.4.6: bytes of a head's error reply kept (protected); the application sees at most MESSAGE_LIMIT characters
UPSTREAM_MESSAGE_LIMIT=1000


class Gateway:
    def __init__(self,path,transport=None,max_pending=128,upstream_ca=None,upstream_certificate=None,upstream_key=None):
        self.broker=CapacityBroker(path)
        self.upstream_tls=ssl.create_default_context(cafile=upstream_ca)
        if upstream_certificate:self.upstream_tls.load_cert_chain(upstream_certificate,upstream_key)
        self.transport=transport or self.upstream
        self.actual_transport=transport is None
        self.max_pending=max_pending
        # 0.4.1: one scheduler; calls this process is executing (their leases are never expired
        # by this process while the call runs); counters the service publishes in the observation.
        self.scheduler=Scheduler(self);self._live=set();self._live_lock=threading.Lock()
        self.counters={};self._counter_lock=threading.Lock();self._counters_since=time.time()
        self.broker.local_leases=self.live_leases
        self.broker.log=lambda event,**fields:self.emit(event,**fields) # 0.4.4: slow or refused ledger writes
        # 0.4.4: results this process finished, and when each request's status was last read
        # (requests whose caller stopped polling, e.g. a worker that died, are reported as unclaimed).
        self._finished={};self._last_read={};self._claims_lock=threading.Lock()
        with self.broker.connect() as db:
            db.executescript(SCHEMA)
            if 'started_at' not in {r['name'] for r in db.execute('PRAGMA table_info(capacity_invocations)')}:
                db.execute('ALTER TABLE capacity_invocations ADD COLUMN started_at REAL')
            # 0.4.4: _execute counted active invocations by scanning every stored result inside the write lock.
            db.execute('CREATE INDEX IF NOT EXISTS capacity_invocations_state ON capacity_invocations(state)')
            db.executescript(SCHEMA_0_4_6)
        # 0.4.6: per-head qualification facts (see _facts) and application-scoped fleet snapshots (see fleet).
        self._facts_cache={};self._fleet_cache={};self._cache_lock=threading.Lock()
        self._routes={};self._routes_lock=threading.Lock()
        self.broker.reconcile_expired()
        with self.broker.write('startup') as db:db.execute("UPDATE capacity_invocations SET state='unknown',updated=? WHERE state='active' AND request_id IN (SELECT request_id FROM capacity_requests WHERE status IN ('expired','cancelled'))",(time.time(),))

    def emit(self,event,**fields):
        """Operational log hook; the service runtime replaces it with its sanitized JSON log."""

    def count(self,name,n=1):
        with self._counter_lock:self.counters[name]=self.counters.get(name,0)+n

    def live_leases(self):
        with self._live_lock:return tuple(self._live)

    ROUTE_SAMPLES=512

    def note_route(self,route,seconds,status=None):
        """0.4.6: calls and handler time per HTTP route since this process started (published with the counters)."""
        with self._routes_lock:
            item=self._routes.get(route)
            if item is None:
                if len(self._routes)>=64:route='other';item=self._routes.get(route)
                if item is None:item=self._routes[route]={'count':0,'ms_total':0.0,'ms_max':0.0,'errors':0,'recent':collections.deque(maxlen=self.ROUTE_SAMPLES)}
            ms=seconds*1000;item['count']+=1;item['ms_total']+=ms;item['ms_max']=max(item['ms_max'],ms);item['recent'].append(ms)
            if status is not None and status>=500:item['errors']+=1

    def route_stats(self):
        with self._routes_lock:
            out={}
            for route,item in self._routes.items():
                recent=sorted(item['recent'])
                pick=lambda f:round(recent[min(len(recent)-1,int(len(recent)*f))],1) if recent else None
                out[route]={'count':item['count'],'ms_total':round(item['ms_total']),'ms_max':round(item['ms_max'],1),'errors_5xx':item['errors'],
                            'recent_p50_ms':pick(.5),'recent_p95_ms':pick(.95),'recent_samples':len(recent)}
            return out

    COUNTERS_KEY='gateway_counters'

    def publish_counters(self,**limits):
        """Write this process's load counters (since its start) to the ledger for the observation file.

        Called by the service's health cycle, never per request, so overload adds no write.
        """
        self.sweep_unclaimed()
        with self._counter_lock:counters=dict(self.counters)
        value={'since':self._counters_since,'updated_at':time.time(),
               'scheduler_passes':self.scheduler.passes,'executing_now':len(self.live_leases()),'counters':counters,'limits':limits,
               'ledger':self.broker.ledger_stats(), # 0.4.4: write-lock waits and holds per transaction label
               'routes':self.route_stats(),'health_cycle':self.last_health_cycle} # 0.4.6, additive
        with self.broker.write('publish_counters') as db:
            db.execute('INSERT INTO capacity_service_policy VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(self.COUNTERS_KEY,json.dumps(value,sort_keys=True)))
        return value

    UNCLAIMED_SECONDS=120

    def _note_finished(self,app,rid,state):
        with self._claims_lock:self._finished[rid]=(time.time(),app,state)

    def unread_results(self):
        """0.4.4: results this process finished whose status has not been read since (graceful stop waits briefly for them)."""
        with self._claims_lock:
            now=time.time()
            return sum(1 for rid,(finished,_,_) in self._finished.items()
                       if (self._last_read.get(rid) or 0)<finished and now-finished<self.UNCLAIMED_SECONDS)

    def sweep_unclaimed(self,now=None):
        """0.4.4: a result this process finished whose status nobody read within UNCLAIMED_SECONDS.

        The usual cause is a caller (workflow worker) that stopped or died while its request was
        queued or running. Each is logged once as request.unclaimed and counted; nothing is replayed.
        """
        now=time.time() if now is None else now;out=[]
        with self._claims_lock:
            for rid,(finished,app,state) in list(self._finished.items()):
                read=self._last_read.get(rid)
                if read is not None and read>=finished:del self._finished[rid];self._last_read.pop(rid,None);continue
                if now-finished>=self.UNCLAIMED_SECONDS:
                    out.append({'request_id':rid,'app_id':app,'state':state,'finished_at':finished,'last_read_at':read})
                    del self._finished[rid];self._last_read.pop(rid,None)
            for rid,read in list(self._last_read.items()):
                if now-read>3600 and rid not in self._finished:del self._last_read[rid]
        for item in out:
            self.count('results_unclaimed');self.emit('request.unclaimed',**item)
        return out

    def cancel(self,app,token,request_id):
        """Client cancellation (POST /v1/requests/<id>/cancel); a freed slot is offered to the next request at once."""
        self.broker.finish(app,token,request_id,cancel=True,wait=REQUEST_WRITE_WAIT)
        self.scheduler.wake()
        return self.result(app,token,request_id)

    def _durable(self,label,deadline,body):
        """A write that must not be lost (a result, a failure's settlement): retry LedgerBusy until `deadline`."""
        while True:
            left=deadline-time.time()
            try:
                with self.broker.write(label,wait=max(1.0,min(BACKGROUND_WRITE_WAIT,left))) as db:return body(db)
            except LedgerBusy:
                self.count('ledger_retries')
                if time.time()>=deadline:raise

    def upstream(self,endpoint,operation,payload,timeout):
        origin=urllib.parse.urlsplit(endpoint)
        address=ipaddress.ip_address(origin.hostname)
        if origin.scheme not in {'http','https'} or not (address.is_private or address.is_loopback) or address.is_link_local or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('','/'):
            raise ValueError('Only an approved private inference origin is allowed')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        request=urllib.request.Request(endpoint.rstrip('/')+operation,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=self.upstream_tls)).open(request,timeout=timeout) as response:
                data=response.read(16*1024*1024+1)
                if len(data)>16*1024*1024:raise ValueError('Upstream response exceeds the bounded service envelope')
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            # 0.4.6 (G4): the head's explanation (bounded, read once, kept on the exception). The same HTTPError is
            # raised again, so recovery.classify keeps its class (4xx request fault, 5xx model on head).
            try:http_error_body(exc)
            except Exception:pass
            raise

    @staticmethod
    def validate_response(operation,response,model):
        if not isinstance(response,dict) or response.get('error') or response.get('model')!=model:raise ValueError('Upstream did not return the qualified model response')
        if operation=='/api/embed':
            rows=response.get('embeddings')
            if not isinstance(rows,list) or not rows or any(not isinstance(row,list) or not row or any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in row) for row in rows):raise ValueError('Embedding response is incomplete or non-finite')
            if len({len(row) for row in rows})!=1:raise ValueError('Embedding dimensions are inconsistent')
        elif response.get('done') is not True:raise ValueError('Incomplete generation cannot publish completion')
        elif operation=='/api/generate' and not isinstance(response.get('response'),str):raise ValueError('Generation response text is missing')
        elif operation=='/api/chat' and not isinstance(response.get('message'),dict):raise ValueError('Chat response message is missing')

    def refresh_health(self):
        """Probe only explicitly enrolled origins. Never loads a model or enrolls LAN hosts.

        Active/uncertain execution is never preempted. Held (quarantined) heads are
        released only by run_recovery's probe or an operator, maintenance only by its
        qualification; transient disconnects recover only when the qualified version
        and digest remain unchanged. 0.4.0 also reads /api/ps (loaded models) for placement.
        """
        cycle=time.monotonic();ledger_wait_seconds(reset=True);heads_seconds={};local_failures=[];head_failures=[]
        # 0.4.6: the head's answer and the Gateway's own bookkeeping are separate stages. 0.4.5 caught every exception
        # in one block: a LedgerBusy while recording inventory marked a head that had just answered as offline, and a
        # refused health write ended the whole cycle for the remaining heads. Now only a failed version/tags call marks
        # a head offline; a local ledger or persistence failure leaves its health as it was and is logged with its stage.
        try:self.broker.reconcile_expired()
        except Exception as exc:local_failures.append(self._local_failure(None,'expire',exc))
        with self.broker.connect() as db:
            resources=[dict(r) for r in db.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference' AND health!='retired'")]
        for resource in resources:
            # Unqualified, quarantined and maintenance heads are observed (read-only
            # version/tags) for inventory, but their health is operator-owned.
            managed=resource['health'] not in ('quarantined','maintenance','unqualified')
            stage='version'
            try:
                asked=time.monotonic()
                version=self.transport(resource['endpoint'],'/api/version',None,3)
                stage='tags'
                tags=self.transport(resource['endpoint'],'/api/tags',None,3).get('models',[])
                stage='tags_reply'
                installed={m['name']:m.get('digest') for m in tags}
                heads_seconds[resource['resource_id']]=time.monotonic()-asked
            except Exception as exc:
                # Head communication failed (no answer, refused, or an answer that is not a version/tags reply).
                head_failures.append(resource['resource_id'])
                cause=(type(exc).__name__+': '+str(exc))[:200]
                self.count('health_head_failures');self.emit('health.head_failed',resource_id=resource['resource_id'],stage=stage,error=cause)
                if not managed:continue
                try:
                    with self.broker.write('health') as db:
                        changed=db.execute("UPDATE capacity_resources SET health='offline',last_seen=0 WHERE resource_id=? AND endpoint=? AND last_seen=? AND qualified_models=? AND health NOT IN ('quarantined','maintenance','unqualified','retired')",
                            (resource['resource_id'],resource['endpoint'],resource['last_seen'],resource['qualified_models'])).rowcount
                        if changed and resource['health']!='offline':
                            self.broker._event(db,'infrastructure',None,'resource.health_changed',{'resource_id':resource['resource_id'],'from':resource['health'],'to':'offline',
                                                                                             'stage':stage,'cause':cause})
                except Exception as local:local_failures.append(self._local_failure(resource['resource_id'],'health_write',local))
                continue
            loaded=self._read_ps(resource) # placement facts; a head without /api/ps keeps "loaded unknown"
            local_stage='qualification_read'
            try:
                # A model that left the list (removed from the head, digest or runtime changed) needs requalification.
                healthy=self._current_models(resource,version,installed,json.loads(resource['qualified_models']))
                health='healthy' if healthy else 'degraded'
                local_stage='head_record'
                # 0.4.6: inventory, runtime and health in one write transaction per head (0.4.5: three).
                with self.broker.write('health') as db:
                    self._inventory_in(db,resource,version,installed)
                    self._runtime_in(db,resource,tags,loaded)
                    if not managed:continue
                    # Preserve qualifications across transient outages. Execution
                    # validates the same proof again immediately before dispatch.
                    changed=db.execute("UPDATE capacity_resources SET health=?,last_seen=?,qualified_models=? WHERE resource_id=? AND endpoint=? AND last_seen=? AND qualified_models=? AND health NOT IN ('quarantined','maintenance','unqualified','retired')",
                        (health,time.time() if health=='healthy' else 0,json.dumps(healthy),
                         resource['resource_id'],resource['endpoint'],resource['last_seen'],resource['qualified_models'])).rowcount
                    if changed and health!=resource['health']:
                        self.broker._event(db,'infrastructure',None,'resource.health_changed',{'resource_id':resource['resource_id'],'from':resource['health'],'to':health})
                    for model in (set(json.loads(resource['qualified_models']))-set(healthy) if changed else ()):
                        self.broker._event(db,'infrastructure',None,'qualification.invalidated',{'resource_id':resource['resource_id'],'model':model})
            except Exception as exc:
                # The head answered; the Gateway could not record it (LedgerBusy, a persistence error). Its health
                # is left as it was: a stale head stops admission through last_seen on its own, never through this.
                local_failures.append(self._local_failure(resource['resource_id'],local_stage,exc))
        # 0.4.6: bounded retention of the protected upstream error rows (at most every UPSTREAM_PRUNE_SECONDS); a failure
        # is a local failure of this cycle and appears in its summary.
        try:self.prune_upstream_errors()
        except Exception as exc:local_failures.append(self._local_failure(None,'upstream_error_retention',exc))
        # 0.4.6: a temporary lab identity past its expiry is revoked and withdrawn from the heads (a killed tool's leftover).
        try:
            expired=self.broker.expire_temporary_clients()
            if expired:self._safe_emit('temporary_client.expired',app_ids=expired)
        except Exception as exc:local_failures.append(self._local_failure(None,'temporary_client_expiry',exc))
        # 0.4.4: a slow cycle says whether the heads (version/tags answers) or the ledger (write waits) took the time.
        total=time.monotonic()-cycle
        self.last_health_cycle={'at':time.time(),'total_ms':round(total*1000),'ledger_wait_ms':round(ledger_wait_seconds()*1000),
                                'heads_ms':round(sum(heads_seconds.values())*1000),'head_failures':head_failures,
                                'local_failures':[f['stage'] for f in local_failures]}
        if total>=self.SLOW_HEALTH_SECONDS:
            slowest=sorted(heads_seconds.items(),key=lambda kv:-kv[1])[:3]
            self.emit('health.slow_cycle',total_ms=round(total*1000),ledger_wait_ms=round(ledger_wait_seconds()*1000),
                      heads_ms=round(sum(heads_seconds.values())*1000),slowest_heads={k:round(v*1000) for k,v in slowest},
                      head_failures=head_failures,local_failures=len(local_failures))
        self.scheduler.wake(health=True)
        return self.last_health_cycle

    UPSTREAM_ERROR_DAYS=14        # 0.4.6: capacity_upstream_errors keeps at most this many days ...
    UPSTREAM_ERROR_ROWS=20000     # ... and at most this many rows (newest kept); each row is at most ~6 KiB
    UPSTREAM_PRUNE_SECONDS=600
    _pruned_at=0.0

    def prune_upstream_errors(self,*,now=None,force=False):
        """0.4.6: bounded retention for the protected upstream error rows; at most every UPSTREAM_PRUNE_SECONDS."""
        now=time.time() if now is None else now
        if not force and now-self._pruned_at<self.UPSTREAM_PRUNE_SECONDS:return None
        self._pruned_at=now
        with self.broker.write('upstream_error_retention') as db:
            aged=db.execute('DELETE FROM capacity_upstream_errors WHERE at<?',(now-self.UPSTREAM_ERROR_DAYS*86400,)).rowcount
            cut=db.execute('SELECT error_id FROM capacity_upstream_errors ORDER BY error_id DESC LIMIT 1 OFFSET ?',(self.UPSTREAM_ERROR_ROWS,)).fetchone()
            excess=db.execute('DELETE FROM capacity_upstream_errors WHERE error_id<=?',(cut[0],)).rowcount if cut else 0
        if aged or excess:self._safe_emit('upstream.errors_pruned',aged=aged,excess=excess)
        return {'aged':aged,'excess':excess}

    SLOW_HEALTH_SECONDS=10
    last_health_cycle=None

    def _local_failure(self,resource_id,stage,exc):
        """0.4.6: the Gateway's own bookkeeping failed during a health cycle; never reported as a head failure."""
        item={'resource_id':resource_id,'stage':stage,'error':(type(exc).__name__+': '+str(exc))[:200],
              'ledger_busy':isinstance(exc,LedgerBusy)}
        self.count('health_local_failures');self.emit('health.local_failure',**item)
        return item

    def _newest_operation_proofs(self,db,resource):
        """{(model, purpose): newest evidence row} over operation tests that actually ran on this endpoint."""
        newest={}
        for q in db.execute('SELECT * FROM capacity_qualification_evidence WHERE resource_id=? ORDER BY at DESC',(resource['resource_id'],)):
            e=json.loads(q['evidence'])
            if e.get('purpose') not in OPERATION_PURPOSES or e.get('endpoint')!=resource['endpoint'] or refused_evidence(e):continue
            newest.setdefault((q['model'],e['purpose']),q)
        return newest

    def _current_models(self,resource,version,installed,models):
        """The listed models with at least one operation purpose whose newest test passed on this digest, runtime and endpoint.

        0.4.1: a failed test withdraws only the purpose it tested; a test that was never sent changes nothing.
        """
        # 0.4.6: from the per-head facts (the newest test of each purpose is the same as _newest_operation_proofs's).
        with self.broker.connect() as db:facts=self._facts(db,resource)
        return [model for model in models
                if any((q:=facts.get(model,{}).get('newest',{}).get(p)) is not None and q['passed'] and q['digest']==installed.get(model) and json.loads(q['version'])==version
                       for p in OPERATION_PURPOSES)]

    def _proof(self,db,resource,model,purpose):
        """The newest test of this purpose that ran on this endpoint, if it passed; else None.

        Read from the ledger every time: dispatch and qualification use this, never the cached facts.
        """
        rows=db.execute('SELECT * FROM capacity_qualification_evidence WHERE resource_id=? AND model=? ORDER BY at DESC,rowid DESC',(resource['resource_id'],model))
        return _proof_from(((q,json.loads(q['evidence'])) for q in rows),resource,purpose)

    # ------------------------------------------------------------ 0.4.6 per-head qualification facts (G1)

    def _facts(self,db,resource):
        """{model: {'proofs': {purpose: row or None}, 'newest': {purpose: row}, 'view': _qualification_view}} for one head.

        Qualification evidence is immutable (insert-only triggers), so a head's facts are fully determined by
        its endpoint and the set of its evidence rows. They are cached under (endpoint, row count, highest rowid)
        and rebuilt as soon as a new test is recorded. Inventory, health, suspensions, occupancy and application
        authorization are never cached here: every caller reads them live. Dispatch does not use this cache.
        """
        return self._facts_entry(db,resource)[1]

    def _facts_entry(self,db,resource):
        """(cache key, facts) for one head; see _facts."""
        rid=resource['resource_id']
        stamp=db.execute('SELECT count(*),max(rowid) FROM capacity_qualification_evidence WHERE resource_id=?',(rid,)).fetchone()
        key=(resource['endpoint'],stamp[0],stamp[1])
        with self._cache_lock:hit=self._facts_cache.get(rid)
        if hit and hit[0]==key:
            self.count('facts_cache_hits');return hit
        rows=db.execute('SELECT rowid AS evidence_rowid,* FROM capacity_qualification_evidence WHERE resource_id=? ORDER BY at DESC,rowid DESC',(rid,)).fetchall()
        key=(resource['endpoint'],len(rows),max((r['evidence_rowid'] for r in rows),default=None)) # the rows actually read
        by_model={}
        for q in rows:by_model.setdefault(q['model'],[]).append((dict(q),json.loads(q['evidence'])))
        facts={}
        for model,items in by_model.items():
            usable=[(q,e) for q,e in items if e.get('endpoint')==resource['endpoint'] and not refused_evidence(e)]
            newest={}
            for q,e in usable:newest.setdefault(e.get('purpose'),q)
            facts[model]={'proofs':{p:_proof_from(items,resource,p) for p in OPERATION_PURPOSES+CAPABILITY_PURPOSES},
                          'newest':newest,'view':_view_from(items[:200],resource)}
        with self._cache_lock:self._facts_cache[rid]=(key,facts)
        self.count('facts_cache_builds')
        return key,facts

    def _record_runtime(self,resource,tags):
        """Placement facts: model sizes (/api/tags) and what is loaded now (/api/ps).

        A head that does not answer /api/ps keeps "loaded unknown" (loaded NULL); never a failure.
        """
        loaded=self._read_ps(resource)
        with self.broker.write('record_runtime') as db:self._runtime_in(db,resource,tags,loaded)
        return loaded

    def _read_ps(self,resource):
        """0.4.6: the /api/ps read on its own (network, outside any write); None when the head does not answer it."""
        try:
            ps=self.transport(resource['endpoint'],'/api/ps',None,3)
            if isinstance(ps,dict) and isinstance(ps.get('models'),list) and all(isinstance(m,dict) and isinstance(m.get('name'),str) for m in ps['models']):
                return [{'name':m['name'],'digest':m.get('digest'),'size':m.get('size') if isinstance(m.get('size'),int) else None,
                         'expires_at':m.get('expires_at') if isinstance(m.get('expires_at'),str) else None} for m in ps['models']]
        except Exception:pass
        return None

    def _runtime_in(self,db,resource,tags,loaded):
        """Write the placement facts inside the caller's write transaction (0.4.6)."""
        sizes={m['name']:m['size'] for m in tags if isinstance(m.get('size'),int) and not isinstance(m.get('size'),bool)}
        # 0.4.3: families per model (placement_policy.serial_families); /api/tags details.family and details.families.
        families={}
        for m in tags:
            details=m.get('details') if isinstance(m.get('details'),dict) else {}
            names=[details.get('family')]+list(details.get('families') or [])
            names=sorted({n[:100] for n in names if isinstance(n,str) and n})[:16]
            if isinstance(m.get('name'),str) and names:families[m['name']]=names
        now=time.time()
        db.execute('INSERT INTO capacity_inventory_runtime(resource_id,loaded,loaded_at,sizes,observed_at,families) VALUES(?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET '
                   'loaded=excluded.loaded,loaded_at=excluded.loaded_at,sizes=excluded.sizes,observed_at=excluded.observed_at,families=excluded.families',
                   (resource['resource_id'],json.dumps(loaded) if loaded is not None else None,now if loaded is not None else None,json.dumps(sizes,sort_keys=True),now,
                    json.dumps(families,sort_keys=True)))

    def _mark_loaded(self,resource_id,model,digest):
        """A completed call leaves its model resident (keep_alive 2m) until the next /api/ps read says otherwise."""
        with self.broker.connect() as db:
            row=db.execute('SELECT loaded FROM capacity_inventory_runtime WHERE resource_id=?',(resource_id,)).fetchone()
        if not row or row['loaded'] is None or any(m.get('name')==model for m in json.loads(row['loaded'])):return
        with self.broker.write('mark_loaded') as db:
            row=db.execute('SELECT loaded FROM capacity_inventory_runtime WHERE resource_id=?',(resource_id,)).fetchone()
            if not row or row['loaded'] is None:return
            loaded=json.loads(row['loaded'])
            if any(m.get('name')==model for m in loaded):return
            loaded.append({'name':model,'digest':digest,'size':None,'expires_at':None})
            db.execute('UPDATE capacity_inventory_runtime SET loaded=? WHERE resource_id=?',(json.dumps(loaded),resource_id))

    def _record_inventory(self,resource,version,installed):
        """Authoritative observed inventory; profiles chosen at install time are never assumed."""
        with self.broker.write('record_inventory') as db:self._inventory_in(db,resource,version,installed)

    def _inventory_in(self,db,resource,version,installed):
        """Write the observed inventory inside the caller's write transaction (0.4.6)."""
        runtime=json.dumps(version,sort_keys=True)
        prior=db.execute('SELECT * FROM capacity_inventory WHERE resource_id=?',(resource['resource_id'],)).fetchone()
        db.execute('INSERT INTO capacity_inventory VALUES(?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET endpoint=excluded.endpoint,observed_at=excluded.observed_at,runtime_version=excluded.runtime_version,models=excluded.models',
            (resource['resource_id'],resource['endpoint'],time.time(),runtime,json.dumps(installed,sort_keys=True)))
        rid=resource['resource_id']
        if not prior or prior['endpoint']!=resource['endpoint']:
            self.broker._event(db,'infrastructure',None,'inventory.observed',{'resource_id':rid,'models':len(installed)});return
        before=json.loads(prior['models'])
        for model in sorted(set(installed)-set(before)):self.broker._event(db,'infrastructure',None,'inventory.model_added',{'resource_id':rid,'model':model})
        for model in sorted(set(before)-set(installed)):self.broker._event(db,'infrastructure',None,'inventory.model_removed',{'resource_id':rid,'model':model})
        for model in sorted(m for m in set(before)&set(installed) if before[m]!=installed[m]):
            self.broker._event(db,'infrastructure',None,'inventory.digest_changed',{'resource_id':rid,'model':model})
        if prior['runtime_version']!=runtime:self.broker._event(db,'infrastructure',None,'inventory.runtime_changed',{'resource_id':rid})

    def result(self,app,token,request_id,*,claim=True):
        """The request's status envelope. claim=False (0.4.6): the Gateway's own read at the end of a background
        execution, which must not count as the caller collecting it (0.4.4-0.4.5 counted it, so a graceful stop never
        waited for a /v1/submit result and could stop before the caller read it)."""
        # 0.4.1: a status read takes no write lock unless something is actually past its time
        # (0.4.0 ran reconcile_expired, a BEGIN IMMEDIATE, on every status poll).
        # 0.4.4: bounded, and never fatal to the read: the scheduler and the health cycle expire it otherwise.
        if self.broker.has_expired():
            try:self.broker.reconcile_expired(wait=REQUEST_WRITE_WAIT)
            except LedgerBusy:self.count('status_reconcile_skipped')
        self.broker.authenticate(app,token)
        if claim:
            with self._claims_lock:self._last_read[request_id]=time.time()
        # Both halves of the public envelope must come from one SQLite
        # snapshot. Separate reads can pair a queued/unassigned request with
        # a newer invocation's dispatch evidence.
        with self.broker.connect() as db:
            db.execute('BEGIN')
            request=db.execute('SELECT * FROM capacity_requests WHERE request_id=? AND app_id=?',(request_id,app)).fetchone()
            if request is None:raise PermissionError('Request belongs to another namespace or does not exist')
            request=dict(request)
            row=db.execute('SELECT * FROM capacity_invocations WHERE request_id=? AND app_id=?',(request_id,app)).fetchone()
            times=db.execute('SELECT admitted_at,completed_at FROM capacity_request_times WHERE request_id=?',(request_id,)).fetchone()
        # 0.4.4: authorized again after the snapshot, so a revocation that committed before this read always wins
        # (the first check ran in an earlier snapshot; under load a revoked client could read a result in between).
        self.broker.authenticate(app,token)
        result={'contract':CONTRACT,'request':request,'status':row['state'] if row else request['status']}
        # Lifecycle times (0.4.1, additive; Gateway clock, Unix seconds). Each is absent until it happened.
        result['submitted_at']=request['created']
        if times and times['admitted_at'] is not None and request['resource_id']:result['admitted_at']=times['admitted_at']
        if row and row['started_at'] is not None:result['execution_started_at']=row['started_at']
        record=json.loads(row['result']) if row and row['result'] else None
        if record and record.get('finished_at') is not None:result['execution_finished_at']=record['finished_at']
        if times and times['completed_at'] is not None and request['status']=='completed':result['completed_at']=times['completed_at']
        if row and row['state']=='active' and request['status'] in {'expired','cancelled','completed'}:
            result.update(status='cancelled' if request['status']=='cancelled' else 'unknown',error='No durable completed execution result; interrupted execution is unknown, not successful')
            try:
                with self.broker.write('status_settle',wait=REQUEST_WRITE_WAIT) as db:
                    db.execute("UPDATE capacity_invocations SET state=?,updated=? WHERE request_id=? AND state='active'",(result['status'],time.time(),request_id))
            except LedgerBusy:self.count('status_settle_skipped') # the answer above is already final; the row follows on a later read
        if row and row['result']:result['result']=json.loads(row['result'])
        if row and row['error']:result['error']=row['error']
        if row and row['state']!='active':
            # 0.4.6, additive: what the head answered when it refused this request (any attempt), bounded; the full
            # protected body stays in the ledger for the operator (lifecycle upstream-errors).
            # Diagnostic only: an unreadable table never fails the status read.
            try:
                with self.broker.connect() as db:
                    errors=[dict(r) for r in db.execute('SELECT attempt,resource_id,status,phase,failure_class,message,body_truncated,digest,runtime_version,at '
                                                        'FROM capacity_upstream_errors WHERE request_id=? AND app_id=? ORDER BY error_id',(request_id,app))]
            except Exception:errors=[];self.count('upstream_read_failures')
            if errors:result['upstream_errors']=errors
        return result

    def models(self,app,token):
        """Healthy, fresh heads authorized for the caller, and the permitted models qualified there now.

        0.4.6 (G2): `capacity_evidence` (the per-head capacity map) and `qualification_evidence` (one model's test
        document) are separate names. 0.4.5 reused `evidence` for both, so every head after the first one with a
        qualified model received a test document as its capacity map and reported the default parallel limit
        (for example 1, unmeasured_default) where a declared 4 applied. The scheduler was not affected (it reads
        the map itself, placement.View). The reply's shape is exactly 0.2.0's: a suspended model still drops out here;
        GET /v1/fleet reports it as installed, qualified and temporarily unavailable, with its reason (G5).
        """
        policy=self.broker.authenticate(app,token);resources=[];permitted=json.loads(policy['models'])
        with self.broker.connect() as db:
            db.execute('BEGIN') # one snapshot
            now=time.time();ctx=self._context(db,app,now)
            suspended=ctx['suspended'];placement=ctx['placement'];capacity_evidence=ctx['capacity_evidence']
            for row in ctx['rows']:
                if row['health']!='healthy' or not row['last_seen']>now-120:continue
                models=[m for m in json.loads(row['qualified_models']) if m in permitted];details={}
                limits={l['model']:l for l in model_capacity.head_limits(db,row['resource_id'],models,placement,evidence=capacity_evidence)}
                facts=ctx['facts'][row['resource_id']]
                for model in models:
                    # 0.2.0 keys are computed from operation purposes only, unchanged in meaning.
                    # 0.4.1: a purpose counts while its newest test passed (a later failure withdraws only it).
                    proofs=facts.get(model,{}).get('proofs',{})
                    current={p:q for p in OPERATION_PURPOSES if (q:=proofs.get(p)) is not None}
                    qualified=max(current.values(),key=lambda q:q['at']) if current else None
                    if qualified:
                        qualification_evidence=json.loads(qualified['evidence'])
                        same={p:q for p,q in current.items() if q['digest']==qualified['digest'] and q['version']==qualified['version']}
                        details[model]={'capabilities':qualification_evidence.get('metadata',{}).get('capabilities',[]),'digest':qualified['digest'],
                            'qualified_operations':sorted(same),
                            'qualification_purpose':qualification_evidence.get('purpose'),'observed_at':qualified['at']}
                        details[model]['execution_policy']=qualification_evidence.get('execution_policy',{})
                        if 'completion' in same:details[model]['execution_policy']=json.loads(same['completion']['evidence']).get('execution_policy',{})
                        view=facts[model]['view']
                        details[model].update(descriptor=self.descriptor(model,qualification_evidence,qualified),
                            declared_capabilities={'source':'runtime_declared','values':details[model]['capabilities']},
                            qualifications=copy.deepcopy(view['states']),qualified_capabilities=list(view['qualified']),
                            policy={'source':'operator_policy',**details[model]['execution_policy']},
                            availability={'source':'gateway_observation','resource_health':row['health'],'last_seen':row['last_seen'],'fresh':True},
                            application_suitability={'source':'not_assessed_by_gateway','note':'Technical qualification is not task suitability'},
                            # 0.4.4, additive: calls of this model the head takes at once, and why (declared, measured,
                            # serial_family, slots or policy). Further calls wait in the Gateway queue or go to another head.
                            parallel={'limit':limits[model]['limit'],'source':limits[model]['source'],'reason':limits[model]['reason']})
                        # 0.4.0: a suspended model/operation drops out here exactly as an unqualified one does.
                        details[model]['qualified_operations']=[p for p in details[model]['qualified_operations']
                            if not recovery.suspension_reason(suspended,row['resource_id'],model,qualified['digest'],p)]
                        if not details[model]['qualified_operations']:del details[model]
                resources.append({'resource_id':row['resource_id'],'models':[m for m in models if m in details],'model_details':details})
        return {'contract':CONTRACT,'resources':resources}

    @staticmethod
    def descriptor(model,evidence,proof):
        """Runtime-declared model facts at qualification time, through an allowlist only."""
        metadata=evidence.get('metadata') or {};details=metadata.get('details') or {};info=metadata.get('model_info') or {}
        def text(value):return value[:100] if isinstance(value,str) else None
        def number(value):return value if isinstance(value,int) and not isinstance(value,bool) else None
        architecture=text(info.get('general.architecture'))
        version=evidence.get('version') if isinstance(evidence.get('version'),dict) else {}
        return {'source':'runtime_declared','evidence_id':proof['evidence_id'],'observed_at':proof['at'],
            'name':model,'tag':model.split(':',1)[1] if ':' in model else None,'digest':proof['digest'],
            'family':text(details.get('family')),
            'families':[f[:100] for f in details.get('families') or [] if isinstance(f,str)][:16] or None,
            'parameter_size':text(details.get('parameter_size')),'quantization_level':text(details.get('quantization_level')),
            'format':text(details.get('format')),'architecture':architecture,
            'context_length':number(info.get(f'{architecture}.context_length')) if architecture else None,
            'embedding_length':number(info.get(f'{architecture}.embedding_length')) if architecture else None,
            'runtime_version':text(version.get('version')),
            'model_version':None} # the runtime publishes no model version; the digest is the build identity

    def _qualification_view(self,db,resource,model):
        """Newest evidence per purpose, judged against the current operation qualification (read from the ledger)."""
        rows=[(q,json.loads(q['evidence'])) for q in db.execute('SELECT * FROM capacity_qualification_evidence WHERE resource_id=? AND model=? ORDER BY at DESC,rowid DESC LIMIT 200',(resource['resource_id'],model))]
        return _view_from(rows,resource)

    def _inventory(self,db,resource):
        row=db.execute('SELECT * FROM capacity_inventory WHERE resource_id=?',(resource['resource_id'],)).fetchone()
        if not row or row['endpoint']!=resource['endpoint']:return None
        return {'observed_at':row['observed_at'],'fresh':row['observed_at']>time.time()-FRESH_SECONDS,
                'runtime_version':json.loads(row['runtime_version']) if row['runtime_version'] else None,'models':json.loads(row['models'])}

    def availability(self,app,token,model,operation='/api/generate',*,num_ctx=None,capabilities=()):
        """Why a permitted model can or cannot run now, without other clients' private activity."""
        policy=self.broker.authenticate(app,token)
        if operation not in {'/api/generate','/api/chat','/api/embed'}:raise PermissionError('Only inference/embedding execution is exposed')
        capabilities=sorted(set(capabilities))
        if set(capabilities)-set(CAPABILITY_PURPOSES):raise ValueError('Unknown capability')
        if num_ctx is not None and (isinstance(num_ctx,bool) or not isinstance(num_ctx,int) or not 512<=num_ctx<=65536):raise ValueError('Context allocation is outside policy')
        answer={'contract':CONTRACT,'model':model,'operation':operation,'num_ctx':num_ctx,'required_capabilities':capabilities}
        limits=self.broker.client_limits(app)
        with self.broker.connect() as db:
            db.execute('BEGIN')
            ctx=self._context(db,app,time.time())
        status,reasons=self._status_in(ctx,json.loads(policy['models']),limits,model,operation,num_ctx,capabilities)
        return {**answer,'status':status,'resources':reasons}

    # ------------------------------------------------------------ 0.4.6 one read snapshot for discovery (G1)

    def _context(self,db,app,now):
        """Everything availability needs for the heads authorized for `app`, read once in the caller's snapshot.

        0.4.5 re-read every one of these per head, per model and per operation (a /v1/policy of 36 models: 72 full
        availability calculations). Qualification facts come from the per-head cache (_facts); everything else
        (health, inventory, suspensions, occupancy, authorization) is read live here, on every call.
        """
        rows=[r for r in db.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference'").fetchall()
              if app in json.loads(r['applications']) and 'inference' in json.loads(r['permitted_uses'])]
        occupancy={};model_occupancy={}
        for r in db.execute("SELECT resource_id,model,count(*) n FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND resource_id IS NOT NULL GROUP BY resource_id,model"):
            occupancy[r['resource_id']]=occupancy.get(r['resource_id'],0)+r['n'];model_occupancy[(r['resource_id'],r['model'])]=r['n']
        runtime={r['resource_id']:r['families'] for r in db.execute('SELECT resource_id,families FROM capacity_inventory_runtime')}
        entries={r['resource_id']:self._facts_entry(db,r) for r in rows}
        return {'now':now,'rows':rows,'suspended':recovery.active(db),'placement':recovery.load_policy(db)['placement'],
                'capacity_evidence':model_capacity.load(db),
                'inventory':{r['resource_id']:self._inventory(db,r) for r in rows},
                'facts':{rid:entry[1] for rid,entry in entries.items()},'facts_keys':{rid:entry[0] for rid,entry in entries.items()},
                'slots':{r[0]:r[1] for r in db.execute('SELECT resource_id,slots FROM capacity_resource_capacity')},
                'families':{rid:json.loads(v) if v else {} for rid,v in runtime.items()},
                'occupancy':occupancy,'model_occupancy':model_occupancy,'limits':{}}

    def _model_limit(self,ctx,row,model):
        """The effective per-model limit on this head (model_capacity.effective), as placement applies it."""
        key=(row['resource_id'],model)
        if key not in ctx['limits']:
            inventory=ctx['inventory'].get(row['resource_id']) or {}
            rows=ctx['capacity_evidence'].get(key,{})
            ctx['limits'][key]=model_capacity.effective(model,slots=ctx['slots'].get(row['resource_id'],1),
                families=ctx['families'].get(row['resource_id'],{}).get(model) or [],runtime_version=model_capacity.live_version(inventory.get('runtime_version')),
                digest=(inventory.get('models') or {}).get(model),declared=rows.get('declared'),measured=rows.get('measured'),policy=ctx['placement'])
        return ctx['limits'][key]

    def _reason_in(self,ctx,row,model,purpose,num_ctx,capabilities):
        """Why this model can or cannot run on this head now (one of the availability reasons)."""
        now=ctx['now']
        if row['health'] in ('quarantined','maintenance','offline','retired'):return 'resource_unhealthy'
        if row['health']=='unqualified':return 'not_qualified' # new or moved head: qualification also observes inventory
        # Degraded/unqualified heads continue to the specific cause below.
        if row['health']=='healthy' and row['last_seen']<=now-FRESH_SECONDS:return 'resource_stale'
        inventory=ctx['inventory'].get(row['resource_id'])
        if not inventory or not inventory['fresh']:return 'inventory_stale'
        if model not in inventory['models']:return 'not_installed'
        facts=ctx['facts'][row['resource_id']].get(model)
        states=(facts['view'] if facts else _view_from([],row))['states']
        proof=facts['proofs'].get(purpose) if facts else None
        if not proof:
            other='embedding' if purpose=='completion' else 'completion'
            return 'qualified_other_operation' if states[other]['state']=='passed' else 'not_qualified'
        if proof['digest']!=inventory['models'][model]:return 'digest_changed'
        if json.loads(proof['version'])!=inventory['runtime_version']:return 'runtime_changed'
        if model not in json.loads(row['qualified_models']) or states[purpose]['state']!='passed':return 'not_qualified'
        if row['health']!='healthy':return 'resource_unhealthy'
        limit=(json.loads(proof['evidence']).get('execution_policy') or {}).get('max_context',1024)
        if purpose=='completion' and num_ctx and num_ctx>limit:return 'context_exceeds_qualification'
        if any(states[c]['state']!='passed' for c in capabilities):return 'capability_not_qualified'
        suspended=recovery.suspension_reason(ctx['suspended'],row['resource_id'],model,inventory['models'][model],purpose)
        if suspended:return suspended # model_suspended (every head) or model_suspended_on_resource
        # Occupancy only: who holds the head is never disclosed.
        if ctx['occupancy'].get(row['resource_id'],0)>=ctx['slots'].get(row['resource_id'],1):return 'busy'
        # 0.4.6: also busy when this model's own per-head limit is in use (placement would not admit another call there).
        limit=self._model_limit(ctx,row,model)
        if limit['source']!='slots' and ctx['model_occupancy'].get((row['resource_id'],model),0)>=limit['limit']:return 'busy'
        return 'available'

    def _status_in(self,ctx,permitted,limits,model,operation,num_ctx,capabilities):
        """(status, {resource_id: reason}) for one model and operation, as availability() answers it."""
        if model not in permitted:return 'not_permitted',{}
        if operation not in limits['operations']:return 'operation_not_permitted',{}
        if num_ctx and limits['max_context'] and operation!='/api/embed' and num_ctx>limits['max_context']:return 'context_exceeds_policy',{}
        purpose='embedding' if operation=='/api/embed' else 'completion'
        reasons={row['resource_id']:self._reason_in(ctx,row,model,purpose,num_ctx,capabilities) for row in sorted(ctx['rows'],key=lambda r:r['resource_id'])}
        statuses=set(reasons.values())
        status=('no_authorized_resource' if not reasons else 'available' if 'available' in statuses else 'busy' if 'busy' in statuses
                else statuses.pop() if len(statuses)==1 else 'unavailable')
        return status,reasons

    def policy(self,app,token,*,include_availability=True):
        """The caller's own operator-set policy and per-model availability.

        0.4.6: the per-model availability comes from one read snapshot (_context) instead of two complete
        availability() calls per authorized model. include_availability=False (GET /v1/policy?availability=0)
        returns the operator policy only: no head, inventory or qualification is read at all.
        """
        client=self.broker.authenticate(app,token);models=json.loads(client['models']);limits=self.broker.client_limits(app)
        out={'contract':CONTRACT,'application_id':app,
            'policy':{'source':'operator_policy','enabled':bool(client['enabled']),'models':models,'max_concurrency':client['max_concurrency'],
                      'queue_limit':client['queue_limit'],'priority':client['priority'],'priority_aging_seconds':5,'max_output_tokens':8192,
                      'max_deadline_seconds':3600,'operations':limits['operations'],'max_context':limits['max_context']}}
        if not include_availability:
            return {**out,'models':[{'model':m} for m in models],'availability_included':False}
        with self.broker.connect() as db:
            db.execute('BEGIN')
            ctx=self._context(db,app,time.time())
        out['models']=[{'model':m,'operations':{op:self._status_in(ctx,models,limits,m,op,None,())[0] for op in ('/api/generate','/api/embed')}} for m in models]
        out['availability_included']=True;out['observed_at']=ctx['now']
        return out

    # ------------------------------------------------------------ 0.4.6 application-scoped fleet snapshot (G1, G5)

    FLEET_OPERATIONS=('/api/generate','/api/chat','/api/embed')

    def fleet(self,app,token,*,models=None,operations=None,num_ctx=None,capabilities=()):
        """One application-scoped, timestamped snapshot of what the caller may run (GET /v1/fleet).

        For each model (the requested ones, else every permitted model) three separate states:
          installed  from the heads' observed inventory (/api/tags), with its freshness;
          qualified  from qualification evidence: operations, capabilities (native tools, structured output,
                     structured chat, ...) and qualified context, per head, with the evidence's time;
          available  now: the availability status per operation and the reason per head (suspension, busy,
                     stale, unhealthy, digest or runtime changed ...).
        So an installed model that is suspended is "installed, qualified, temporarily unavailable: model_suspended",
        never "not installed". A model the caller is not permitted gets only status not_permitted: nothing about
        heads, inventory or other applications is disclosed, and occupancy is a count only.

        The same snapshot is served to every caller of this application with the same scope while its inputs are
        unchanged, for at most FLEET_SNAPSHOT_SECONDS (snapshot.valid_until). Its inputs are read on every call; it
        is reused only when they are identical. It is advisory: every real request is admitted and dispatch-checked
        again (authorization, digest, qualification, health), so a snapshot can never authorize a revoked request.
        """
        client=self.broker.authenticate(app,token);permitted=json.loads(client['models']);limits=self.broker.client_limits(app)
        operations=list(operations or ('/api/generate','/api/embed'))
        if set(operations)-set(self.FLEET_OPERATIONS) or len(operations)>3:raise PermissionError('Only inference/embedding execution is exposed')
        capabilities=sorted(set(capabilities or ()))
        if set(capabilities)-set(CAPABILITY_PURPOSES):raise ValueError('Unknown capability')
        if num_ctx is not None and (isinstance(num_ctx,bool) or not isinstance(num_ctx,int) or not 512<=num_ctx<=65536):raise ValueError('Context allocation is outside policy')
        requested=list(dict.fromkeys(models)) if models else list(permitted)
        if len(requested)>256 or any(not isinstance(m,str) or not 0<len(m)<=200 for m in requested):raise ValueError('Up to 256 model names')
        with self.broker.connect() as db:
            db.execute('BEGIN')
            ctx=self._context(db,app,time.time())
        scope={'models':requested,'operations':operations,'num_ctx':num_ctx,'capabilities':capabilities}
        state=self._fleet_inputs(ctx,client,limits)
        snapshot_id='FLEET-'+hashlib.sha256(json.dumps({'app':app,'scope':scope,'state':state},sort_keys=True,default=str).encode()).hexdigest()[:32]
        key=(app,json.dumps(scope,sort_keys=True))
        with self._cache_lock:hit=self._fleet_cache.get(key)
        if hit and hit['snapshot']['snapshot_id']==snapshot_id and ctx['now']<hit['snapshot']['valid_until']:
            self.count('fleet_snapshot_hits');out=copy.deepcopy(hit);out['snapshot'].update(served_from_cache=True,served_at=ctx['now']);return out
        self.count('fleet_snapshot_builds')
        entries=[self._fleet_model(ctx,permitted,limits,m,operations,num_ctx,capabilities) for m in requested]
        out={'contract':CONTRACT,'application_id':app,
             'snapshot':{'snapshot_id':snapshot_id,'generated_at':ctx['now'],'served_at':ctx['now'],'valid_until':ctx['now']+FLEET_SNAPSHOT_SECONDS,
                         'max_age_seconds':FLEET_SNAPSHOT_SECONDS,'scope':'application','served_from_cache':False,
                         'authoritative':False,'note':'Advisory: every request is admitted and checked again at dispatch'},
             'request':scope,'models':entries}
        with self._cache_lock:
            if len(self._fleet_cache)>=256:self._fleet_cache.clear() # bounded: scopes are few in practice
            self._fleet_cache[key]=copy.deepcopy(out)
        return out

    @staticmethod
    def _fleet_inputs(ctx,client,limits):
        """Every input of a fleet snapshot, for its identity: equal inputs give an equal snapshot."""
        now=ctx['now']
        return {'client':{k:client[k] for k in ('enabled','models','max_concurrency','queue_limit','priority')},'limits':limits,
                'heads':[(r['resource_id'],r['endpoint'],r['health'],r['qualified_models'],r['applications'],r['permitted_uses'],
                          r['health']=='healthy' and r['last_seen']>now-FRESH_SECONDS) for r in ctx['rows']],
                'inventory':{rid:(v['fresh'],v['runtime_version'],v['models']) if v else None for rid,v in ctx['inventory'].items()},
                'evidence':ctx['facts_keys'], # (endpoint, evidence rows, highest rowid) per head
                'suspensions':[(s['suspension_id'],s['state']) for s in ctx['suspended']],
                'occupancy':sorted((k[0],k[1],v) for k,v in ctx['model_occupancy'].items()),'slots':ctx['slots'],
                'capacity':sorted((k[0],k[1],src,row['parallel'],row['digest'],row['runtime_version']) for k,v in ctx['capacity_evidence'].items() for src,row in v.items()),
                'placement':ctx['placement']}

    def _fleet_model(self,ctx,permitted,limits,model,operations,num_ctx,capabilities):
        if model not in permitted:return {'model':model,'authorized':False,'status':'not_permitted'}
        heads={};installed_on=[];qualified_on=[];fresh_inventory=True
        for row in sorted(ctx['rows'],key=lambda r:r['resource_id']):
            rid=row['resource_id'];inventory=ctx['inventory'].get(rid)
            fresh=bool(inventory and inventory['fresh'])
            digest=((inventory or {}).get('models') or {}).get(model)
            facts=ctx['facts'][rid].get(model)
            view=facts['view'] if facts else _view_from([],row)
            proofs=facts['proofs'] if facts else {}
            head={'health':row['health'],'fresh':row['health']=='healthy' and row['last_seen']>ctx['now']-FRESH_SECONDS,
                  'inventory':{'installed':digest is not None if inventory else None,'digest':digest,
                               'observed_at':inventory['observed_at'] if inventory else None,'fresh':fresh},
                  'qualification':{'listed':model in json.loads(row['qualified_models']),
                                   'operations':sorted(p for p in OPERATION_PURPOSES if proofs.get(p)),
                                   'states':copy.deepcopy(view['states']),'qualified_capabilities':list(view['qualified']),
                                   'max_context':(json.loads(proofs['completion']['evidence']).get('execution_policy') or {}).get('max_context') if proofs.get('completion') else None,
                                   'digest':(proofs.get('completion') or proofs.get('embedding') or {}).get('digest')},
                  'availability':{op:self._reason_in(ctx,row,model,'embedding' if op=='/api/embed' else 'completion',
                                                     None if op=='/api/embed' else num_ctx,() if op=='/api/embed' else capabilities) for op in operations}}
            if digest is not None:
                limit=self._model_limit(ctx,row,model)
                head['parallel']={'limit':limit['limit'],'source':limit['source'],'reason':limit['reason']}
                head['suspension']=recovery.suspension_reason(ctx['suspended'],rid,model,digest,None)
            heads[rid]=head
            if not fresh:fresh_inventory=False
            if digest is not None:installed_on.append(rid)
            if head['qualification']['operations'] and head['qualification']['listed']:qualified_on.append(rid)
        installed_state='installed' if installed_on else 'unknown' if not fresh_inventory or not heads else 'not_installed'
        qualified_state=('qualified' if qualified_on else 'not_qualified' if installed_on else 'not_applicable')
        available={op:self._status_in(ctx,permitted,limits,model,op,None if op=='/api/embed' else num_ctx,() if op=='/api/embed' else capabilities)[0] for op in operations}
        return {'model':model,'authorized':True,
                'installed':{'state':installed_state,'resources':installed_on,'inventory_fresh':fresh_inventory},
                'qualified':{'state':qualified_state,'resources':qualified_on,
                             'capabilities':sorted({c for rid in qualified_on for c in heads[rid]['qualification']['qualified_capabilities']}),
                             'max_context':max((heads[rid]['qualification']['max_context'] or 0 for rid in qualified_on),default=None) or None},
                'available':{'status':available,'any':any(s in ('available','busy') for s in available.values())},
                'resources':heads}

    def recover_service_startup(self):
        """Only the exclusive service owner calls this after process restart.

        Prompt bodies are intentionally not durable. Never grant an orphaned
        queued request a lease or replay unknown execution. In-flight leases
        remain held until expiry, which holds the head (uncertain execution) until
        its deadline has passed and a probe returns it.
        """
        with self.broker.write('startup_reconcile') as db:
            rows=db.execute("SELECT request_id,app_id FROM capacity_invocations WHERE state='active'").fetchall()
            for row in rows:
                db.execute("UPDATE capacity_invocations SET state='unknown',error='Service restart interrupted execution ownership; no replay',updated=? WHERE request_id=?",(time.time(),row['request_id']))
                db.execute("UPDATE capacity_requests SET status='cancelled' WHERE request_id=? AND status='queued' AND executing=0",(row['request_id'],))
                self.broker._event(db,row['app_id'],row['request_id'],'invocation.unknown',{})
            self.broker._event(db,'infrastructure',None,'service.restart_reconciled',{'interrupted_requests':len(rows)})
        return {'interrupted_requests':len(rows)}

    def usage(self,app,token):
        self.broker.authenticate(app,token)
        with self.broker.connect() as db:
            counts={r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM capacity_requests WHERE app_id=? GROUP BY status',(app,))}
            results=[json.loads(r['result']) for r in db.execute("SELECT result FROM capacity_invocations WHERE app_id=? AND state='completed' AND result IS NOT NULL",(app,))]
        return {'contract':CONTRACT,'application_id':app,'requests':counts,
            'completed_execution_ms':sum(r.get('duration_ms',0) for r in results),
            'completed_output_tokens':sum(r.get('response',{}).get('eval_count',0) or 0 for r in results),
            'scope':'completed results only; unknown execution is not refunded or reported as zero usage'}

    def execute(self,app,token,body,asynchronous=False):
        operation,payload,canonical=self.validate(body)
        # Application operation/context policy is checked before any request exists.
        self.broker.authenticate(app,token);limits=self.broker.client_limits(app)
        if operation not in limits['operations']:raise ClientPolicyError('Operation is not permitted for this application')
        if limits['max_context'] and operation!='/api/embed' and payload.get('options',{}).get('num_ctx',0)>limits['max_context']:
            raise ClientPolicyError('Requested context exceeds this application policy')
        # 0.4.4: the operation is recorded in the submission's own transaction (one write fewer per call).
        request=self.broker.submit(app,token,body['client_key'],body['model'],body['nodes'],float(body['deadline']),
                                   operation='embedding' if operation=='/api/embed' else 'completion')
        return self._execute(app,token,body,request,operation,payload,canonical,asynchronous)

    def qualify(self,resource_id,model,*,operator,purpose='completion',timeout=90,max_context=1024):
        """Privileged host-side maintenance, never a registered application's API.

        Access to this database/command must be confined to the infrastructure
        service administrator. Uses the SAME unique capacity ledger reservation.
        """
        if not operator or purpose not in OPERATION_PURPOSES+CAPABILITY_PURPOSES or not 1<=timeout<=300:raise ValueError('Explicit bounded maintenance policy required')
        if not isinstance(max_context,int) or not 512<=max_context<=65536:raise ValueError('Bounded operator context policy required')
        self.broker.reconcile_expired();rid='QUAL-'+uuid.uuid4().hex;deadline=time.time()+timeout
        with self.broker.write('qualify_lease') as db:
            resource=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(resource_id,)).fetchone()
            if not resource or resource['resource_type']!='shared_inference':raise PermissionError('Only explicitly authorized inference resources may be qualified')
            if resource['health']=='quarantined':raise PermissionError('Quarantined execution must be reconciled before qualification')
            if db.execute("SELECT 1 FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(resource_id,)).fetchone():raise ValueError('Qualification cannot interrupt allocated capacity')
            db.execute('INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid,'maintenance:'+operator,rid,model,json.dumps([resource_id]),'leased',time.time(),deadline,'LEASE-'+uuid.uuid4().hex,resource_id,deadline,1))
            db.execute("UPDATE capacity_resources SET health='maintenance' WHERE resource_id=?",(resource_id,))
        evidence={'resource_id':resource_id,'model':model,'purpose':purpose,'operator':operator,'lease_request_id':rid,
                  'endpoint':resource['endpoint'],'actual_execution':self.actual_transport,
                  'execution_transport':'http' if self.actual_transport else 'injected_test_transport','passed':False}
        evidence['execution_policy']={'max_context':max_context,'max_output_tokens':8192,'allocation':'exclusive physical head'}
        returned=False;dispatched=False;capability=purpose in CAPABILITY_PURPOSES
        def call(path,payload=None):
            remaining=deadline-time.time()
            if remaining<=0:raise ValueError('Qualification deadline expired') # before sending: nothing ran
            return self.transport(resource['endpoint'],path,payload,remaining)
        try:
            evidence['version']=call('/api/version')
            tags=call('/api/tags')['models'];installed=next((m for m in tags if m['name']==model),None)
            self._record_inventory(dict(resource),evidence['version'],{m['name']:m.get('digest') for m in tags})
            if not installed:raise ValueError('Qualification model not installed')
            evidence['digest']=installed.get('digest');evidence['metadata']=call('/api/show',{'model':model})
            if not isinstance(evidence['digest'],str) or not evidence['digest']:raise ValueError('Qualification requires an observed model digest')
            if DECLARED[purpose] not in evidence['metadata'].get('capabilities',[]):raise ValueError('Metadata does not support qualification operation')
            if capability:
                # Capability evidence extends a current completion qualification; it never replaces one.
                with self.broker.connect() as db:base=self._proof(db,dict(resource),model,'completion')
                if base and not (base['digest']==evidence['digest'] and json.loads(base['version'])==evidence['version']):base=None
                if not base:raise PermissionError('Capability qualification requires a current completion qualification of this digest and runtime')
                evidence['execution_policy']=json.loads(base['evidence'])['execution_policy'];evidence['base_evidence_id']=base['evidence_id']
            began=time.time()
            if purpose=='completion':
                dispatched=True
                response=call('/api/generate',{'model':model,'prompt':COMPLETION_TEST_PROMPT,'stream':False,'think':False,'keep_alive':'2m',
                    'options':{'num_ctx':max_context,'num_predict':COMPLETION_TEST_TOKENS,'temperature':0,'seed':42}})
                returned=True
                evidence['passed'],evidence['measurement']=completion_test(response)
                evidence['test']={'prompt':COMPLETION_TEST_PROMPT,'num_predict':COMPLETION_TEST_TOKENS,'rule':'output produced: done and eval_count > 0 (stop or length; reasoning counts)'}
            elif purpose=='embedding':
                dispatched=True
                response=call('/api/embed',{'model':model,'input':['Synthetic qualification fixture.'],'keep_alive':'2m'})
                returned=True
                evidence['passed']=bool(response.get('embeddings') and response['embeddings'][0])
                if evidence['passed'] and isinstance(response['embeddings'][0],list):evidence['embedding_dimensions']=len(response['embeddings'][0])
            else:
                operation,payload,check,evidence['fixture']=qualification_fixtures.build(purpose,model,evidence['execution_policy']['max_context'])
                dispatched=True;response=call(operation,payload);returned=True
                self.validate_response(operation,response,model)
                evidence['passed'],evidence['measurement']=check(response)
            if not capability:self.validate_response('/api/embed' if purpose=='embedding' else '/api/generate',response,model)
            returned=True;evidence.update(response=response,duration_ms=(time.time()-began)*1000)
            if time.time()>deadline:raise TimeoutError('Late qualification cannot publish capacity')
        except Exception as exc:
            evidence['error']=str(exc)[:400];evidence['passed']=False;failure=exc
            if isinstance(exc,urllib.error.HTTPError):
                # 0.4.6 (G4): the head's own explanation of a refused test (bounded; operator-only evidence). Never raises.
                try:
                    text,truncated,_=http_error_body(exc)
                    evidence['upstream_error']={'status':exc.code,'message':redact(upstream_message(text))[:UPSTREAM_MESSAGE_LIMIT],'truncated':truncated}
                except Exception:evidence['upstream_error']={'status':getattr(exc,'code',None),'capture_error':True}
        else:failure=None
        finally:
            # 0.4.1: a test that was never sent (wrong purpose for the model, not installed, no digest,
            # no current completion base, deadline) is a refusal: recorded, but it changes nothing.
            refused=not dispatched
            if refused:evidence['refused']=True
            with self.broker.write('qualify_record') as db:
                lease=db.execute('SELECT status FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()
                valid=lease['status']=='leased' and time.time()<=deadline
                evidence['passed']=bool(evidence['passed'] and valid)
                evidence_id='EVIDENCE-'+uuid.uuid4().hex;evidence['evidence_id']=evidence_id
                db.execute('INSERT INTO capacity_qualification_evidence VALUES(?,?,?,?,?,?,?,?)',
                    (evidence_id,resource_id,model,evidence.get('digest'),json.dumps(evidence.get('version')),int(evidence['passed']),json.dumps(evidence),time.time()))
                # Only an unanswered dispatched call leaves physical execution uncertain (0.4.0
                # classes): an HTTP error reply is an answer, and nothing ran before dispatch.
                uncertain=bool(dispatched and not returned and failure is not None and recovery.classify(failure,'dispatch')==recovery.UNCERTAIN)
                if capability or refused:
                    # A failed capability test never withdraws completion capacity; a refusal withdraws nothing.
                    db.execute('UPDATE capacity_resources SET health=? WHERE resource_id=?',(resource['health'],resource_id))
                    db.execute('UPDATE capacity_requests SET status=?,executing=0 WHERE request_id=?',('expired' if uncertain else 'completed' if returned else 'cancelled',rid))
                else:
                    # The test ran: only the purpose it tested changes. The model stays listed while any
                    # operation purpose's newest test passed on this digest and runtime.
                    models=[m for m in json.loads(resource['qualified_models']) if m!=model]
                    newest=self._newest_operation_proofs(db,dict(resource))
                    if any((q:=newest.get((model,p))) is not None and q['passed'] and q['digest']==evidence.get('digest') and json.loads(q['version'])==evidence.get('version')
                           for p in OPERATION_PURPOSES):models.append(model)
                    # Head health reflects the head, not one model: healthy while it serves any qualified model.
                    db.execute('UPDATE capacity_resources SET qualified_models=?,health=?,last_seen=? WHERE resource_id=?',
                        (json.dumps(models),'healthy' if models else 'degraded',time.time(),resource_id))
                    db.execute('UPDATE capacity_requests SET status=?,executing=0 WHERE request_id=?',('completed' if evidence['passed'] else 'expired' if uncertain else 'cancelled',rid))
                if uncertain:
                    recovery.hold_resource(self.broker,db,resource_id,request_id=rid,model=model,operation=DECLARED[purpose] if purpose in OPERATION_PURPOSES else 'completion',
                                           hold_until=deadline,source='qualification')
                self.broker._event(db,'infrastructure',rid,'qualification.completed',{'resource_id':resource_id,'passed':evidence['passed'],'purpose':purpose,
                                                                                     **({'refused':True} if refused else {})})
            self.scheduler.wake(health=True)
        return evidence

    MEASURE_PROMPT='Count from 1 to 40, separated by spaces.'
    MEASURE_TOKENS=48

    def measure_parallel(self,resource_id,model,*,parallel,operator,num_ctx=None,timeout=180):
        """0.4.4: measure how many calls of this model the head really runs at once (qualify --parallel N).

        Privileged host-side maintenance, like qualify: the head must have no allocation, and it is in
        maintenance (no client traffic) while measured. One warm-up call loads the model, then `parallel`
        short generations are sent at once. Overlap is judged from each reply's own Ollama timings
        (model_capacity.judge_overlap), never from hub-side wall time. The result is stored as source
        'measured' with the head's runtime version, the model's digest and num_ctx; it is used only
        while all three still match (and min() with a declared value).
        """
        if not operator or isinstance(parallel,bool) or not isinstance(parallel,int) or not 2<=parallel<=64 or not 10<=timeout<=900:
            raise ValueError('Explicit operator, parallel 2-64 and a bounded timeout required')
        self.broker.reconcile_expired();rid='MEASURE-'+uuid.uuid4().hex;deadline=time.time()+timeout
        with self.broker.write('measure_lease') as db:
            resource=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(resource_id,)).fetchone()
            if not resource or resource['resource_type']!='shared_inference':raise PermissionError('Only explicitly authorized inference resources may be measured')
            if resource['health'] in ('quarantined','maintenance'):raise PermissionError('A held or maintenance head cannot be measured')
            if db.execute("SELECT 1 FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(resource_id,)).fetchone():raise ValueError('Measurement cannot interrupt allocated capacity')
            db.execute('INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid,'maintenance:'+operator,rid,model,json.dumps([resource_id]),'leased',time.time(),deadline,'LEASE-'+uuid.uuid4().hex,resource_id,deadline,1))
            db.execute("UPDATE capacity_resources SET health='maintenance' WHERE resource_id=?",(resource_id,))
        resource=dict(resource);out={'resource_id':resource_id,'model':model,'requested_parallel':parallel,'operator':operator,'measurement_id':rid,'stored':False}
        dispatched=False;answered=True
        def call(path,payload=None):
            remaining=deadline-time.time()
            if remaining<=0:raise TimeoutError('Measurement deadline expired')
            return self.transport(resource['endpoint'],path,payload,remaining)
        try:
            version=call('/api/version');tags=call('/api/tags').get('models',[])
            installed=next((m for m in tags if m.get('name')==model),None)
            if not installed or not installed.get('digest'):raise ValueError('Model not installed on this head')
            with self.broker.connect() as db:qualified=self._proof(db,resource,model,'completion')
            if not qualified or qualified['digest']!=installed['digest'] or json.loads(qualified['version'])!=version:
                raise PermissionError('Measure only a model with a current completion qualification of this digest and runtime')
            limit=json.loads(qualified['evidence']).get('execution_policy',{}).get('max_context',1024)
            num_ctx=num_ctx or limit
            if num_ctx>limit:raise ValueError('num_ctx exceeds the qualified context')
            def payload(seed):return {'model':model,'prompt':self.MEASURE_PROMPT,'stream':False,'think':False,'keep_alive':'2m',
                                      'options':{'num_ctx':num_ctx,'num_predict':self.MEASURE_TOKENS,'temperature':0,'seed':seed}}
            dispatched=True;answered=False
            call('/api/generate',payload(1)) # warm-up: loading is not part of the measurement
            replies=[None]*parallel;errors=[]
            def one(i):
                try:replies[i]=call('/api/generate',payload(100+i))
                except Exception as exc:errors.append(exc)
            threads=[threading.Thread(target=one,args=(i,),daemon=True) for i in range(parallel)]
            for t in threads:t.start()
            for t in threads:t.join(max(1,deadline-time.time()))
            if errors or any(r is None for r in replies):raise (errors[0] if errors else TimeoutError('A measurement call did not return'))
            answered=True
            for r in replies:
                passed,_=completion_test(r)
                if not passed or r.get('model')!=model:raise ValueError('A measurement call did not produce output')
            measured,intervals=model_capacity.judge_overlap(replies)
            out.update(measured_parallel=measured,runtime_version=model_capacity.live_version(version),digest=installed['digest'],num_ctx=num_ctx,
                       calls=[{k:r.get(k) for k in ('created_at','total_duration','load_duration','prompt_eval_duration','eval_duration','eval_count')} for r in replies],
                       compute_intervals=[[round(a,3),round(b,3)] for a,b in intervals])
        except Exception as exc:
            out['error']=str(exc)[:300];failure=exc
        else:failure=None
        uncertain=bool(dispatched and not answered and failure is not None and recovery.classify(failure,'dispatch')==recovery.UNCERTAIN)
        with self.broker.write('measure_record') as db:
            lease=db.execute('SELECT status FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()
            valid=failure is None and lease['status']=='leased' and time.time()<=deadline
            db.execute('UPDATE capacity_resources SET health=? WHERE resource_id=?',(resource['health'],resource_id))
            db.execute('UPDATE capacity_requests SET status=?,executing=0 WHERE request_id=?',('expired' if uncertain else 'completed' if valid else 'cancelled',rid))
            if uncertain:
                recovery.hold_resource(self.broker,db,resource_id,request_id=rid,model=model,operation='completion',hold_until=deadline,source='measurement')
            if valid:
                detail={k:out[k] for k in ('requested_parallel','calls','compute_intervals','operator')}
                db.execute("INSERT INTO capacity_model_capacity VALUES(?,?,'measured',?,?,?,?,?,?,?) ON CONFLICT(resource_id,model,source) DO UPDATE SET "
                           "parallel=excluded.parallel,digest=excluded.digest,runtime_version=excluded.runtime_version,num_ctx=excluded.num_ctx,"
                           "evidence=excluded.evidence,detail=excluded.detail,recorded_at=excluded.recorded_at",
                           (resource_id,model,out['measured_parallel'],out['digest'],out['runtime_version'],out['num_ctx'],rid,json.dumps(detail),time.time()))
                out['stored']=True
            self.broker._event(db,'infrastructure',rid,'model_capacity.measured',{'resource_id':resource_id,'model':model,'requested':parallel,
                                                                           'measured':out.get('measured_parallel'),'stored':out['stored']})
        self.scheduler.wake(health=True)
        return out

    @staticmethod
    def _provenance(request,operation,payload,expected_digest,required,qualified,version,view,response,*,began,leased_at,started,finished,
                    attempt=1,attempts=None,ledger_wait=None):
        """Result-level evidence for reproducible comparison; no prompt or output content."""
        # 0.4.6: a chat request with a format also names structured_chat (the qualified shape for it); additive.
        requested=sorted(({'chat'} if operation=='/api/chat' else set())|({'tools'} if payload.get('tools') else set())|
            ({'structured_output'} if payload.get('format') else set())|({'vision'} if payload.get('images') else set())|
            ({'structured_chat'} if operation=='/api/chat' and payload.get('format') else set()))
        counters={k:response.get(k) if isinstance(response.get(k),int) and not isinstance(response.get(k),bool) else None
                  for k in ('prompt_eval_count','eval_count','total_duration','load_duration','prompt_eval_duration','eval_duration')}
        def ms(ns):return ns/1e6 if ns is not None else None
        return {'request_id':request['request_id'],'client_key':request['client_key'],'operation':operation,
            'requested_model':request['model'],'expected_digest':expected_digest,
            'executed_model':response.get('model',request['model']),'executed_digest':qualified['digest'],
            'resource_id':request['resource_id'],'lease_id':request['lease_id'],
            'runtime_version':version.get('version') if isinstance(version,dict) else None,
            'qualification_evidence_id':qualified['evidence_id'],
            'applied_options':dict(payload.get('options',{})) if operation!='/api/embed' else {},
            'dispatch_controls':{'keep_alive':payload.get('keep_alive'),'stream':payload.get('stream'),'think':payload.get('think')},
            'required_capabilities':required,'capabilities_requested':requested,
            'capabilities_verified':[c for c in requested if view['states'].get(c,{}).get('state')=='passed'],
            'timing_ms':{'admission_wait':(leased_at-began)*1000,'dispatch_checks':(started-leased_at)*1000,
                         'gateway_queue':(started-began)*1000,'model_execution':(finished-started)*1000,
                         # 0.4.4, additive: where the time went. ledger_wait is inside admission_wait/dispatch_checks;
                         # runtime_* are Ollama's own durations (load includes any wait for a busy runner);
                         # transfer_and_other = model_execution minus Ollama's total_duration.
                         'ledger_wait':ledger_wait*1000 if ledger_wait is not None else None,
                         'runtime_load_or_runner_wait':ms(counters['load_duration']),'runtime_prompt_eval':ms(counters['prompt_eval_duration']),
                         'runtime_generation':ms(counters['eval_duration']),'runtime_total':ms(counters['total_duration']),
                         'transfer_and_other':(finished-started)*1000-ms(counters['total_duration']) if counters['total_duration'] is not None else None},
            'attempt':attempt,'attempts':attempts or [{'attempt':attempt,'resource_id':request['resource_id'],'failure_class':None}],
            'model_substitution':None, # 0.4.4: the Gateway never runs another model than the one requested
            'runtime_counters':counters,'unavailable_counters':sorted(k for k,v in counters.items() if v is None),
            'scope':'Gateway-measured: model_execution is the upstream call only and excludes application tool execution and workflow time; durations from the runtime are nanoseconds; no hardware utilization is measured'}

    @staticmethod
    def validate(body):
        allowed={'contract','client_key','model','nodes','deadline','operation','payload','expected_digest','required_capabilities'}
        if set(body)-allowed or body.get('contract')!=CONTRACT:raise ValueError('Unsupported inference contract or request field')
        required=body.get('required_capabilities')
        if required is not None and (not isinstance(required,list) or not all(isinstance(c,str) for c in required) or set(required)-set(CAPABILITY_PURPOSES)):
            raise ValueError('required_capabilities must list qualified capability purposes')
        operation=body['operation'];payload=dict(body['payload'])
        if operation not in {'/api/generate','/api/chat','/api/embed'}:raise PermissionError('Only inference/embedding execution is exposed')
        fields={'prompt','messages','options','format','tools','think','input','truncate','images'}
        if set(payload)-fields:raise ValueError('Client model, streaming, residency and unload controls are forbidden')
        if operation=='/api/generate' and not str(payload.get('prompt','')).strip():raise ValueError('Nonempty generation prompt required')
        if operation=='/api/chat' and not payload.get('messages'):raise ValueError('Chat messages required')
        if operation=='/api/embed' and not payload.get('input'):raise ValueError('Embedding input required')
        if 'tools' in payload:
            # 0.4.6: Ollama's tool form, checked before any request exists: a malformed tool is the client's fault (400),
            # never something the head is asked to judge.
            tools=payload['tools']
            if not isinstance(tools,list) or len(tools)>64:raise ValueError('tools must be a list of up to 64 tool definitions')
            for tool in tools:
                function=tool.get('function') if isinstance(tool,dict) else None
                if (not isinstance(tool,dict) or tool.get('type','function')!='function' or not isinstance(function,dict)
                        or not isinstance(function.get('name'),str) or not 0<len(function['name'])<=200
                        or not isinstance(function.get('description',''),str)
                        or not isinstance(function.get('parameters',{}),dict)):
                    raise ValueError('Each tool must be {"type": "function", "function": {"name": str, "description": str, "parameters": object}}')
        if 'images' in payload and (not isinstance(payload['images'],list) or not all(isinstance(i,str) for i in payload['images'])):
            raise ValueError('images must be a list of base64 strings')
        options=payload.setdefault('options',{}) if operation!='/api/embed' else {}
        if not isinstance(options,dict):raise ValueError('Inference options must be an object')
        if set(options)-{'num_predict','num_ctx','temperature','seed','top_p','top_k','repeat_penalty','stop'}:raise ValueError('Unsupported inference option')
        if operation!='/api/embed':
            options.setdefault('num_predict',600)
            if isinstance(options['num_predict'],bool) or not isinstance(options['num_predict'],int) or not 1<=options['num_predict']<=8192:raise ValueError('A bounded output-token allocation is required')
            if 'num_ctx' in options and (not isinstance(options['num_ctx'],int) or not 512<=options['num_ctx']<=65536):raise ValueError('Context allocation is outside policy')
            # 0.4.6: Ollama answers a wrongly typed option with HTTP 500 (measured on 0.40.16: 'option "temperature" must be
            # of type float32'), which classifies as model_on_head and suspends a healthy model on that head (two such
            # requests on two heads: everywhere). Such a request is the client's fault: refused here (400), never sent.
            number=lambda v:isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
            # A whole-number float (seed 7.0, top_k 40.0) is accepted, as Capstone's settings and Ollama accept it.
            integer=lambda v:number(v) and int(v)==v
            for key,check in (('temperature',number),('top_p',number),('repeat_penalty',number),('seed',integer),('top_k',integer)):
                if key in options and not check(options[key]):raise ValueError('Inference option %s has the wrong type'%key)
            if 'stop' in options and (not isinstance(options['stop'],list) or len(options['stop'])>16 or not all(isinstance(s,str) and len(s)<=200 for s in options['stop'])):
                raise ValueError('Inference option stop must list up to 16 strings')
        identity={'operation':operation,'payload':payload,'expected_digest':body.get('expected_digest')}
        # Omitted in 0.2.0-style requests, so their idempotency hashes are unchanged.
        if required:identity['required_capabilities']=sorted(set(required))
        canonical=json.dumps(identity,sort_keys=True)
        if len(canonical.encode())>1024*1024:raise ValueError('Inference input exceeds the bounded request size')
        return operation,payload,canonical

    def _execute(self,app,token,body,request,operation,payload,canonical,asynchronous):
        rid=request['request_id'];digest=hashlib.sha256(canonical.encode()).hexdigest();owner=uuid.uuid4().hex
        rejected=False
        # Background budget: the request row exists now, and a queued request without its invocation row
        # must not be left behind by a refused write (the client's retry with the same key finds either).
        with self.broker.write('invocation',wait=BACKGROUND_WRITE_WAIT) as db:
            old=db.execute('SELECT * FROM capacity_invocations WHERE request_id=?',(rid,)).fetchone()
            if old:
                if old['payload_hash']!=digest:raise ValueError('Idempotency key reused with different inference content')
            else:
                live=db.execute('SELECT status,deadline,lease_id FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()
                if (live['status']=='cancelled' and live['lease_id'] is None and live['deadline']>time.time() and
                        db.execute("SELECT 1 FROM capacity_events WHERE request_id=? AND event_type='overload.rejected'",(rid,)).fetchone()):
                    # 0.4.1: the same client_key retried after a 503 overload refusal. It never ran or
                    # held a lease, so it queues again (same request ID and deadline).
                    db.execute("UPDATE capacity_requests SET status='queued' WHERE request_id=?",(rid,))
                    self.broker._event(db,app,rid,'request.resubmitted',{'after':'overload.rejected'})
                    request=dict(request,status='queued')
                if db.execute("SELECT count(*) FROM capacity_invocations WHERE state='active'").fetchone()[0]>=self.max_pending:
                    # Commit rejection so an unclaimed request cannot later
                    # acquire a lease with no execution worker to release it.
                    db.execute("UPDATE capacity_requests SET status='cancelled' WHERE request_id=? AND executing=0",(rid,))
                    self.broker._event(db,app,rid,'overload.rejected',{})
                    rejected=True
                else:db.execute('INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,result,error,updated) VALUES(?,?,?,?,?,?,?,?)',(rid,app,digest,owner,'active',None,None,time.time()))
        if rejected:
            self.count('pending_budget_503')
            raise GatewayOverload('Gateway pending execution budget exhausted; retry with the same client_key',retry_after=2)
        if old:return self.result(app,token,rid)
        self.scheduler.wake()
        if asynchronous:
            threading.Thread(target=self._run,args=(app,token,request,owner,operation,payload,body.get('expected_digest'),tuple(body.get('required_capabilities') or ())),daemon=True,name='admitted-inference').start()
            return self.result(app,token,rid)
        return self._run(app,token,request,owner,operation,payload,body.get('expected_digest'),tuple(body.get('required_capabilities') or ()))

    def _run(self,app,token,request,owner,operation,payload,expected_digest=None,required=()):
        """Execute one admitted request; on a model-on-head fault or an unreachable head, re-place it once.

        Nothing ran in those two classes, so the same request (same ID, client key and
        deadline) may run on another head. Uncertain execution is never replayed.
        """
        rid=request['request_id'];required=sorted(set(required));original=copy.deepcopy(payload);began=time.time();attempt=0
        ledger_wait_seconds(reset=True) # 0.4.4: this worker thread's ledger waits go into the result's timing
        while True:
            attempt+=1
            request=self._attempt(app,token,request,owner,operation,copy.deepcopy(original),expected_digest,required,began,attempt)
            if request is None:break
        try:return self.result(app,token,rid,claim=threading.current_thread().name!='admitted-inference')
        except PermissionError:
            # A revoked client's background worker has no caller to answer.
            if threading.current_thread().name=='admitted-inference':return None
            raise

    def _dynamic_nodes(self,app,token,model,purpose,expected_digest,required,context):
        """Heads a dynamic request may use now (a placement hint: placement and dispatch check again).

        0.4.6: waiting requests with the same application, model and shape share one computation per scheduler
        generation (0.4.5: every waiting request rebuilt the whole /v1/models view after every health cycle).
        """
        self.broker.authenticate(app,token)
        key=(app,model,purpose,expected_digest,tuple(required),context)
        generation=self.scheduler.generation;now=time.monotonic()
        with self._cache_lock:
            hit=getattr(self,'_nodes_cache',{}).get(key)
            if hit and hit[0]==generation and now-hit[1]<5:return list(hit[2])
        eligible=self._dynamic_nodes_now(app,token,model,purpose,expected_digest,required,context)
        with self._cache_lock:
            cache=self.__dict__.setdefault('_nodes_cache',{})
            if len(cache)>=1024:cache.clear()
            cache[key]=(generation,now,tuple(eligible))
        return eligible

    def _dynamic_nodes_now(self,app,token,model,purpose,expected_digest,required,context):
        eligible=[]
        for resource in self.models(app,token)['resources']:
            detail=resource['model_details'].get(model,{})
            if (purpose in detail.get('qualified_operations',[]) and
                (not expected_digest or detail.get('digest')==expected_digest) and
                all(detail.get('qualifications',{}).get(c,{}).get('state')=='passed' for c in required) and
                (purpose=='embedding' or context<=detail.get('execution_policy',{}).get('max_context',1024))):eligible.append(resource['resource_id'])
        return eligible

    def _attempt(self,app,token,request,owner,operation,payload,expected_digest,required,began,attempt):
        """One placement and dispatch. Returns the re-queued request to try again, else None."""
        rid=request['request_id'];purpose='embedding' if operation=='/api/embed' else 'completion'
        stop=threading.Event();phase='admission';installed=None;live=False;upstream_operation=None
        try:
            request=self._wait_for_lease(app,token,request,purpose,expected_digest,required,payload)
            self.broker.executing(app,token,rid);phase='checks';leased_at=time.time()
            with self._live_lock:self._live.add(rid);live=True
            threading.Thread(target=renew_until_stopped,args=(self.broker,app,token,rid,stop),
                             kwargs={'log':self.emit,'count':self.count},daemon=True,name='gateway-lease').start()
            with self.broker.connect() as db:resource=dict(db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(request['resource_id'],)).fetchone())
            budget=max(.001,request['deadline']-time.time())
            upstream_operation='/api/tags'
            models=self.transport(resource['endpoint'],'/api/tags',None,min(5,budget)).get('models',[])
            installed=next((m for m in models if m['name']==request['model']),None)
            if not installed:raise ValueError('Qualified model is no longer installed')
            if expected_digest and installed.get('digest')!=expected_digest:raise ClientPolicyError('Model digest changed from the pinned expected_digest')
            upstream_operation='/api/show'
            metadata=self.transport(resource['endpoint'],'/api/show',{'model':request['model']},min(5,budget))
            capability='embedding' if operation=='/api/embed' else 'completion'
            if capability not in metadata.get('capabilities',[]):raise ValueError('Model capability does not match operation')
            with self.broker.connect() as db:qualified=self._proof(db,resource,request['model'],capability)
            if not qualified:raise ClientPolicyError('Model lacks operation-specific infrastructure qualification')
            upstream_operation='/api/version'
            version=self.transport(resource['endpoint'],'/api/version',None,min(5,budget))
            if qualified['digest']!=installed.get('digest') or json.loads(qualified['version'])!=version:
                raise PermissionError('Model digest or inference service version changed; requalification required')
            with self.broker.connect() as db:view=self._qualification_view(db,resource,request['model'])
            if any(view['states'][c]['state']!='passed' for c in required):raise ClientPolicyError('Required capability is not qualified for this model on this resource')
            if operation!='/api/embed':
                context_limit=json.loads(qualified['evidence']).get('execution_policy',{}).get('max_context',1024)
                app_limit=self.broker.client_limits(app)['max_context']
                options=payload.setdefault('options',{});options.setdefault('num_ctx',min(context_limit,app_limit) if app_limit else context_limit)
                if options['num_ctx']>context_limit:raise ClientPolicyError('Requested context exceeds operator-qualified allocation')
            payload['model']=request['model'];payload['keep_alive']='2m'
            if operation!='/api/embed':payload['stream']=False
            # 0.4.1: state leased is the test, not expires: this process owns the lease (see _expire).
            current_lease=self.broker.get(app,token,rid)
            if current_lease['status']!='leased' or current_lease['deadline']<=time.time():raise AllocationEnded('Allocation ended before model dispatch')
            policy=self.broker.authenticate(app,token)
            if request['model'] not in json.loads(policy['models']):raise ClientPolicyError('Model authorization withdrawn before dispatch')
            with self.broker.connect() as db:current=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(resource['resource_id'],)).fetchone()
            if current['health']!='healthy' or app not in json.loads(current['applications']) or 'inference' not in json.loads(current['permitted_uses']):raise PermissionError('Resource admission withdrawn before dispatch')
            execution_started=time.time()
            with self.broker.write('started') as db:db.execute('UPDATE capacity_invocations SET started_at=? WHERE request_id=?',(execution_started,rid))
            phase='dispatch';upstream_operation=operation
            response=self.transport(resource['endpoint'],operation,payload,max(.001,request['deadline']-time.time()))
            phase='answered'
            try:self.validate_response(operation,response,request['model'])
            except ValueError as exc:raise InvalidReply(str(exc)) from None
            current_lease=self.broker.get(app,token,rid)
            if current_lease['status']!='leased' or current_lease['deadline']<=time.time():raise TimeoutError('Late/cancelled inference result rejected')
            if response.get('model') and response['model']!=request['model']:raise InvalidReply('Upstream returned a different model')
            response.pop('thinking',None)
            if isinstance(response.get('message'),dict):response['message'].pop('thinking',None)
            finished=time.time()
            record={'response':response,'resource_id':resource['resource_id'],'model':request['model'],
                    'model_digest':installed.get('digest'),
                    'queued_at':began,'started_at':execution_started,'finished_at':finished,
                    'queue_ms':(execution_started-began)*1000,'duration_ms':(finished-execution_started)*1000,
                    'lease_id':request['lease_id'],'application_id':app}
            with self.broker.connect() as db:
                attempts=[{'attempt':r['attempt'],'resource_id':r['resource_id'],'failure_class':r['failure_class']}
                          for r in db.execute('SELECT attempt,resource_id,failure_class FROM capacity_request_attempts WHERE request_id=? ORDER BY attempt',(rid,))]
            record['provenance']=self._provenance(request,operation,payload,expected_digest,required,qualified,version,view,response,
                began=began,leased_at=leased_at,started=execution_started,finished=finished,
                attempt=attempt,attempts=attempts+[{'attempt':attempt,'resource_id':resource['resource_id'],'failure_class':None}],
                ledger_wait=ledger_wait_seconds())
            # Commit release and the private result together. Cancellation or
            # expiry racing the response must win over result publication.
            def complete(db):
                now=time.time()
                current=db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()
                # Still leased = nobody expired or cancelled it, so this process owns the execution and
                # its answer is final. 0.4.0 also required expires>now: one late renewal lost the result.
                if current['status']!='leased' or current['deadline']<=now:raise TimeoutError('Allocation ended before durable result acceptance')
                if current['expires']<=now:self.broker._event(db,app,rid,'lease.extended',{'resource_id':resource['resource_id'],'reason':'renewal_late'})
                db.execute("UPDATE capacity_requests SET status='completed',executing=0 WHERE request_id=?",(rid,))
                db.execute("UPDATE capacity_invocations SET state='completed',result=?,updated=? WHERE request_id=? AND execution_owner=?",(json.dumps(record),now,rid,owner))
                db.execute('INSERT INTO capacity_request_times(request_id,completed_at) VALUES(?,?) ON CONFLICT(request_id) DO UPDATE SET completed_at=excluded.completed_at',(rid,now))
                self.broker._event(db,app,rid,'completed',{'resource_id':resource['resource_id']})
            # 0.4.4: a busy ledger never loses a finished result while the lease is still ours: retried until the deadline.
            self._durable('complete',request['deadline'],complete)
            self._note_finished(app,rid,'completed')
            stop.set()
            if live:
                with self._live_lock:self._live.discard(rid);live=False
            self.scheduler.wake() # the slot is free: place the next request now
            self._mark_loaded(resource['resource_id'],request['model'],installed.get('digest'))
            return None
        except Exception as exc:
            stop.set()
            # 0.4.3: the request stays live until it is settled, so the broker's expiry pass leaves a call
            # at its deadline to _settle (the finally below discards it).
            try:return self._settle(app,token,request,owner,exc,phase,purpose,installed,attempt,operation=operation,payload=payload,
                                    upstream_operation=upstream_operation)
            except Exception as error:
                # 0.4.6: last resort. A raise from _settle would leave the lease executing and its expiry would hold the head.
                return self._settle_fallback(app,request,owner,exc,phase,attempt,error,purpose=purpose)
        finally:
            stop.set()
            if live:
                with self._live_lock:self._live.discard(rid)

    def _wait_for_lease(self,app,token,request,purpose,expected_digest,required,payload):
        """Wait (without holding the write lock) until the scheduler grants a lease, the request ends, or its deadline.

        A dynamic request's eligible heads are recomputed when health or qualification changed
        (scheduler generation) or every 10 s, and written only when they changed.
        """
        rid=request['request_id'];scheduler=self.scheduler;event=scheduler.register(rid)
        dynamic=request.get('node_policy')=='authorized_dynamic';generation=None;computed=0;nodes=None
        try:
            while True:
                if dynamic and (generation!=scheduler.generation or time.monotonic()-computed>=10):
                    generation=scheduler.generation;computed=time.monotonic()
                    eligible=self._dynamic_nodes(app,token,request['model'],purpose,expected_digest,required,payload.get('options',{}).get('num_ctx',0))
                    if eligible!=nodes:
                        with self.broker.write('nodes') as db:
                            db.execute("UPDATE capacity_requests SET nodes=? WHERE request_id=? AND status='queued' AND node_policy='authorized_dynamic'",(json.dumps(eligible),rid))
                        nodes=eligible;scheduler.wake()
                current=self.broker.get(app,token,rid)
                if current['status']=='leased':return current
                if current['status']!='queued':raise TimeoutError('Request ended before execution admission')
                remaining=current['deadline']-time.time()
                if remaining<=0:raise TimeoutError('Admission deadline exceeded')
                event.wait(min(scheduler.TICK,remaining));event.clear()
        finally:scheduler.unregister(rid)

    def _settle_fallback(self,app,request,owner,exc,phase,attempt,error,*,purpose=None):
        """0.4.6: settle a failure when _settle itself raised. The request fails and its attempt is recorded. Uncertain
        execution gets exactly the normal path's action, at once: the head is held until the request's deadline (unless,
        as on the normal path, the call ran out its own deadline on a head that still answers). Every other failure is
        no longer executing, so its lease ending cannot be mistaken for a lost call. No suspension, re-placement or
        replay. Idempotent after a partly completed _settle (the hold is extended, events are written once). Never raises."""
        rid=request['request_id']
        try:failure=recovery.classify(exc,phase)
        except Exception:failure=None
        resource_id=request.get('resource_id') if phase!='admission' else None
        if failure==recovery.UNCERTAIN and resource_id:
            try:
                if self._deadline_on_live_head(exc,phase,request):failure=recovery.DEADLINE
            except Exception:pass
        self.count('settle_fallbacks')
        self._safe_emit('request.settle_fallback',request_id=rid,phase=phase,failure_class=failure,error=(type(error).__name__+': '+str(error))[:200])
        def settle(db):
            now=time.time()
            if resource_id:self.broker.record_attempt(db,rid,attempt,resource_id,failure)
            if failure==recovery.UNCERTAIN and resource_id:
                # The same immediate action as _settle for this class (an existing hold is only extended).
                recovery.hold_resource(self.broker,db,resource_id,request_id=rid,model=request['model'],operation=purpose,
                                       hold_until=request['deadline'],source='dispatch',now=now)
            if failure!=recovery.UNCERTAIN:db.execute('UPDATE capacity_requests SET executing=0 WHERE request_id=?',(rid,))
            live=db.execute('SELECT status FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()
            state='cancelled' if live and live['status'] in {'cancel_requested','cancelled'} else 'failed'
            # Only an invocation still active is settled here; if _settle already recorded it, its event is not repeated.
            first=db.execute("UPDATE capacity_invocations SET state=?,error=?,updated=? WHERE request_id=? AND state='active'",(state,str(exc)[:400],now,rid)).rowcount
            if live and live['status'] in ('leased','cancel_requested'):
                done='cancel_requested' if failure==recovery.UNCERTAIN else 'cancelled'
                db.execute('UPDATE capacity_requests SET status=? WHERE request_id=?',(done,rid))
                self.broker._event(db,app,rid,done,{'reason':'settle_fallback'})
            if first and state=='failed':self.broker._event(db,app,rid,'invocation.failed',{'class':failure} if failure else {})
            return state
        try:
            state=self._durable('settle_fallback',max(time.time(),request.get('deadline') or 0)+120,settle)
            self._note_finished(app,rid,state)
        except Exception as again:
            self._safe_emit('request.settle_fallback_failed',request_id=rid,error=(type(again).__name__+': '+str(again))[:200])
        try:self.scheduler.wake(health=True)
        except Exception:pass
        return None

    def _safe_emit(self,event,**fields):
        try:self.emit(event,**fields)
        except Exception:pass

    def _upstream_capture(self,exc,rid,attempt,app,token,resource_id,model,installed,upstream_operation,phase,failure,operation,payload):
        """0.4.6 (G4): the row for capacity_upstream_errors, or None. Never raises: each part that fails is left out
        and the row says which (capture_error); nothing here can stop the request's settlement."""
        if not isinstance(exc,urllib.error.HTTPError):return None
        try:
            try:text,truncated,decode_errors=http_error_body(exc)
            except Exception:text,truncated,decode_errors='',False,0
            try:body=redact(text,(token,));message=redact(upstream_message(text),(token,))[:UPSTREAM_MESSAGE_LIMIT]
            except Exception:body=message=None
            shape=request_shape(operation,payload)
            try:shape_text=json.dumps(shape,sort_keys=True,default=str)
            except Exception:shape_text=json.dumps({'capture_error':'shape_not_serializable'})
            digest=(installed or {}).get('digest') if isinstance(installed,dict) else None
            status=exc.code if isinstance(exc.code,int) else None
            self.count('upstream_http_errors')
            self._safe_emit('upstream.http_error',request_id=rid,attempt=attempt,resource_id=resource_id,model=model,status=status,operation=upstream_operation,
                            failure_class=failure,phase=phase,body_bytes=len(text.encode('utf-8','replace')),truncated=bool(truncated),
                            capture_error=shape.get('capture_error') if isinstance(shape,dict) else None)
            return (attempt,app,resource_id,model,digest,upstream_operation,phase,failure,status,message,body,int(bool(truncated)),int(decode_errors),shape_text)
        except Exception as error:
            self.count('upstream_capture_failures');self._safe_emit('upstream.capture_failed',request_id=rid,stage='capture',error=type(error).__name__)
            return None

    def _settle(self,app,token,request,owner,exc,phase,purpose,installed,attempt,*,operation=None,payload=None,upstream_operation=None):
        """Apply exactly the action of the failure's class (recovery.classify), then retry once or fail."""
        rid=request['request_id'];failure=recovery.classify(exc,phase)
        resource_id=request.get('resource_id') if phase!='admission' else None
        if failure==recovery.UNCERTAIN and resource_id and self._deadline_on_live_head(exc,phase,request):failure=recovery.DEADLINE
        try:self.broker.authenticate(app,token);revoked=False
        except PermissionError:revoked=True
        # 0.4.6 (G4): an HTTP error reply from the head is kept with what was asked (shape only, no content), bounded
        # and redacted, in the protected ledger table; the class and its action below are unchanged.
        # Everything here is diagnostic and must never stop the settlement below (a raise would leave the lease
        # executing, and its expiry would hold a healthy head as uncertain execution).
        upstream=self._upstream_capture(exc,rid,attempt,app,token,resource_id,request.get('model'),installed,upstream_operation or operation,
                                        phase,failure,operation,payload) if resource_id else None
        # 0.4.4: a failure's settlement is retried through a busy ledger (bounded): an unsettled definite
        # failure would otherwise run out its lease and be mistaken for uncertain execution.
        settle_by=max(time.time(),request.get('deadline') or 0)+120
        def settle(db):
            now=time.time()
            if resource_id:self.broker.record_attempt(db,rid,attempt,resource_id,failure)
            if upstream:
                # Its own savepoint: a failed diagnostic row is dropped, never the settlement.
                db.execute('SAVEPOINT upstream_error')
                try:
                    version=db.execute('SELECT runtime_version FROM capacity_inventory WHERE resource_id=?',(resource_id,)).fetchone()
                    db.execute('INSERT INTO capacity_upstream_errors(request_id,attempt,app_id,resource_id,model,digest,operation,phase,failure_class,status,message,body,'
                               'body_truncated,decode_errors,shape,runtime_version,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (rid,*upstream,model_capacity.live_version(version[0]) if version else None,now))
                    db.execute('RELEASE SAVEPOINT upstream_error')
                except Exception as error:
                    db.execute('ROLLBACK TO SAVEPOINT upstream_error');db.execute('RELEASE SAVEPOINT upstream_error')
                    self.count('upstream_capture_failures');self._safe_emit('upstream.capture_failed',request_id=rid,stage='record',error=type(error).__name__)
            if failure==recovery.DEADLINE:
                # 0.4.3: the call used up its own deadline on a head that still answers: this request fails
                # alone. No hold, no fault, no replay; the head keeps serving.
                self.broker._event(db,app,rid,'invocation.deadline_exceeded',{'resource_id':resource_id})
            elif failure==recovery.UNCERTAIN:
                # The head may still be working: hold the whole head until the request's deadline has passed.
                recovery.hold_resource(self.broker,db,resource_id,request_id=rid,model=request['model'],operation=purpose,
                                       hold_until=request['deadline'],source='dispatch',now=now)
            elif failure==recovery.MODEL_ON_HEAD:
                recovery.suspend_model_on_head(self.broker,db,resource_id,request['model'],(installed or {}).get('digest'),purpose,
                                               request_id=rid,source='dispatch',now=now)
            elif failure==recovery.HEAD_UNREACHABLE:
                prior=db.execute('SELECT health FROM capacity_resources WHERE resource_id=?',(resource_id,)).fetchone()
                if prior and prior['health'] in ('healthy','degraded'):
                    db.execute("UPDATE capacity_resources SET health='offline',last_seen=0 WHERE resource_id=?",(resource_id,))
                    self.broker._event(db,'infrastructure',rid,'resource.health_changed',{'resource_id':resource_id,'from':prior['health'],'to':'offline'})
            elif phase=='checks' and failure is None and not revoked:
                # 0.3.0 behaviour for a failed read-only pre-dispatch check: the heartbeat re-validates the head.
                db.execute("UPDATE capacity_resources SET health='degraded' WHERE resource_id=? AND health NOT IN ('quarantined','offline')",(resource_id,))
            # Any answer from the head, and any failure before the call was sent, means nothing is executing now.
            if failure!=recovery.UNCERTAIN:db.execute('UPDATE capacity_requests SET executing=0 WHERE request_id=?',(rid,))
        self._durable('settle',settle_by,settle)
        # Any settled failure may have freed a slot or changed a head's health or suspensions.
        self.scheduler.wake(health=failure is not None or phase=='checks')
        if failure in (recovery.MODEL_ON_HEAD,recovery.HEAD_UNREACHABLE) and attempt==1 and not revoked and self._replaceable(app,token,request,purpose):
            again=self.broker.requeue(rid,resource_id,attempt,failure)
            if again:return again
        def record_failure(db):
            live=db.execute('SELECT status FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()
            state='cancelled' if live['status'] in {'cancel_requested','cancelled'} else 'failed'
            db.execute('UPDATE capacity_invocations SET state=?,error=?,updated=? WHERE request_id=?',(state,str(exc)[:400],time.time(),rid))
            # Event metadata only; the private error string stays in the invocation row.
            if state=='failed':self.broker._event(db,app,rid,'invocation.failed',{'class':failure} if failure else {})
            return state
        state=self._durable('settle_record',settle_by,record_failure)
        self._note_finished(app,rid,state)
        if revoked:self.broker.release_revoked(rid)
        else:self.broker.finish(app,token,rid,cancel=True,execution_owner=owner,wait=max(BACKGROUND_WRITE_WAIT,settle_by-time.time()))
        self.scheduler.wake()
        return None

    def _deadline_on_live_head(self,exc,phase,request):
        """0.4.3: True when a dispatched call timed out at its own deadline and its head still answers.

        Only a plain timeout in the dispatch phase that ended at (or after) the request's deadline
        qualifies; a reset, truncation or a timeout before the deadline stays uncertain_execution.
        The head must answer /api/version within failure_policy.deadline_liveness_seconds (0 = off).
        """
        if phase!='dispatch' or not isinstance(exc,TimeoutError) or time.time()<request['deadline']-1:return False
        with self.broker.connect() as db:
            wait=recovery.load_policy(db)['failure']['deadline_liveness_seconds']
            row=db.execute('SELECT endpoint FROM capacity_resources WHERE resource_id=?',(request['resource_id'],)).fetchone()
        if not wait or not row:return False
        try:
            version=self.transport(row['endpoint'],'/api/version',None,wait)
            return isinstance(version,dict)
        except Exception:return False

    def _replaceable(self,app,token,request,purpose):
        """Another head could take this request now or later (busy is fine; it then waits in the queue)."""
        with self.broker.connect() as db:
            tried={r[0] for r in db.execute('SELECT resource_id FROM capacity_request_attempts WHERE request_id=?',(request['request_id'],))}
            live=db.execute('SELECT * FROM capacity_requests WHERE request_id=?',(request['request_id'],)).fetchone()
            if live['status']!='leased' or live['deadline']<=time.time():return False
            if live['node_policy']=='authorized_dynamic':nodes=None
            else:nodes=set(json.loads(live['nodes']))
            suspended=recovery.active(db);now=time.time()
            for row in db.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference' AND health='healthy' AND last_seen>?",(now-FRESH_SECONDS,)):
                rid=row['resource_id']
                if rid in tried or (nodes is not None and rid not in nodes):continue
                if app not in json.loads(row['applications']) or 'inference' not in json.loads(row['permitted_uses']) or request['model'] not in json.loads(row['qualified_models']):continue
                inventory=self._inventory(db,row)
                if recovery.suspension_reason(suspended,rid,request['model'],(inventory or {}).get('models',{}).get(request['model']),purpose):continue
                return True
        return False

    # ------------------------------------------------------------ automatic recovery (0.4.0)

    def run_recovery(self,now=None):
        """Probe every suspension or hold that is due. Called by the service's recovery thread.

        Returns one outcome per suspension looked at. Never places client traffic.
        """
        self.broker.reconcile_expired()
        now=time.time() if now is None else now
        with self.broker.connect() as db:
            due=[dict(r) for r in db.execute("SELECT * FROM capacity_suspensions WHERE (state IN ('suspended','held') AND next_probe<=?) "
                                             "OR (state='needs_attention' AND manual_probe=1) ORDER BY next_probe,suspension_id",(now,))]
            policy=recovery.load_policy(db)['failure']
        out=[]
        for suspension in due:
            try:out.append(self._recover(suspension,now,policy))
            except Exception as exc:out.append({'suspension_id':suspension['suspension_id'],'outcome':'error','detail':type(exc).__name__})
        if any(o.get('outcome')=='recovered' for o in out):self.scheduler.wake(health=True)
        return out

    def _wait(self,suspension,now,policy,reason):
        """Not yet safe or not possible to probe: look again later. A held head that never becomes safe escalates."""
        with self.broker.write('recovery_wait') as db:
            if suspension['scope']==recovery.RESOURCE and reason in ('head_unreachable','runner_loaded') and now-max(suspension['hold_until'] or 0,suspension['since'])>policy['hold_max_wait_seconds']:
                db.execute("UPDATE capacity_suspensions SET state='needs_attention',waiting=?,next_probe=NULL,manual_probe=0 WHERE suspension_id=?",(reason,suspension['suspension_id']))
                self.broker._event(db,'infrastructure',None,'resource.needs_attention',{'suspension_id':suspension['suspension_id'],'resource_id':suspension['resource_id'],'reason':reason})
                return {'suspension_id':suspension['suspension_id'],'outcome':'needs_attention','waiting':reason}
            state='needs_attention' if suspension['state']=='needs_attention' else suspension['state']
            db.execute('UPDATE capacity_suspensions SET waiting=?,next_probe=?,manual_probe=CASE WHEN ?=\'needs_attention\' THEN 0 ELSE manual_probe END WHERE suspension_id=?',
                       (reason,None if state=='needs_attention' else now+policy['safe_recheck_seconds'],state,suspension['suspension_id']))
        return {'suspension_id':suspension['suspension_id'],'outcome':'waiting','waiting':reason}

    def _recover(self,suspension,now,policy):
        scope=suspension['scope']
        with self.broker.connect() as db:
            resources={r['resource_id']:dict(r) for r in db.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference'")}
        if scope==recovery.RESOURCE:
            resource=resources.get(suspension['resource_id'])
            if not resource or resource['health']!='quarantined':return {'suspension_id':suspension['suspension_id'],'outcome':'skipped'}
            if now<(suspension['hold_until'] or 0):return self._wait(suspension,now,policy,'deadline')
            with self.broker.connect() as db:
                if db.execute("SELECT 1 FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(resource['resource_id'],)).fetchone():
                    return self._wait(suspension,now,policy,'active_reservation')
            safe=self._safe_to_probe(resource,policy)
            if safe!='safe':return self._wait(suspension,now,policy,safe)
            return self._probe_held(suspension,resource,now,policy)
        if scope==recovery.RESOURCE_MODEL:
            resource=resources.get(suspension['resource_id'])
            if not resource or resource['health']!='healthy' or resource['last_seen']<=time.time()-FRESH_SECONDS:return self._wait(suspension,now,policy,'resource_unavailable')
            outcome,detail=self._probe(resource,suspension['model'],suspension['operation'],suspension,policy)
            return self._after_probe(suspension,resource,outcome,detail,now,policy)
        # scope MODEL: probe on one head that holds this build and could otherwise serve it.
        with self.broker.connect() as db:suspended=recovery.active(db)
        others=[s for s in suspended if s['suspension_id']!=suspension['suspension_id']]
        for resource in sorted(resources.values(),key=lambda r:r['resource_id']):
            if resource['health']!='healthy' or resource['last_seen']<=time.time()-FRESH_SECONDS or suspension['model'] not in json.loads(resource['qualified_models']):continue
            with self.broker.connect() as db:inventory=self._inventory(db,resource)
            digest=(inventory or {}).get('models',{}).get(suspension['model'])
            if suspension['digest'] and digest!=suspension['digest']:continue
            if recovery.suspension_reason(others,resource['resource_id'],suspension['model'],digest,suspension['operation']):continue
            outcome,detail=self._probe(resource,suspension['model'],suspension['operation'],suspension,policy)
            if outcome=='no_free_slot':continue
            return self._after_probe(suspension,resource,outcome,detail,now,policy)
        return self._wait(suspension,now,policy,'no_probe_head')

    def _safe_to_probe(self,resource,policy):
        """Safe-to-probe signal for a held head (Ollama has no in-flight counter).

        Safe means: the head answers /api/version, and two /api/ps reads ps_settle_seconds
        apart both list no loaded model. Ollama never unloads a runner while a request is
        using it (it stays listed past its expires_at), and unloads an idle runner when its
        keep_alive ends; the Gateway sends keep_alive 2m on every call, so an idle held head
        empties within 2 minutes of its last request. No clock comparison with the head is
        needed. The original deadline has also passed, so the Gateway closed its connection
        and Ollama has abandoned that request (measured on Ollama 0.40.16).
        """
        try:
            self.transport(resource['endpoint'],'/api/version',None,3)
            first=self.transport(resource['endpoint'],'/api/ps',None,3)
            if policy['ps_settle_seconds']:time.sleep(policy['ps_settle_seconds'])
            second=self.transport(resource['endpoint'],'/api/ps',None,3)
        except Exception:return 'head_unreachable'
        for ps in (first,second):
            if not isinstance(ps,dict) or not isinstance(ps.get('models'),list):return 'head_unreachable'
            if ps['models']:return 'runner_loaded' # busy, or idle within its keep_alive: look again later
        return 'safe'

    def _current_qualification(self,resource,model,operation,version,installed):
        with self.broker.connect() as db:q=self._proof(db,resource,model,operation)
        return q if q is not None and q['digest']==installed.get(model) and json.loads(q['version'])==version else None

    def _probe(self,resource,model,operation,suspension,policy):
        """The qualification test (exact READY / embedding fixture) on this head and model, under a maintenance lease.

        Returns (outcome, detail): passed | failed | uncertain | unreachable | no_free_slot.
        """
        rid='PROBE-'+uuid.uuid4().hex;deadline=time.time()+policy['probe_timeout_seconds']
        with self.broker.write('probe_lease') as db:
            try:db.execute('INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (rid,'maintenance:gateway-recovery',rid,model,json.dumps([resource['resource_id']]),'leased',time.time(),deadline,'LEASE-'+uuid.uuid4().hex,resource['resource_id'],deadline,1))
            except Exception as exc:
                if 'slots exhausted' in str(exc):return 'no_free_slot',None
                raise
            db.execute('INSERT OR IGNORE INTO capacity_request_routing VALUES(?,?)',(rid,operation))
        outcome,detail,phase='failed',None,'checks'
        def call(path,payload=None):return self.transport(resource['endpoint'],path,payload,max(.001,deadline-time.time()))
        try:
            version=call('/api/version');tags=call('/api/tags').get('models',[])
            installed={m['name']:m.get('digest') for m in tags}
            qualified=self._current_qualification(resource,model,operation,version,installed)
            if not qualified:detail='qualification_not_current'
            else:
                limit=(json.loads(qualified['evidence']).get('execution_policy') or {}).get('max_context',1024)
                phase='dispatch'
                if operation=='embedding':
                    response=call('/api/embed',{'model':model,'input':['Synthetic qualification fixture.'],'keep_alive':'2m'})
                    phase='answered';self.validate_response('/api/embed',response,model);passed=True
                else:
                    response=call('/api/generate',{'model':model,'prompt':COMPLETION_TEST_PROMPT,'stream':False,'think':False,'keep_alive':'2m',
                                                   'options':{'num_ctx':limit,'num_predict':COMPLETION_TEST_TOKENS,'temperature':0,'seed':42}})
                    phase='answered';passed=completion_test(response)[0]
                outcome,detail=('passed',None) if passed else ('failed','not_ready')
        except Exception as exc:
            failure=recovery.classify(exc,phase)
            outcome={recovery.UNCERTAIN:'uncertain',recovery.HEAD_UNREACHABLE:'unreachable'}.get(failure,'failed')
            detail=recovery.http_detail(exc)
        with self.broker.write('probe_record') as db:
            now=time.time()
            db.execute('UPDATE capacity_requests SET status=?,executing=0 WHERE request_id=?',('expired' if outcome=='uncertain' else 'completed' if outcome=='passed' else 'cancelled',rid))
            db.execute('INSERT INTO capacity_probes VALUES(?,?,?,?,?,?,?,?)',(rid,suspension['suspension_id'],resource['resource_id'],model,operation,int(outcome=='passed'),detail,now))
            if outcome=='uncertain':
                recovery.hold_resource(self.broker,db,resource['resource_id'],request_id=rid,model=model,operation=operation,hold_until=deadline,source='probe')
            if outcome in ('passed','failed','uncertain'):
                self.broker._event(db,'infrastructure',rid,'probe.passed' if outcome=='passed' else 'probe.failed',
                                   {'suspension_id':suspension['suspension_id'],'resource_id':resource['resource_id'],'model':model,'operation':operation})
        return outcome,detail

    def _probe_failed(self,db,suspension,now,policy):
        failed=suspension['probes_failed']+1;backoff=policy['probe_backoff_seconds']
        if suspension['state']=='needs_attention' or failed>=len(backoff):
            db.execute("UPDATE capacity_suspensions SET state='needs_attention',probes_failed=?,next_probe=NULL,manual_probe=0,waiting=NULL WHERE suspension_id=?",(failed,suspension['suspension_id']))
            if suspension['state']!='needs_attention':
                self.broker._event(db,'infrastructure',None,'resource.needs_attention',{'suspension_id':suspension['suspension_id'],'resource_id':suspension['resource_id'],
                                                                                         'model':suspension['model'],'scope':suspension['scope']})
            return 'needs_attention'
        db.execute('UPDATE capacity_suspensions SET probes_failed=?,next_probe=?,manual_probe=0,waiting=NULL WHERE suspension_id=?',(failed,now+backoff[failed],suspension['suspension_id']))
        return 'probe_failed'

    def _after_probe(self,suspension,resource,outcome,detail,now,policy):
        sid=suspension['suspension_id']
        if outcome in ('unreachable','no_free_slot'):return self._wait(suspension,now,policy,'resource_unavailable' if outcome=='unreachable' else 'no_free_slot')
        with self.broker.write('after_probe') as db:
            if outcome=='passed':
                recovery.resolve(self.broker,db,suspension,state='recovered',by='gateway-recovery',resolution='probe_passed',event='model.recovered')
                return {'suspension_id':sid,'outcome':'recovered','resource_id':resource['resource_id']}
            return {'suspension_id':sid,'outcome':self._probe_failed(db,suspension,now,policy),'detail':detail,'resource_id':resource['resource_id']}

    def _probe_held(self,suspension,resource,now,policy):
        """Held head: probe one small model already qualified on it, then the model that failed.

        Small passes and failed model passes: the head returns. Small passes and the failed
        model fails with an answer: the head returns and only that model is suspended on it.
        """
        sid=suspension['suspension_id']
        try:
            version=self.transport(resource['endpoint'],'/api/version',None,3);tags=self.transport(resource['endpoint'],'/api/tags',None,3).get('models',[])
        except Exception:return self._wait(suspension,now,policy,'head_unreachable')
        installed={m['name']:m.get('digest') for m in tags};sizes={m['name']:m.get('size') for m in tags}
        current=[]
        for model in json.loads(resource['qualified_models']):
            for operation in OPERATION_PURPOSES:
                if self._current_qualification(resource,model,operation,version,installed):current.append((model,operation))
        failed=(suspension['model'],suspension['operation'] or 'completion') if suspension['model'] else None
        small=sorted((c for c in current if c[0]!=(failed or (None,))[0]),key=lambda c:(sizes.get(c[0]) if isinstance(sizes.get(c[0]),int) else 1<<62,c[0],c[1]))
        steps=([small[0]] if small else [])+([failed] if failed and failed in current else [])
        if not steps:
            with self.broker.write('probe_none') as db:
                db.execute('INSERT INTO capacity_probes VALUES(?,?,?,?,?,?,?,?)',('PROBE-'+uuid.uuid4().hex,sid,resource['resource_id'],None,None,0,'no_current_qualification',time.time()))
                self.broker._event(db,'infrastructure',None,'probe.failed',{'suspension_id':sid,'resource_id':resource['resource_id']})
                return {'suspension_id':sid,'outcome':self._probe_failed(db,suspension,now,policy),'detail':'no_current_qualification'}
        results=[]
        for index,(model,operation) in enumerate(steps):
            outcome,detail=self._probe(resource,model,operation,suspension,policy)
            results.append((model,operation,outcome,detail))
            if outcome!='passed':break
        last_model,last_operation,last_outcome,last_detail=results[-1]
        head_ok=results[0][2]=='passed'
        if last_outcome=='unreachable':return self._wait(suspension,now,policy,'head_unreachable')
        with self.broker.write('probe_held_record') as db:
            if last_outcome=='uncertain' or not head_ok:
                return {'suspension_id':sid,'outcome':self._probe_failed(db,suspension,now,policy),'detail':last_detail,'probes':[r[:3] for r in results]}
            models=self._current_models(resource,version,installed,json.loads(resource['qualified_models']))
            db.execute("UPDATE capacity_resources SET health=?,qualified_models=?,last_seen=? WHERE resource_id=? AND health='quarantined'",
                       ('healthy' if models else 'degraded',json.dumps(models),time.time(),resource['resource_id']))
            recovery.resolve(self.broker,db,suspension,state='recovered',by='gateway-recovery',resolution='probe_passed',event='resource.recovered')
            outcome={'suspension_id':sid,'outcome':'recovered','resource_id':resource['resource_id'],'probes':[r[:3] for r in results]}
            if last_outcome=='failed' and (last_model,last_operation)==failed and len(results)>1:
                # The head answered: only the failed model stays out of service on it.
                outcome['model_suspension']=recovery.suspend_model_on_head(self.broker,db,resource['resource_id'],last_model,installed.get(last_model),last_operation,
                                                                           request_id=suspension['request_id'],source='held_probe')[0]
        return outcome
