"""Representative documentation and workflow resumption on the existing runtime."""
from fastapi import APIRouter,HTTPException
from pydantic import Field
from ..schemas import Authorised
from ..runtime import get_runtime
from ...coordination import CaseRepository,StaleCase
from ... import erp_research,identity,qms,qms_policy,caseflow

router=APIRouter(prefix='/api',tags=['case coordination'])

class Note(Authorised):
    body:str=Field(min_length=1,max_length=6000)
    version:int=Field(ge=0)
    command_id:str=Field(min_length=8,max_length=160)
    amends_note_id:str|None=None

class Tag(Authorised):
    tag:str
    operation:str='add'
    version:int=Field(ge=0)

class ContactReview(Authorised):
    decision:str
    version:int=Field(ge=0)

class FieldCompletion(Authorised):
    procedure_id:str
    customer_report:str=Field(min_length=10)
    version:int=Field(ge=0)

def safe(call):
    try:return call()
    except (ValueError,PermissionError) as exc:raise HTTPException(409,str(exc)) from exc

@router.get('/cases/{cid}/coordination')
def read(cid:str):return safe(lambda:CaseRepository(get_runtime().store).snapshot(cid))

@router.post('/cases/{cid}/notes')
def note(cid:str,body:Note):
    rt=get_runtime()
    result=safe(lambda:CaseRepository(rt.store).note(cid,body.body,body.authorizer,version=body.version,
        command_id=body.command_id,amends=body.amends_note_id))
    rt.announce_case(cid,'case_updated');return result

@router.post('/cases/{cid}/tags')
def tag(cid:str,body:Tag):
    rt=get_runtime();safe(lambda:CaseRepository(rt.store).tag(cid,body.tag,body.operation,body.authorizer,[body.note],body.version))
    rt.announce_case(cid,'case_updated');return {'ok':True}

@router.post('/contacts/{observation_id}/review')
def contact(observation_id:str,body:ContactReview):
    return safe(lambda:erp_research.review_contact(get_runtime().store,observation_id,body.decision,body.authorizer,body.note,body.version))

@router.post('/cases/{cid}/coordination/resume')
def resume(cid:str,body:Authorised):
    rt=get_runtime()
    rt.store.audit('workflow_resume_requested',actor=body.authorizer,case_id=cid,summary=body.note or 'Recheck current domain approval prerequisites')
    # rc.27: resume runs inside the case's pinned inference session with its pinned operational policy (C1),
    # not the default orchestrator outside any session.
    result=safe(lambda:rt.ops.flow.pre_investigate(cid,resume=True))
    rt.announce_case(cid,'case_updated');return result

@router.post('/cases/{cid}/field-completion')
def field_completion(cid:str,body:FieldCompletion):
    rt=get_runtime();store=rt.store;case=store.case(cid)
    if not case:raise HTTPException(404,'Unknown case')
    device=store.device(case.get('serial_number')) if case.get('serial_number') else None
    authorized=identity.case_revision_authorized(store,case)
    context={'model':case['model'],'revision':case['revision'],
        'revision_context':identity.revision_context(
            device, case.get('revision_confirmed_by') if authorized else None),
        'compatibility':rt.ops.flow.agent.toolbox.compatibility,'safety_hold':bool(case['safety_hold'] or case['priority']=='Critical')}
    sources=[c for c in qms.load_chunks() if c.procedure_id==body.procedure_id]
    if not sources or not all(qms_policy.authorize(c,operation='customer_answer',recipient='customer',context=context).allowed for c in sources):
        raise HTTPException(409,'Field completion requires an applicable approved procedure and no safety hold')
    def record():
        with store.transaction():
            CaseRepository(store).commit(cid,body.version,'field_completion_review',body.authorizer,
                {'procedure_id':body.procedure_id,'customer_report':body.customer_report},body.authorizer+':completion:'+str(body.version)+':'+cid)
            item=caseflow.ChecklistBuilder(store,cid,case.get('run_id')).add('verify_field_completion','Review customer-confirmed completion of authorized checks',caseflow.DONE,
                tool_result={'procedure_id':body.procedure_id,'customer_report':body.customer_report,'completed':True})
            row=store.one("SELECT item_id FROM checklist_items WHERE case_id=? AND action='verify_field_completion' ORDER BY seq DESC LIMIT 1",(cid,))
            store.update('checklist_items','item_id',row['item_id'],{'approval_decision':'confirmed','authorizer':body.authorizer,'feedback':body.note})
        return {'ok':True}
    return safe(record)
