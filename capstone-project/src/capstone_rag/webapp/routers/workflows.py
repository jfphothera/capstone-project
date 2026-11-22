"""Operational follow-up actions in the existing case/runtime."""
from fastapi import APIRouter, HTTPException
from ... import movement, operations_workflow, qms, qms_policy
from ..runtime import get_runtime
from ..schemas import Authorised, ReturnMethod, SimulatedReply, RepresentativeFeedback, ExchangeProposal

router = APIRouter(prefix='/api', tags=['simulated workflow'])


@router.post('/cases/{case_id}/simulated-reply-preview')
def preview(case_id: str, body: Authorised):
    rt = get_runtime()
    try:
        return operations_workflow.preview_customer_reply(rt.store, case_id, body.authorizer, rt.prefer_llm)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/cases/{case_id}/simulated-reply')
def simulated_reply(case_id: str, body: SimulatedReply):
    rt = get_runtime()
    try:
        result = operations_workflow.add_customer_reply(rt.store, case_id, body.text, body.authorizer, body.parent_message_id,
                                                       continue_investigation=body.continue_investigation)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result['duplicate']:
        if body.continue_investigation:
            rt.ops.queue.submit(case_id, rt.store.case(case_id)['priority'])
        rt.announce_case(case_id, 'case_updated')
    return result


@router.post('/cases/{case_id}/feedback')
def feedback(case_id: str, body: RepresentativeFeedback):
    rt = get_runtime()
    try:
        return operations_workflow.record_feedback(rt.store, case_id, body.target_id, body.category,
            body.correction, body.explanation, body.authorizer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/feedback/{proposal_id}/replay')
def replay(proposal_id: str, body: Authorised):
    rt = get_runtime()
    try:
        return operations_workflow.replay_reviewed_feedback(rt.store, rt.ops.flow.agent, proposal_id, body.authorizer)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/cases/{case_id}/exchange-proposal')
def exchange(case_id: str, body: ExchangeProposal):
    rt = get_runtime()
    case = rt.store.case(case_id)
    if not case or not case['identity_verified'] or not case.get('serial_number'):
        raise HTTPException(400, 'An exchange requires verified account and identified equipment')
    source = next(c for c in qms.load_chunks() if c.document_id == 'QMS-DOC-0304')
    authorization = qms_policy.authorize(source, operation='internal_workflow', recipient='internal')
    if not authorization.allowed:
        raise HTTPException(409, 'Controlled movement procedure does not authorize this proposal')
    decision = movement.MovementDecision(movement.DEVICE_REPLACEMENT, movement.OUTBOUND,
        [{'part_number': 'UNIT', 'name': 'Replacement device'}], rule='MR-EXCHANGE',
        reason=body.note, evidence={'procedure': source.procedure_id, 'prepared_by': body.authorizer,
            'authorization': authorization.as_dict(), 'requires_verified_return_receipt': True})
    result = rt.erp.propose_movement(case_id, decision, account_id=case['account_id'], serial_number=case['serial_number'])
    rt.store.audit('exchange_proposal_prepared', actor=body.authorizer, case_id=case_id,
        summary='Linked return and replacement proposed; neither movement authorized', detail=result.as_dict())
    rt.announce_case(case_id, 'case_updated')
    return result.as_dict()


@router.post('/rmas/{rma_id}/return-method')
def return_method(rma_id: str, body: ReturnMethod):
    rt = get_runtime()
    try:
        rt.erp.select_return_method(rma_id, body.method, body.authorizer, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    row = rt.store.one('SELECT * FROM rmas WHERE rma_id=?', (rma_id,))
    rt.announce_case(row['case_id'], 'case_updated')
    return {'ok': True, 'return_method': body.method}


@router.post('/rmas/{rma_id}/receive')
def receive(rma_id: str, body: Authorised):
    rt = get_runtime()
    try:
        result = rt.erp.receive_return(rma_id, body.authorizer, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    rt.announce_case(result['case_id'], 'case_updated')
    return {'ok': True, 'shipment': result}
