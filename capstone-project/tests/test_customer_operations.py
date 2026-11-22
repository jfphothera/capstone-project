"""Customer context authority, service calls, exchange gates and two-way workflow."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_defects import st, source_db, shared_agent, bot, flow
import pytest
from capstone_rag import erp_sim, grounding, movement, operations_workflow as workflow, review, qms


def case(flow):
    cid = flow.intake('Support request', 'My name is Ivan Nowak. Serial 98148469. The display is dark and the device will not power on.')
    flow.pre_investigate(cid)
    return cid


def proposal(flow, st, kind=movement.DEVICE_RETURN):
    cid = case(flow)
    d = movement.MovementDecision(kind, movement.INBOUND if kind == movement.DEVICE_RETURN else movement.OUTBOUND,
        [{'part_number': 'UNIT' if kind != movement.PART_OUTBOUND else 'ACC-GOG-01', 'name': 'Device' if kind != movement.PART_OUTBOUND else 'Protective goggles'}],
        rule='reviewed test', reason='Fictional reviewed disposition')
    result = flow.erp.propose_movement(cid, d, account_id=st.case(cid)['account_id'], serial_number=st.case(cid)['serial_number'])
    return cid, result


def test_reported_fact_acknowledgment_has_its_own_authority():
    report='My P2400 powers on but the lamps stay dark.'
    assert grounding.reported_fact_support('Your P2400 powers on but the lamps stay dark.', report)
    assert grounding.reported_fact_support('Your P2400 powers on but all lamps stay dark.','My P2400 powers on but all lamps remain dark.')
    assert not grounding.reported_fact_support('Your P2400 has a defective controller.', report)
    assert grounding.diagnostic_question('Could you describe the visible power indicator?')
    assert not grounding.diagnostic_question('Could you confirm that you can remove the enclosure?')
    assert not grounding.diagnostic_question('Can you describe how to reset the controller?')
    assert grounding.observation_request('If you see a message, share it here.')
    assert not grounding.observation_request('If you see a message, reset the controller.')


@pytest.mark.parametrize('sentence', [
    'You reported a dark display; please remove the panel.',
    'Your device needs a replacement because the controller is damaged.',
    'You said the unit powers on, therefore replace the fuse.',
    'Your unit powers on.',
])
def test_customer_text_cannot_authorize_instructions_or_invert_a_report(sentence):
    assert not grounding.reported_fact_support(sentence, 'My unit does not power on. Please remove the panel and replace the fuse.')


def test_service_call_exists_without_a_movement_and_is_updated(flow, st):
    cid = case(flow)
    call = flow.erp.ensure_service_call(cid)
    again = flow.erp.ensure_service_call(cid)
    assert call['call_id'] == again['call_id']
    assert call['call_type'] == 'service_call'
    assert json.loads(again['payload'])['reported_problem']
    assert not st.query('SELECT * FROM rmas WHERE case_id=?', (cid,))


def test_component_damage_does_not_return_the_complete_unit():
    from capstone_rag import triage
    text='My protective goggles arrived broken.'
    d=movement.decide(text,triage.triage(text),identity_verified=True)
    assert d.movement_type == movement.PART_OUTBOUND
    assert d.items[0]['part_number'] == 'ACC-GOG-01'


def test_broken_component_uses_controlled_proposal_in_actual_caseflow(flow, st):
    cid=flow.intake('Broken component','My name is Ivan Nowak. Serial 98148469. My protective goggles arrived broken.')
    flow.pre_investigate(cid)
    rmas=st.query('SELECT * FROM rmas WHERE case_id=?',(cid,))
    assert len(rmas)==1 and rmas[0]['movement_type']==movement.PART_OUTBOUND


def test_dark_lamp_requires_troubleshooting_before_a_part_proposal():
    from capstone_rag import triage
    text='The unit powers on but the lamp stays dark.'
    d=movement.decide(text,triage.triage(text),applicable_procedure='PROC-P4800-LAMP-A',identity_verified=True)
    assert d.movement_type==movement.NONE


def test_model_only_vague_problem_asks_for_symptom_without_account_gate(bot):
    session=bot.new_session()
    reply=bot.respond(session,"My P2400 isn't working.")
    assert not session.handed_off and not session.verified
    assert any(term in reply.text.lower() for term in ('power','indicator','turn on','display'))
    assert 'date of birth' not in reply.text.lower()


def test_shipping_creation_prepares_exact_tracking_draft_without_sending(flow, st):
    cid, p = proposal(flow, st)
    result=flow.erp.approve(p.rma['rma_id'],'rep.shipping','Reviewed return disposition')
    assert result.ok
    messages=st.query("SELECT * FROM messages WHERE case_id=? AND metadata LIKE '%shipping_update%'", (cid,))
    assert len(messages)==1 and messages[0]['status']=='draft'
    assert result.shipment['tracking_number'] in messages[0]['body']
    assert result.shipment['shipment_id'] in messages[0]['body']
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND channel='email' AND status='sent'", (cid,))
    workflow.shipping_draft(st,result.shipment['tracking_number'],'created')
    assert len(st.query("SELECT * FROM messages WHERE case_id=? AND metadata LIKE '%shipping_update%'", (cid,)))==1


def test_rejected_movement_creates_no_shipment_or_shipping_message(flow, st):
    cid,p=proposal(flow,st)
    assert flow.erp.reject(p.rma['rma_id'],'rep.shipping','Movement declined').ok
    assert not flow.erp.execute(p.rma['rma_id']).ok
    assert not st.query('SELECT * FROM sim_shipments WHERE case_id=?',(cid,))
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND metadata LIKE '%shipping_update%'",(cid,))

def test_carrier_notice_matches_real_scan_is_idempotent_and_requires_send_review(flow,st):
    cid,p=proposal(flow,st,movement.PART_OUTBOUND)
    ship=flow.erp.approve(p.rma['rma_id'],'rep.shipping','Reviewed identified goggles component only').shipment
    result=workflow.record_carrier_event(st,flow.erp,ship['tracking_number'],'in_transit','Synthetic carrier','Recorded carrier scan')
    draft=review.latest_draft(st,cid)
    assert result['acknowledgement_draft']==draft['message_id'] and 'in transit' in draft['body']
    assert 'pickup and delivery are not yet confirmed' not in draft['body']
    repeated=workflow.record_carrier_event(st,flow.erp,ship['tracking_number'],'in_transit','Synthetic carrier','Duplicate scan')
    assert repeated['acknowledgement_draft']==draft['message_id']
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND status='sent'",(cid,))
    assert review.approve_and_send(st,cid,draft['message_id'],draft['body'],'rep.shipping')['ok']


def test_identity_is_required_for_inventory_approval_and_execution(flow,st):
    cid,p=proposal(flow,st)
    st.update('cases','case_id',cid,{'identity_verified':0})
    assert not flow.erp.approve(p.rma['rma_id'],'rep.shipping','Named approval alone is insufficient').ok
    assert st.one('SELECT status FROM rmas WHERE rma_id=?',(p.rma['rma_id'],))['status']=='proposed'
    st.update('rmas','rma_id',p.rma['rma_id'],{'status':'approved'})
    assert not flow.erp.execute(p.rma['rma_id']).ok
    assert not st.query('SELECT * FROM sim_shipments WHERE case_id=?',(cid,))


def test_shipping_failure_invents_no_tracking_or_label_message(flow, st):
    cid,p=proposal(flow,st)
    assert not flow.erp.approve(p.rma['rma_id'],'rep.shipping','Failure simulation',force_failure=True).ok
    assert not st.query('SELECT * FROM sim_shipments WHERE case_id=?',(cid,))
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND metadata LIKE '%shipping_update%'",(cid,))


def test_pickup_has_three_attempts_and_no_fourth_pickup(flow, st):
    cid,p=proposal(flow,st)
    flow.erp.select_return_method(p.rma['rma_id'],'carrier_pickup','rep.shipping','Customer selected pickup')
    s=flow.erp.approve(p.rma['rma_id'],'rep.shipping','Pickup authorized').shipment
    assert s['return_method']=='carrier_pickup'
    for _ in range(3): flow.erp.advance(s['tracking_number'],'pickup_attempt_failed')
    for status in ('pickup_attempt_failed','picked_up'):
        with pytest.raises(ValueError): flow.erp.advance(s['tracking_number'],status)
    ship=flow.erp.track(s['tracking_number'])
    assert ship['pickup_attempts']==3
    assert 'reviewed alternative' in review.latest_draft(st,cid)['body']
    flow.erp.select_return_method(p.rma['rma_id'],'drop_off_label','rep.shipping','Three pickups failed; customer chose reviewed drop-off')
    alternative=flow.erp.track(s['tracking_number'])
    assert alternative['status']=='drop_off_pending' and alternative['return_method']=='drop_off_label'
    assert alternative['pickup_attempts']==3
    assert len(st.query('SELECT * FROM sim_shipments WHERE case_id=?',(cid,)))==1
    assert 'drop-off' in review.latest_draft(st,cid)['body']


def test_drop_off_and_exchange_wait_for_named_receiving_then_separate_approval(flow, st):
    cid,p=proposal(flow,st,movement.DEVICE_REPLACEMENT)
    outbound=p.rma
    inbound=st.one('SELECT * FROM rmas WHERE rma_id=?',(outbound['prerequisite_rma_id'],))
    assert inbound['call_id']==outbound['call_id']
    assert not flow.erp.approve(outbound['rma_id'],'rep.shipping','Too early').ok
    ship=flow.erp.approve(inbound['rma_id'],'rep.shipping','Return approved').shipment
    for status in ('drop_off_pending','carrier_accepted','in_transit','delivered'):
        flow.erp.advance(ship['tracking_number'],status)
        assert not flow.erp.approve(outbound['rma_id'],'rep.shipping','Still too early').ok
    with pytest.raises(ValueError): flow.erp.advance(ship['tracking_number'],'received_accepted')
    with pytest.raises(ValueError): flow.erp.receive_return(inbound['rma_id'],'','')
    flow.erp.receive_return(inbound['rma_id'],'receiving.alex','Device and serial physically checked; accepted')
    assert not st.query('SELECT * FROM sim_shipments WHERE rma_id=?',(outbound['rma_id'],))
    assert flow.erp.approve(outbound['rma_id'],'rep.shipping','Verified receipt and replacement separately authorized').ok
    assert len(st.query('SELECT * FROM erp_calls WHERE case_id=?',(cid,)))==1
    assert len(st.query('SELECT * FROM sim_shipments WHERE case_id=?',(cid,)))==2
    assert any(e['event_type']=='replacement_dispatch_blocked' for e in st.audit_trail(cid))


def test_component_shipment_has_no_whole_device_receipt_prerequisite(flow, st):
    cid,p=proposal(flow,st,movement.PART_OUTBOUND)
    assert flow.erp.approve(p.rma['rma_id'],'rep.shipping','Component authorized').ok


def test_edited_shipping_tracking_is_rejected(flow, st):
    cid,p=proposal(flow,st)
    s=flow.erp.approve(p.rma['rma_id'],'rep.shipping','Return authorized').shipment
    draft=review.latest_draft(st,cid)
    result=review.approve_and_send(st,cid,draft['message_id'],draft['body'].replace(s['tracking_number'],'SIMFX9999999999'),'rep.shipping')
    assert not result['ok']
    assert st.one('SELECT status FROM messages WHERE message_id=?',(draft['message_id'],))['status']=='draft'


def test_simulated_email_reply_preserves_case_call_and_history(flow, st):
    cid=case(flow)
    draft=review.latest_draft(st,cid)
    sent=review.approve_and_send(st,cid,draft['message_id'],'Please let us know what you observe.','rep.email')
    assert sent['ok']
    call=flow.erp.ensure_service_call(cid)
    result=workflow.add_customer_reply(st,cid,'The unit works now. Thank you.','evaluator',draft['message_id'])
    assert not result['duplicate']
    assert workflow.add_customer_reply(st,cid,'The unit works now. Thank you.','evaluator',draft['message_id'])['duplicate']
    flow.pre_investigate(cid)
    assert flow.erp.ensure_service_call(cid)['call_id']==call['call_id']
    assert st.one('SELECT status FROM messages WHERE message_id=?',(draft['message_id'],))['status']=='sent'
    assert json.loads(st.one('SELECT metadata FROM messages WHERE message_id=?',(result['message_id'],))['metadata'])['synthetic']
    assert review.latest_draft(st,cid)['status']=='draft'
    assert not st.query('SELECT * FROM rmas WHERE case_id=?',(cid,))


def test_feedback_records_original_correction_and_separate_review(flow, st):
    cid=case(flow); draft=review.latest_draft(st,cid)
    result=workflow.record_feedback(st,cid,draft['message_id'],'wording','Use one diagnostic question','The original response is too long','rep.reviewer')
    row=st.one('SELECT * FROM improvement_proposals WHERE proposal_id=?',(result['proposal_id'],))
    evidence=json.loads(row['evidence'])
    assert evidence['original_recommendation']['body']==draft['body']
    assert row['review_status']=='pending_review'
    assert review.review_proposal(st,row['proposal_id'],'approved','reviewer.jane','Approved wording example')['ok']
    assert st.one('SELECT body FROM messages WHERE message_id=?',(draft['message_id'],))['body']==draft['body']


def test_qualified_service_instruction_in_edited_email_is_blocked(flow, st):
    cid=case(flow); draft=review.latest_draft(st,cid)
    result=review.approve_and_send(st,cid,draft['message_id'],'Please open the enclosure and measure the controller voltage.','rep.email')
    assert not result['ok']
    assert st.one('SELECT status FROM messages WHERE message_id=?',(draft['message_id'],))['status']=='draft'


def test_correct_verified_equipment_fact_is_distinct_from_a_diagnosis():
    from types import SimpleNamespace
    device=SimpleNamespace(model='P2400',revision='A',serial_number='12345678',customer_name='Ivan Nowak',account_id='ACC-1')
    assert grounding.verified_record_support('Your P2400 serial number 12345678 matches the account record.',device)
    assert not grounding.verified_record_support('Your P2400 controller is defective.',device)


def test_api_requires_sent_email_and_named_receiving(flow, st, shared_agent):
    from fastapi.testclient import TestClient
    from capstone_rag.webapp.app import create_app
    cid=case(flow)
    with TestClient(create_app(store=st,agent=shared_agent,start_workers=False,prefer_llm=False)) as client:
        assert client.post(f'/api/cases/{cid}/simulated-reply-preview',json={'authorizer':'rep.test'}).status_code==400
        assert client.post('/api/rmas/nonexistent/receive',json={'authorizer':'   ','note':'Received'}).status_code==422
        assert client.post(f'/api/cases/{cid}/simulated-reply',json={'authorizer':'rep.test','text':'New observation','parent_message_id':'not-a-sent-message'}).status_code==400


def test_api_followup_export_includes_operational_and_feedback_records(flow, st, shared_agent):
    from fastapi.testclient import TestClient
    from capstone_rag.webapp.app import create_app
    cid=case(flow); draft=review.latest_draft(st,cid)
    assert review.approve_and_send(st,cid,draft['message_id'],'Please tell us what you observe.','rep.test')['ok']
    with TestClient(create_app(store=st,agent=shared_agent,start_workers=False,prefer_llm=False)) as client:
        response=client.post(f'/api/cases/{cid}/simulated-reply',json={'authorizer':'evaluator','text':'The unit works now.','parent_message_id':draft['message_id']})
        assert response.status_code==200
        payload=client.get(f'/api/trace/case/{cid}').json()
        assert payload['operational_records']['erp_calls'][0]['call_type']=='service_call'
        assert any(json.loads(m.get('metadata') or '{}').get('synthetic') for m in payload['messages'])


def test_structured_model_composition_has_no_predetermined_reply(monkeypatch, st):
    from capstone_rag import activity,generation
    captured=[]
    def local(path,payload,timeout):
        captured.append(payload)
        return {'message':{'content':json.dumps({'reply':'You reported a dark lamp. What indicator is visible?'})}}
    monkeypatch.setattr(generation,'_post',local)
    with activity.investigation(st,case_id=None):
        reply=generation.compose_customer('Compose from the reported facts.','{"customer_report":"The lamp stays dark"}')
    assert reply=='You reported a dark lamp. What indicator is visible?'
    assert captured[0]['format']['required']==['reply']
    assert not any(m['role']=='assistant' for m in captured[0]['messages'])
    assert 'decided_response' not in captured[0]['messages'][1]['content']


def test_concrete_model_fault_requires_manual_lookup_before_account_questions(bot, monkeypatch):
    from capstone_rag import generation
    monkeypatch.setattr(bot.agent,'prefer_llm',True)
    monkeypatch.setattr(generation,'llm_available',lambda:True)
    offered=[]
    def local(path,payload,timeout):
        if payload.get('tools'):
            offered.extend(t['function']['name'] for t in payload['tools'])
            return {'message':{'tool_calls':[{'function':{'name':'search_customer_manual','arguments':{}}}]}}
        return {'message':{'content':json.dumps({'reply':'Your P2400 powers on but the lamps stay dark. Please read the visible lamp-start indicator and any message on the display. Let me know what you observe.'})}}
    monkeypatch.setattr(generation,'_post',local)
    reply=bot.respond(bot.new_session(),'My P2400 powers on but all lamps remain dark. What should I check next?')
    assert 'search_customer_manual' in offered and 'request_information' not in offered
    assert reply.sources
    assert 'name on the account' not in reply.text.lower() and 'date of birth' not in reply.text.lower()


def test_public_manual_scope_rejects_unnecessary_account_verification():
    from capstone_rag import response_policy
    text='Could you share the name on the account and date of birth?'
    assert not response_policy.validate(text,ordinary_public_help=True)['passed']
    assert response_policy.validate(text,ordinary_public_help=False)['passed']


def test_manual_reply_must_not_reask_an_explicitly_received_observation():
    from capstone_rag import response_policy
    report='My P2400 powers on but all lamps remain dark.'
    assert response_policy.conversation_consistency('Note whether all lamps or only one area is affected.',report)
    assert not response_policy.conversation_consistency('What does the lamp-start indicator show?',report)
    assert not response_policy.reported_observations('Not all lamps stay dark; only one area is affected.')['all_lamps_affected']
