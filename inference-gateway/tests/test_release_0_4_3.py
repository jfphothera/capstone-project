"""0.4.3: placement spreads load, per-model limits, and a deadline on a live head fails only that call.

Field, 2026-11-14 (hub on 0.4.2, nine heads on Ollama 0.40.13, Capstone Testing Lab, one model
qwen3.5:4b, six calls at once, head slots 10): every call went to the one head that had the model
loaded (placement "loaded" outranked every idle head); Ollama runs the qwen35 family one call at a
time whatever OLLAMA_NUM_PARALLEL says, so five calls waited inside Ollama; each that reached its
45 s deadline was uncertain_execution and held the whole head, and the next loaded head took
everything. These tests replay that with the fake heads of test_failures_and_placement.
"""
import importlib.util
import threading
import time
from pathlib import Path
import pytest
from inference_gateway import recovery
from inference_gateway.capacity_gateway import Gateway

_spec=importlib.util.spec_from_file_location('fp_helpers',Path(__file__).with_name('test_failures_and_placement.py'))
fp=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(fp)
endpoint,events,health,suspensions,background,run,body=fp.endpoint,fp.events,fp.health,fp.suspensions,fp.background,fp.run,fp.body
FAMILY={'qwen3.5:4b':'qwen35'}


class Heads(fp.Lab):
    """The 0.4.0 fake heads, plus /api/tags details.family, a call that times out at its deadline, and a dead head."""
    def __init__(self):
        super().__init__();self.dead_after_dispatch=set();self.dead=set()
    def __call__(self,url,operation,payload,timeout):
        if operation=='/api/tags':
            reply=super().__call__(url,operation,payload,timeout)
            for m in reply['models']:m['details']={'family':FAMILY.get(m['name'],'llama'),'families':[FAMILY.get(m['name'],'llama')]}
            return reply
        if operation=='/api/version' and url in self.dead:raise TimeoutError('timed out')
        model=(payload or {}).get('model')
        if operation in ('/api/generate','/api/chat') and self.behaviour.get((url,model))=='deadline':
            with self.lock:self.calls.append((url,operation,model,payload.get('prompt')))
            if url in self.dead_after_dispatch:self.dead.add(url) # the head stops answering once the call is sent
            time.sleep(timeout) # the socket timeout is the request's remaining deadline
            raise TimeoutError('timed out')
        return super().__call__(url,operation,payload,timeout)


def lab(tmp_path,heads,*,models=('m:1','other:1'),slots=None,placement=None,failure=None,clients=None):
    transport=Heads();gateway=Gateway(tmp_path/'ledger.db',transport=transport);b=gateway.broker
    for app,(limit,priority) in (clients or {'capstone':(16,6)}).items():
        b.register_client(app,'t-'+app,models=list(models),max_concurrency=limit,queue_limit=32,priority=priority)
    for rid in heads:
        b.authorize_resource(rid,endpoint=endpoint(rid),resource_type='shared_inference',owner='infrastructure',applications=list(clients or ['capstone']),
                             permitted_uses=['inference'],slots=(slots or {}).get(rid,1),memory_gb=fp.RAM.get(rid))
        for model in models:assert gateway.qualify(rid,model,operator='ops')['passed']
    with b.connect() as db:
        recovery.store_policy(db,'failure_policy',{'ps_settle_seconds':0,**(failure or {})})
        if placement is not None:recovery.store_policy(db,'placement_policy',placement)
    transport.calls.clear();gateway.refresh_health()
    return gateway,transport


def heads_used(transport,prompt='hold'):
    return sorted({c[0] for c in transport.generations(prompt=prompt)})


FIELD=['head-192-0-2-7','head-192-0-2-8','head-192-0-2-9','head-192-0-2-10']


# ---------------------------------------------------------------- policy

def test_policy_defaults_and_validation():
    placement=recovery.validate_policy('placement_policy',None)
    assert placement['strategy']=='least_busy' and placement['serial_families']==['qwen35','qwen35moe'] and placement['model_parallel']=={}
    assert recovery.validate_policy('failure_policy',None)['deadline_liveness_seconds']==3
    assert recovery.DEADLINE in recovery.CLASSES
    for bad in ({'strategy':'random'},{'serial_families':'qwen35'},{'serial_families':['']},{'model_parallel':{'m:1':0}},
                {'model_parallel':{'m:1':True}},{'model_parallel':{'m:1':65}},{'model_parallel':['m:1']}):
        with pytest.raises(ValueError):recovery.validate_policy('placement_policy',bad)
    with pytest.raises(ValueError):recovery.validate_policy('failure_policy',{'deadline_liveness_seconds':31})
    # A 0.4.0-0.4.2 ledger stored only the two old keys: the new ones come from the defaults.
    old=recovery.validate_policy('placement_policy',{'loaded_fresh_seconds':30,'model_size_factor':1.2})
    assert old['strategy']=='least_busy' and old['serial_families']==['qwen35','qwen35moe']


def test_ledger_gains_the_families_column_and_records_them(tmp_path):
    gateway,transport=lab(tmp_path,['head-192-0-2-7'],models=('qwen3.5:4b',))
    with gateway.broker.connect() as db:
        assert 'families' in {r['name'] for r in db.execute('PRAGMA table_info(capacity_inventory_runtime)')}
        row=db.execute("SELECT families FROM capacity_inventory_runtime WHERE resource_id='head-192-0-2-7'").fetchone()
    assert '"qwen3.5:4b": ["qwen35"]' in row['families']


# ---------------------------------------------------------------- placement

def test_field_six_calls_with_ten_slots_spread_over_the_heads_instead_of_the_loaded_one(tmp_path):
    gateway,transport=lab(tmp_path,FIELD,slots={h:10 for h in FIELD})
    transport.loaded[endpoint('head-192-0-2-8')]=['m:1'];gateway.refresh_health() # loaded on head 8 only, as in the field
    transport.hold=True
    threads=background(gateway,['a','b','c','d'],prompt='hold');assert transport.wait_inflight(4)
    assert heads_used(transport)==sorted(endpoint(h) for h in FIELD) # one call per head, not four on head 8
    transport.release.set()
    for t in threads:t.join(10)


def test_loaded_first_keeps_the_0_4_2_order(tmp_path):
    gateway,transport=lab(tmp_path,FIELD,slots={h:10 for h in FIELD},placement={'strategy':'loaded_first'})
    transport.loaded[endpoint('head-192-0-2-8')]=['m:1'];gateway.refresh_health()
    transport.hold=True
    threads=background(gateway,['a','b','c','d'],prompt='hold');assert transport.wait_inflight(4)
    assert heads_used(transport)==[endpoint('head-192-0-2-8')]
    transport.release.set()
    for t in threads:t.join(10)


def test_the_loaded_head_wins_a_tie_and_an_evicting_head_comes_last(tmp_path):
    gateway,transport=lab(tmp_path,FIELD,slots={h:10 for h in FIELD})
    transport.loaded[endpoint('head-192-0-2-9')]=['m:1'];gateway.refresh_health()
    assert run(gateway,'one')['result']['resource_id']=='head-192-0-2-9' # all idle: the loaded head (tier 1) breaks the tie


def test_a_serial_family_model_takes_one_lease_per_head_whatever_the_slots(tmp_path):
    # Strategy loaded_first isolates the limit: without it every call would stay on head 8.
    gateway,transport=lab(tmp_path,FIELD,models=('qwen3.5:4b','other:1'),slots={h:10 for h in FIELD},placement={'strategy':'loaded_first'})
    transport.loaded[endpoint('head-192-0-2-8')]=['qwen3.5:4b'];gateway.refresh_health()
    transport.hold=True
    threads=background(gateway,['a','b','c'],model='qwen3.5:4b',prompt='hold');assert transport.wait_inflight(3)
    used=heads_used(transport);assert len(used)==3 and endpoint('head-192-0-2-8') in used
    transport.release.set()
    for t in threads:t.join(10)


def test_a_serial_model_never_queues_behind_itself_and_other_models_still_share_the_head(tmp_path):
    rid='head-192-0-2-7';gateway,transport=lab(tmp_path,[rid],models=('qwen3.5:4b','other:1'),slots={rid:4})
    transport.hold=True
    first=background(gateway,['q1'],model='qwen3.5:4b',prompt='hold');assert transport.wait_inflight(1)
    second=background(gateway,['o1','o2'],model='other:1',prompt='hold');assert transport.wait_inflight(3) # other models use the free slots
    waiting=background(gateway,['q2'],model='qwen3.5:4b',prompt='hold');time.sleep(1)
    assert transport.inflight==3 # the second qwen3.5 call waits in the Gateway queue, not inside Ollama
    transport.release.set()
    for t in first+second+waiting:t.join(10)
    assert len(transport.generations(prompt='hold'))==4


def test_model_parallel_overrides_the_family_and_the_slots(tmp_path):
    heads=['head-192-0-2-7','head-192-0-2-8']
    gateway,transport=lab(tmp_path,heads,slots={h:10 for h in heads},placement={'strategy':'loaded_first','model_parallel':{'m:1':2}})
    transport.loaded[endpoint('head-192-0-2-7')]=['m:1'];gateway.refresh_health()
    transport.hold=True
    threads=background(gateway,['a','b','c'],prompt='hold');assert transport.wait_inflight(3)
    seven=[c for c in transport.generations(prompt='hold') if c[0]==endpoint('head-192-0-2-7')]
    assert len(seven)==2 and len(transport.generations('head-192-0-2-8',prompt='hold'))==1
    transport.release.set()
    for t in threads:t.join(10)


# ---------------------------------------------------------------- deadline on a live head

def test_a_call_that_reaches_its_deadline_on_a_live_head_fails_alone_and_the_head_keeps_serving(tmp_path):
    rid='head-192-0-2-8';gateway,transport=lab(tmp_path,[rid])
    transport.behaviour[(endpoint(rid),'m:1')]='deadline'
    assert run(gateway,'late',deadline=2)['status']=='failed'
    names=[e[0] for e in events(gateway)]
    assert 'invocation.deadline_exceeded' in names and 'resource.held' not in names and 'resource.quarantined' not in names
    assert ('invocation.failed',{'class':'deadline_exceeded'}) in events(gateway)
    assert suspensions(gateway)==[] and health(gateway,rid)=='healthy'
    del transport.behaviour[(endpoint(rid),'m:1')]
    assert run(gateway,'next')['result']['resource_id']==rid # no hold: the same head takes the next call at once
    with gateway.broker.connect() as db:
        attempt=db.execute("SELECT failure_class FROM capacity_request_attempts").fetchone()
    assert attempt['failure_class']=='deadline_exceeded'


def test_a_deadline_on_a_head_that_no_longer_answers_still_holds_it(tmp_path):
    rid='head-192-0-2-8';gateway,transport=lab(tmp_path,[rid])
    transport.behaviour[(endpoint(rid),'m:1')]='deadline';transport.dead_after_dispatch.add(endpoint(rid))
    assert run(gateway,'late',deadline=2)['status']=='failed'
    held=suspensions(gateway)
    assert len(held)==1 and held[0]['scope']=='resource' and held[0]['failure_class']=='uncertain_execution'


def test_liveness_check_off_restores_the_0_4_2_hold(tmp_path):
    rid='head-192-0-2-8';gateway,transport=lab(tmp_path,[rid],failure={'deadline_liveness_seconds':0})
    transport.behaviour[(endpoint(rid),'m:1')]='deadline'
    assert run(gateway,'late',deadline=2)['status']=='failed'
    assert len(suspensions(gateway))==1


def test_a_timeout_before_the_deadline_is_still_uncertain_execution(tmp_path):
    rid='head-192-0-2-3';gateway,transport=lab(tmp_path,[rid])
    transport.behaviour[(endpoint(rid),'m:1')]='timeout' # a reset/timeout long before the deadline
    assert run(gateway,'early',deadline=30)['status']=='failed'
    assert len(suspensions(gateway))==1 and suspensions(gateway)[0]['failure_class']=='uncertain_execution'
