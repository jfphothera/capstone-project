"""Simulated correspondence and service workflow on the existing operational store."""
import hashlib
import json
import re

from . import activity, generation, store as store_mod


def replay_reviewed_feedback(store, agent, proposal_id, authorizer):
    """Run one reviewed example through the existing bounded writer/validator.

    This is a case-local replay, never a QMS edit or held-out-label mutation.
    """
    from . import identity, qms_policy, support_workflow
    from .react import ReasoningLoop
    from types import SimpleNamespace
    proposal = store.one("SELECT * FROM improvement_proposals WHERE proposal_id=? AND kind='representative_feedback'", (proposal_id,))
    if not proposal or proposal['review_status'] != 'approved':
        raise ValueError('Only an approved reviewed correction can be replayed')
    evidence = json.loads(proposal['evidence'])
    cid = evidence['case_id']; case = store.case(cid)
    question = '\n'.join(m['body'] for m in store.messages(cid) if m['direction'] == 'inbound')
    with activity.investigation(store, case_id=cid):
        revision_authorized = identity.case_revision_authorized(store, case)
        safety_hold = bool(case.get('is_adverse_event') or case.get('priority') == 'Critical' or case.get('safety_hold'))
        if safety_hold:
            raise ValueError('Safety hold blocks ordinary response replay; qualified review is required')
        device_row=store.device(case.get('serial_number')) if case.get('serial_number') else None
        context={'model':case.get('model'),'revision':case.get('revision'),
                 'revision_context':identity.revision_context(
                     device_row,
                     case.get('revision_confirmed_by') if revision_authorized else None),
                 'safety_hold':safety_hold,'compatibility':agent.toolbox.compatibility}
        used={row['chunk_id'] for row in evidence.get('sources',[]) if row.get('status')=='USED'}
        accepted=[c for c in agent.toolbox.index.chunks if c.chunk_id in used and qms_policy.authorize(c,operation='customer_answer',recipient='customer',context=context).allowed]
        manual = ({'chunks':accepted,'procedure':accepted[0].procedure_id,'context':context} if accepted else
                  support_workflow.customer_manual_search(
                      agent.toolbox, question, case.get('model'), case.get('revision'),
                      revision_authorized, device=device_row,
                      revision_context=context['revision_context']))
        if not manual['chunks']:
            raise ValueError('No applicable authorized public source is available for a response replay')
        device = SimpleNamespace(model=case.get('model'), revision=case.get('revision'), serial_number=case.get('serial_number'),
            customer_name=case.get('customer_name') if case.get('identity_verified') else 'there', account_id=None)
        loop=ReasoningLoop(agent.toolbox,prefer_llm=agent.prefer_llm)
        gen,report=loop.write_verified_reply(question,device,manual['chunks'],manual['procedure'],
            memory_note='Approved representative wording feedback for this example only: '+evidence['human_correction'], public_context=True)
        if not gen or not report.grounded or not qms_policy.disclosure_check(gen.text,manual['chunks'],context=manual['context'])['passed']:
            raise ValueError('Replay failed authorization or grounding; no customer message was sent')
        mid=f"{cid}-REPLAY-{store.next_id('message','{:05d}')}"
        store.insert('messages',{'message_id':mid,'case_id':cid,'created_at':store_mod.now(),'direction':'internal',
            'channel':'review_replay','author':authorizer,'subject':f'Reviewed replay {proposal_id}','body':gen.text,
            'status':'recorded','metadata':json.dumps({'kind':'reviewed_replay','proposal_id':proposal_id,'generation_mode':gen.mode,'grounded':report.grounded})})
        store.audit('reviewed_example_replayed',actor=authorizer,case_id=cid,summary=f'{proposal_id} replayed; {gen.mode}; not sent',
            detail={'original':evidence['original_recommendation'],'correction':evidence['human_correction'],'replay_message_id':mid,'response':gen.text,'grounding':report.as_dict()})
        return {'ok':True,'text':gen.text,'generation_mode':gen.mode,'message_id':mid,'sent':False}

FEEDBACK_CATEGORIES = ('classification', 'priority', 'qms_source', 'wording',
                       'identity_device', 'unnecessary_escalation', 'movement_shipping', 'other')


def record_carrier_event(store, erp, tracking, status, location='', note=''):
    """Record the actual simulated scan, then prepare its separately reviewed notice."""
    shipment=erp.advance(tracking,status,location=location,note=note)
    draft=None
    if shipment and shipment.get('case_id') and status in ('in_transit','picked_up','pickup_attempt_failed','delivered'):
        milestone=f"{status}:{shipment.get('pickup_attempts') or 1}"
        draft=shipping_draft(store,tracking,milestone)
    return {'shipment':shipment,'acknowledgement_draft':draft}

def shipping_draft(store, tracking, milestone):
    from . import qms, qms_policy
    controls = [c for c in qms.load_chunks() if c.document_id == 'QMS-DOC-0307']
    public = [c for c in qms.load_chunks() if c.document_id == 'QMS-DOC-0207']
    if not controls or not public or not all(qms_policy.authorize(c, operation='internal_workflow', recipient='internal').allowed for c in controls):
        raise ValueError('Controlled return-shipping instructions are unavailable; communication requires review')
    if not all(qms_policy.authorize(c, operation='customer_answer', recipient='customer').allowed for c in public):
        raise ValueError('Customer shipping guidance is not authorized')
    ship = store.one('SELECT * FROM sim_shipments WHERE tracking_number=?', (tracking,))
    if not ship:
        return None
    rma = store.one('SELECT * FROM rmas WHERE rma_id=?', (ship['rma_id'],))
    case = store.case(ship['case_id'])
    key = f'shipping:{ship["shipment_id"]}:{milestone}'
    existing = store.one('SELECT message_id FROM messages WHERE idempotency_key=?', (key,))
    if existing:
        return existing['message_id']
    inbound = ship['direction'] == 'inbound'
    if milestone == 'received_accepted':
        detail = 'Our simulated receiving team has confirmed that the returned device was received and accepted. Any linked replacement must now be separately reviewed and authorized.'
    elif ship['status'] == 'delivered':
        detail = ('The simulated carrier records delivery of the return. Our receiving team still needs to confirm physical receipt and acceptance.' if inbound else
                  'The simulated carrier records delivery of your shipment. Please let us know whether the contents arrived as expected.')
    elif ship['status'] == 'pickup_attempt_failed':
        attempts = ship.get('pickup_attempts') or 0
        detail = (f'The simulated pickup attempt was unsuccessful ({attempts} of 3 attempts). ' +
                  ('Please contact support to arrange a reviewed alternative return method.' if attempts >= 3 else
                   'Please keep the packaged device ready for the next simulated pickup attempt.'))
    elif ship['status'] == 'picked_up':
        detail = 'The simulated carrier has confirmed pickup of your packaged device. This does not yet mean our receiving team has received it.'
    elif ship['status'] == 'in_transit':
        detail = 'The simulated carrier records your shipment in transit. Delivery and receiving acceptance have not yet been confirmed.'
    elif inbound and ship.get('return_method') == 'carrier_pickup':
        detail = ('A simulated carrier pickup is arranged, allowing up to three attempts. Package the device securely in its original box. '
                  'Keep it indoors and hand it directly to the simulated carrier at the arranged pickup; do not leave it unattended outside.')
    elif inbound:
        detail = ('Your simulated return label is ready. Package the device securely in its original box, attach the label, '
                  'and take it to a simulated carrier drop-off location. Label creation does not mean the carrier has accepted the package.')
    else:
        items = ', '.join(i.get('name', 'item') for i in json.loads(ship['contents'] or '[]'))
        detail = f'A simulated outbound shipping label has been created for {items}. Carrier pickup and delivery are not yet confirmed.'
    linked = store.query('SELECT rma_id FROM rmas WHERE prerequisite_rma_id=?', (rma['rma_id'],))
    if linked and milestone != 'received_accepted':
        detail += ' A replacement device will be considered only after our receiving team confirms receipt and acceptance of the original, followed by separate authorization.'
    body = (f"Hello {case.get('customer_name') or 'there'},\n\n{detail}\n\n"
            f"Simulated tracking number: {tracking}.\nLabel reference: {ship['shipment_id']}.\n\n"
            'This is a local shipping simulation; no real carrier has been contacted.\n\nKind regards,\nCustomer Support')
    mid = f"{case['case_id']}-SHIP-{store.next_id('draft', '{:05d}')}"
    superseded = store.query("SELECT message_id FROM messages WHERE case_id=? AND direction='outbound' AND channel!='qa_notification' AND status='draft'", (case['case_id'],))
    for earlier in superseded:
        store.update('messages','message_id',earlier['message_id'],{'status':'superseded'})
    if superseded:
        store.audit('drafts_superseded_by_shipping_update',actor='agent',case_id=case['case_id'],
            summary=f'{len(superseded)} earlier drafts retained as superseded',detail={'earlier':superseded,'new_message_id':mid})
    store.insert('messages', {'message_id': mid, 'case_id': case['case_id'], 'run_id': case.get('run_id'),
        'created_at': store_mod.now(), 'direction': 'outbound', 'channel': 'email', 'author': 'agent',
        'subject': f"Shipping update: {case['subject']}", 'body': body, 'status': 'draft',
        'idempotency_key': key, 'metadata': json.dumps({'kind': 'shipping_update', 'shipment_id': ship['shipment_id'],
            'milestone': milestone, 'recorded_status': ship['status'], 'procedure': 'PROC-OPS-RETURN-SHIPPING',
            'generation_mode': 'recorded_facts', 'requires_email_approval': True})})
    store.audit('shipping_communication_drafted', actor='agent', case_id=case['case_id'],
        summary=f'{mid}: recorded tracking {tracking}; awaiting email approval',
        detail={'message_id': mid, 'shipment_id': ship['shipment_id'], 'tracking': tracking, 'milestone': milestone,
                'procedure': 'PROC-OPS-RETURN-SHIPPING'})
    return mid


def validate_shipping_message(store, message, body):
    meta = json.loads(message.get('metadata') or '{}')
    if meta.get('kind') != 'shipping_update':
        return {'passed': True, 'failures': []}
    ship = store.one('SELECT * FROM sim_shipments WHERE shipment_id=? AND case_id=?', (meta['shipment_id'], message['case_id']))
    failures = []
    if not ship or ship['tracking_number'] not in body or ship['shipment_id'] not in body:
        failures.append('Retain the exact recorded tracking number and label reference')
    if ship and ship['status'] == 'label_created' and re.search(r'(?<!not )(?<!not yet )\b(?:has shipped|was picked up|has been delivered|replacement dispatched)\b', body, re.I):
        failures.append('Label creation is not pickup, dispatch or delivery')
    allowed = {s['tracking_number'] for s in store.query('SELECT tracking_number FROM sim_shipments WHERE case_id=?', (message['case_id'],))}
    if set(re.findall(r'SIMFX\d+', body)) - allowed:
        failures.append('Unknown tracking identifier in customer communication')
    return {'passed': not failures, 'failures': failures}


def last_sent(store, case_id):
    return store.one("SELECT * FROM messages WHERE case_id=? AND direction='outbound' AND channel='email' AND status='sent' ORDER BY created_at DESC LIMIT 1", (case_id,))


def preview_customer_reply(store, case_id, authorizer, prefer_llm=True):
    """The fictional customer sees only their messages and the actual sent email."""
    sent = last_sent(store, case_id)
    if not sent:
        raise ValueError('Send an approved simulated email before generating a customer reply')
    customer_messages = [m['body'] for m in store.messages(case_id) if m['direction'] == 'inbound']
    with activity.investigation(store, case_id=case_id):
        activity.emit('simulation', 'Simulated customer reply requested', 'running', {'parent_message_id': sent['message_id'], 'representative': authorizer})
        mode = 'template'
        text = 'Thank you for your reply. Could you clarify what information you need from me next?'
        if prefer_llm and generation.llm_available():
            text = generation.compose_customer(
                'Compose a fictional customer email replying to the actual support email. Use only the customer history and sent email. '
                'Add one plausible observation or question. Do not claim to know internal decisions, QMS, ERP or approvals. '
                'Do not invent a tracking number, device identity or serious injury. Under 90 words. Only the reply. End with ---END---.',
                json.dumps({'customer_history': customer_messages[-4:], 'support_email': sent['body']}))
            if not text.strip() or len(text.split()) > 110 or re.search(r'QMS|ERP|PROC-|QMS-DOC-|internal approval', text, re.I):
                raise ValueError('Generated simulated customer reply failed context validation; enter a manual reply')
            mode = 'llm'
        known_tracking=set(re.findall(r'SIMFX\d+',sent['body']))
        if set(re.findall(r'SIMFX\d+',text))-known_tracking:
            raise ValueError('Simulated customer reply invented a tracking number')
        activity.emit('simulation', 'Simulated customer reply preview prepared; not yet added to case', evidence={'text': text, 'generation_mode': mode, 'synthetic': True})
        return {'text': text, 'generation_mode': mode, 'synthetic': True, 'parent_message_id': sent['message_id']}


def add_customer_reply(store, case_id, text, authorizer, parent_message_id, continue_investigation=True):
    if not authorizer.strip() or not text.strip():
        raise ValueError('A named evaluator and reply text are required')
    sent = store.one("SELECT * FROM messages WHERE message_id=? AND case_id=? AND direction='outbound' AND channel='email' AND status='sent'", (parent_message_id, case_id))
    if not sent:
        raise ValueError('The simulated reply must respond to an approved sent email in this case')
    key = hashlib.sha256((case_id + parent_message_id + text.strip()).encode()).hexdigest()
    existing = store.one('SELECT message_id FROM messages WHERE idempotency_key=?', (key,))
    if existing:
        return {'message_id': existing['message_id'], 'duplicate': True}
    mid = f"{case_id}-SIM-IN-{store.next_id('message', '{:05d}')}"
    case = store.case(case_id)
    store.insert('messages', {'message_id': mid, 'case_id': case_id, 'created_at': store_mod.now(),
        'run_id': case.get('run_id'), 'direction': 'inbound', 'channel': 'email', 'author': case.get('customer_name') or 'Simulated customer',
        'subject': f"Re: {case['subject']}", 'body': text.strip(), 'status': 'received',
        'parent_message_id': parent_message_id, 'idempotency_key': key,
        'metadata': json.dumps({'synthetic': True, 'evaluator': authorizer, 'account_id': case.get('account_id'), 'kind': 'simulated_customer_reply',
                     'recorded_only': not continue_investigation})})
    from . import caseflow, triage
    before = case['status']
    result = triage.triage(text)
    # rc.27: 'Record reply only' keeps the reply on the same case for the representative (no queued pass); the default
    # (record and continue investigation) is unchanged. Safety triage below runs either way.
    after = caseflow.NEW if continue_investigation else caseflow.HUMAN_REVIEW
    store.update('cases', 'case_id', case_id, {'status': after, 'ai_state': caseflow.QUEUED if continue_investigation else caseflow.COMPLETED,
        'closed_at':None,'resolved':0,'updated_at': store_mod.now()})
    for call in store.query("SELECT call_id FROM erp_calls WHERE case_id=? AND call_type='service_call'",(case_id,)):
        store.update('erp_calls','call_id',call['call_id'],{'service_status':'active_followup'})
    if triage.requires_immediate_escalation(result):
        store.update('cases', 'case_id', case_id, {'priority': triage.CRITICAL})
        caseflow.CaseFlow(store)._raise_quality_notification(case_id, result)
    # Supersede stale drafts explicitly; they remain in history and cannot be sent.
    for draft in store.query("SELECT message_id FROM messages WHERE case_id=? AND status='draft' AND direction='outbound'", (case_id,)):
        store.update('messages', 'message_id', draft['message_id'], {'status': 'superseded'})
    store.audit('simulated_customer_reply_received', actor=authorizer, case_id=case_id,
        summary=f'{mid}: ' + ('same case reopened for follow-up investigation' if continue_investigation else 'reply recorded for the representative (no investigation pass)'),
        detail={'synthetic': True, 'parent_message_id': parent_message_id, 'before': before, 'after': after, 'continue_investigation': continue_investigation})
    return {'message_id': mid, 'duplicate': False, 'synthetic': True, 'continue_investigation': continue_investigation}


def record_feedback(store, case_id, target_id, category, correction, explanation, authorizer):
    if category not in FEEDBACK_CATEGORIES or not all(x.strip() for x in (correction, explanation, authorizer)):
        raise ValueError('Select a feedback category and provide correction, explanation and representative')
    original = store.one('SELECT * FROM messages WHERE case_id=? AND message_id=?', (case_id, target_id))
    original = original or store.one('SELECT * FROM checklist_items WHERE case_id=? AND item_id=?', (case_id, target_id))
    if not original:
        raise ValueError('Select a specific decision or response from this case')
    pid = store.next_id('proposal', 'IMP-{:06d}')
    sources = store.query('SELECT * FROM retrieved_sources WHERE case_id=?', (case_id,))
    evidence = {'case_id': case_id, 'target_id': target_id, 'original_recommendation': original,
        'human_correction': correction, 'explanation': explanation, 'sources': sources,
        'representative': authorizer, 'at': store_mod.now(), 'category': category}
    store.insert('improvement_proposals', {'proposal_id': pid, 'created_at': store_mod.now(),
        'kind': 'representative_feedback', 'trigger_summary': f'{case_id}: {category}',
        'body': correction, 'evidence': json.dumps(evidence), 'review_status': 'pending_review'})
    store.audit('representative_feedback_recorded', actor=authorizer, case_id=case_id,
        summary=f'{pid}: {category}; pending review, no automatic rule or QMS change', detail=evidence)
    return {'ok': True, 'proposal_id': pid}
