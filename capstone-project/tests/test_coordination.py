import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_defects import st, source_db, shared_agent, flow
from capstone_rag.coordination import CaseRepository, StaleCase, CaseBusy
from capstone_rag.interpretation import interpret,validate_finding
from capstone_rag import erp_research, identity
import pytest

def test_generic_accessory_examples_are_not_a_packing_list():
    from capstone_rag import response_policy
    for word in ('typically','normally','usually'):
        assert not response_policy.validate(f'The accessories {word} include goggles, a spare lamp and a mains cord.')['passed']
    assert response_policy.validate('Could you confirm which items you received?')['passed']

@pytest.mark.parametrize('body,kind,task',[
 ('Automated call-back request logged from inbound IVR session.','callback_request','callback'),
 ('Voicemail received via phone_ai routing; transcription not available in this channel.','voicemail_without_transcript','review_phone_record'),
 ('Caller inquiry captured by phone AI assistant; no further transcript recorded.','phone_inquiry_without_transcript','review_phone_record'),
 ('Caller selected refill-code option via phone menu; call logged by AI phone agent.','ivr_refill_selection','review_phone_record'),
 ('Call completed. Customer confirmed issue resolved.','completed_call_record',None),
 ('Internal task: update our documentation. No response needed.','internal_task','internal_review')])
def test_real_no_reply_workflow(flow,st,body,kind,task):
    cid=flow.intake('Imported record',body,channel='unknown')
    result=flow.pre_investigate(cid)
    assert result['interpretation']['message_type']==kind
    assert not [m for m in st.messages(cid) if m['direction']=='outbound']
    assert not st.query('SELECT * FROM rmas WHERE case_id=?',(cid,))
    assert st.query('SELECT * FROM case_notes WHERE case_id=?',(cid,))
    assert bool(st.query('SELECT * FROM contact_tasks WHERE case_id=?',(cid,)))==bool(task)

def test_spans_are_actual_input():
    body='Please call me back. My order has not arrived.'
    finding=interpret(body)
    assert validate_finding(finding,body)
    finding['evidence'][0]['quote']='invented'
    assert not validate_finding(finding,body)

def test_cas_command_dedup_and_atomic_note(flow,st):
    cid=flow.intake('Question','I need a guide');repo=CaseRepository(st);v=repo.version(cid)
    note=repo.note(cid,'Customer asks for guide','rep.test',version=v,command_id='same-note')
    assert repo.note(cid,'Customer asks for guide','rep.test',version=v,command_id='same-note')['duplicate']
    with pytest.raises(StaleCase):repo.note(cid,'Stale correction','rep.test',version=v)
    repo.note(cid,'Actually asks for setup guide','rep.test',amends=note['note_id'],version=repo.version(cid))
    assert len(st.query('SELECT * FROM case_notes WHERE case_id=?',(cid,)))==2
    assert len(st.query('SELECT * FROM coordination_outbox WHERE case_id=?',(cid,)))==2
    with pytest.raises(ValueError):repo.note(cid,'Different command','rep.test',command_id='same-note')

def test_leases_exclude_a_second_worker(flow,st):
    cid=flow.intake('Question','A question');repo=CaseRepository(st)
    with repo.lease(cid):
        with pytest.raises(CaseBusy):
            with repo.lease(cid):pass
    with repo.lease(cid):pass

def test_fuzzy_candidate_is_never_authority(st):
    result=erp_research.candidates(st,'ivan nowka','98148468')
    assert result['candidates'] and result['verification_points_granted']==0
    verification=identity.verify(st,identity.Claim(name='ivan nowka',serial='98148468'))
    assert not verification.verified
    assert erp_research.folded('José O’Neil')==erp_research.folded('Neil Jose O')
    assert identity.normalise_name('José')!=identity.normalise_name('Josè')
    assert identity.normalise_name('José')!=identity.normalise_name('Jose')

def test_contact_observation_preserves_existing_contact_and_requires_verification(flow,st):
    cid=flow.intake('Contact','My name is Ivan Nowak. Serial 98148469. Email: ivan.new@example.test')
    st.update('cases','case_id',cid,{'account_id':'ACC-00010'})
    message=st.messages(cid)[0];erp_research.observe_contacts(st,cid,message)
    obs=st.one('SELECT * FROM contact_observations WHERE case_id=?',(cid,))
    before=st.customer('ACC-00010')['email'];repo=CaseRepository(st)
    with pytest.raises(ValueError):erp_research.review_contact(st,obs['observation_id'],'approve','rep.test','customer supplied',repo.version(cid))
    st.update('cases','case_id',cid,{'identity_verified':1})
    erp_research.review_contact(st,obs['observation_id'],'approve','rep.test','Verified customer confirmed new alternate',repo.version(cid))
    assert st.customer('ACC-00010')['email']==(before or 'ivan.new@example.test')
    assert st.one('SELECT * FROM approved_contacts WHERE observation_id=?',(obs['observation_id'],))
