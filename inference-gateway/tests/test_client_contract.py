"""Pins the client-visible discovery, policy, availability and result shapes (docs/CLIENT_CONTRACT.md).

Every 0.2.0 key must keep its meaning; 0.3.0 additions are pinned exactly.
"""
import json
import threading
import pytest
from shared_inference_client import Client
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_gateway_server import Server

V020_DETAIL={'capabilities','digest','qualified_operations','qualification_purpose','observed_at','execution_policy'}
V030_DETAIL={'descriptor','declared_capabilities','qualifications','qualified_capabilities','policy','availability','application_suitability'}
V041_LIFECYCLE={'submitted_at','admitted_at','execution_finished_at','completed_at'}
V044_DETAIL={'parallel'} # 0.4.4: calls of the model the head takes at once, and the source of that limit
V044_PROVENANCE={'attempt','attempts','model_substitution'}
V044_TIMING={'ledger_wait','runtime_load_or_runner_wait','runtime_prompt_eval','runtime_generation','runtime_total','transfer_and_other'}
V020_RECORD={'response','resource_id','model','model_digest','queued_at','started_at','finished_at','queue_ms','duration_ms','lease_id','application_id'}
PROVENANCE={'request_id','client_key','operation','requested_model','expected_digest','executed_model','executed_digest','resource_id','lease_id',
            'runtime_version','qualification_evidence_id','applied_options','dispatch_controls','required_capabilities','capabilities_requested',
            'capabilities_verified','timing_ms','runtime_counters','unavailable_counters','scope'}
SHOW={'capabilities':['completion','embedding','tools'],'details':{'family':'llama','families':['llama'],'parameter_size':'8.0B',
      'quantization_level':'Q4_K_M','format':'gguf'},'model_info':{'general.architecture':'llama','llama.context_length':8192,
      'llama.embedding_length':4096},'modelfile':'PRIVATE-MODELFILE','template':'PRIVATE-TEMPLATE','license':'PRIVATE-LICENSE'}


def transport(endpoint,operation,payload,timeout):
    if operation=='/api/version':return {'version':'0.12.3'}
    if operation=='/api/tags':return {'models':[{'name':'fixture:1','digest':'FIXTURE-DIGEST'}]}
    if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
    if operation=='/api/show':return SHOW
    if operation=='/api/embed':return {'model':'fixture:1','embeddings':[[.1,.2,.3]]}
    if operation=='/api/chat' and payload.get('tools'):
        return {'done':True,'model':'fixture:1','message':{'role':'assistant','content':'','tool_calls':[{'function':{'name':'record_value','arguments':{'value':42}}}]}}
    return {'done':True,'model':'fixture:1','response':'READY','eval_count':3,'prompt_eval_count':5,'done_reason':'stop','thinking':'PRIVATE-THINKING'}


@pytest.fixture
def service(tmp_path):
    gateway=Gateway(tmp_path/'ledger.db',transport=transport)
    gateway.broker.register_client('capstone','test-capstone',models=['fixture:1','permitted-not-installed:1'],max_concurrency=2,queue_limit=4,priority=6)
    gateway.broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',
        owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
    assert gateway.qualify('head-192-0-2-3','fixture:1',operator='test-operator',max_context=2048)['passed']
    assert gateway.qualify('head-192-0-2-3','fixture:1',operator='test-operator',purpose='embedding')['passed']
    assert gateway.qualify('head-192-0-2-3','fixture:1',operator='test-operator',purpose='tools')['passed']
    server=Server(('127.0.0.1',0),gateway);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield gateway,Client('http://127.0.0.1:'+str(server.server_port),'capstone','test-capstone')
    server.shutdown();server.server_close();thread.join(5)


def test_model_discovery_shape(service):
    gateway,client=service;models=client.models()
    assert set(models)=={'contract','resources'} and models['contract']=='shared.inference/1'
    [resource]=models['resources']
    assert set(resource)=={'resource_id','models','model_details'} and resource['models']==['fixture:1']
    detail=resource['model_details']['fixture:1']
    assert set(detail)==V020_DETAIL|V030_DETAIL|V044_DETAIL
    assert detail['parallel']=={'limit':1,'source':'slots','reason':None}
    # 0.2.0 meaning: declared capabilities; only completion/embedding are operations.
    assert detail['capabilities']==['completion','embedding','tools'] and detail['qualified_operations']==['completion','embedding']
    assert detail['digest']=='FIXTURE-DIGEST' and detail['qualification_purpose']=='embedding'
    assert detail['execution_policy']=={'max_context':2048,'max_output_tokens':8192,'allocation':'exclusive physical head'}
    descriptor=detail['descriptor']
    assert descriptor['source']=='runtime_declared' and descriptor['family']=='llama' and descriptor['quantization_level']=='Q4_K_M'
    assert descriptor['parameter_size']=='8.0B' and descriptor['context_length']==8192 and descriptor['embedding_length']==4096
    assert descriptor['runtime_version']=='0.12.3' and descriptor['tag']=='1' and descriptor['model_version'] is None
    assert detail['declared_capabilities']=={'source':'runtime_declared','values':['completion','embedding','tools']}
    states=detail['qualifications']
    assert states['completion']['state']=='passed' and states['completion']['max_context']==2048 and states['completion']['source']=='measured'
    assert states['embedding']['embedding_dimensions']==3 and states['tools']['state']=='passed' and states['tools']['fixture']=='tools/1'
    assert {states[p]['state'] for p in ('chat','structured_output','vision','context')}=={'not_tested'}
    assert detail['qualified_capabilities']==['completion','embedding','tools']
    assert detail['policy']['source']=='operator_policy' and detail['application_suitability']['source']=='not_assessed_by_gateway'
    assert 'PRIVATE' not in json.dumps(models)


def test_policy_and_availability_are_scoped_and_explain_unavailability(service):
    gateway,client=service
    policy=client.policy()
    assert policy['policy']=={'source':'operator_policy','enabled':True,'models':['fixture:1','permitted-not-installed:1'],'max_concurrency':2,
        'queue_limit':4,'priority':6,'priority_aging_seconds':5,'max_output_tokens':8192,'max_deadline_seconds':3600,
        'operations':['/api/chat','/api/embed','/api/generate'],'max_context':None}
    assert policy['models']==[{'model':'fixture:1','operations':{'/api/generate':'available','/api/embed':'available'}},
                              {'model':'permitted-not-installed:1','operations':{'/api/generate':'not_installed','/api/embed':'not_installed'}}]
    assert client.availability('not-permitted:1')['status']=='not_permitted'
    assert client.availability('fixture:1',num_ctx=4096)['status']=='context_exceeds_qualification'
    assert client.availability('fixture:1',capabilities=['vision'])['status']=='capability_not_qualified'
    assert client.availability('fixture:1',capabilities=['tools'])=={'contract':'shared.inference/1','model':'fixture:1','operation':'/api/generate',
        'num_ctx':None,'required_capabilities':['tools'],'status':'available','resources':{'head-192-0-2-3':'available'}}


def test_result_envelope_provenance_and_usage_shape(service):
    gateway,client=service
    result=client.infer('fixture:1',None,'/api/generate',{'prompt':'Synthetic fixture','think':True,'options':{'num_predict':8,'temperature':0,'seed':7}},
                        client_key='trial-1',expected_digest='FIXTURE-DIGEST',timeout=10)
    assert result['status']=='completed'
    # 0.4.1 adds lifecycle times (Gateway clock): submitted_at, admitted_at, execution_finished_at, completed_at.
    assert set(result)=={'contract','request','status','execution_started_at','result'}|V041_LIFECYCLE
    assert result['submitted_at']<=result['admitted_at']<=result['execution_started_at']<=result['execution_finished_at']<=result['completed_at']
    assert (result['submitted_at'],result['execution_started_at'],result['execution_finished_at'])==(
        result['request']['created'],result['result']['started_at'],result['result']['finished_at'])
    assert set(result['request'])=={'request_id','app_id','client_key','model','nodes','status','created','deadline','lease_id',
                                    'resource_id','expires','executing','node_policy'}
    # Existing wire quirk: nodes is the ledger's JSON text, not a list; executing is 0/1.
    assert result['request']['node_policy']=='authorized_dynamic' and json.loads(result['request']['nodes'])==['head-192-0-2-3']
    record=result['result']
    assert set(record)==V020_RECORD|{'provenance'}
    assert record['model_digest']=='FIXTURE-DIGEST' and record['resource_id']=='head-192-0-2-3' and record['application_id']=='capstone'
    assert record['response']['eval_count']==3 and 'thinking' not in record['response']
    provenance=record['provenance'];assert set(provenance)==PROVENANCE|V044_PROVENANCE
    assert provenance['attempt']==1 and provenance['model_substitution'] is None and provenance['attempts'][-1]['resource_id']==record['resource_id']
    assert provenance['request_id']==result['request']['request_id'] and provenance['client_key']=='trial-1'
    assert provenance['expected_digest']==provenance['executed_digest']=='FIXTURE-DIGEST' and provenance['runtime_version']=='0.12.3'
    assert provenance['applied_options']=={'num_predict':8,'temperature':0,'seed':7,'num_ctx':2048} # gateway default disclosed
    assert provenance['qualification_evidence_id'].startswith('EVIDENCE-') and provenance['dispatch_controls']['stream'] is False
    assert provenance['runtime_counters']['eval_count']==3 and 'total_duration' in provenance['unavailable_counters']
    assert set(provenance['timing_ms'])=={'admission_wait','dispatch_checks','gateway_queue','model_execution'}|V044_TIMING
    tools=client.infer('fixture:1',['head-192-0-2-3'],'/api/chat',{'messages':[{'role':'user','content':'x'}],'tools':[{'type':'function','function':{'name':'f'}}]},
                       client_key='trial-tools',required_capabilities=['tools'],timeout=10)
    assert tools['status']=='completed'
    assert tools['result']['provenance']['capabilities_requested']==['chat','tools'] and tools['result']['provenance']['capabilities_verified']==['tools']
    refused=client.infer('fixture:1',['head-192-0-2-3'],'/api/generate',{'prompt':'x'},client_key='needs-vision',required_capabilities=['vision'],timeout=10)
    assert refused['status']=='failed' and 'capability' in refused['error'] and 'execution_started_at' not in refused
    embedded=client.infer('fixture:1',['head-192-0-2-3'],'/api/embed',{'input':['Synthetic fixture']},client_key='trial-2',timeout=10)
    assert embedded['status']=='completed' and embedded['result']['response']['embeddings']==[[.1,.2,.3]]
    usage=client.request('/v1/usage')
    assert set(usage)=={'contract','application_id','requests','completed_execution_ms','completed_output_tokens','scope'}
    assert usage['requests']=={'completed':3,'cancelled':1}
