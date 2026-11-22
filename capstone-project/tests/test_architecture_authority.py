import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_defects import st, source_db, shared_agent, bot, flow
from capstone_rag import identity, operations_workflow, triage
import pytest

def test_identity_never_grants_revision_authority(st):
    device=st.one("SELECT * FROM devices WHERE revision_status='inferred' LIMIT 1")
    case={'serial_number':device['serial_number'],'model':device['model'],'revision':device['revision'],'identity_verified':1}
    assert not identity.case_revision_authorized(st,case)
    case.update(revision_confirmed_by='representative',revision_confirmed_at='2026-10-28',revision_confirmation='rejected')
    assert not identity.case_revision_authorized(st,case)
    case['revision_confirmation']='confirmed'
    assert identity.case_revision_authorized(st,case)

def test_budget_failure_is_not_field_completion(flow,st):
    cid=flow.intake('Fault','Device will not power on')
    assert not flow._field_completion(cid,'PROC-P4800-PWR-A')
    st.insert('checklist_items',{'item_id':'MODEL-BUDGET','case_id':cid,'seq':1,'action':'reasoning_attempts_exhausted',
        'description':'Model attempts exhausted','actor':'ai','status':'failed','executed_at':'2026-10-28'})
    assert not flow._field_completion(cid,'PROC-P4800-PWR-A')

def test_post_handoff_hazard_is_added_to_same_case(bot,st):
    session=bot.new_session()
    bot.respond(session,'Please connect me to a representative')
    cid=session.handoff_case_id
    count=st.one('SELECT count(*) n FROM cases')['n']
    reply=bot.respond(session,'Now there is a burning smell and smoke from the unit')
    assert st.one('SELECT count(*) n FROM cases')['n']==count
    assert st.case(cid)['priority']=='Critical'
    assert st.query('SELECT * FROM qa_notifications WHERE case_id=?',(cid,))
    assert 'stop using' in reply.text.lower()
    assert any('burning smell' in m['body'] for m in st.messages(cid))

def test_safety_hold_blocks_feedback_replay(flow,st):
    cid=flow.intake('Hazard','The unit is smoking and smells burned')
    st.insert('improvement_proposals',{'proposal_id':'IMP-SAFETY','kind':'representative_feedback','review_status':'approved',
        'created_at':'2026-10-28','body':'Explain the next check','evidence':json.dumps({'case_id':cid,'human_correction':'Continue'})})
    with pytest.raises(ValueError,match='Safety hold'):
        operations_workflow.replay_reviewed_feedback(st,flow.agent,'IMP-SAFETY','rep.test')
