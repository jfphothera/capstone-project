import copy,json,sqlite3,time,threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from capstone_rag import inference,experiments,store
from capstone_rag.ai_settings import Registry,private_url

@pytest.fixture
def registry(tmp_path):return Registry(tmp_path/'registry.db')

@pytest.fixture
def routed(tmp_path,registry):
    st=store.Store(str(tmp_path/'case.db'));st.routing_enabled=True;st.inference_registry=registry
    yield st
    st.close()

def test_pinned_case_does_not_follow_active_profile_changes(routed,registry):
    with inference.session(routed,'first',case_id='CASE-test') as s:assert s['snapshot']['id']=='gateway-default'
    custom=copy.deepcopy(registry.active());custom.update(id='custom-gateway',name='Custom Gateway')
    registry.put('profiles',custom);registry.activate('custom-gateway')
    with inference.session(routed,'second',case_id='CASE-test') as s:assert s['snapshot']['id']=='gateway-default'
    with inference.session(routed,'new',case_id='CASE-new') as s:assert s['snapshot']['id']=='custom-gateway'
    assert len(routed.query('SELECT * FROM inference_snapshots'))==2

def test_actual_role_request_and_latency_are_persisted(routed,monkeypatch):
    seen=[]
    def transport(url,path,payload=None,timeout=5):
        if path=='/api/ps':return {'models':[]}
        seen.append((url,payload));return {'model':payload['model'],'message':{'content':'ok','thinking':'private'},'total_duration':123000000,'eval_count':2}
    monkeypatch.setattr(inference,'http',transport)
    with inference.session(routed,'turn',case_id='CASE-one'),inference.specialist('customer_communication'):
        result=inference.infer('/api/chat',{'options':{'num_predict':20},'messages':[]},10)
    assert 'thinking' not in result['message']
    row=routed.one('SELECT * FROM inference_calls');payload=json.loads(row['payload'])
    assert row['role']=='customer_communication' and row['endpoint_id']=='gateway'
    assert seen[0][1]['model']=='qwen3.5:4b' and payload['timings']['eval_count']==2
    assert payload['duration_ms']>=payload['slot_wait_ms']

def test_caught_transport_failure_still_fails_session(routed,monkeypatch):
    monkeypatch.setattr(inference,'http',lambda *a,**k:(_ for _ in ()).throw(ConnectionError('offline')))
    with pytest.raises(RuntimeError,match='no alternate model'):
        with inference.session(routed,'turn'):
            try:inference.infer('/api/chat',{},1)
            except ConnectionError:pass
    assert routed.one('SELECT status FROM inference_calls')['status']=='failed'

def test_inference_evidence_is_append_only_without_first_session(routed):
    routed.insert('response_reviews',dict(review_id='R',message_id='M',body_hash='hash',passed=0,payload='{}',at=time.time()))
    with pytest.raises(sqlite3.IntegrityError):routed.execute('DELETE FROM response_reviews')

def test_endpoint_lease_serializes_independent_connections(registry):
    lock=threading.Lock();running=0;peak=0
    def worker():
        nonlocal running,peak
        other=Registry(registry.path)
        with inference.endpoint_slot(other,'http://127.0.0.1:11434',time.monotonic()+5):
            with lock:running+=1;peak=max(peak,running)
            time.sleep(.15)
            with lock:running-=1
    with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:worker(),range(2)))
    assert peak==1

def test_public_inference_endpoints_and_credentials_rejected():
    for url in ['https://8.8.8.8','http://user:password@127.0.0.1','http://127.0.0.1/api/chat']:
        with pytest.raises(ValueError):private_url(url)

def test_preview_counts_source_profiles_and_does_not_activate(registry):
    first=copy.deepcopy(registry.active());first.update(id='comparison-a',name='Comparison A')
    second=copy.deepcopy(registry.active());second.update(id='comparison-b',name='Comparison B')
    registry.put('profiles',first);registry.put('profiles',second)
    spec={'profile_ids':['comparison-a','comparison-b'],'scenario_ids':experiments.DEFAULT_SCENARIOS,'repetitions':2}
    p=experiments.preview(registry,spec)
    assert p['planned_runs']==20 and len(p['source_revision'])==64
    assert registry.active()['id']=='gateway-default'
    p['profiles'][0]['routes']['investigation']['model']='changed'
    assert registry.get('profiles','comparison-a')['routes']['investigation']['model']=='qwen3.5:4b'

def test_disabled_embedding_profile_cannot_enter_experiment(registry):
    with pytest.raises(ValueError,match='not validated and enabled'):experiments.preview(registry,{'profile_ids':['gateway-default'],'scenario_ids':['phone-log'],'retrieval_profile':'qwen3-experimental'})

def test_checkpoint_recheck_never_starts_new_investigation(routed):
    from capstone_rag.experiment_worker import apply_command
    value=apply_command(routed,None,'CASE-no-checkpoint',{'action':'resume_checkpoint','authorizer':'test reviewer','rationale':'Check only'})
    assert value['resumed'] is False and not routed.query('SELECT * FROM workflow_runs')

def test_scripted_permissions_are_scenario_specific():
    assert experiments.SMOKE['safety-complaint']['review_script']==[]
    component=experiments.SMOKE['component-approval']['review_script']
    assert sum(s['action']=='approve_component' for s in component)==1
    assert not any(s['action']=='approve_movement' for s in component)

def test_structured_truncation_gets_one_attributed_same_route_correction(routed,monkeypatch):
    from capstone_rag import generation
    count=0
    def response(url,path,payload=None,timeout=5):
        nonlocal count
        if path=='/api/ps':return {'models':[]}
        count+=1
        return {'model':payload['model'],'done_reason':'length' if count==1 else 'stop','message':{'content':'{"passed":' if count==1 else '{"passed": true,"issues":[]}'}}
    monkeypatch.setattr(inference,'http',response)
    with inference.session(routed,'review-turn'),inference.specialist('independent_validation'):
        value=generation.compose_structured('Review concisely',{}, {'type':'object','required':['passed','issues']},purpose='independent_validation',max_tokens=50)
    calls=routed.query('SELECT * FROM inference_calls')
    assert value['passed'] and count==2 and {x['model'] for x in calls}=={'qwen3.5:4b'}
    assert {x['role'] for x in calls}=={'independent_validation'}

def test_experimental_vectors_reject_tampering_and_wrong_query_dimensions(tmp_path,registry,monkeypatch):
    import hashlib,numpy as np
    from capstone_rag import retrieval_profiles as retrieval
    path=tmp_path/'separate.npz';metadata={'model_digest':'digest'}
    np.savez(path,matrix=np.array([[1.,0.]],dtype=np.float32),metadata=json.dumps(metadata))
    profile={'path':str(path),'index_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'model_digest':'digest','dimensions':2,'corpus_hash':'corpus','model':'experimental'}
    monkeypatch.setattr(retrieval.qms,'load_chunks',lambda:[object()])
    monkeypatch.setattr(retrieval,'corpus_hash',lambda chunks:'corpus')
    index=retrieval.ExperimentalIndex(profile,registry,{'url':'http://127.0.0.1:11434'})
    monkeypatch.setattr(inference,'embed',lambda *a,**k:([[1.,0.,0.]],{}))
    with pytest.raises(RuntimeError,match='dimensions'):index.search('query')
    with path.open('ab') as stream:stream.write(b'tampered')
    with pytest.raises(RuntimeError,match='checksum'):retrieval.ExperimentalIndex(profile,registry,{})

def test_contradictory_review_cannot_pass_or_erase_raw_model_conclusion():
    from capstone_rag.generation import review_conclusion
    raw={'passed':True,'issues':['Unsafe instruction; named approval absent']}
    effective=review_conclusion(raw)
    assert raw['passed'] is True and effective['model_reported_passed'] is True
    assert effective['passed'] is False and effective['contract_consistent'] is False
    assert review_conclusion({'passed':True,'issues':[]})['passed'] is True
    with pytest.raises(ValueError):review_conclusion({'passed':'true','issues':[]})

def test_changed_source_review_keeps_paused_run_and_writes_no_command(registry,tmp_path):
    from capstone_rag.ai_settings import canonical
    run={'id':'RUN-old','experiment_id':'EXP-old','directory':str(tmp_path),'status':'paused','source_revision':'obsolete'}
    with registry.connect() as db:db.execute('INSERT INTO experiment_runs VALUES (?,?,?)',(run['id'],run['experiment_id'],canonical(run)))
    with pytest.raises(ValueError,match='Source changed'):experiments.Manager(registry).command(run['id'],{'action':'resume_checkpoint','authorizer':'reviewer','rationale':'Recheck'})
    assert registry.get('experiment_runs',run['id'])['status']=='paused'
    assert not list(tmp_path.glob('command-*.json'))
