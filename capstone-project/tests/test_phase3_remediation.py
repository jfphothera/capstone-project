"""Independent acceptance for whole-artifact Phase 3 remediation."""
from __future__ import annotations
import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_defects import flow,st,source_db,shared_agent  # noqa:F401
from capstone_rag import independent_review,response_policy,review,generation
from capstone_rag import inference,store as store_mod
from capstone_rag.ai_settings import Registry
from capstone_rag.collaboration import Coordinator
from capstone_rag import planning
from capstone_rag.reasoning_strategies import observation_driven_qms
from capstone_rag import telemetry


def _case(flow,st):
    device=st.one("SELECT * FROM devices WHERE serial_number='68177-DV5'")
    cid=flow.intake('Remediation acceptance',f"Serial {device['serial_number']}. Please review my request.")
    st.update('cases','case_id',cid,{'serial_number':device['serial_number'],
        'model':device['model'],'revision':device['revision']})
    return cid,device


def _draft(st,cid,mid,body,parent=None):
    st.insert('messages',{'message_id':mid,'case_id':cid,'run_id':'WF-REMEDIATION',
        'created_at':f'2026-10-29T00:00:{len(st.messages(cid)):02d}Z','direction':'outbound',
        'channel':'email','author':'ai','subject':'Draft','body':body,'status':'draft',
        'approved_by':None,'approved_at':None,'parent_message_id':parent,'metadata':'{}'})


def _state():
    return {'qms':{'sources':[],'evidence_contract':{'mandatory_context_chunk_ids':[],
        'procedure_ids':[]}},'planning':{'selected_action':'existing_workflow'},'result':{}}


def _evaluate(st,cid,wid,cycle,semantic=None):
    row,pkg=independent_review.freeze(st,cid,wid,_state(),cycle)
    verdict=independent_review.evaluate(st,row,pkg,semantic)
    return row,pkg,verdict,json.loads(verdict['issues'])


def _rma(st,cid,status='proposed',movement='device_return',approved=False):
    rid=f'R-{status}-{movement}'
    st.insert('rmas',{'rma_id':rid,'call_id':None,'case_id':cid,'run_id':'WF-REMEDIATION',
        'created_at':'2026-10-29','movement_type':movement,'direction':'inbound',
        'items':'[]','reason':'test','status':status,'approved_by':'rep.test' if approved else None,
        'approved_at':'2026-10-29' if approved else None,'idempotency_key':f'KEY-{rid}',
        'prerequisite_rma_id':None,'return_method':'drop_off_label','received_at':None,
        'received_by':None,'receipt_note':None})
    return rid


def test_exact_confirmed_defect_reproduces_as_two_blocking_issues(flow,st):
    cid,_=_case(flow,st);_draft(st,cid,'BAD-BOTH',
        'We confirmed that your device serial is 681:77-DV5, and the requested action is approved.')
    _,_,verdict,issues=_evaluate(st,cid,'WF-BOTH',0,{'passed':True,'issues':[]})
    assert not verdict['passed']
    assert {i['issue_category'] for i in issues}>={'identifier_inconsistency','unsupported_consequential_claim'}


def test_correction_repairs_both_then_full_artifact_passes_with_provenance(flow,st):
    cid,device=_case(flow,st);_draft(st,cid,'BAD-CORRECT',
        'We confirmed that your device serial is 681:77-DV5, and the requested action is approved.')
    _,pkg,verdict,issues=_evaluate(st,cid,'WF-CORRECT',0,{'passed':False,
        'issues':['The identifier and unsupported approval are incorrect.']})
    result=independent_review.correct(st,pkg,verdict,1)
    corrected=st.one('SELECT * FROM messages WHERE message_id=?',(result['corrected_artifact_id'],))
    assert '681:77-DV5' not in corrected['body'] and 'approved' not in corrected['body'].lower()
    assert 'respond with the next supported step' in corrected['body']
    _,_,second,second_issues=_evaluate(st,cid,'WF-CORRECT',1,{'passed':True,'issues':[]})
    record=st.one('SELECT * FROM artifact_validation_records WHERE workflow_id=? AND cycle=1',('WF-CORRECT',))
    assert second['passed'] and not second_issues and record['disposition']=='pass'
    assert set(json.loads(record['resolved_issue_ids']))=={i['issue_id'] for i in issues}
    assert st.one('SELECT body,status FROM messages WHERE message_id=?',('BAD-CORRECT',))=={
        'body':'We confirmed that your device serial is 681:77-DV5, and the requested action is approved.',
        'status':'validation_failed'}


def test_serial_only_partial_fix_still_fails_approval(flow,st):
    cid,_=_case(flow,st);_draft(st,cid,'PARTIAL-SERIAL',
        'We confirmed that your device serial is 68177-DV5, and the requested action is approved.')
    _,_,verdict,issues=_evaluate(st,cid,'WF-PARTIAL-S',1,{'passed':True,'issues':[]})
    assert not verdict['passed'] and {i['issue_category'] for i in issues}=={'unsupported_consequential_claim'}


def test_approval_only_partial_fix_still_fails_identifier(flow,st):
    cid,_=_case(flow,st);_draft(st,cid,'PARTIAL-APPROVAL',
        'Your serial is 681:77-DV5. The request has not been approved or completed.')
    _,_,verdict,issues=_evaluate(st,cid,'WF-PARTIAL-A',1,{'passed':True,'issues':[]})
    assert not verdict['passed'] and any(i['issue_category']=='identifier_inconsistency' for i in issues)


def test_new_claim_introduced_by_correction_is_new_and_blocking(flow,st):
    cid,_=_case(flow,st);_draft(st,cid,'SAFE-FIRST','A representative is reviewing your request.')
    _evaluate(st,cid,'WF-NEW',0,{'passed':True,'issues':[]})
    st.update('messages','message_id','SAFE-FIRST',{'status':'validation_failed'})
    _draft(st,cid,'NEW-CLAIM','Your replacement has been authorized and sent.','SAFE-FIRST')
    _,_,verdict,issues=_evaluate(st,cid,'WF-NEW',1,{'passed':True,'issues':[]})
    record=st.one('SELECT * FROM artifact_validation_records WHERE workflow_id=? AND cycle=1',('WF-NEW',))
    assert not verdict['passed'] and issues and json.loads(record['new_issue_ids'])


@pytest.mark.parametrize('body',[
    'Your requested action is approved.',
    'We have authorized your request.',
    'Your replacement has been cleared and sent.',
    'A refund was processed for this request.',
    'Your warranty entitlement is confirmed.',
    'A return label was created for you.'])
def test_semantic_pass_cannot_override_unsupported_commitment(flow,st,body):
    cid,_=_case(flow,st);_draft(st,cid,'CLAIM-'+str(abs(hash(body))),body)
    _,_,verdict,issues=_evaluate(st,cid,'WF-'+str(abs(hash(body))),0,{'passed':True,'issues':[]})
    assert not verdict['passed'] and any(i['issue_category']=='unsupported_consequential_claim' for i in issues)


def test_semantic_critic_issue_is_recorded_as_advisory_not_blocking(flow,st):
    # rc.16 policy: the model critic advises; deterministic checks gate.
    # A named human (or the lab's synthetic reviewer) still approves every send.
    cid,_=_case(flow,st);_draft(st,cid,'SEMANTIC-CONTRADICTION','A representative is reviewing your request.')
    semantic=generation.review_conclusion({'passed':True,'issues':['The reply does not answer the request.']})
    _,_,verdict,issues=_evaluate(st,cid,'WF-SEMANTIC',0,semantic)
    advisory=[i for i in issues if i['issue_category']=='semantic_quality']
    assert verdict['passed'] and advisory and all(i['severity']=='advisory' for i in advisory)


def test_semantic_timeout_is_recorded_but_deterministic_failure_still_blocks(flow,st):
    cid,_=_case(flow,st);_draft(st,cid,'SEMANTIC-TIMEOUT','A representative is reviewing your request.')
    semantic={'passed':False,'issues':['Independent semantic review was unavailable; automatic acceptance is not permitted.'],
              'review_unavailable':True,'review_error':'timeout'}
    _,_,verdict,issues=_evaluate(st,cid,'WF-TIMEOUT',0,semantic)
    assert verdict['passed'] and any(i['issue_code']=='review_unavailable' and i['severity']=='advisory' for i in issues)
    _draft(st,cid,'SEMANTIC-TIMEOUT-CLAIM','Your replacement has been cleared and sent.')
    _,_,blocked,blocking=_evaluate(st,cid,'WF-TIMEOUT-CLAIM',0,semantic)
    assert not blocked['passed'] and any(i['issue_category']=='unsupported_consequential_claim' for i in blocking)


def test_actual_approved_rma_claim_and_true_pending_claim_are_supported(flow,st):
    cid,_=_case(flow,st);approved=_rma(st,cid,'approved','device_return',True)
    assert response_policy.validate_case(st,cid,f'Your RMA {approved} is approved.')['passed']
    cid2,_=_case(flow,st);proposed=_rma(st,cid2,'proposed','replacement',False)
    value=response_policy.validate_case(st,cid2,f'Your replacement request {proposed} is pending human approval.')
    assert value['passed'] and proposed in value['authoritative_operational_refs']


def test_exhaustion_is_human_review_and_send_boundary_blocks(flow,st):
    cid,_=_case(flow,st);_draft(st,cid,'EXHAUSTED','Your requested action is approved.')
    _,_,verdict,_=_evaluate(st,cid,'WF-EXHAUST',2,{'passed':True,'issues':[]})
    record=st.one('SELECT * FROM artifact_validation_records WHERE workflow_id=?',('WF-EXHAUST',))
    assert not verdict['passed'] and record['disposition']=='human_review'
    sent=review.approve_and_send(st,cid,'EXHAUSTED','Your requested action is approved.',
                                 'rep.test','I reviewed it')
    assert not sent['ok'] and st.one('SELECT status FROM messages WHERE message_id=?',('EXHAUSTED',))['status']=='draft'


def test_configured_gpu_failure_is_attributed_bounded_and_recovers_without_fallback(tmp_path,monkeypatch):
    registry=Registry(tmp_path/'registry.db');db=store_mod.Store(str(tmp_path/'gpu-failure.db'))
    db.routing_enabled=True;db.inference_registry=registry
    snapshot=registry.snapshot(registry.get('profiles','gateway-default'))
    # Minimal case record is unnecessary for inference; coordinator needs one.
    db.insert('cases',{'case_id':'CASE-GPU-FAIL','created_at':'2026-10-29','updated_at':'2026-10-29',
        'channel':'email','subject':'endpoint failure','status':'Open','priority':'Normal',
        'classification':'general_enquiry','case_version':0,'safety_hold':0})
    coordinator=Coordinator(db,'WF-GPU-FAIL');task=coordinator.delegate(case_id='CASE-GPU-FAIL',
        issuer='coordinator',recipient='independent_validation',purpose='Model critique',based_on_version=0)
    coordinator.start(task['task_id'],'independent_validation');urls=[]
    def offline(url,*args,**kwargs):urls.append(url);raise ConnectionError('simulated GPU offline')
    monkeypatch.setattr(inference,'http',offline)
    with pytest.raises(RuntimeError,match='no alternate model'):
        with inference.session(db,'GPU-OFFLINE',case_id='CASE-GPU-FAIL',snapshot=snapshot),inference.specialist('independent_validation'):
            try:inference.infer('/api/chat',{'messages':[]},1)
            except ConnectionError:pass
    assert coordinator.fail(task['task_id'],'independent_validation','simulated GPU offline')=='retryable'
    failed=db.one("SELECT * FROM inference_calls WHERE investigation_id='GPU-OFFLINE'")
    assert failed['endpoint_id']=='gateway' and failed['model']=='qwen3.5:4b' and failed['status']=='failed'
    arena=db.one("SELECT * FROM arena_events WHERE event_type='inference.failed'")
    assert arena['inference_node']=='https://192.0.2.254:8443' and set(urls)=={'https://192.0.2.254:8443'}
    before={t:db.one(f'SELECT count(*) AS n FROM {t}')['n'] for t in ('messages','rmas','approvals')}
    def online(url,path,payload=None,timeout=5):
        urls.append(url)
        if path=='/api/ps':return {'models':[]}
        return {'model':payload['model'],'message':{'content':'{"passed":true,"issues":[]}'},
                'total_duration':100,'load_duration':10,'eval_duration':50,'eval_count':2}
    monkeypatch.setattr(inference,'http',online);coordinator.start(task['task_id'],'independent_validation')
    with inference.session(db,'GPU-RECOVERY',case_id='CASE-GPU-FAIL',snapshot=snapshot),inference.specialist('independent_validation'):
        result=inference.infer('/api/chat',{'messages':[]},5)
    coordinator.complete(task['task_id'],'independent_validation',{'review':result['message']['content']})
    recovered=db.one("SELECT * FROM inference_calls WHERE investigation_id='GPU-RECOVERY'")
    assert recovered['endpoint_id']=='gateway' and recovered['model']=='qwen3.5:4b' and recovered['status']=='completed'
    assert {t:db.one(f'SELECT count(*) AS n FROM {t}')['n'] for t in before}==before
    assert all(url=='https://192.0.2.254:8443' for url in urls);db.close()


def test_complex_planning_compares_sequences_prunes_and_changes_with_facts(flow,st):
    cid,_=_case(flow,st);intake={'outbound_message':{'decision':'draft_for_review'},
        'intents':[{'kind':'return'},{'kind':'replacement'}]}
    base={'verified':False,'identified_part':False,'field_completion':False,
        'received_original':False,'safety_hold':False,'public_sources':True,
        'operational_qms_missing':False,'evidence_ids':['QMS#SCOPE'],'advisory_case_notes':[]}
    first=planning.plan(st,cid,'TOT-1','I need an exchange or replacement',intake,base,False,
                        st.case(cid)['case_version'])
    received=_rma(st,cid,'executed','device_return',True)
    st.update('rmas','rma_id',received,{'received_at':'2026-10-29','received_by':'svc.test'})
    second=planning.plan(st,cid,'TOT-2','I need an exchange or replacement',intake,
        {**base,'verified':True,'received_original':True},False,st.case(cid)['case_version'])
    assert first['mode']=='tree_of_thought' and len({tuple(p['action_sequence']) for p in first['plans']})>2
    assert any(p['rejection_reasons'] for p in first['plans']) and first['selected_action']=='clarify'
    assert second['selected_action']=='replacement' and second['selected_action_sequence'][-1]=='await_named_approval'
    transition=Coordinator(st,'TOT-2').transition(case_id=cid,plan=second,
        permitted_command='propose_replacement',resulting_state='Human Review Needed',
        execution_status='deferred_for_human_approval')
    assert transition['selected_action']=='replacement' and transition['execution_status']=='deferred_for_human_approval'
    assert first['expanded_states']<=6 and second['expanded_states']<=6 and first['max_depth']==3


def test_react_authorized_first_hit_skips_retry_and_tool_error_terminates():
    calls=[]
    def hit(query,*args,**kwargs):calls.append(query);return {'chunks':[type('C',(),{'chunk_id':'QMS#1'})()],
        'procedures':['PROC-GENERAL'],'contract':{},'evidence':{}}
    _,trace=observation_driven_qms(hit,'power',model='P4800')
    assert len(calls)==1 and trace['termination']=='authorized_evidence_returned'
    def broken(*args,**kwargs):raise TimeoutError('retrieval unavailable')
    result,error=observation_driven_qms(broken,'power',model='P4800')
    assert not result['chunks'] and error['termination']=='tool_error' and len(error['rounds'])==1


def test_broadcast_events_exclude_customer_content_and_retired_identity_is_stable(flow,st):
    cid,_=_case(flow,st);coordinator=Coordinator(st,'WF-PRIVACY')
    task=coordinator.delegate(case_id=cid,issuer='erp_research',recipient='qms_research',
        purpose='Investigate Mary Cohen DOB 1967-03-21 serial 68177-DV5',
        based_on_version=st.case(cid)['case_version'],evidence_ids=['MESSAGE-SAFE-ID'])
    coordinator.start(task['task_id'],'qms_research');coordinator.complete(task['task_id'],'qms_research',
        {'private_result':'must not be broadcast'})
    encoded=json.dumps(telemetry.replay(st,0,100)).lower()
    for secret in ('mary cohen','1967-03-21','68177-dv5','private_result','thinking','request\"'):
        assert secret not in encoded
    before=next(a for a in telemetry.registry(st) if a['agent_id']=='qms_research')
    telemetry.retire(st,'qms_research');telemetry.ensure_registry(st)
    after=next(a for a in telemetry.registry(st) if a['agent_id']=='qms_research')
    assert after['retired']==1 and after['color']==before['color']
    assert st.one("SELECT 1 FROM arena_events WHERE event_type='agent.retired' AND agent_id='qms_research'")


def test_dynamic_agent_registry_scales_and_reinstates_without_identity_loss(tmp_path):
    st=store_mod.Store(str(tmp_path/'dynamic-agents.db'))
    created=[telemetry.register(st,f'dynamic-{i:03d}',f'Dynamic Specialist {i}',f'group-{i%5}',
                                f'node-{i%7}') for i in range(100)]
    assert len({row['agent_id'] for row in created})==100
    assert len({row['color'] for row in created})==100
    target=created[73]
    telemetry.retire(st,target['agent_id'])
    restored=telemetry.register(st,target['agent_id'],target['display_name'],target['role'],
                                target['logical_home_node'])
    assert restored['retired']==0 and restored['color']==target['color']
    event_types=[row['event_type'] for row in telemetry.replay(st,0,1000)
                 if row['agent_id']==target['agent_id']]
    assert event_types[-2:]==['agent.retired','agent.reinstated']
