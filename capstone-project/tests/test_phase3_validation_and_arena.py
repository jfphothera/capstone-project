"""Phase 3 independent validation, bounded reasoning, and live telemetry acceptance."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_defects import flow, st, source_db, shared_agent  # noqa: F401
from capstone_rag import independent_review, telemetry
from capstone_rag.collaboration import Coordinator
from capstone_rag.reasoning_strategies import observation_driven_qms,MAX_QMS_ROUNDS
from capstone_rag.webapp.app import create_app
from capstone_rag.orchestration import CaseOrchestrator
from capstone_rag import planning


def _state(flow, cid, source=None):
    case=flow.store.case(cid)
    return {'qms':{'sources':source or [],'evidence_contract':{'mandatory_context_chunk_ids':[],
        'procedure_ids':[]}},'planning':{'selected_action':'existing_workflow'},'result':{}}


def test_malformed_advisory_serial_is_detected_and_original_is_immutable(flow,st):
    device=st.one('SELECT * FROM devices LIMIT 1')
    cid=flow.intake('Identifier check',f"Serial {device['serial_number']}. Please help.")
    st.update('cases','case_id',cid,{'serial_number':device['serial_number']})
    original={'recommended_explanation':'The serial is 681:77-DV5.'}
    st.insert('specialist_findings',{'finding_id':'BAD-SERIAL','case_id':cid,
        'role':'resolution_planning','payload':json.dumps(original),'based_on_version':st.case(cid)['case_version'],
        'command_id':'phase3:bad','at':'2026-10-29T00:00:00Z'})
    row,pkg=independent_review.freeze(st,cid,'WF-SERIAL',_state(flow,cid),0)
    verdict=independent_review.evaluate(st,row,pkg)
    issues=json.loads(verdict['issues'])
    assert any(i['issue_category']=='identifier_inconsistency' for i in issues)
    fixed=independent_review.correct(st,pkg,verdict,1)
    assert fixed['changed']
    assert json.loads(st.one('SELECT payload FROM specialist_findings WHERE finding_id=?',('BAD-SERIAL',))['payload'])==original
    assert device['serial_number'] in st.one('SELECT payload FROM artifact_versions WHERE artifact_id=?',(fixed['corrected_artifact_id'],))['payload']


def test_communication_correction_changes_draft_and_revalidates(flow,st):
    device=st.one('SELECT * FROM devices LIMIT 1');cid=flow.intake('Draft check','Please confirm my device.')
    # rc.25 (C4): showing an account serial is allowed only after verification;
    # this fixture is about identifier correction, so the case is verified.
    st.update('cases','case_id',cid,{'serial_number':device['serial_number'],'identity_verified':1})
    st.insert('messages',{'message_id':'BAD-DRAFT','case_id':cid,'run_id':'WF-DRAFT','created_at':'2026-10-29',
        'direction':'outbound','channel':'email','author':'ai','subject':'Draft','body':'Your serial is 681:77-DV5.',
        'status':'draft','approved_by':None,'approved_at':None,'parent_message_id':None,'metadata':'{}'})
    row,pkg=independent_review.freeze(st,cid,'WF-DRAFT',_state(flow,cid),0)
    verdict=independent_review.evaluate(st,row,pkg);result=independent_review.correct(st,pkg,verdict,1)
    corrected=st.one('SELECT * FROM messages WHERE message_id=?',(result['corrected_artifact_id'],))
    assert corrected['body']!='Your serial is 681:77-DV5.'
    assert device['serial_number'] in corrected['body']
    row2,pkg2=independent_review.freeze(st,cid,'WF-DRAFT',_state(flow,cid),1)
    assert independent_review.evaluate(st,row2,pkg2)['passed']==1
    assert st.one('SELECT status FROM messages WHERE message_id=?',('BAD-DRAFT',))['status']=='validation_failed'


def test_qms_violation_requires_human_and_cannot_be_rewritten(flow,st):
    cid=flow.intake('QMS check','Please help.')
    st.insert('messages',{'message_id':'QMS-DRAFT','case_id':cid,'run_id':'WF-QMS','created_at':'2026-10-29',
        'direction':'outbound','channel':'email','author':'ai','subject':'Draft','body':'Follow PROC-FABRICATED.',
        'status':'draft','approved_by':None,'approved_at':None,'parent_message_id':None,
        'metadata':json.dumps({'source_chunk_ids':['UNAUTHORIZED']})})
    row,pkg=independent_review.freeze(st,cid,'WF-QMS',_state(flow,cid),0)
    verdict=independent_review.evaluate(st,row,pkg)
    assert verdict['human_escalation']==1
    assert independent_review.correct(st,pkg,verdict,1)['changed'] is False


def test_observation_retry_is_result_driven_and_bounded():
    calls=[]
    def search(query,*args,**kwargs):
        calls.append(query)
        return {'chunks':[] if len(calls)==1 else [SimpleNamespace(chunk_id='QMS#1')],
                'procedures':[],'contract':{},'evidence':{}}
    result,trace=observation_driven_qms(search,'no power',model='P4800')
    assert len(calls)==MAX_QMS_ROUNDS and calls[0]!=calls[1]
    assert trace['rounds'][0]['observation']['source_count']==0
    assert result['chunks'][0].chunk_id=='QMS#1'


def test_no_model_means_no_blind_retrieval_retry():
    calls=[]
    def search(query,*args,**kwargs):calls.append(query);return {'chunks':[],'procedures':[],'contract':{}}
    _,trace=observation_driven_qms(search,'unknown symptom')
    assert calls==['unknown symptom'] and trace['termination']=='no_applicable_authorized_evidence'


def test_event_sequence_and_handoff_ack_are_distinct(flow,st):
    cid=flow.intake('Telemetry','Help')
    c=Coordinator(st,'WF-TELEMETRY')
    task=c.delegate(case_id=cid,issuer='erp_research',recipient='qms_research',purpose='evidence',
        based_on_version=st.case(cid)['case_version'])
    sent=st.one("SELECT * FROM arena_events WHERE event_type='handoff.sent' AND task_id=?",(task['task_id'],))
    assert sent and not st.one("SELECT 1 FROM arena_events WHERE event_type='handoff.received' AND task_id=?",(task['task_id'],))
    c.start(task['task_id'],'qms_research')
    received=st.one("SELECT * FROM arena_events WHERE event_type='handoff.received' AND task_id=?",(task['task_id'],))
    assert received['sequence']>sent['sequence'] and sent['correlation_id']==received['correlation_id']
    rows=telemetry.replay(st,sent['sequence']-1,10)
    assert [r['sequence'] for r in rows]==sorted(r['sequence'] for r in rows)


def test_unavailable_resources_are_never_fabricated():
    assert all(not v['available'] and v['value'] is None and v['status']=='not_instrumented'
               for v in telemetry.resource_status().values())


def test_repeated_validation_failure_exhausts_budget_to_human_review():
    state={'validation_cycle':2,'validation':{'passed':False,'human_escalation':False}}
    assert CaseOrchestrator._after_validation(state)=='human_review'


def test_selective_planner_publishes_hard_limits():
    assert planning.MAX_DEPTH==3 and planning.MAX_EXPANDED_STATES==6
    intake={'outbound_message':{'decision':'draft_for_review'},'intents':[]}
    assert not planning.complex_case('What is my case status?',intake)


def test_rejected_plan_can_be_reconsidered_without_execution(flow,st):
    cid=flow.intake('Plan reconsideration','Please compare return and clarification.')
    finding={'selected_action':'clarify','plans':[{'action':'clarify','feasible':True,
        'action_sequence':['request_missing_fact']},{'action':'device_return','feasible':True,
        'action_sequence':['verify_return_prerequisites','propose_return']}]}
    st.insert('specialist_findings',{'finding_id':'PLAN-FINDING','case_id':cid,
        'role':'resolution_planning','payload':json.dumps(finding),'based_on_version':st.case(cid)['case_version'],
        'command_id':'phase3:replan','at':'2026-10-29T00:00:00Z'})
    package={'case':st.case(cid),'specialist_findings':[{'finding_id':'PLAN-FINDING',
        'role':'resolution_planning','payload':finding}]}
    verdict={'workflow_id':'WF-REPLAN','verdict_id':'VERDICT-REPLAN','human_escalation':0,
        'correction_target':'resolution_planning','issues':json.dumps([{
            'affected_artifact':'PLAN-FINDING','recommended_correction_target':'resolution_planning'}])}
    before=st.one('SELECT count(*) AS n FROM rmas WHERE case_id=?',(cid,))['n']
    result=independent_review.correct(st,package,verdict,1)
    fixed=json.loads(st.one('SELECT payload FROM artifact_versions WHERE artifact_id=?',
                            (result['corrected_artifact_id'],))['payload'])
    assert fixed['reconsideration']['action']=='device_return'
    assert fixed['reconsideration']['authority']=='proposal_only_no_execution'
    assert st.one('SELECT count(*) AS n FROM rmas WHERE case_id=?',(cid,))['n']==before


def test_arena_data_apis_require_runtime_token(st):
    app=create_app(prefer_llm=False,store=st,start_workers=False,developer_token='phase3-secret')
    with TestClient(app) as client:
        assert client.get('/api/arena/events').status_code==403
        assert client.post('/api/arena/session',json={'token':'wrong'}).status_code==403
        assert client.post('/api/arena/session',json={'token':'phase3-secret'}).status_code==200
        response=client.get('/api/arena/events',headers={'X-Capstone-Developer-Token':'phase3-secret'})
        assert response.status_code==200 and response.json()['durable_replay']
        status=client.get('/api/arena/status',headers={'X-Capstone-Developer-Token':'phase3-secret'}).json()
        assert status['mode']=='live' and status['resources']['gpu']['status']=='not_instrumented'


def test_arena_is_absent_from_customer_and_representative_shells():
    from pathlib import Path
    root=Path(__file__).parents[1]/'src'/'capstone_rag'/'webapp'/'static'
    assert 'agent-arena' not in (root/'index.html').read_text(encoding='utf-8')
    assert 'agent-arena' not in (root/'chat.html').read_text(encoding='utf-8')


def test_agent_arena_ui_is_not_part_of_capstone(st):
    """The Arena UI is its own project; Capstone only reports activity (arena_events, /api/arena/*)."""
    from pathlib import Path
    root=Path(__file__).parents[1]/'src'/'capstone_rag'/'webapp'/'static'
    assert not [p.name for p in root.rglob('agent_arena*')]
    assert 'agent-arena' not in (root/'pressure_testing.html').read_text(encoding='utf-8')
    app=create_app(prefer_llm=False,store=st,start_workers=False,developer_token='phase3-secret')
    with TestClient(app) as client:
        assert client.get('/agent-arena',follow_redirects=False).status_code==404
        assert client.get('/agent-arena-login').status_code==404
