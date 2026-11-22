"""Representative actions: approvals, corrections, regeneration, closure.

Every function here is something a named person did, so every function
writes an audit event with that person's name on it. Nothing overwrites
history: a corrected recommendation keeps its original text, a rejected
draft stays in the record as rejected, and closing a case without resolving
it is recorded as exactly that rather than tidied away.

The regeneration path is the Reflexion loop with a human in it. The
representative's written feedback is fed back to the agent the same way the
automatic grounding failure is, the new draft is verified by the same code,
and if it still cannot be verified the decision stays with the person
instead of being retried forever.
"""

from __future__ import annotations

import json
from typing import Optional

from . import caseflow, lab_guards, store as store_mod


# --- drafts ----------------------------------------------------------------

def latest_draft(store, case_id: str) -> Optional[dict]:
    return store.one(
        "SELECT * FROM messages WHERE case_id=? AND direction='outbound' "
        "AND channel!='qa_notification' AND status='draft' "
        "ORDER BY created_at DESC LIMIT 1", (case_id,))


def _sync_response_decision(store, case_id, message_id, decision, authorizer, note):
    for item in store.checklist(case_id):
        if item['action'] != 'prepare_response' or item.get('approval_decision'):
            continue
        result = json.loads(item.get('tool_result') or '{}')
        if result.get('message_id') == message_id:
            store.update('checklist_items','item_id',item['item_id'],{'approval_decision':decision,
                'authorizer':authorizer,'decided_at':store_mod.now(),'feedback':note,
                'consequence':f'Associated customer draft {decision}; sending remains separately recorded'})


def approve_and_send(store, case_id: str, message_id: str, body: str,
                     authorizer: str, review_override: str = '') -> dict:
    with store.transaction():
        result = _approve_and_send(store,case_id,message_id,body,authorizer,review_override)
    _settle_reviews(store, case_id)
    return result


def _settle_reviews(store, case_id):
    try:
        from . import review_requests
        review_requests.settle(store, case_id)
    except Exception:
        pass


def _approve_and_send(store, case_id: str, message_id: str, body: str,
                     authorizer: str, review_override: str = '') -> dict:
    """Send the customer reply. Only a representative can do this."""
    if not authorizer.strip():return {'ok':False,'reason':'A named representative is required'}
    original = store.one("SELECT * FROM messages WHERE message_id=?",
                         (message_id,))
    if original is None:
        return {"ok": False, "reason": f"no message {message_id}"}
    if original['case_id']!=case_id:return {'ok':False,'reason':'Message does not belong to this case'}
    if original["status"] != "draft":
        return {"ok": False, "duplicate": True,
                "reason": f"{message_id} is already {original['status']}"}

    # Strip the internal note before anything goes to the customer.
    clean = body.split("[For representative:")[0].rstrip()
    from . import response_policy
    facts = response_policy.case_facts(store, case_id)
    verdict = response_policy.validate_case(store, case_id, clean)
    from .operations_workflow import validate_shipping_message
    shipping_verdict = validate_shipping_message(store, original, clean)
    verdict['failures'].extend(shipping_verdict['failures'])
    procedural = response_policy.validate_procedural_edit(store,case_id,original,clean)
    verdict['failures'].extend(procedural['failures'])
    verdict['passed'] = not verdict['failures']
    if not verdict['passed']:
        store.audit('draft_consistency_rejected', actor=authorizer, case_id=case_id,
                    summary='Sending blocked: reply contradicts current case facts',
                    detail={'draft': clean, 'validation': verdict, 'facts': facts})
        return {'ok': False, 'reason': 'Reply conflicts with confirmed case details; please revise it.',
                'validation': verdict}
    independent = store.one('SELECT * FROM response_reviews WHERE message_id=? ORDER BY at DESC LIMIT 1',(message_id,))
    artifact_validation = store.one(
        'SELECT * FROM artifact_validation_records WHERE artifact_id=? ORDER BY cycle DESC,at DESC LIMIT 1',
        (message_id,))
    if artifact_validation and artifact_validation['disposition'] != 'pass':
        return {'ok':False,
            'reason':'Independent whole-artifact validation has unresolved blocking issues; rework or human escalation must complete before sending.',
            'validation_record':artifact_validation}
    try:
        revision_meta = (json.loads(original.get('metadata') or '{}') or {}).get('revision')
    except (TypeError, ValueError):
        revision_meta = None
    if artifact_validation is None and revision_meta:
        # rc.25 review round 2: an explicit revision is validated on its own
        # frozen package. Without that record it fails closed - it never
        # borrows a predecessor's pass.
        store.audit('send_blocked', actor=authorizer, case_id=case_id,
                    summary='Send blocked: the revised draft has no validation record of its own',
                    detail={'message_id': message_id, 'revision_id': revision_meta.get('revision_id')})
        return {'ok': False, 'reason': 'This revised draft has no independent validation of its own; sending stays blocked until it is validated or a human reviews it.',
                'validation_record': None}
    if artifact_validation is None:
        # A regenerated or re-investigated draft has no validation of its own.
        # It may not be used to get around an earlier version's unresolved
        # failure: walk the predecessor chain and refuse if any version's latest
        # whole-artifact validation is not a pass.
        parent, seen = original.get('parent_message_id'), set()
        while parent and parent not in seen:
            seen.add(parent)
            prior = store.one('SELECT * FROM artifact_validation_records WHERE artifact_id=? ORDER BY cycle DESC,at DESC LIMIT 1',(parent,))
            if prior and prior['disposition'] != 'pass':
                store.audit('regenerated_draft_blocked', actor=authorizer, case_id=case_id,
                            summary='Send blocked: an earlier version of this reply failed independent validation',
                            detail={'message_id':message_id,'failed_predecessor':parent,'disposition':prior['disposition']})
                return {'ok':False,'reason':'An earlier version of this reply failed independent validation and that failure is unresolved; it needs human review, not a regenerated draft.',
                        'validation_record':prior}
            row = store.one('SELECT parent_message_id FROM messages WHERE message_id=?',(parent,))
            parent = row['parent_message_id'] if row else None
    if independent and not independent['passed']:
        if not review_override.strip():
            return {'ok':False,'reason':'Independent review raised a concern. Request a revision or record a named review rationale before sending.','review':json.loads(independent['payload'])}
        store.audit('independent_review_human_override',actor=authorizer,case_id=case_id,
            summary='Representative reviewed independent model concern; deterministic checks still passed',
            detail={'message_id':message_id,'review_id':independent['review_id'],'rationale':review_override})
    edited = clean.strip() != (original["body"] or "").split(
        "[For representative:")[0].rstrip().strip()

    now = store_mod.now()
    store.update("messages", "message_id", message_id, {
        "body": clean, "status": "sent", "approved_by": authorizer,
        "approved_at": now})
    store.insert("approvals", {
        "approval_id": store.next_id("approval", "APV-{:06d}"),
        "case_id": case_id, "run_id": None, "at": now,
        "subject_type": "customer_message", "subject_id": message_id,
        "decision": "approved", "authorizer": authorizer,
        "note": "edited before sending" if edited else "sent as drafted"})
    # A sent acknowledgement never hides unresolved safety work: a held case
    # stays in the human review queue until a recorded determination clears it.
    held = bool((store.case(case_id) or {}).get('safety_hold'))
    store.update("cases", "case_id", case_id,
                 {"status": caseflow.HUMAN_REVIEW if held else caseflow.AWAITING, "updated_at": now})
    store.audit("customer_message_sent", actor=authorizer, case_id=case_id,
                summary=f"{message_id} sent"
                        f"{' (edited by representative)' if edited else ''}",
                detail={"edited": edited})
    _sync_response_decision(store,case_id,message_id,'approved',authorizer,'Approved and sent to simulated outbox')
    return {"ok": True, "edited": edited}


def reject_draft(store, case_id: str, message_id: str, authorizer: str,
                 note: str = "") -> dict:
    store.update("messages", "message_id", message_id,
                 {"status": "rejected", "approved_by": authorizer,
                  "approved_at": store_mod.now()})
    store.insert("approvals", {
        "approval_id": store.next_id("approval", "APV-{:06d}"),
        "case_id": case_id, "run_id": None, "at": store_mod.now(),
        "subject_type": "customer_message", "subject_id": message_id,
        "decision": "rejected", "authorizer": authorizer, "note": note})
    store.audit("customer_message_rejected", actor=authorizer,
                case_id=case_id, summary=f"{message_id} rejected: {note}")
    _sync_response_decision(store,case_id,message_id,'rejected',authorizer,note)
    _settle_reviews(store, case_id)
    return {"ok": True}


def regenerate_reply(ops, case_id: str, message_id: str, feedback: str,
                     authorizer: str, objections=None, idempotency_key=None) -> dict:
    """Representative-requested correction (rc.25: explicit revision).

    The existing draft is revised against its own frozen authorized evidence
    (``revision.revise``). The reviewer's text is an instruction, never a new
    customer request, and the replacement keeps parent linkage, provenance and
    its own validation record. ``ok`` is True only for a validated replacement.
    """
    from . import activity, revision
    with activity.investigation(ops.store, case_id=case_id):
        activity.emit('regeneration', 'Representative requested a corrected draft', 'retrying',
                      {'message_id': message_id, 'feedback': feedback, 'authorizer': authorizer})
        result = revision.revise(ops.store, case_id, message_id, objections=objections, feedback=feedback,
                                 authorizer=authorizer, authority='human_representative', idempotency_key=idempotency_key,
                                 prefer_llm=getattr(getattr(ops, 'flow', None) and ops.flow.agent, 'prefer_llm', True))
        result.setdefault('summary', result.get('reason') or result.get('outcome'))
        return result


# --- checklist items -------------------------------------------------------

def decide_item(store, item_id: str, decision: str, authorizer: str,
                feedback: str = "") -> dict:
    """Approve or reject one finding, not the whole case.

    A decision here is not only a note. Where the finding authorised or
    blocked something downstream, that consequence is applied in the same
    call and reported back, so the representative can see what their decision
    did rather than guessing (defect C8).
    """
    item = store.one("SELECT * FROM checklist_items WHERE item_id=?",
                     (item_id,))
    if item is None:
        return {"ok": False, "reason": "no such item"}
    if item["approval_decision"]:
        return {"ok": False, "duplicate": True,
                "reason": f"already {item['approval_decision']}"}

    now = store_mod.now()
    consequence = _apply_consequence(store, item, decision, authorizer,
                                     feedback)
    store.update("checklist_items", "item_id", item_id, {
        "approval_decision": decision, "authorizer": authorizer,
        "decided_at": now, "feedback": feedback or None,
        "consequence": consequence or None})
    store.insert("approvals", {
        "approval_id": store.next_id("approval", "APV-{:06d}"),
        "case_id": item["case_id"], "run_id": item["run_id"], "at": now,
        "subject_type": "checklist_item", "subject_id": item_id,
        "decision": decision, "authorizer": authorizer, "note": feedback})
    store.audit(f"checklist_item_{decision}", actor=authorizer,
                case_id=item["case_id"],
                summary=f"{item['action']} {decision}"
                        f"{': ' + feedback if feedback else ''}"
                        f"{' | ' + consequence if consequence else ''}",
                detail={"item_id": item_id, "consequence": consequence})

    if decision == "rejected" and feedback:
        _consider_improvement(store, item, feedback)
    return {"ok": True, "consequence": consequence}


def _apply_consequence(store, item, decision, authorizer, feedback) -> str:
    """Carry out whatever the decision on this finding actually changes."""
    from . import erp_sim

    action = item["action"]
    case_id = item["case_id"]

    if action == "determine_product_movement":
        rma = store.one(
            "SELECT * FROM rmas WHERE case_id=? AND status=? "
            "ORDER BY created_at DESC LIMIT 1", (case_id, erp_sim.PROPOSED))
        if rma is None:
            return ""
        erp = erp_sim.ErpSimulator(store)
        if decision == "rejected":
            res = erp.cancel(
                rma["rma_id"], authorizer,
                note=feedback or "movement recommendation rejected by "
                                 "representative")
            return (f"{rma['rma_id']} canceled - it can no longer be "
                    f"approved or executed" if res.ok else res.reason)
        # Approving the finding records agreement with the recommendation.
        # It deliberately does not execute: the movement still has to be
        # authorised on the RMA control, which is the single real gate.
        return (f"{rma['rma_id']} still requires authorisation on the "
                f"movement panel before anything is created")

    if action == "verify_identity" and decision == "approved":
        store.update("cases", "case_id", case_id, {
            "identity_verified": 1, "updated_at": store_mod.now()})
        store.audit("identity_attested_by_representative", actor=authorizer,
                    case_id=case_id,
                    summary="representative attested the customer identity "
                            "that automatic verification could not confirm",
                    detail={"item_id": item["item_id"],
                            "note": feedback,
                            # rc.25 (C4): a named human attestation and an
                            # autonomous reviewer's recorded two-point match
                            # are different evidence and stay distinguishable.
                            "basis": ("recorded_two_point_customer_erp_match"
                                      if lab_guards.autonomous(authorizer)
                                      else "named_human_attestation")})
        return "identity recorded as attested by the representative"

    if action == "prepare_response" and decision == "rejected":
        draft = latest_draft(store, case_id)
        if draft:
            reject_draft(store, case_id, draft["message_id"], authorizer,
                         note=feedback or "response finding rejected")
            return f"draft {draft['message_id']} rejected; nothing was sent"
        return "no draft was outstanding"

    if action == "screen_adverse_event" and decision == "rejected":
        return ("recorded as challenged; use the adverse-event determination "
                "control to change the classification")

    return ""


def _consider_improvement(store, item, feedback):
    """Repeated corrections of the same step become a reviewable proposal.

    A proposal never changes anything by itself. It is queued for review, and
    controlled QMS instructions stay authoritative regardless of what any
    proposal says.
    """
    similar = store.query(
        "SELECT * FROM checklist_items WHERE action=? AND "
        "approval_decision='rejected'", (item["action"],))
    if len(similar) < 3:
        return None
    existing = store.one(
        "SELECT * FROM improvement_proposals WHERE kind='agent_behaviour' "
        "AND trigger_summary=? AND review_status='pending_review'",
        (item["action"],))
    if existing:
        return existing["proposal_id"]
    pid = store.next_id("proposal", "IMP-{:05d}")
    store.insert("improvement_proposals", {
        "proposal_id": pid, "created_at": store_mod.now(),
        "kind": "agent_behaviour", "trigger_summary": item["action"],
        "body": (f"The step '{item['action']}' has been corrected by a "
                 f"representative {len(similar)} times. The most recent "
                 f"correction was: {feedback}"),
        "evidence": json.dumps([s["item_id"] for s in similar][-5:]),
        "review_status": "pending_review", "reviewed_by": None,
        "reviewed_at": None})
    store.audit("improvement_proposed", actor="system",
                case_id=item["case_id"],
                summary=f"{pid} raised after {len(similar)} corrections of "
                        f"{item['action']}")
    return pid


# --- case level ------------------------------------------------------------

def override_priority(store, case_id: str, priority: str, reason: str,
                      authorizer: str) -> dict:
    case = store.case(case_id)
    if case is None:
        return {"ok": False}
    store.update("cases", "case_id", case_id, {
        "priority": priority,
        "priority_reason": f"overridden by {authorizer}: {reason}",
        "priority_overridden_by": authorizer,
        "updated_at": store_mod.now()})
    store.audit("priority_overridden", actor=authorizer, case_id=case_id,
                summary=f"{case['priority']} -> {priority}: {reason}",
                detail={"from": case["priority"], "to": priority,
                        "reason": reason,
                        "original_reason": case["priority_reason"]})
    return {"ok": True}


def assign(store, case_id: str, assignee: str, authorizer: str) -> dict:
    store.update("cases", "case_id", case_id,
                 {"assigned_to": assignee, "updated_at": store_mod.now()})
    store.audit("case_assigned", actor=authorizer, case_id=case_id,
                summary=f"assigned to {assignee}")
    return {"ok": True}


def close_case(store, case_id: str, authorizer: str, note: str = "",
               resolved: bool = True) -> dict:
    """Close the case. An unresolved closure stays visible in reporting.

    rc.25 (C5): a held safety report is never closed as routinely resolved,
    and is closed unresolved only after its QA determination and escalation.
    """
    from .lab_guards import closure
    allowed, why = closure(store, case_id, authorizer, resolved)
    if not allowed:
        store.audit("case_closure_refused", actor=authorizer, case_id=case_id, summary=why[:240],
                    detail={"resolved": resolved})
        return {"ok": False, "reason": why, "closure_refused": True}
    now = store_mod.now()
    store.update("cases", "case_id", case_id, {
        "status": caseflow.RESOLVED if resolved else caseflow.CLOSED,
        "closed_at": now, "updated_at": now,
        "resolution": note or ("resolved" if resolved
                               else "closed without resolution"),
        "resolved": 1 if resolved else 0})
    store.audit("case_closed", actor=authorizer, case_id=case_id,
                summary=("resolved" if resolved
                         else "CLOSED WITHOUT RESOLUTION") + f": {note}",
                detail={"resolved": resolved})
    from .erp_sim import ErpSimulator
    call=ErpSimulator(store).ensure_service_call(case_id)
    store.update('erp_calls','call_id',call['call_id'],{'service_status':'resolved' if resolved else 'closed_unresolved'})
    _settle_reviews(store, case_id)
    return {"ok": True}


def review_proposal(store, proposal_id: str, decision: str, authorizer: str,
                    note: str = "") -> dict:
    """Approve or reject a reusable improvement proposal.

    An approved proposal is a note to the operation, not a change to the
    controlled documentation. Nothing here can alter a QMS document, a
    compatibility mapping or a revision record - the deterministic checks
    stay authoritative whatever a proposal says, and that is enforced by the
    proposal having no write path to them at all.
    """
    if decision not in ("approved", "rejected"):
        return {"ok": False, "reason": "decision must be approved or rejected"}
    row = store.one(
        "SELECT * FROM improvement_proposals WHERE proposal_id=?",
        (proposal_id,))
    if row is None:
        return {"ok": False, "reason": f"no proposal {proposal_id}"}
    if row["review_status"] != "pending_review":
        return {"ok": False, "duplicate": True,
                "reason": f"{proposal_id} is already {row['review_status']}"}
    now = store_mod.now()
    store.update("improvement_proposals", "proposal_id", proposal_id, {
        "review_status": decision, "reviewed_by": authorizer,
        "reviewed_at": now})
    store.audit(f"improvement_proposal_{decision}", actor=authorizer,
                case_id=(json.loads(row.get('evidence') or '{}').get('case_id') if row['kind']=='representative_feedback' else None),
                summary=f"{proposal_id} {decision}: {note}",
                detail={"trigger": row["trigger_summary"], "note": note, 'before': row['review_status'], 'after': decision})
    return {"ok": True, "proposal_id": proposal_id, "decision": decision,
            "applies_to_controlled_documents": False}


def determine_adverse_event(store, notification_id: str, determination: str,
                            authorizer: str, rationale: str = '') -> dict:
    """Record the human decision on an uncertain adverse event."""
    states = {
        'confirmed adverse event': (1, 'confirmed', 'adverse_event_confirmed'),
        'suspected adverse event': (1, 'suspected', 'adverse_event_suspected'),
        'device hazard without confirmed injury': (0, 'none', 'device_safety_hazard'),
        'not an adverse event': (0, 'none', 'complaint_malfunction'),
        'insufficient information': (0, 'suspected', 'adverse_event_suspected'),
    }
    if determination not in states:
        raise ValueError('Select a structured safety determination.')
    if len(rationale.strip()) < 3:
        raise ValueError('Provide a supporting rationale separately from the structured determination.')
    original = store.one('SELECT * FROM qa_notifications WHERE notification_id=?', (notification_id,))
    if not original:
        raise ValueError('Notification not found')
    store.update("qa_notifications", "notification_id", notification_id, {
        "determination": determination, "determined_by": authorizer,
        "determined_at": store_mod.now(), "status": "determined",
        'determination_note': rationale})
    n = store.one("SELECT * FROM qa_notifications WHERE notification_id=?",
                  (notification_id,))
    store.audit("adverse_event_determined", actor=authorizer,
                case_id=n["case_id"] if n else None,
                summary=f"{notification_id}: {determination}",
                detail={'original_screening': original, 'decision': determination,
                        'rationale': rationale, 'authorizer': authorizer})
    if n and n['case_id']:
        adverse, certainty, classification = states[determination]
        current_case = store.case(n['case_id']) or {}
        inbound = store.one('SELECT body FROM messages WHERE case_id=? AND direction=? ORDER BY created_at LIMIT 1', (n['case_id'], 'inbound')) or {}
        hazard_text = ((original.get('body') or '') + ' ' + inbound.get('body', '')).lower()
        hazard_remains = current_case.get('classification') == 'device_safety_hazard' or any(
            term in hazard_text for term in ('device hazard', 'burning smell', 'electrical odor', 'sparks'))
        if determination == 'not an adverse event' and hazard_remains:
            classification = 'device_safety_hazard'
        changes = {'is_adverse_event': adverse, 'ae_certainty': certainty,
                   'classification': classification}
        if determination != 'not an adverse event' or hazard_remains:
            changes.update(priority='Critical', priority_reason=f'Safety review: {determination}; determined by {authorizer}', status=caseflow.HUMAN_REVIEW)
        store.update('cases', 'case_id', n['case_id'], changes)
        for item in store.query('SELECT * FROM checklist_items WHERE case_id=? AND action=?',
                                (n['case_id'], 'screen_adverse_event')):
            store.update('checklist_items', 'item_id', item['item_id'], {
                'approval_decision': 'determined', 'authorizer': authorizer,
                'decided_at': store_mod.now(), 'feedback': rationale,
                'consequence': f'{determination}; original automated screening retained'})
    return {"ok": True, 'determination': determination, 'rationale': rationale,
            'determined_by': authorizer, 'effect': 'Safety history retained; human safety review remains required for harm, hazard or uncertainty.'}


def acknowledge_delivery(store, case_id: str, tracking_number: str) -> str:
    """Draft a factual acknowledgement once a return is recorded delivered.

    Only called when the simulated shipment actually reached `delivered`, so
    the wording cannot claim a state the store has not recorded.
    """
    ship = store.one("SELECT * FROM sim_shipments WHERE tracking_number=?",
                     (tracking_number,))
    if not ship or ship["status"] != "delivered":
        return ""
    if ship['case_id'] != case_id:
        return ''
    from .operations_workflow import shipping_draft
    return shipping_draft(store,tracking_number,'delivered:1')
