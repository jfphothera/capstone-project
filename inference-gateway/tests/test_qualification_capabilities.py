"""Technical capability qualification: strict fixtures, isolation from capacity, invalidation."""
import json
import pytest
from inference_gateway.capacity_gateway import Gateway
from inference_gateway import qualification_fixtures


class Head:
    """Simulated runtime whose replies are programmable per fixture."""
    def __init__(self):
        self.version={'version':'0.12.3'};self.digest='D1';self.capabilities=['completion','embedding','tools','vision']
        self.replies={};self.raise_on=set();self.calls=[]
    def __call__(self,endpoint,operation,payload,timeout):
        if operation=='/api/version':return self.version
        if operation=='/api/tags':return {'models':[{'name':'m:1','digest':self.digest}]}
        if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
        if operation=='/api/show':return {'capabilities':self.capabilities}
        kind=('tools' if payload.get('tools') else 'structured_output' if payload.get('format') else 'vision' if payload.get('images')
              else 'context' if 'code word' in str(payload.get('prompt','')) else 'chat' if operation=='/api/chat' else 'embedding' if operation=='/api/embed' else 'completion')
        self.calls.append(kind)
        if kind in self.raise_on:raise TimeoutError('simulated lost response')
        if kind in self.replies:return self.replies[kind](payload)
        if kind=='embedding':return {'model':'m:1','embeddings':[[.1]*768]}
        if kind=='chat':return {'done':True,'model':'m:1','message':{'role':'assistant','content':'READY'}}
        if kind=='tools':return chat({'tool_calls':[{'function':{'name':'record_value','arguments':{'value':42}}}]})
        if kind=='structured_output':return generate('{"status": "READY", "count": 3}')
        if kind=='vision':return generate('Red.')
        if kind=='context':return generate(qualification_fixtures.CODE_WORD,prompt_eval_count=int(payload['options']['num_ctx']*.55))
        return generate('READY')


def generate(text,**extra):return {'done':True,'model':'m:1','response':text,**extra}
def chat(message):return {'done':True,'model':'m:1','message':{'role':'assistant','content':'',**message}}


@pytest.fixture
def setup(tmp_path):
    head=Head();gateway=Gateway(tmp_path/'ledger.db',transport=head)
    gateway.broker.register_client('capstone','token',models=['m:1'])
    gateway.broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',
        owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
    return gateway,head


def qualify(gateway,purpose,**kwargs):return gateway.qualify('head-192-0-2-3','m:1',operator='test-operator',purpose=purpose,**kwargs)
def health(gateway):
    with gateway.broker.connect() as db:return dict(db.execute("SELECT health,qualified_models FROM capacity_resources").fetchone())
def detail(gateway):
    [resource]=gateway.models('capstone','token')['resources'];return resource['model_details']['m:1']


def test_every_capability_passes_only_its_exact_fixture(setup):
    gateway,head=setup
    assert qualify(gateway,'completion',max_context=4096)['passed']
    embedding=qualify(gateway,'embedding');assert embedding['passed'] and embedding['embedding_dimensions']==768
    for purpose in ('chat','tools','structured_output','vision','context'):
        evidence=qualify(gateway,purpose)
        assert evidence['passed'],(purpose,evidence.get('error'),evidence.get('measurement'))
        assert evidence['fixture']['id']==purpose+'/1' and evidence['execution_policy']['max_context']==4096 and evidence['base_evidence_id']
    states=detail(gateway)['qualifications']
    # 0.4.6 added structured_chat (chat with a JSON schema; tests/test_release_0_4_6.py): not tested here.
    assert {p:s['state'] for p,s in states.items()}=={**{p:'passed' for p in ('completion','embedding','chat','tools','structured_output','vision','context')},'structured_chat':'not_tested'}
    assert states['context']['max_context']==4096 and states['embedding']['embedding_dimensions']==768


@pytest.mark.parametrize('purpose,reply',[
    ('tools',lambda p:chat({'tool_calls':[{'function':{'name':'record_value','arguments':{'value':'42'}}}]})),
    ('tools',lambda p:chat({'tool_calls':[{'function':{'name':'record_value','arguments':{'value':42}}}]*2})),
    ('tools',lambda p:chat({'tool_calls':[{'function':{'name':'record_value','arguments':'{"value": 42, "extra": 1}'}}]})),
    ('tools',lambda p:chat({'content':'record_value(42)'})),
    ('structured_output',lambda p:generate('{"status": "READY", "count": 3, "note": "x"}')),
    ('structured_output',lambda p:generate('{"status": "READY", "count": true}')),
    ('structured_output',lambda p:generate('Sure! {"status": "READY", "count": 3}')),
    ('vision',lambda p:generate('dark red')),
    ('chat',lambda p:chat({'content':'READY!'})),
    ('context',lambda p:generate(qualification_fixtures.CODE_WORD,prompt_eval_count=100)), # truncated prompt
    ('context',lambda p:generate(qualification_fixtures.CODE_WORD)), # no evaluated-token evidence
    ('context',lambda p:generate('alpha',prompt_eval_count=4000)),
])
def test_near_miss_outputs_do_not_grant_a_capability(setup,purpose,reply):
    gateway,head=setup;assert qualify(gateway,'completion',max_context=4096)['passed']
    head.replies[purpose]=reply
    evidence=qualify(gateway,purpose)
    assert not evidence['passed'] and detail(gateway)['qualifications'][purpose]['state']=='failed'
    # A failed capability test never withdraws completion capacity or the head.
    assert health(gateway)=={'health':'healthy','qualified_models':'["m:1"]'}


def test_capability_needs_current_completion_and_declared_support(setup):
    gateway,head=setup
    evidence=qualify(gateway,'tools')
    assert not evidence['passed'] and 'completion qualification' in evidence['error'] and head.calls==[]
    assert qualify(gateway,'completion')['passed'];head.capabilities=['completion']
    evidence=qualify(gateway,'vision')
    assert not evidence['passed'] and 'Metadata' in evidence['error'] and 'vision' not in head.calls
    assert health(gateway)['health']=='healthy' # nothing dispatched, nothing uncertain


def test_unanswered_capability_call_quarantines_the_head(setup):
    gateway,head=setup;assert qualify(gateway,'completion')['passed']
    head.raise_on={'tools'}
    assert not qualify(gateway,'tools')['passed']
    assert health(gateway)['health']=='quarantined'
    with gateway.broker.connect() as db:
        assert db.execute("SELECT count(*) FROM capacity_events WHERE event_type='resource.quarantined'").fetchone()[0]==1


def test_invalidation_after_digest_runtime_endpoint_context_and_failed_requalification(setup):
    gateway,head=setup
    assert qualify(gateway,'completion',max_context=2048)['passed'] and qualify(gateway,'tools')['passed'] and qualify(gateway,'context')['passed']
    # Changed effective context: completion requalified at a smaller limit.
    assert qualify(gateway,'completion',max_context=1024)['passed']
    states=detail(gateway)['qualifications']
    assert states['tools']['state']==states['context']['state']=='invalidated' and states['tools']['reason']=='context_policy_changed'
    assert qualify(gateway,'tools')['passed'] and detail(gateway)['qualifications']['tools']['state']=='passed'
    # Failed requalification supersedes an older pass.
    head.replies['tools']=lambda p:chat({'content':'no'})
    assert not qualify(gateway,'tools')['passed'] and detail(gateway)['qualifications']['tools']['state']=='failed'
    head.replies.clear()
    # Digest change: health refresh withdraws the model; new completion evidence invalidates old capabilities.
    head.digest='D2';gateway.refresh_health()
    assert gateway.models('capstone','token')['resources']==[]
    assert gateway.availability('capstone','token','m:1')['status']=='digest_changed'
    assert qualify(gateway,'completion')['passed']
    states=detail(gateway)['qualifications']
    assert states['completion']['state']=='passed' and states['tools']['state']=='invalidated' and states['tools']['reason']=='digest_or_runtime_changed'
    # Runtime change.
    head.version={'version':'0.13.0'};gateway.refresh_health()
    assert gateway.availability('capstone','token','m:1')['status']=='runtime_changed'
    # Endpoint move.
    head.version={'version':'0.12.3'};head.digest='D1'
    gateway.broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12399',resource_type='shared_inference',
        owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
    assert gateway.availability('capstone','token','m:1')['status']=='not_qualified'
    # Resource revocation: the application no longer sees the head at all.
    gateway.broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12399',resource_type='shared_inference',
        owner='infrastructure',applications=[],permitted_uses=['inference'])
    assert gateway.availability('capstone','token','m:1')['status']=='no_authorized_resource'


def test_completion_answer_from_a_different_model_does_not_qualify(setup):
    gateway,head=setup
    head.replies['completion']=lambda p:{'done':True,'model':'other:1','response':'READY'}
    evidence=qualify(gateway,'completion')
    assert not evidence['passed'] and health(gateway)['qualified_models']=='[]'


def test_fixtures_are_reproducible_and_synthetic():
    first=qualification_fixtures.build('context','m:1',2048);second=qualification_fixtures.build('context','m:1',2048)
    assert first[3]['sha256']==second[3]['sha256'] and first[3]['nonce']!=second[3]['nonce']
    assert first[1]['prompt'].startswith('Run '+first[3]['nonce']) and first[1]['options']['num_ctx']==2048
    for purpose in ('chat','tools','structured_output','vision'):
        operation,payload,check,record=qualification_fixtures.build(purpose,'m:1',1024)
        assert payload['model']=='m:1' and payload['stream'] is False and record['id']==purpose+'/1'
    png=__import__('base64').b64decode(qualification_fixtures.build('vision','m:1',1024)[1]['images'][0])
    assert png.startswith(b'\x89PNG') and len(png)<1000
