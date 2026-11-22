"""Release 0.4.1: one test (or more) per field item A and B (item C is in test_deployment.py).

A  concurrency: one scheduler instead of a write-lock spin, status reads without the write
   lock, lease renewal that survives a busy ledger, 503 + Retry-After over the connection
   limit and the pending budget, a client that retries with the same identity, lifecycle times.
B  qualification: per-purpose withdrawal, refusals change nothing, a head is not degraded by
   one model, and a completion test that asks "does the model answer".
The fake heads raise what urllib raises; answers are the field answers of 2026-11-12.
"""
import functools
import io
import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
import pytest
import shared_inference_client
from shared_inference_client import Client
from inference_gateway import capacity_gateway
from inference_gateway.capacity_gateway import Gateway,completion_test
from inference_gateway.capacity_gateway_server import Server,server_tls
from inference_gateway import recovery

HEAD='head-192-0-2-7'
ENDPOINT='http://127.0.0.1:20007'


class Head:
    """One fake Ollama head. generate() answers per model; hold=True blocks generations until release."""
    ANSWERS={ # (text, tokens needed to finish)
        'gemma3:1b':('Ready.',3),'qwen3:0.6b':('Ready.',3),
        'qwen3:4b':('We are to return exactly the word "READY". So we output READY.\n</think>\n\nREADY',40),
        'deepseek-r1:8b':('<think>\nOkay, so I need to return exactly the word READY. That is simple enough.\n</think>\n\nREADY',60),
        'deepseek-r1:14b':('<think>\nOkay, so I need to answer with one word, READY.\n</think>\n\nREADY',55),
        'smollm2:135m':("I'm sorry, but I can't comply with that request.",12),
        'granite4:350m':("I'm sorry, I cannot do that.",9),
        'endless:1':('<think>\n'+'still thinking '*400,10**6),'silent:1':('',0)}
    def __init__(self):
        self.lock=threading.Lock();self.entered=threading.Event();self.release=threading.Event()
        self.hold=False;self.calls=[];self.capabilities={'all-minilm:latest':['embedding'],'dual:1':['completion','embedding']}
        self.models=['m:1','dual:1','all-minilm:latest',*self.ANSWERS];self.fail_completion=set()
    def __call__(self,endpoint,operation,payload,timeout):
        if operation=='/api/version':return {'version':'0.40.13'}
        if operation=='/api/tags':return {'models':[{'name':n,'digest':'DIGEST-'+n,'size':1<<30} for n in self.models]}
        if operation=='/api/ps':return {'models':[]}
        if operation=='/api/show':return {'capabilities':self.capabilities.get(payload['model'],['completion'])}
        model=payload['model']
        with self.lock:self.calls.append((operation,model,payload.get('prompt')))
        if operation=='/api/embed':return {'model':model,'embeddings':[[.1,.2,.3]]}
        if payload.get('prompt')==capacity_gateway.COMPLETION_TEST_PROMPT:
            text,needed=self.ANSWERS.get(model,('READY',1))
            if model in self.fail_completion:text,needed=self.ANSWERS['silent:1'] # a model that produces nothing
            limit=payload['options']['num_predict']
            if limit<needed:return {'model':model,'done':True,'done_reason':'length','response':text[:limit*4],'eval_count':limit}
            return {'model':model,'done':True,'done_reason':'stop','response':text,'eval_count':needed if text else 0}
        self.entered.set()
        if self.hold:assert self.release.wait(20)
        return {'model':model,'done':True,'done_reason':'stop','response':'answer','eval_count':2}
    def generations(self):return [c for c in self.calls if c[0]!='/api/embed' and c[2]!=capacity_gateway.COMPLETION_TEST_PROMPT]


def gateway_with(tmp_path,*,slots=1,models=('m:1',),max_concurrency=50,queue_limit=500):
    head=Head();gateway=Gateway(tmp_path/'ledger.db',transport=head);b=gateway.broker
    for app in ('capstone','capstone-research'):
        b.register_client(app,'t-'+app,models=list(models)+['dual:1','all-minilm:latest'],max_concurrency=max_concurrency,queue_limit=queue_limit)
    b.authorize_resource(HEAD,endpoint=ENDPOINT,resource_type='shared_inference',owner='infrastructure',
                         applications=['capstone','capstone-research'],permitted_uses=['inference'],slots=slots)
    for model in models:assert gateway.qualify(HEAD,model,operator='ops')['passed']
    gateway.refresh_health();head.calls.clear()
    return gateway,head


def body(key,model='m:1',deadline=30,operation='/api/generate'):
    payload={'input':['x']} if operation=='/api/embed' else {'prompt':'flow '+key,'options':{'num_predict':8}}
    return {'contract':'shared.inference/1','client_key':key,'model':model,'nodes':['@authorized'],'deadline':time.time()+deadline,
            'operation':operation,'payload':payload}


def write_locks(broker):
    """Count BEGIN IMMEDIATE (the exclusive write lock) and every write statement from now on."""
    counter={'begin_immediate':0,'writes':0};lock=threading.Lock();original=broker.connect
    @contextmanager
    def connect():
        with original() as db:
            def trace(sql):
                head=sql.lstrip()[:16].upper()
                with lock:
                    if head.startswith('BEGIN IMMEDIATE'):counter['begin_immediate']+=1
                    if head.startswith(('INSERT','UPDATE','DELETE','REPLACE')):counter['writes']+=1
            db.set_trace_callback(trace);yield db
    broker.connect=connect
    return counter


def status_of(gateway,rid):
    with gateway.broker.connect() as db:return db.execute('SELECT status FROM capacity_requests WHERE request_id=?',(rid,)).fetchone()[0]


def wait_until(check,timeout=10,step=.02):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if check():return True
        time.sleep(step)
    return False


def events(gateway,kind):
    with gateway.broker.connect() as db:return [r[0] for r in db.execute('SELECT request_id FROM capacity_events WHERE event_type=?',(kind,))]


@pytest.fixture
def http(tmp_path):
    started=[]
    def start(gateway,**kwargs):
        server=Server(('127.0.0.1',0),gateway,**kwargs);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        started.append((server,thread));return server
    yield start
    for server,thread in started:server.shutdown();server.server_close();thread.join(5)


# ---------------------------------------------------------------- A1 scheduler, reads outside the write lock

def test_waiting_requests_cost_one_admission_pass_per_event_not_a_write_lock_spin(tmp_path):
    """0.4.0: 20 waiting requests = 20 BEGIN IMMEDIATE every 50 ms (~400/s). 0.4.1: ~1 pass/s while nothing changes."""
    gateway,head=gateway_with(tmp_path,slots=1);head.hold=True
    gateway.execute('capstone','t-capstone',body('running'),asynchronous=True);assert head.entered.wait(5)
    waiting=[gateway.execute('capstone-research','t-capstone-research',body('w%d'%i),asynchronous=True)['request']['request_id'] for i in range(20)]
    assert wait_until(lambda:len(gateway.scheduler.waiters)==20)
    counter=write_locks(gateway.broker);time.sleep(3)
    assert counter['begin_immediate']<=8,counter # one tick a second (+ slack), never per waiter
    assert all(status_of(gateway,rid)=='queued' for rid in waiting)
    head.release.set()
    assert wait_until(lambda:all(status_of(gateway,rid)=='completed' for rid in waiting),20)
    assert len(head.generations())==21 and not events(gateway,'lease.expired')


def test_a_status_read_takes_no_write_lock(tmp_path):
    gateway,head=gateway_with(tmp_path);head.hold=True
    rid=gateway.execute('capstone','t-capstone',body('one'),asynchronous=True)['request']['request_id'];assert head.entered.wait(5)
    counter=write_locks(gateway.broker)
    for _ in range(50):assert gateway.result('capstone','t-capstone',rid)['status']=='active'
    assert counter=={'begin_immediate':0,'writes':0}
    head.release.set();assert wait_until(lambda:gateway.result('capstone','t-capstone',rid)['status']=='completed')


def test_a_freed_slot_is_given_to_the_next_request_at_once(tmp_path):
    """Release wakes the scheduler: the next request starts in well under the 1 s tick."""
    gateway,head=gateway_with(tmp_path,slots=1)
    gateway.scheduler.TICK=5.0 # only the release event can be what places the second request quickly
    head.hold=True;first=gateway.execute('capstone','t-capstone',body('first'),asynchronous=True)['request']['request_id'];assert head.entered.wait(5)
    second=gateway.execute('capstone','t-capstone',body('second'),asynchronous=True)['request']['request_id']
    assert wait_until(lambda:second in gateway.scheduler.waiters)
    head.hold=False;released=time.time();head.release.set()
    assert wait_until(lambda:status_of(gateway,second)=='completed',5)
    assert time.time()-released<1.5 and status_of(gateway,first)=='completed'


# ---------------------------------------------------------------- A2 lease renewal

def test_renewal_retries_every_failure_until_stopped_and_stops_only_when_the_lease_ended():
    calls=[];log=[];stopped=threading.Event()
    class Broker:
        def renew(self,app,token,rid):
            calls.append(time.monotonic())
            if len(calls) in (1,2,3):raise sqlite3.OperationalError('database is locked')
            if len(calls)==6:raise ValueError('Expired lease cannot be renewed')
    thread=threading.Thread(target=capacity_gateway.renew_until_stopped,args=(Broker(),'a','t','REQ-1',stopped),
                            kwargs={'interval':.2,'log':lambda e,**f:log.append(e)});thread.start();thread.join(10)
    assert len(calls)==6 and not thread.is_alive()
    assert log==['lease.renewal_failed']*3+['lease.renewal_recovered','lease.renewal_ended']
    stopped.set()


def test_a_busy_ledger_during_generation_neither_expires_the_lease_nor_holds_the_head(tmp_path,monkeypatch):
    """Field: head 7 'Allocation ended before durable result acceptance' after a renewal failed once."""
    gateway,head=gateway_with(tmp_path);head.hold=True;log=[]
    gateway.emit=lambda event,**fields:log.append(event)
    monkeypatch.setattr(capacity_gateway,'renew_until_stopped',functools.partial(capacity_gateway.renew_until_stopped,interval=.2))
    def locked(*args,**kwargs):raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(gateway.broker,'renew',locked) # every renewal fails
    rid=gateway.execute('capstone','t-capstone',body('long'),asynchronous=True)['request']['request_id'];assert head.entered.wait(5)
    assert wait_until(lambda:log.count('lease.renewal_failed')>=2) # still retrying, not stopped on the first error
    with gateway.broker.connect() as db:db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(time.time()-1,rid))
    gateway.broker.reconcile_expired() # this process runs the call: the lease is extended, not expired
    assert status_of(gateway,rid)=='leased' and events(gateway,'lease.extended')==[rid]
    with gateway.broker.connect() as db:db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(time.time()-1,rid))
    head.release.set() # the answer arrives with expires in the past and nobody having expired it
    assert wait_until(lambda:gateway.result('capstone','t-capstone',rid)['status']=='completed')
    with gateway.broker.connect() as db:assert not recovery.active(db)
    assert not events(gateway,'lease.expired') and gateway.counters['lease_renewal_failures']>=2


def test_a_lease_whose_executor_is_gone_still_expires_and_holds_the_head(tmp_path):
    """The 0.4.0 safety rule is kept: only this process's own live calls are extended."""
    gateway,head=gateway_with(tmp_path);b=gateway.broker
    request=b.submit('capstone','t-capstone','orphan','m:1',[HEAD],time.time()+30)
    b.admit('capstone','t-capstone',request['request_id']);b.executing('capstone','t-capstone',request['request_id'])
    with b.connect() as db:db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(time.time()-1,request['request_id']))
    b.reconcile_expired()
    assert status_of(gateway,request['request_id'])=='expired'
    with b.connect() as db:assert [s['scope'] for s in recovery.active(db)]==['resource']


# ---------------------------------------------------------------- A3 503 over the limits

def refused(url,body=None):
    data=None if body is None else json.dumps(body).encode()
    with pytest.raises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(url,data=data),timeout=5)
    return caught.value


def test_over_the_connection_limit_the_client_gets_503_with_retry_after(tmp_path,http):
    gateway,head=gateway_with(tmp_path);server=http(gateway,max_connections=1)
    url='http://127.0.0.1:%d'%server.server_port
    busy=socket.create_connection(('127.0.0.1',server.server_port));time.sleep(.2) # holds the only connection
    for request_body in (None,body('x')): # GET, and a POST whose body must be read before answering
        answer=refused(url+('/health' if request_body is None else '/v1/submit'),request_body)
        assert answer.code==503 and answer.headers['Retry-After']=='1' and answer.headers['X-Inference-Service-ID']=='shared-inference'
        assert json.loads(answer.read())['retry_after_seconds']==1
    assert gateway.counters['connections_503']==2
    # The 0.4.1 client waits Retry-After and gets through once the connection frees.
    threading.Timer(1.5,busy.close).start()
    client=Client(url,'capstone','t-capstone')
    result=client.infer('m:1',None,'/api/generate',{'prompt':'after the limit'},timeout=20)
    assert result['status']=='completed' and client.retries['submit']>=1
    assert gateway.publish_counters(max_connections=1)['counters']['connections_503']>=3
    from inference_gateway.capacity_observe import snapshot
    assert snapshot(gateway.broker.path)['gateway_load']['counters']['connections_503']>=3


def test_over_the_connection_limit_with_tls_the_answer_is_still_503(tmp_path,http,certificates):
    good=certificates['good'];gateway,head=gateway_with(tmp_path)
    server=http(gateway,max_connections=1,tls=server_tls(good['server_certificate'],good['server_key']))
    busy=socket.create_connection(('127.0.0.1',server.server_port));time.sleep(.2)
    client=Client('https://127.0.0.1:%d'%server.server_port,'capstone','t-capstone',ca_file=good['ca_file_for_clients'])
    with pytest.raises(urllib.error.HTTPError) as caught:client.request('/health',timeout=5)
    assert caught.value.code==503 and caught.value.headers['Retry-After']=='1'
    busy.close()


def test_a_full_pending_budget_answers_503_and_the_same_client_key_succeeds_later(tmp_path,http):
    gateway,head=gateway_with(tmp_path);gateway.max_pending=1;head.hold=True;server=http(gateway)
    url='http://127.0.0.1:%d'%server.server_port;client=Client(url,'capstone','t-capstone')
    first=client.request('/v1/submit',body('first'));assert head.entered.wait(5)
    second=body('second')
    with pytest.raises(urllib.error.HTTPError) as caught:client.request('/v1/submit',second)
    assert caught.value.code==503 and caught.value.headers['Retry-After']=='2'
    head.hold=False;head.release.set()
    assert wait_until(lambda:client.status(first['request']['request_id'])['status']=='completed')
    again=client.request('/v1/submit',second) # the same body and client_key: re-opened, not a new request
    assert wait_until(lambda:client.status(again['request']['request_id'])['status']=='completed')
    rid=again['request']['request_id']
    assert events(gateway,'overload.rejected')==[rid] and events(gateway,'request.resubmitted')==[rid]
    with gateway.broker.connect() as db:assert db.execute("SELECT count(*) FROM capacity_requests WHERE client_key='second'").fetchone()[0]==1


# ---------------------------------------------------------------- A4 client library

def test_client_version_is_0_4_1():
    # 0.4.1's retry behaviour is unchanged in 0.4.2 (Gateway 0.4.6 adds methods only; tests/test_release_0_4_6.py).
    assert shared_inference_client.__version__ in ('0.4.1','0.4.2')


def test_status_reads_that_time_out_or_get_503_are_retried_until_the_deadline(tmp_path,http,monkeypatch):
    gateway,head=gateway_with(tmp_path);head.hold=True;server=http(gateway)
    client=Client('http://127.0.0.1:%d'%server.server_port,'capstone','t-capstone');real=client.request;failures=[]
    def flaky(path,body=None,timeout=120):
        if path.startswith('/v1/requests/') and len(failures)<4:
            failures.append(path)
            if len(failures)==2:raise urllib.error.HTTPError(path,503,'busy',{'Retry-After':'0'},io.BytesIO(b'{}'))
            if len(failures)==3:raise urllib.error.URLError(ConnectionResetError(10054,'reset'))
            raise socket.timeout('The read operation timed out') # 0.4.0 client: the flow failed here
        return real(path,body,timeout)
    monkeypatch.setattr(client,'request',flaky)
    threading.Timer(1.0,head.release.set).start()
    result=client.infer('m:1',None,'/api/generate',{'prompt':'busy gateway'},timeout=20)
    assert result['status']=='completed' and len(failures)==4 and client.retries['status']==4
    assert len({path for path in failures})==1 # always the same request_id


def test_a_submit_whose_answer_was_lost_is_retried_with_the_same_client_key_and_runs_once(tmp_path,http,monkeypatch):
    gateway,head=gateway_with(tmp_path);server=http(gateway)
    client=Client('http://127.0.0.1:%d'%server.server_port,'capstone','t-capstone');real=client.request;submits=[]
    def lost(path,body=None,timeout=120):
        answer=real(path,body,timeout)
        if path=='/v1/submit':
            submits.append(answer['request']['request_id'])
            if len(submits)==1:raise TimeoutError('timed out') # the Gateway took it; the answer never arrived
        return answer
    monkeypatch.setattr(client,'request',lost)
    result=client.infer('m:1',None,'/api/generate',{'prompt':'once'},client_key='flow-7-call-1',timeout=20)
    assert result['status']=='completed' and submits[0]==submits[1]==result['request']['request_id']
    assert len(head.generations())==1


def test_final_answers_are_not_retried(tmp_path,http):
    gateway,head=gateway_with(tmp_path);server=http(gateway)
    client=Client('http://127.0.0.1:%d'%server.server_port,'capstone','t-capstone');began=time.monotonic()
    with pytest.raises(urllib.error.HTTPError) as caught:client.infer('not-permitted:1',None,'/api/generate',{'prompt':'x'},timeout=20)
    assert caught.value.code==403 and time.monotonic()-began<2 and client.retries['submit']==0


# ---------------------------------------------------------------- A5 lifecycle times

def test_status_replies_carry_lifecycle_times_that_prove_same_head_overlap(tmp_path,http):
    gateway,head=gateway_with(tmp_path,slots=2);head.hold=True;server=http(gateway)
    client=Client('http://127.0.0.1:%d'%server.server_port,'capstone','t-capstone')
    out={}
    threads=[threading.Thread(target=lambda k:out.__setitem__(k,client.infer('m:1',None,'/api/generate',{'prompt':k},timeout=20)),args=(k,)) for k in ('a','b')]
    for t in threads:t.start()
    assert wait_until(lambda:len(head.generations())==2);head.release.set()
    for t in threads:t.join(20)
    a,b=out['a'],out['b']
    for r in (a,b):
        assert r['submitted_at']<=r['admitted_at']<=r['execution_started_at']<=r['execution_finished_at']<=r['completed_at']
    assert a['request']['resource_id']==b['request']['resource_id']==HEAD
    assert a['execution_started_at']<b['execution_finished_at'] and b['execution_started_at']<a['execution_finished_at'] # overlapped
    queued=gateway.execute('capstone','t-capstone',body('later'),asynchronous=True)
    assert 'admitted_at' not in queued or queued['request']['resource_id'] # absent until admitted


# ---------------------------------------------------------------- B qualification

@pytest.mark.parametrize('model,passes,matches',[('gemma3:1b',True,True),('qwen3:0.6b',True,True),('qwen3:4b',True,True),
    ('deepseek-r1:8b',True,True),('deepseek-r1:14b',True,True),('smollm2:135m',True,False),('granite4:350m',True,False),('endless:1',True,False),('silent:1',False,False)])
def test_completion_qualification_passes_every_model_that_answers(tmp_path,model,passes,matches):
    gateway,head=gateway_with(tmp_path,models=())
    evidence=gateway.qualify(HEAD,model,operator='ops')
    assert evidence['passed'] is passes and evidence['measurement']['answer_matches_ready'] is matches
    assert evidence['test']['num_predict']==128 and not evidence.get('refused')
    # With the 0.4.0 test (24 tokens, exact READY) only an exact 'READY' within 24 tokens passed.
    text,needed=Head.ANSWERS[model]
    assert (needed<=24 and text.strip()=='READY')is False


def test_completion_rule_on_the_field_answers():
    """0.4.2 rule: output produced (done, eval_count > 0); stop or length; reasoning counts."""
    ok=lambda text,reason='stop',count=10:completion_test({'done':True,'done_reason':reason,'response':text,'eval_count':count})
    assert ok('Ready.')[0] and ok('Ready.')[1]['answer_matches_ready']
    qwen=ok('We are to return exactly the word "READY". So we output\n</think>\n\nREADY')
    assert qwen[0] and qwen[1]['reasoning'] and qwen[1]['finished_reasoning'] and qwen[1]['answer_matches_ready']
    # DeepSeek-R1 on the lab and on a real 0.40.16: still reasoning at 128 tokens. A working model.
    deepseek=ok('<think>\nFirst, the user said: "Return exactly the word READY." So, I need to',reason='length',count=128)
    assert deepseek[0] and deepseek[1]['reasoning'] and not deepseek[1]['finished_reasoning'] and not deepseek[1]['answered']
    assert ok('READY',reason='length')[0] and ok("I'm sorry, I cannot do that.")[0]
    assert not ok('',count=0)[0] and not ok('   ',count=0)[0] # nothing produced
    assert not completion_test({'done':False,'response':'READY','eval_count':3})[0]
    assert completion_test({'done':True,'response':'READY'})[0] and not completion_test({'done':True,'response':''})[0] # no eval_count reported
    assert completion_test({'done':True,'response':'','thinking':'Okay, so','eval_count':128,'done_reason':'length'})[0] # reasoning in its own field


def test_an_embedding_model_given_the_completion_test_keeps_its_embedding_qualification(tmp_path):
    gateway,head=gateway_with(tmp_path,models=('m:1',))
    assert gateway.qualify(HEAD,'all-minilm:latest',operator='ops',purpose='embedding')['passed']
    refused=gateway.qualify(HEAD,'all-minilm:latest',operator='ops') # line 418 in 0.4.0: refused, and the model was removed
    assert refused['refused'] and not refused['passed'] and 'Metadata does not support' in refused['error']
    with gateway.broker.connect() as db:row=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(HEAD,)).fetchone()
    assert set(json.loads(row['qualified_models']))=={'m:1','all-minilm:latest'} and row['health']=='healthy'
    gateway.refresh_health()
    detail=gateway.models('capstone','t-capstone')['resources'][0]['model_details']['all-minilm:latest']
    assert detail['qualified_operations']==['embedding'] and detail['qualifications']['completion']['state']=='not_tested'
    assert gateway.availability('capstone','t-capstone','all-minilm:latest','/api/embed')['status']=='available'
    result=gateway.execute('capstone','t-capstone',body('embed',model='all-minilm:latest',operation='/api/embed'))
    assert result['status']=='completed'


def test_a_0_4_0_refusal_row_is_ignored_and_requalifying_restores_the_pair(tmp_path):
    """The lab ledger holds 0.4.0 rows like this one (no 'refused' flag); re-running embedding qualification restores the pair."""
    gateway,head=gateway_with(tmp_path,models=('m:1',))
    assert gateway.qualify(HEAD,'all-minilm:latest',operator='ops',purpose='embedding')['passed']
    legacy={'resource_id':HEAD,'model':'all-minilm:latest','purpose':'completion','endpoint':ENDPOINT,'passed':False,'digest':'DIGEST-all-minilm:latest',
            'error':'Metadata does not support qualification operation','metadata':{'capabilities':['embedding']}}
    with gateway.broker.connect() as db:
        db.execute('INSERT INTO capacity_qualification_evidence VALUES(?,?,?,?,?,?,?,?)',('EVIDENCE-legacy',HEAD,'all-minilm:latest','DIGEST-all-minilm:latest',
                   json.dumps({'version':'0.40.13'}),0,json.dumps(legacy),time.time()))
        db.execute("UPDATE capacity_resources SET qualified_models='[\"m:1\"]' WHERE resource_id=?",(HEAD,)) # what 0.4.0's finally block did
    assert gateway.availability('capstone','t-capstone','all-minilm:latest','/api/embed')['resources'][HEAD]=='not_qualified'
    assert gateway.qualify(HEAD,'all-minilm:latest',operator='ops',purpose='embedding')['passed']
    gateway.refresh_health()
    detail=gateway.models('capstone','t-capstone')['resources'][0]['model_details']['all-minilm:latest']
    assert detail['qualified_operations']==['embedding'] and detail['qualifications']['completion']['state']=='not_tested'


def test_a_failed_test_withdraws_only_the_purpose_it_tested_and_never_degrades_the_head(tmp_path):
    gateway,head=gateway_with(tmp_path,models=('m:1','dual:1'))
    assert gateway.qualify(HEAD,'dual:1',operator='ops',purpose='embedding')['passed']
    head.fail_completion.add('dual:1')
    failed=gateway.qualify(HEAD,'dual:1',operator='ops') # ran and did not finish: completion is withdrawn
    assert not failed['passed'] and not failed.get('refused') and failed['measurement']['output'] is False
    with gateway.broker.connect() as db:row=db.execute('SELECT * FROM capacity_resources WHERE resource_id=?',(HEAD,)).fetchone()
    assert row['health']=='healthy' and set(json.loads(row['qualified_models']))=={'m:1','dual:1'}
    gateway.refresh_health()
    assert gateway.availability('capstone','t-capstone','dual:1','/api/generate')['status']=='qualified_other_operation'
    assert gateway.availability('capstone','t-capstone','dual:1','/api/embed')['status']=='available'
    assert gateway.availability('capstone','t-capstone','m:1','/api/generate')['status']=='available'
    detail=gateway.models('capstone','t-capstone')['resources'][0]['model_details']['dual:1']
    assert detail['qualified_operations']==['embedding'] and detail['qualifications']['completion']['state']=='failed'
    assert gateway.execute('capstone','t-capstone',body('e',model='dual:1',operation='/api/embed'))['status']=='completed'
    refused=gateway.execute('capstone','t-capstone',body('g',model='dual:1',deadline=3))
    assert refused['status']!='completed' and not head.generations()
    # The only model on a head failing its test: the head has nothing to serve, as in 0.4.0.
    head.fail_completion.add('m:1')
    assert not gateway.qualify(HEAD,'m:1',operator='ops')['passed']
    with gateway.broker.connect() as db:assert db.execute('SELECT health FROM capacity_resources WHERE resource_id=?',(HEAD,)).fetchone()[0]=='healthy' # dual:1 embedding remains


def test_a_refused_qualification_changes_nothing(tmp_path):
    gateway,head=gateway_with(tmp_path,models=('m:1',))
    with gateway.broker.connect() as db:before=dict(db.execute('SELECT qualified_models,health FROM capacity_resources WHERE resource_id=?',(HEAD,)).fetchone())
    for model,purpose in (('not-installed:1','completion'),('all-minilm:latest','completion'),('m:1','tools')):
        head.capabilities.setdefault('m:1',['completion'])
        evidence=gateway.qualify(HEAD,model,operator='ops',purpose=purpose)
        if purpose=='tools':assert not evidence['passed'] # a capability prerequisite or metadata refusal
        else:assert evidence['refused'] and not evidence['passed']
        with gateway.broker.connect() as db:assert dict(db.execute('SELECT qualified_models,health FROM capacity_resources WHERE resource_id=?',(HEAD,)).fetchone())==before
    assert not head.generations()
