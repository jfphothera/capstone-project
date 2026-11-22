import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_defects import st, source_db, shared_agent, flow
from capstone_rag import review
from capstone_rag.orchestration import CaseOrchestrator,require_capability
from capstone_rag.coordination import CaseRepository,StaleCase
import pytest

def test_graph_runs_specialists_and_resumes_durable_human_interrupt(flow,st):
    cid=flow.intake('Missing accessory','My name is Ivan Nowak. Serial 98148469. An accessory was missing from the box, but I do not know which item.')
    result=flow.pre_investigate(cid)
    assert result['paused_for_review']
    roles={r['role'] for r in st.query('SELECT * FROM specialist_findings WHERE case_id=?',(cid,))}
    assert {'ticket_interpretation','erp_research','qms_research','safety_complaint','documentation','resolution_planning','operations','communication_review','independent_validation','reviewed_learning'}<=roles
    assert st.query('SELECT * FROM checkpoints')
    plans=st.query('SELECT * FROM candidate_plans WHERE case_id=?',(cid,))
    assert any(p['status']=='pruned' for p in plans)
    assert any(p['parent_plan_id'] for p in plans)
    # A fresh controller uses the same SQLite checkpoint; resume itself grants
    # no sending or inventory authority.
    resumed=CaseOrchestrator(flow).run(cid,resume=True)
    assert resumed['paused_for_review']
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND status='sent'",(cid,))
    assert not st.query('SELECT * FROM sim_shipments WHERE case_id=?',(cid,))
    for m in st.query("SELECT * FROM messages WHERE case_id=? AND status='draft'",(cid,)):
        review.reject_draft(st,cid,m['message_id'],'rep.test','No email required for this test')
    for r in st.query("SELECT * FROM rmas WHERE case_id=? AND status='proposed'",(cid,)):
        flow.erp.reject(r['rma_id'],'rep.test','Do not dispatch')
    final=CaseOrchestrator(flow).run(cid,resume=True)
    assert not final['paused_for_review']

def test_specialist_cannot_grant_human_or_erp_permissions():
    for role in ('ticket_interpretation','erp_research','resolution_planning','communication_review'):
        for tool in ('send_email','execute_movement','approve_contact','modify_qms'):
            with pytest.raises(PermissionError):require_capability(role,tool)

def test_independent_concern_needs_named_review_and_cannot_waive_facts(flow,st):
    import hashlib,time
    cid=flow.intake('Missing accessory','My name is Ivan Nowak. Serial 98148469. An accessory is missing, but I do not know which item.')
    flow.pre_investigate(cid)
    draft=review.latest_draft(st,cid)
    st.insert('response_reviews',dict(review_id='test-independent-concern',message_id=draft['message_id'],body_hash=hashlib.sha256(draft['body'].encode()).hexdigest(),passed=0,payload=json.dumps({'passed':False,'issues':['Please check phrasing.']}),at=time.time()))
    assert not review.approve_and_send(st,cid,draft['message_id'],draft['body'],'')['ok']
    assert not review.approve_and_send(st,cid,draft['message_id'],draft['body'],'rep.test')['ok']
    invalid='Your replacement device has already been shipped and delivered.'
    assert not review.approve_and_send(st,cid,draft['message_id'],invalid,'rep.test','Reviewed model concern')['ok']
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND status='sent'",(cid,))
    assert review.approve_and_send(st,cid,draft['message_id'],draft['body'],'rep.test','Reviewed the actual clarification against packing-list source; no unsupported claim.')['ok']
    assert st.one("SELECT * FROM audit_events WHERE case_id=? AND event_type='independent_review_human_override'",(cid,))

def test_independent_reviewer_receives_customer_visible_text(flow,st,tmp_path,monkeypatch):
    from capstone_rag import inference,generation
    from capstone_rag.ai_settings import Registry
    cid=flow.intake('Missing goggles','My name is Ivan Nowak. Serial 98148469. Protective goggles were missing from the box.')
    flow.pre_investigate(cid)
    assert '[For representative:' in review.latest_draft(st,cid)['body']
    captured=[]
    def reviewer(system,context,*args,**kwargs):
        captured.append(context);return {'passed':True,'issues':[]}
    monkeypatch.setattr(generation,'compose_structured',reviewer)
    monkeypatch.setattr(flow.agent,'prefer_llm',True)
    st.routing_enabled=True;st.inference_registry=Registry(tmp_path/'review-registry.db')
    with inference.session(st,'review-proof',case_id=cid):
        result=CaseOrchestrator(flow)._validate({'cid':cid,'body':'Protective goggles missing','erp':{},'qms':{}})
    assert result['passed'] and captured
    assert all('[For representative:' not in value['draft'] for value in captured)
    assert st.one('SELECT * FROM response_reviews')['passed']==1

def test_simple_identified_part_skips_tree_and_notes_are_shared(flow,st):
    cid=flow.intake('Missing goggles','My name is Ivan Nowak. Serial 98148469. Protective goggles were missing from the box.')
    note=CaseRepository(st).note(cid,'Customer expects a packing-list review before dispatch.','rep.test')
    flow.pre_investigate(cid)
    finding=json.loads(st.one("SELECT payload FROM specialist_findings WHERE case_id=? AND role='resolution_planning'",(cid,))['payload'])
    assert finding['mode']=='single_path'
    assert not st.query('SELECT * FROM candidate_plans WHERE case_id=?',(cid,))

def test_model_cannot_select_a_pruned_inventory_branch(flow,st,monkeypatch):
    from capstone_rag import planning,generation
    cid=flow.intake('Missing accessory','An accessory is missing')
    monkeypatch.setattr(generation,'llm_available',lambda:True)
    monkeypatch.setattr(generation,'compose_structured',lambda *a,**k:{'preferred_action':'part_shipment','plans':[{'action':'part_shipment','rationale':'Try shipping despite missing item evidence'}]})
    result=planning.plan(st,cid,'TEST-PLAN','An accessory is missing',{'outbound_message':{'decision':'draft_for_review'},'intents':[]},
        {'verified':True,'identified_part':False,'field_completion':False,'received_original':False,'safety_hold':False,'public_sources':False},True)
    assert result['selected_action']!='part_shipment'
    assert result['model_preferred_action']=='part_shipment'

def test_append_only_records_reject_sql_mutation(flow,st):
    cid=flow.intake('Callback','Please call me back')
    repo=CaseRepository(st);repo.note(cid,'Original observation','rep.test')
    with pytest.raises(sqlite3.IntegrityError):st.execute("UPDATE case_notes SET body='rewritten' WHERE case_id=?",(cid,))
    st._conn.rollback()
    assert st.one('SELECT body FROM case_notes WHERE case_id=?',(cid,))['body']=='Original observation'

@pytest.mark.parametrize('followup,verified',[
    ('Which item should I look for?',True),
    ('Correction: my name is Mary Cohen.',False),
    ('Correction: my serial number is 123.',False)])
def test_shared_claims_reuse_evidence_but_latest_corrections_win(flow,st,followup,verified):
    cid=flow.intake('Follow-up','My name is Ivan Nowak. Serial 98148469. An accessory is missing.')
    st.insert('messages',{'message_id':'FOLLOW-UP-CLAIM','case_id':cid,'created_at':'2099-01-01','direction':'inbound','channel':'email','body':followup,'status':'received'})
    from capstone_rag import task_context
    from capstone_rag.collaboration import Coordinator
    from capstone_rag.orchestration import ROLE_CAPABILITIES
    task=Coordinator(st,'WF-CLAIM').delegate(case_id=cid,issuer='ticket_interpretation',recipient='erp_research',purpose='test',based_on_version=0,required_inputs=[],evidence_ids=[],permitted_tools=sorted(ROLE_CAPABILITIES['erp_research']),operation_scope='advisory_finding',dependency_ids=[],idempotency_key='WF-CLAIM:task:erp')
    with task_context.bind(task_id=task['task_id'],workflow_id='WF-CLAIM',agent_id='erp_research'):
        finding=CaseOrchestrator(flow)._erp({'cid':cid,'body':followup})
    assert finding['verification']['verified']==verified
    assert len(finding['evidence_message_ids'])==2

def test_tool_outside_its_delegated_grant_is_refused_and_audited(flow,st):
    import pytest as _pytest
    from capstone_rag import task_context
    from capstone_rag.collaboration import Coordinator
    from capstone_rag.orchestration import authorize_tool
    cid=flow.intake('Grant','My name is Ivan Nowak. Serial 98148469. An accessory is missing.')
    task=Coordinator(st,'WF-GRANT').delegate(case_id=cid,issuer='erp_research',recipient='qms_research',purpose='test',based_on_version=0,required_inputs=[],evidence_ids=[],permitted_tools=['public_manual_search'],operation_scope='advisory_finding',dependency_ids=[],idempotency_key='WF-GRANT:task:qms')
    with _pytest.raises(PermissionError):authorize_tool(st,cid,'public_manual_search')  # no task bound
    with task_context.bind(task_id=task['task_id'],workflow_id='WF-GRANT',agent_id='qms_research'):
        assert authorize_tool(st,cid,'public_manual_search')
        with _pytest.raises(PermissionError):authorize_tool(st,cid,'exact_verification')  # not in grant or role
        with _pytest.raises(PermissionError):authorize_tool(st,'CASE-OTHER','public_manual_search')  # other case
    with task_context.bind(task_id=task['task_id'],workflow_id='WF-GRANT',agent_id='erp_research'):
        with _pytest.raises(PermissionError):authorize_tool(st,cid,'public_manual_search')  # caller is not the recipient
    assert st.one("SELECT COUNT(*) AS n FROM audit_events WHERE case_id=? AND event_type='tool_permission_denied'",(cid,))['n']>=3

def test_explicit_reinvestigation_preserves_old_checkpoint_and_starts_fresh(flow,st):
    cid=flow.intake('Unknown accessory','My name is Ivan Nowak. Serial 98148469. An accessory is missing.')
    first=flow.pre_investigate(cid)
    assert first['paused_for_review']
    second=flow.pre_investigate(cid)
    assert first['workflow_id']!=second['workflow_id']
    assert st.one('SELECT status FROM workflow_runs WHERE workflow_id=?',(first['workflow_id'],))['status']=='superseded'
    assert st.query('SELECT * FROM checkpoints WHERE thread_id=?',(first['workflow_id'],))
