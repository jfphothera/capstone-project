"""LangGraph coordinates the existing CaseFlow, rather than a second application."""
import json
import sqlite3
import time
from types import SimpleNamespace
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command
from langchain_core.tools import StructuredTool
from . import activity, identity, triage, interpretation, erp_research, planning, response_policy, generation, inference
from .coordination import CaseRepository,uid,timestamp
from .collaboration import Coordinator
from . import task_context,runtime_identity,config,role_contracts

class State(TypedDict,total=False):
    cid:str
    workflow_id:str
    message_id:str
    body:str
    intake:dict
    parallel_evidence:dict
    safety:dict
    erp:dict
    qms:dict
    planning:dict
    result:dict
    validation:dict
    correction:dict
    validation_cycle:int
    collaboration:dict
    started:float

ROLE_CAPABILITIES={
    'ticket_interpretation':{'interpret'}, 'erp_research':{'candidate_discovery','exact_verification'},
    'qms_research':{'public_manual_search'},'safety_complaint':{'screen'},'documentation':{'append_observation'},
    'resolution_planning':{'compare_plans'},'operations':{'existing_caseflow'},
    'communication_review':{'review_draft'},'independent_validation':{'validate'},
    'communication_correction':{'correct_artifact'},'reviewed_learning':{'reviewed_recall'}}

BULK_KEYS={'retrieval_trace','evidence','candidates','evidence_contract','mcp_evidence','mcp_erp_evidence','mcp_ticket_history',
           'snapshots','checklist','chunks','plans','rounds','trace','context'}

def bounded_view(value,limit=6000):
    """A compact, de-duplicated view of a finding for supervisor/peer/reflection
    model calls. Keeps decision-relevant fields (ids, gaps, recommendations,
    verification summary); drops raw retrieval dumps and bulk evidence, which
    stay in the record for audit. Size-bounded so every topology fits the
    model's qualified context instead of sending ~37k tokens of repetition."""
    def shrink(item,depth=0):
        if isinstance(item,dict):
            out={}
            for key,sub in item.items():
                if key in BULK_KEYS:
                    out[key]=f'<{len(sub) if hasattr(sub,"__len__") else 1} item(s) omitted; ids retained elsewhere>'
                    continue
                out[key]=shrink(sub,depth+1)
            return out
        if isinstance(item,list):
            kept=[shrink(sub,depth+1) for sub in item[:8]]
            return kept+([f'<{len(item)-8} more omitted>'] if len(item)>8 else [])
        if isinstance(item,str):
            return item if len(item)<=400 else item[:400]+'…'
        return item
    view=shrink(value)
    text=json.dumps(view,default=str)
    if len(text)>limit:
        view={'summary':text[:limit]+'…','truncated_from_chars':len(text)}
    return view

def require_capability(role,tool):
    if tool not in ROLE_CAPABILITIES.get(role,set()): raise PermissionError(f'{role} cannot invoke {tool}')

def authorize_tool(store,case_id,tool):
    """Enforce delegated tool permissions at the moment of invocation.

    The calling specialist must be running a delegated task (task_context)
    whose recipient is that specialist, whose permitted_tools include
    ``tool``, which belongs to this case, and which is still running. The role
    must also hold the capability. Any failure is audited and raises
    PermissionError, so the step stops instead of acting outside its grant.
    """
    context=task_context.current() or {}
    task=store.one('SELECT * FROM delegated_tasks WHERE task_id=?',(context.get('task_id'),)) if context.get('task_id') else None
    reasons=[]
    if task is None:reasons.append('no active delegated task')
    else:
        try:permitted=set(json.loads(task.get('permitted_tools') or '[]'))
        except (TypeError,ValueError):permitted=set()
        if task.get('recipient')!=context.get('agent_id'):reasons.append('caller is not the task recipient')
        if tool not in permitted:reasons.append(f'{tool} is not in the task grant {sorted(permitted)}')
        if task.get('case_id')!=case_id:reasons.append('task belongs to another case')
        if task.get('status') not in ('running','assigned'):reasons.append(f"task is {task.get('status')}")
    if context.get('agent_id') and tool not in ROLE_CAPABILITIES.get(context.get('agent_id'),set()):
        reasons.append(f"role {context.get('agent_id')} lacks capability {tool}")
    if reasons:
        store.audit('tool_permission_denied',actor=str(context.get('agent_id') or 'unknown'),case_id=case_id,
            summary=f'{tool} refused: '+'; '.join(reasons),detail={'tool':tool,'task_id':context.get('task_id'),'reasons':reasons})
        raise PermissionError(f'{tool} refused: '+'; '.join(reasons))
    return task

# rc.25 (M2): bounded stage budgets reserved under the ORIGINAL case deadline
# replace the old silent elapsed-time cut-offs (120 s planning, 220 s review).
STAGE_RESERVE_SECONDS={'planning_model':45,'review_model':30,'reflection':20,'critic':40,'delegation':60,'peer_lookup':15}
MAX_DELEGATED_TASKS=2
DELEGATION_ROLES=('erp_research','qms_research')   # roles whose inputs exist when a supervisor can ask


def stage_record(stage,status,*,role=None,workflow_id=None,reason=None,inputs=None,outputs=None,effect=None,started=None):
    """One executed/skipped/failed/budget_exhausted record per selected stage."""
    evidence={'stage':stage,'status':status,'role':role,'workflow_id':workflow_id,'reason':reason,
              'inputs':inputs or {},'outputs':outputs or {},'downstream_effect':effect,'record_version':'stage-execution/1'}
    activity.emit('stage_execution',f'{stage} {status}',status,evidence,
                  (time.perf_counter()-started)*1000 if started else None)
    return evidence


def stage_budget(stage):
    """(ok, remaining_seconds): enough of the original case budget remains for this stage."""
    deadline=generation._CASE_DEADLINE.get()
    if deadline is None:return True,None
    remaining=deadline-time.monotonic()
    return remaining>=STAGE_RESERVE_SECONDS.get(stage,30),round(remaining,3)


class CaseOrchestrator:
    def __init__(self,flow,execution_policy=None):
        self.flow=flow;self.store=flow.store;self.repo=CaseRepository(self.store)
        self.execution_policy=dict(execution_policy or {})

    def _passages(self,ids,limit=4,chars=700):
        """Authorized passage text (not just ids) for peer/critic/judge views."""
        from . import qms
        lookup={c.chunk_id:c for c in qms.load_chunks()}
        return [{'id':i,'section':lookup[i].section_path,'text':lookup[i].text[:chars]} for i in list(ids or [])[:limit] if i in lookup]

    def _finding(self,state,role,call):
        repeated=role in ('independent_validation','communication_correction')
        suffix=':'+str(state.get('validation_cycle',0)) if repeated else ''
        command_id=state['workflow_id']+':'+role+suffix
        cached=self.store.one('SELECT payload FROM specialist_findings WHERE command_id=?',(command_id,))
        if cached:return json.loads(cached['payload'])
        started=time.perf_counter();wall=time.time();version=self.repo.version(state['cid'])
        coordinator=Coordinator(self.store,state['workflow_id'])
        issuer={
            'ticket_interpretation':'coordinator','safety_complaint':'coordinator',
            'erp_research':'ticket_interpretation','qms_research':'erp_research',
            'documentation':'ticket_interpretation','resolution_planning':'qms_research',
            'operations':'resolution_planning','communication_review':'operations',
            'independent_validation':'communication_review',
            'communication_correction':'independent_validation',
            'reviewed_learning':'independent_validation'
        }.get(role,'coordinator')
        prior=self.store.one(
            'SELECT task_id FROM delegated_tasks WHERE case_id=? AND workflow_id=? '
            'AND recipient=? ORDER BY created_at DESC LIMIT 1',
            (state['cid'],state['workflow_id'],issuer)) if issuer!='coordinator' else None
        task=self.store.one(
            "SELECT * FROM delegated_tasks WHERE case_id=? AND workflow_id=? "
            "AND recipient=? AND status='assigned' ORDER BY created_at LIMIT 1",
            (state['cid'],state['workflow_id'],role))
        if not task:
            task=coordinator.delegate(case_id=state['cid'],issuer=issuer,recipient=role,
                purpose='Produce a structured current finding for '+role.replace('_',' '),
                based_on_version=version,required_inputs=['current case snapshot','upstream findings'],
                evidence_ids=[state.get('message_id')] if state.get('message_id') else [],
                permitted_tools=sorted(ROLE_CAPABILITIES[role]),operation_scope='advisory_finding',
                dependency_ids=[prior['task_id']] if prior else [],
                idempotency_key=state['workflow_id']+':task:'+role)
        coordinator.start(task['task_id'],role)
        activity.emit('specialist',f'{role} started','running',{'role':role,'based_on_version':version,'capabilities':sorted(ROLE_CAPABILITIES[role])})
        try:
            with task_context.bind(task_id=task['task_id'],workflow_id=state['workflow_id'],agent_id=role),task_context.delegated_liveness(self.store,task['task_id'],state['workflow_id'],role),inference.specialist(role):
                payload=call({**state,'based_on_version':version,
                              'delegation_task_id':task['task_id']})
                inference.raise_if_failed()
                if self.execution_policy.get('peer_exchange') and role in ('qms_research','resolution_planning'):
                    # Real two-turn peer exchange (rc.20). Turn 1 runs on the
                    # peer_reviewer route; turn 2 runs on the specialist's own
                    # route. Each call records its route and executed model.
                    view=bounded_view(payload)
                    peer_started=time.perf_counter()
                    # rc.25 (F): peers see the authorized passage TEXT the finding relies on.
                    passages=self._passages(payload.get('sources') or (state.get('qms') or {}).get('sources'))
                    with inference.specialist('peer_reviewer'):
                        challenge=role_contracts.call('peer_challenge',{'colleague_role':role,'finding':view,'authorized_passages':passages})
                    with inference.specialist(role):
                        reply=role_contracts.call('peer_reply',{'finding':view,'authorized_passages':passages,'peer_question':challenge.get('question'),'peer_disagreement':challenge.get('disagreement')},role=role)
                    payload['peer_exchange']={'turns':[{'from':'peer_reviewer','to':role,**challenge},{'from':role,'to':'peer_reviewer',**reply}],
                        'question':challenge.get('question'),'planning_focus':challenge.get('planning_focus'),
                        'conflict':challenge.get('disagreement'),'resolved':bool(reply.get('resolved')),
                        'sender':role,'receiver':'resolution_planning' if role=='qms_research' else 'communication_review'}
                    payload['peer_exchange']['followup']=self._peer_followup(state,role,payload,reply)
                    stage_record('peer_exchange','executed',role=role,workflow_id=state['workflow_id'],
                        inputs={'passages':[p['id'] for p in passages]},outputs={'resolved':payload['peer_exchange']['resolved']},
                        effect=payload['peer_exchange']['followup'].get('effect'),started=peer_started)
                if self.execution_policy.get('supervisor_checkpoints') and role in ('qms_research','resolution_planning','communication_review'):
                    supervisor_started=time.perf_counter()
                    with inference.specialist('supervisor'):
                        assessment=role_contracts.call('supervise_finding',{'specialist':role,'finding':bounded_view(payload),
                            'allowed_specialists':list(role_contracts.SUPERVISOR_SPECIALISTS)})
                    payload['supervisor_assessment']=assessment
                    requested=assessment.get('requested_specialist')
                    effect='continue'
                    if requested not in role_contracts.SUPERVISOR_SPECIALISTS:
                        assessment['requested_specialist_invalid']=requested;requested='none'
                    if self.execution_policy.get('hierarchical_delegation') and requested in DELEGATION_ROLES and requested!=role:
                        payload.setdefault('specialist_requests',[]).append({'recipient':requested,
                            'purpose':'Supervisor-requested bounded evidence review: '+assessment.get('concern',''),
                            'focus':assessment.get('concern',''),
                            'required_inputs':['current specialist finding'],'evidence_ids':[],
                            'permitted_tools':sorted(ROLE_CAPABILITIES.get(requested,set())),
                            'operation_scope':'advisory_finding'})
                        effect='delegation_requested'
                    elif requested not in ('none',None) and requested!=role:
                        effect='request_recorded_not_dispatchable_here'
                    if assessment.get('continue') is False:
                        # D: a stop is never silently ignored nor taken as correct:
                        # it becomes an explicit unresolved concern the reviewer sees
                        # (and, under G, a dispatched specialist task).
                        payload['supervisor_stop']={'concern':assessment.get('concern'),'requested_specialist':requested,
                            'effect':'surfaced_as_unresolved_concern' if effect=='continue' else effect}
                        effect=payload['supervisor_stop']['effect']
                    stage_record('supervisor','executed',role=role,workflow_id=state['workflow_id'],
                        outputs={'continue':assessment.get('continue'),'requested_specialist':requested},effect=effect,started=supervisor_started)
        except Exception as exc:
            activity.emit('specialist',role+' failed','failed',{'role':role,'message_id':state.get('message_id'),'based_on_version':version,'error':str(exc)},(time.perf_counter()-started)*1000)
            status=coordinator.fail(task['task_id'],role,exc)
            if getattr(self.store,'arena_experiment_run_id',None):
                raise
            payload={'controlled_failure':True,'error':str(exc),'status':status,
                     'missing_evidence':['specialist result unavailable'],
                     'recommended_next_action':'human_review'}
        calls=self.store.query('SELECT call_id,role,endpoint_id,model,status FROM inference_calls WHERE case_id=? AND at>=?',(state['cid'],wall))
        payload['inference_usage']={'llm_requested':bool(calls),'completed_calls':sum(c['status']=='completed' for c in calls),'calls':calls}
        payload['input_references']={'message_id':state.get('message_id'),'based_on_version':version}
        payload['delegation_task_id']=task['task_id']
        if not payload.get('controlled_failure'):
            coordinator.complete(task['task_id'],role,payload,
                missing_evidence=payload.get('missing_evidence',()),
                conflicts=payload.get('conflicts',()),
                recommended_next_action=payload.get('recommended_next_action'))
        for request in payload.get('specialist_requests',[]):
            requested=coordinator.delegate(case_id=state['cid'],issuer=role,
                recipient=request['recipient'],purpose=request['purpose'],
                based_on_version=self.repo.version(state['cid']),
                required_inputs=request.get('required_inputs',()),
                evidence_ids=request.get('evidence_ids',()),
                permitted_tools=request.get('permitted_tools',()),
                operation_scope=request.get('operation_scope','advisory_finding'),
                authorization_context=request.get('authorization_context',{}),
                dependency_ids=[task['task_id']],parent_task_id=task['task_id'],depth=2,
                idempotency_key=state['workflow_id']+':request:'+role+':'+request['recipient']+':'+request['purpose'])
            if self.execution_policy.get('hierarchical_delegation') and request.get('focus') is not None:
                # G (rc.25): a task record is not task execution. Dispatch the
                # bounded task, collect its result and reintegrate it.
                payload.setdefault('delegated_results',[]).append(self._dispatch(state,coordinator,requested,request))
                continue
            already_ran=self.store.one(
                "SELECT 1 FROM delegated_tasks WHERE case_id=? AND workflow_id=? "
                "AND recipient=? AND status='completed' AND task_id!=?",
                (state['cid'],state['workflow_id'],request['recipient'],requested['task_id']))
            if already_ran:
                coordinator.defer(requested['task_id'],'coordinator',
                    'The requested specialist already completed against this snapshot; new customer or authoritative service evidence is required before rerun.')
        if payload.get('delegated_results'):
            # G: the requesting finding consumes its dispatched tasks' results
            # (planning additionally merges ERP/QMS re-check evidence).
            payload['delegated_inputs']=[{'task_id':d['task_id'],'recipient':d['recipient'],'status':d['status'],
                'evidence':(d.get('result') or {}).get('sources') or [],'missing_evidence':(d.get('result') or {}).get('missing_evidence') or [],
                'new_evidence':[i for i in ((d.get('result') or {}).get('sources') or []) if i not in set(payload.get('sources') or payload.get('evidence_ids') or [])]}
                for d in payload['delegated_results']]
        if role in ('operations','documentation'):
            payload['input_case_version']=version
            version=self.repo.version(state['cid'])
        fid=self.repo.finding(state['cid'],role,payload,version,command_id)
        if payload.get('case_status_after_finding'):
            self.store.update('cases','case_id',state['cid'],{'status':payload['case_status_after_finding']})
        activity.emit('specialist',f'{role} finding recorded','completed',{'role':role,'finding_id':fid,'based_on_version':version,'finding':payload},(time.perf_counter()-started)*1000)
        return payload

    def _dispatch(self,state,coordinator,task,request):
        """Execute one supervisor-requested task (G), bounded by role, depth and count."""
        started=time.perf_counter();recipient=request['recipient']
        done=self.store.one("SELECT COUNT(*) AS n FROM delegated_tasks WHERE case_id=? AND workflow_id=? AND depth=2 AND status='completed' "
                            "AND purpose LIKE 'Supervisor-requested%'",(state['cid'],state['workflow_id']))['n']
        ok,remaining=stage_budget('delegation')
        reason=None
        if recipient not in DELEGATION_ROLES:reason='role not dispatchable at this stage'
        elif done>=MAX_DELEGATED_TASKS:reason=f'delegated task limit {MAX_DELEGATED_TASKS} reached'
        elif not ok:reason=f'stage budget exhausted ({remaining}s left)'
        elif recipient=='qms_research' and not (state.get('safety') and state.get('erp')):reason='dependency not available: safety and ERP findings required'
        if reason:
            coordinator.defer(task['task_id'],'coordinator','Delegation not executed: '+reason)
            stage_record('delegation','skipped' if ok else 'budget_exhausted',role=recipient,workflow_id=state['workflow_id'],reason=reason,started=started)
            return {'task_id':task['task_id'],'recipient':recipient,'status':'not_executed','reason':reason}
        focused={**state,'body':state['body']+' '+str(request.get('focus') or '')[:200]}
        coordinator.start(task['task_id'],recipient)
        try:
            with task_context.bind(task_id=task['task_id'],workflow_id=state['workflow_id'],agent_id=recipient),inference.specialist(recipient):
                result=(self._erp if recipient=='erp_research' else self._qms)(focused)
            summary={k:result.get(k) for k in ('sources','missing_evidence','authorized_procedures','recommended_next_action') if k in result}
            if recipient=='erp_research':summary['verified']=(result.get('verification') or {}).get('verified')
            coordinator.complete(task['task_id'],recipient,summary,missing_evidence=result.get('missing_evidence',()),
                                 recommended_next_action=result.get('recommended_next_action'))
            stage_record('delegation','executed',role=recipient,workflow_id=state['workflow_id'],
                         inputs={'focus':request.get('focus')},outputs=summary,effect='reintegrated_into_requesting_finding',started=started)
            return {'task_id':task['task_id'],'recipient':recipient,'status':'completed','result':summary}
        except Exception as exc:
            coordinator.fail(task['task_id'],recipient,exc)
            stage_record('delegation','failed',role=recipient,workflow_id=state['workflow_id'],reason=str(exc)[:200],started=started)
            return {'task_id':task['task_id'],'recipient':recipient,'status':'failed','reason':str(exc)[:200]}

    def _peer_followup(self,state,role,payload,reply):
        """F: an unresolved peer question gets one supported evidence lookup or
        an explicit unresolved record (never a silent drop)."""
        if reply.get('resolved'):
            return {'effect':'no_change_needed','resolved':True}
        if role!='qms_research':
            return {'effect':'recorded_unresolved','resolved':False,'reason':'no supported evidence lookup for this role','missing_evidence':reply.get('missing_evidence')}
        ok,remaining=stage_budget('peer_lookup')
        if not ok:
            return {'effect':'budget_exhausted','resolved':False,'remaining_seconds':remaining}
        from .support_workflow import customer_manual_search
        device=(state.get('erp') or {}).get('device') or {}
        query=state['body']+' '+str(reply.get('missing_evidence') or '')[:160]
        try:
            manual=customer_manual_search(self.flow.agent.toolbox,query,device.get('model'),device.get('revision'),
                bool(device and identity.assess_revision(device).usable_for_troubleshooting),device=device or None,
                revision_context=identity.revision_context(device) if device else None)
        except Exception as exc:
            return {'effect':'lookup_failed','resolved':False,'reason':str(exc)[:200]}
        new=[c.chunk_id for c in manual.get('chunks',[]) if c.chunk_id not in set(payload.get('sources') or [])]
        return {'effect':'evidence_found' if new else 'recorded_unresolved','resolved':bool(new),'lookup_query':query[:300],
                'new_authorized_chunk_ids':new,'authority':'advisory: new chunks are listed for planning, never merged into the frozen contract'}

    def _interpret(self,s):
        case=self.store.case(s['cid'])
        finding=interpretation.interpret(s['body'],case['channel'],s['message_id'])
        if self.flow.agent.prefer_llm and inference.enabled('ticket_interpretation') and finding['message_type']=='customer_request' and not finding['intents']:
            try:
                proposal=role_contracts.call('interpret_request',{'body':s['body'],'channel':case['channel']})
                quote=proposal.get('evidence_quote','')
                if not quote or quote not in s['body']:raise ValueError('Interpretation lacks a literal supporting span')
                finding['model_hypothesis']={**proposal,'authority':'advisory_interpretation','start':s['body'].index(quote)}
            except Exception as exc:finding['model_interpretation_error']=str(exc)
        if not interpretation.validate_finding(finding,s['body']):raise ValueError('Invalid intake evidence spans')
        return finding

    def _safety(self,s):
        result=triage.triage(s['body']);case=self.store.case(s['cid'])
        concurrent=(s.get('parallel_evidence') or {}).get('safety') or {}
        hold=bool(concurrent.get('hold') or triage.requires_immediate_escalation(result) or case.get('safety_hold') or
            self.store.one("SELECT 1 FROM qa_notifications WHERE case_id=? AND status='open'",(s['cid'],)))
        return {'hold':hold,'complaint':result.complaint.as_dict(),'screen':result.adverse_event.as_dict(),
                'concurrent_task_evidence':concurrent,
                'evidence_join_task_id':(s.get('parallel_evidence') or {}).get('join_task_id')}

    def _erp(self,s):
        require_capability('erp_research','candidate_discovery')
        serials=[r['serial_number'] for r in self.store.query('SELECT serial_number FROM devices')]
        names=[r['customer_name'] for r in self.store.query('SELECT customer_name FROM customers')]
        claim=identity.Claim();history=[m for m in self.store.messages(s['cid']) if m['direction']=='inbound']
        for message in history:
            fresh=identity.Claim.from_text(message['body'],known_serials=serials,known_names=names)
            signal=identity.correction_signal(message['body'])
            for field in ('name','dob','serial','phone','email'):
                value=getattr(fresh,field)
                if value is not None or field in signal['fields']:setattr(claim,field,value)
            if fresh.serial or fresh.serial_issue or 'serial' in signal['fields']:
                claim.serial=fresh.serial;claim.serial_issue=fresh.serial_issue;claim.serial_input=fresh.serial_input
            if fresh.dob_issue:claim.dob=None;claim.dob_issue=fresh.dob_issue
        chat=self.store.one('SELECT state_json FROM conversations WHERE case_id=?',(s['cid'],))
        if chat:claim=identity.Claim(**json.loads(chat['state_json']).get('claim',{}))
        def discover(name:str='',serial:str='')->dict:
            """Discover advisory candidates; never authenticate or change ERP records."""
            authorize_tool(self.store,s['cid'],'candidate_discovery')
            return erp_research.candidates(self.store,name,serial)
        tool=StructuredTool.from_function(discover)
        candidates=tool.invoke({'name':claim.name or '', 'serial':claim.serial or ''})
        authorize_tool(self.store,s['cid'],'exact_verification')
        verification=identity.verify(self.store,claim,account_id=self.store.case(s['cid']).get('account_id'))
        advisory=None
        if self.flow.agent.prefer_llm and inference.current() and inference.enabled('erp_research'):
            advisory=role_contracts.call('erp_identity_advisory',{'request':s['body'],'candidates':candidates})
            if advisory.get('suggested_account_id') and advisory['suggested_account_id'] not in {c['account_id'] for c in candidates['candidates']}:
                # A made-up account is a caught hallucination, not a run
                # failure: discard the advisory, record it, and continue on
                # the deterministic verification above (which it never fed).
                from . import activity
                activity.emit('guardrail','Advisory ERP suggestion discarded: account is not an ERP candidate','completed',
                    {'guardrail':'erp_candidate_hallucination','suggested_account_id':advisory['suggested_account_id'],
                     'candidate_count':len(candidates['candidates'])})
                advisory={'discarded':True,'reason':'erp_candidate_hallucination','suggested_account_id':advisory['suggested_account_id']}
        missing=[]
        if not claim.serial:missing.append('canonical serial')
        if not verification.verified:missing.append('two authoritative account verification points')
        device=self.store.device(verification.serial_number) if verification.verified and verification.serial_number else None
        requests=[{'recipient':'qms_research','purpose':'Find authorized guidance at the currently established identity tier',
                   'required_inputs':['customer request','established model if any','revision authority'],
                   'evidence_ids':[m['message_id'] for m in history],
                   'permitted_tools':['public_manual_search'],'operation_scope':'customer_answer'}]
        return {'advisory_model_assessment':advisory,'verification':verification.as_dict(),'claim':dict(claim.__dict__),'evidence_message_ids':[m['message_id'] for m in history],
                'candidates':candidates,'tool_adapter':'langchain_core.StructuredTool',
                'mcp_erp_evidence':(s.get('parallel_evidence') or {}).get('erp'),
                'mcp_ticket_history':(s.get('parallel_evidence') or {}).get('ticket'),
                'device':device,'missing_evidence':missing,'specialist_requests':requests,
                'recommended_next_action':'qms_research' if device or s['body'] else 'request_clarification'}

    def _qms(self,s):
        from .support_workflow import customer_manual_search,consult_operational
        if s['safety']['hold']: return {'sources':[],'blocked':'Safety hold','evidence':{}}
        device=s['erp'].get('device') or {};model=device.get('model')
        unsupported_model = None
        if not model:
            from .model_catalog import resolve_reference
            reference = resolve_reference(s['body'])
            if reference and reference.supported:
                model = reference.canonical
            elif reference:
                unsupported_model = reference.raw
        query=s['body']
        if self.flow.agent.prefer_llm and inference.current() and inference.enabled('qms_research'):
            proposal=role_contracts.call('qms_search_query',{'request':s['body'],'device_model':model})
            query=s['body']+' '+str(proposal.get('query') or '')[:240]
        revision_authority=identity.revision_context(device) if device else None
        from .phase4_runtime import qms_mcp_evidence
        mcp_evidence=qms_mcp_evidence(self.store,s,model=model,
            revision=device.get('revision') if device else None,query=query)
        from .reasoning_strategies import observation_driven_qms
        def search(q,m,r,e,**kwargs):
            authorize_tool(self.store,s['cid'],'public_manual_search')
            return customer_manual_search(self.flow.agent.toolbox,q,m,r,e,**kwargs)
        manual,retrieval_trace=observation_driven_qms(search,query,model=model,
            revision=device.get('revision'),
            exact_revision=bool(device and identity.assess_revision(device).usable_for_troubleshooting),
            device=device,revision_context=revision_authority)
        from . import telemetry
        telemetry.emit(self.store,
            'tool.failed' if retrieval_trace['termination']=='tool_error' else 'tool.completed',
            case_id=s['cid'],workflow_id=s['workflow_id'],agent_id='qms_research',
            correlation_id=s.get('delegation_task_id'),tool_node=runtime_identity.coordinator_id(),
            task_status='failed' if retrieval_trace['termination']=='tool_error' else 'completed',
            summary='Authorized QMS retrieval failed' if retrieval_trace['termination']=='tool_error'
                    else f"Authorized QMS retrieval completed in {len(retrieval_trace['rounds'])} round(s)",
            evidence_refs=[getattr(c,'chunk_id',str(c)) for c in manual.get('chunks',[])],
            detail={'round_count':len(retrieval_trace['rounds']),
                    'termination':retrieval_trace['termination'],
                    'duration_ms':sum(r.get('duration_ms',0) for r in retrieval_trace['rounds'])})
        # QMS conflict review (controlled-document metadata only). A conflict
        # between current approved applicable procedures blocks release and
        # routes the case to human document-control review; declared
        # precedence resolves it by keeping only the authority document.
        from .qms_policy import review_conflicts
        conflict_review=review_conflicts(manual.get('chunks',[]),model=model,revision=device.get('revision') if device else None)
        if conflict_review['resolved']:
            dropped={doc for item in conflict_review['resolved'] for doc in item['over']}
            manual['chunks']=[c for c in manual.get('chunks',[]) if c.document_id not in dropped]
            manual['procedures']=sorted({c.procedure_id for c in manual['chunks']})
            # The evidence contract must follow the resolution, or validation
            # keeps demanding the overridden document's mandatory sections.
            contract=manual.get('contract')
            if contract:
                keep=lambda ids:[i for i in ids if i.split('#',1)[0] not in dropped]
                contract['authorized_chunk_ids']=keep(contract.get('authorized_chunk_ids',[]))
                contract['mandatory_context_chunk_ids']=keep(contract.get('mandatory_context_chunk_ids',[]))
                contract['procedure_ids']=[p for p in contract.get('procedure_ids',[]) if p in manual['procedures']]
                contract['precedence_resolution']=conflict_review['resolved']
        if conflict_review['requires_document_control']:
            from .caseflow import HUMAN_REVIEW
            # The status change is applied after this finding is recorded
            # (_specialist): changing the case mid-specialist made the finding
            # stale and crashed the workflow.
            self.store.audit('qms_conflict_document_control_review',actor='qms_research',case_id=s['cid'],
                summary='Conflicting current approved QMS procedures; release blocked pending document control',
                detail={'conflicts':conflict_review['conflicts'],'workflow_id':s.get('workflow_id')})
            return {'sources':[],'blocked':'QMS conflict requires document-control review','evidence':{},
                    'conflict_review':conflict_review,'missing_evidence':['document-control decision on conflicting procedures'],
                    'recommended_next_action':'human_review','case_status_after_finding':HUMAN_REVIEW}
        operational=consult_operational(s['body'])
        exact=[p for p in manual.get('procedures',[]) if p not in {
            'PROC-MANUAL-P2400','PROC-MANUAL-P3410','PROC-MANUAL-P4800',
            'PROC-MANUAL-SERIESM','PROC-MANUAL-3SERIES','PROC-MANUAL-HOMENB',
            'PROC-GEN-MISSING-ACCESSORY'}]
        requests=[];missing=[]
        if model and (not revision_authority or not revision_authority.get('usable_for_troubleshooting')):
            missing.append('trusted hardware revision for exact-revision troubleshooting')
            requests.append({'recipient':'erp_research',
                'purpose':'Establish trusted hardware revision evidence required by exact QMS policy',
                'required_inputs':['canonical device identity','controlled revision provenance'],
                'evidence_ids':s['erp'].get('evidence_message_ids',[]),
                'permitted_tools':['exact_verification'],
                'operation_scope':'identity_and_revision_evidence',
                'authorization_context':{'may_verify':False,'qms_policy_remains_deterministic':True}})
        requests.append({'recipient':'resolution_planning',
            'purpose':'Plan from authorized QMS evidence and explicit coverage gaps',
            'required_inputs':['authorized source IDs','mandatory context','coverage gaps'],
            'evidence_ids':[c.chunk_id for c in manual['chunks']],
            'permitted_tools':['compare_plans'],'operation_scope':'proposal_only'})
        advisory=None
        if self.flow.agent.prefer_llm and inference.current() and inference.enabled('qms_research'):
            advisory=role_contracts.call('qms_coverage_summary',
                {'request':s['body'],'authorized_procedures':manual.get('procedures',[]),
                 'revision_authority':revision_authority,'missing_evidence':missing})
        return {'retrieval_query':query,'retrieval_trace':retrieval_trace,
                'sources':[c.chunk_id for c in manual['chunks']],'evidence':manual['evidence'],
                'unsupported_model_reference': unsupported_model,
                'evidence_contract': manual.get('contract'),
                'authorized_procedures':manual.get('procedures',[]),'exact_procedures':exact,
                'revision_authority':revision_authority,'missing_evidence':missing,
                'specialist_requests':requests,'advisory_model_assessment':advisory,
                'mcp_evidence':mcp_evidence,
                'recommended_next_action':'resolution_planning',
                'operational':{k:operational[k] for k in ('actions','missing','evidence')}}

    def _document(self,s):
        message=self.store.one('SELECT * FROM messages WHERE message_id=?',(s['message_id'],))
        if s['intake']['outbound_message']['decision']=='draft_for_review':
            self.repo.note(s['cid'],'Observed request type: '+s['intake']['message_type']+'. Explicit intent evidence: '+
                (', '.join(i['kind'] for i in s['intake']['intents']) or 'not established')+'. Findings remain observations, not account or movement authority.',
                'documentation_specialist',kind='investigation',evidence=s['intake']['evidence'],command_id='documentation:'+s['workflow_id'])
        return {'observation':s['intake']['message_type'],'evidence':s['intake']['evidence'],
            'contact_observations':erp_research.observe_contacts(self.store,s['cid'],message,
                s.get('erp',{}).get('verification',{}).get('account_id') if s.get('erp',{}).get('verification',{}).get('verified') else None),'authority':'observation_only'}

    def _plan(self,s):
        erp=s.get('erp',{});device=erp.get('device') or {}
        qms_finding=s.get('qms',{});coordinator=Coordinator(self.store,s['workflow_id'])
        if (qms_finding.get('conflict_review') or {}).get('requires_document_control'):
            # Conflicting controlled documents: nothing to plan from until
            # document control decides. No model may pick a procedure.
            return {'mode':'document_control_hold','selected_action':'human_review','selected_plan_id':None,'plans':[],
                    'conflicts':(qms_finding.get('conflict_review') or {}).get('conflicts',[]),
                    'required_prerequisites':['document-control decision on conflicting procedures'],
                    'authorized_transition':'escalate_human_review','evidence_ids':[]}
        conflicts=[]
        canonical=(qms_finding.get('evidence_contract') or {}).get('canonical_device_context') or {}
        if device and canonical.get('model') and canonical.get('model')!=device.get('model'):
            conflicts.append({'type':'device_model_conflict','erp':device.get('model'),
                              'qms':canonical.get('model')})
        if device and canonical.get('revision') and canonical.get('revision')!=device.get('revision'):
            conflicts.append({'type':'revision_conflict','erp':device.get('revision'),
                              'qms':canonical.get('revision')})
        if conflicts:
            row=coordinator.conflict(s['cid'],'specialist_evidence_conflict',
                'ERP identity context and QMS evidence contract disagree; no authority may be selected by confidence.',
                [erp.get('delegation_task_id'),qms_finding.get('delegation_task_id')],
                s['based_on_version'])
            return {'mode':'conflict_reconciliation','selected_action':'human_review',
                    'selected_plan_id':None,'plans':[],'conflicts':conflicts,
                    'conflict_id':row['conflict_id'],'required_prerequisites':['fresh canonical identity and QMS retrieval'],
                    'authorized_transition':'escalate_conflict','evidence_ids':qms_finding.get('sources',[])}
        part=__import__('re').search(r'\b(?:goggles|mains cord|filter)\b',s['body'],__import__('re').I)
        facts={'verified':erp.get('verification',{}).get('verified',False),'identified_part':bool(part),
            'field_completion':any(self.flow._field_completion(s['cid'],json.loads(r['tool_result'] or '{}').get('procedure_id')) for r in self.store.query("SELECT tool_result FROM checklist_items WHERE case_id=? AND action='verify_field_completion'",(s['cid'],))),
            'received_original':bool(self.store.one('SELECT 1 FROM rmas WHERE case_id=? AND received_at IS NOT NULL AND received_by IS NOT NULL',(s['cid'],))),
            'safety_hold':s['safety']['hold'],'public_sources':bool(s.get('qms',{}).get('sources')),
            'evidence_ids':s.get('qms',{}).get('sources',[]),
            'operational_qms_missing':bool(s.get('qms',{}).get('operational',{}).get('missing')),
            'advisory_case_notes':self.store.query('SELECT note_id,body,authority,author FROM case_notes WHERE case_id=? ORDER BY at DESC LIMIT 8',(s['cid'],))}
        delegated=[d for key in ('erp','qms') for d in (s.get(key) or {}).get('delegated_results') or [] if d.get('status')=='completed']
        extra=[i for d in delegated for i in ((d.get('result') or {}).get('sources') or []) if i not in facts['evidence_ids']]
        if extra:
            facts['evidence_ids']=facts['evidence_ids']+extra;facts['public_sources']=True
        model_planner=self.flow.agent.prefer_llm and inference.enabled('resolution_planning')
        budget_ok,remaining=stage_budget('planning_model')
        if model_planner and not budget_ok:
            stage_record('planning_model','budget_exhausted',role='resolution_planning',workflow_id=s['workflow_id'],
                         reason=f'{remaining}s of the original case budget left; reserve {STAGE_RESERVE_SECONDS["planning_model"]}s')
        plan_started=time.perf_counter()
        result=planning.plan(self.store,s['cid'],s['workflow_id'],s['body'],s['intake'],facts,
                             model_planner and budget_ok,
                             based_on_version=s['based_on_version'],
                             force_tree=bool(self.execution_policy.get('force_tree')),
                             force_direct=self.execution_policy.get('reasoning')=='direct_guarded',
                             max_depth=self.execution_policy.get('max_depth',planning.MAX_DEPTH),
                             max_expanded_states=self.execution_policy.get('max_states',planning.MAX_EXPANDED_STATES))
        result['upstream_delegated_inputs']=[{'task_id':d['task_id'],'recipient':d['recipient'],'added_evidence':[i for i in ((d.get('result') or {}).get('sources') or []) if i in extra]} for d in delegated]
        stage_record('planning','executed',role='resolution_planning',workflow_id=s['workflow_id'],
                     outputs={'mode':result.get('mode'),'model_proposal_used':bool(result.get('model_proposal_used')),
                              'selected_action':result.get('selected_action')},
                     effect='tree_model_assisted' if result.get('model_proposal_used') else 'deterministic_or_direct',started=plan_started)
        if self.execution_policy.get('reflection') and self.flow.agent.prefer_llm and inference.current():
            reflection_started=time.perf_counter();ok,remaining=stage_budget('reflection')
            if not ok:
                stage_record('reflection','budget_exhausted',role='reflection',workflow_id=s['workflow_id'],reason=f'{remaining}s left')
            else:
                gates=next((p.get('hard_gate_verdicts',[]) for p in result.get('plans',[]) if p.get('action')==result.get('selected_action')),[])
                reflection=role_contracts.call('reflect_on_plan',
                    {'request':s['body'],'plan':bounded_view(result),'coverage_gap':qms_finding.get('missing_evidence',[]),'hard_gate_results':gates})
                failed_gates=[g for g in gates if g]   # hard_gate_verdicts lists the failed gate reasons
                grounded=bool(reflection.get('material_defect')) and bool(qms_finding.get('missing_evidence') or failed_gates)
                if reflection.get('proceed') is False and grounded:
                    effect='enacted_block'
                elif reflection.get('proceed') is False:
                    effect='unsupported_block_not_enacted'
                else:
                    effect='no_material_defect' if not reflection.get('material_defect') else 'proceed_with_noted_weakness'
                reflection['effect']=effect;result['reflection']=reflection
                stage_record('reflection','executed',role='reflection',workflow_id=s['workflow_id'],
                             outputs={'proceed':reflection.get('proceed'),'material_defect':reflection.get('material_defect')},effect=effect,started=reflection_started)
        transitions={'existing_workflow':'prepare_response','public_manual':'prepare_grounded_response',
            'clarify':'request_clarification','human_review':'escalate_human_review',
            'safety_review':'escalate_safety_review','part_shipment':'propose_part_movement',
            'device_return':'propose_return','replacement':'propose_replacement'}
        result['evidence_ids']=facts['evidence_ids']
        result['required_prerequisites']=next((p.get('hard_gate_verdicts',[]) for p in result.get('plans',[])
            if p.get('action')==result.get('selected_action')),[])
        result['authorized_transition']=transitions.get(result.get('selected_action'),'escalate_human_review')
        if (result.get('reflection') or {}).get('effect')=='enacted_block':
            # A grounded reflection block is enacted: no operation, human review.
            result['authorized_transition']='escalate_human_review'
            result['reflection_block']={'weakness':result['reflection'].get('weakness'),'recommended_check':result['reflection'].get('recommended_check')}
        result['coverage_gap']=qms_finding.get('missing_evidence',[])
        result['consumed_specialist_tasks']=[x['task_id'] for x in self.store.query(
            "SELECT task_id FROM delegated_tasks WHERE case_id=? AND workflow_id=? AND status='completed'",
            (s['cid'],s['workflow_id']))]
        return result

    def _operate(self,s):
        run=self.store.one('SELECT * FROM workflow_runs WHERE workflow_id=?',(s['workflow_id'],))
        if run['result']:return json.loads(run['result'])
        if run['legacy_started']:
            self.store.update('cases','case_id',s['cid'],{'status':'Human Review Needed','ai_state':'completed'})
            return {'case_id':s['cid'],'status':'Human Review Needed','investigation_incomplete':True,
                    'reason':'Interrupted after domain work began; inspect preserved records before retrying'}
        self.store.update('workflow_runs','workflow_id',s['workflow_id'],{'legacy_started':1})
        result=self.flow._pre_investigate(
            s['cid'], planning=s.get('planning'),
            customer_claim=s.get('erp',{}).get('claim'),
            qms_evidence=s.get('qms'))
        plan=s.get('planning') or {};coordinator=Coordinator(self.store,s['workflow_id'])
        proposed=bool(result.get('proposal'))
        expected=plan.get('authorized_transition','escalate_human_review')
        if expected.startswith('propose_') and not proposed:
            execution_status='blocked';state='Human Review Needed'
        elif proposed:
            execution_status='deferred_for_human_approval';state=result.get('status','Human Review Needed')
        else:
            execution_status='completed' if expected in ('prepare_response','prepare_grounded_response','request_clarification') else 'deferred'
            state=result.get('status','Human Review Needed')
        transition=coordinator.transition(case_id=s['cid'],plan=plan,
            permitted_command=expected,resulting_state=state,execution_status=execution_status,
            prerequisites=plan.get('required_prerequisites',[]),
            human_approvals=['inventory_movement'] if proposed else [],
            detail={'proposal_id':(result.get('proposal') or {}).get('rma_id'),
                    'operations_reused_qms_contract':bool(s.get('qms',{}).get('evidence_contract'))})
        result['plan_transition']=transition
        self.store.update('workflow_runs','workflow_id',s['workflow_id'],{'result':json.dumps(result,default=str)})
        return result

    def _review(self,s):
        draft=self.store.one("SELECT * FROM messages WHERE case_id=? AND status='draft' AND direction='outbound' ORDER BY created_at DESC LIMIT 1",(s['cid'],))
        if not draft:return {'outcome':'no_customer_reply','model_used':False}
        result={'outcome':'draft_requires_human_approval','message_id':draft['message_id'],'model_used':False}
        if (s.get('planning',{}).get('mode')=='tree_of_thought' or self.execution_policy.get('debate')) and self.flow.agent.prefer_llm and inference.enabled('communication_review'):
            ok,remaining=stage_budget('review_model')
            if not ok:
                stage_record('clarity_review','budget_exhausted',role='communication_review',workflow_id=s['workflow_id'],reason=f'{remaining}s left')
            else:
                try:
                    critique=role_contracts.call('review_clarity',{'customer_request':s['body'],'draft':draft['body']})
                    result.update(model_used=True,critique=critique)
                except Exception as exc:result['critique_error']=str(exc)
        if self.execution_policy.get('debate') and self.flow.agent.prefer_llm and inference.current():
            result['debate']=self._debate(s,draft)
        return result

    def _debate(self,s,draft):
        """H (rc.25): critic and judge see the authorized passages; an upheld,
        sourced objection drives one bounded explicit revision (or an explicit
        unresolved decision) BEFORE independent validation. Judge approval never
        bypasses the validator or the send gate."""
        started=time.perf_counter();ok,remaining=stage_budget('critic')
        if not ok:
            stage_record('critic_judge','budget_exhausted',role='communication_review',workflow_id=s['workflow_id'],reason=f'{remaining}s left')
            return {'status':'budget_exhausted','authority':'advisory_only'}
        ids=(s.get('qms') or {}).get('sources',[]) or json.loads(draft.get('metadata') or '{}').get('source_chunk_ids',[])
        passages=self._passages(ids)
        critic=role_contracts.call('critique_draft',{'request':s['body'],'draft':draft['body'],'authorized_passages':passages})
        judge=role_contracts.call('judge_debate',{'draft':draft['body'],'critic':critic,'authorized_passages':passages})
        debate={'critic':critic,'judge':judge,'authority':'advisory_only','passages':[p['id'] for p in passages]}
        if judge.get('proceed_to_validator') is not False or not str(critic.get('strongest_objection') or '').strip():
            debate['effect']='no_change_needed'
        else:
            from . import revision
            objection={'kind':'remove' if critic.get('draft_span') else 'change','draft_span':critic.get('draft_span') or '',
                       'rule':critic.get('strongest_objection'),'source_ids':critic.get('source_ids') or [],'correction':critic.get('strongest_objection')}
            outcome=revision.revise(self.store,s['cid'],draft['message_id'],objections=[objection],authorizer='methodology:critic_judge',
                authority='methodology_critic',idempotency_key=s['workflow_id']+':critic_judge',
                method={'methodology_id':self.execution_policy.get('methodology_id'),'stage':'critic_judge'},presented_source_ids=[p['id'] for p in passages],
                prefer_llm=self.flow.agent.prefer_llm,escalate_case=False)
            debate['revision']={k:outcome.get(k) for k in ('outcome','message_id','revision_id','reason')}
            debate['effect']='revised' if outcome.get('ok') else 'explicit_unresolved:'+str(outcome.get('outcome'))
        stage_record('critic_judge','executed',role='communication_review',workflow_id=s['workflow_id'],
                     outputs={'judge_proceeds':judge.get('proceed_to_validator')},effect=debate['effect'],started=started)
        return debate

    def _validate(self,s):
        from . import independent_review, telemetry
        cycle=int(s.get('validation_cycle',0))
        workflow_id=s.get('workflow_id') or 'DIRECT-VALIDATION-'+s['cid']
        if not s.get('qms'):
            prior=self.store.one("SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research' ORDER BY at DESC LIMIT 1",(s['cid'],))
            if prior:s={**s,'qms':json.loads(prior['payload'])}
        package_row,package=independent_review.freeze(self.store,s['cid'],workflow_id,s,cycle)
        semantic=None
        draft=package.get('draft')
        if draft and self.flow.agent.prefer_llm and inference.current() and inference.enabled('independent_validation'):
            from . import qms,qms_policy
            case=self.store.case(s['cid']);facts=response_policy.case_facts(self.store,s['cid'])
            device=facts.get('device')
            context={'model':case.get('model'),'revision':case.get('revision'),
                     'revision_context':identity.revision_context(device) if device else {},
                     'compatibility':qms.load_compatibility(),'safety_hold':bool(case.get('safety_hold'))}
            allowed=set(s.get('qms',{}).get('sources',[]))|{r['chunk_id'] for r in self.store.query("SELECT chunk_id FROM retrieved_sources WHERE case_id=? AND status='USED'",(s['cid'],))}
            evidence=[{'chunk_id':c.chunk_id,'text':c.text,'model':c.model,'applicable_revisions':c.applicable_revisions} for c in qms.load_chunks() if c.chunk_id in allowed and qms_policy.authorize(c,operation='customer_answer',recipient='customer',context=context).allowed]
            try:
                deterministic=response_policy.validate_case(self.store,s['cid'],draft['body'])
                proposal=role_contracts.call('validate_draft',
                    {'request':s['body'],'draft':draft['body'].split('[For representative:')[0].rstrip(),'authorized_public_evidence':evidence,'deterministic_checks':deterministic,'identity':s.get('erp',{}).get('verification',{}),'human_approval':'All customer sending and consequential movements require separate recorded human authorization'})
                proposal=generation.review_conclusion(proposal)
                proposal['review_scope']='customer-visible text; internal representative annotation excluded just as at sending'
                self.store.insert('response_reviews',dict(review_id=uid('REVIEW'),message_id=draft['message_id'],body_hash=__import__('hashlib').sha256(draft['body'].encode()).hexdigest(),passed=int(proposal['passed']),payload=json.dumps(proposal),at=time.time()))
                semantic=proposal
            except Exception as exc:
                semantic={'passed':False,
                    'issues':['Independent semantic review was unavailable; automatic acceptance is not permitted.'],
                    'review_error':str(exc),'review_unavailable':True,
                    'policy':'deterministic guards remain authoritative; semantic outage fails closed to rework/review'}
        concerns=[(f['role'],(json.loads(f['payload'] or '{}').get('supervisor_stop') or {})) for f in self.store.query(
            'SELECT role,payload FROM specialist_findings WHERE case_id=? AND command_id LIKE ?',(s['cid'],workflow_id+':%'))]
        concerns=[(r,c) for r,c in concerns if c]
        if concerns:
            semantic=dict(semantic or {'passed':True,'issues':[]})
            semantic['supervisor_concerns']=[{'role':r,**c} for r,c in concerns]
        verdict=independent_review.evaluate(self.store,package_row,package,semantic)
        issues=json.loads(verdict['issues'])
        result={'passed':bool(verdict['passed']),'issues':issues,
            'package_id':package_row['package_id'],'package_hash':package_row['package_hash'],
            'verdict_id':verdict['verdict_id'],'correction_target':verdict['correction_target'],
            'human_escalation':bool(verdict['human_escalation']),'cycle':cycle,
            'authority':'independent_frozen_evidence_validator','semantic_review':semantic}
        if not result['passed']:
            telemetry.emit(self.store,'validation.failed',case_id=s['cid'],workflow_id=workflow_id,
                agent_id='independent_validation',correlation_id=verdict['verdict_id'],
                task_status='failed',summary=f'Validation failed with {len(issues)} issue(s)',
                evidence_refs=[package_row['package_id'],verdict['verdict_id']],
                detail={'categories':[i['issue_category'] for i in issues],
                        'human_escalation':result['human_escalation']})
        return result

    def _correct(self,s):
        from . import independent_review, telemetry
        current=int(s.get('validation_cycle',0));next_cycle=current+1
        package_row=self.store.one('SELECT * FROM validation_packages WHERE package_id=?',
                                   (s['validation']['package_id'],))
        package=json.loads(package_row['payload'])
        verdict=self.store.one('SELECT * FROM validation_verdicts WHERE verdict_id=?',
                               (s['validation']['verdict_id'],))
        telemetry.emit(self.store,'correction.requested',case_id=s['cid'],workflow_id=s['workflow_id'],
            agent_id='independent_validation',sender_agent_id='independent_validation',
            receiver_agent_id='communication_correction',correlation_id=verdict['verdict_id'],
            task_status='requested',summary='Bounded artifact correction requested',
            evidence_refs=[verdict['verdict_id']])
        result=independent_review.correct(self.store,package,verdict,next_cycle)
        telemetry.emit(self.store,'correction.completed',case_id=s['cid'],workflow_id=s['workflow_id'],
            agent_id='communication_correction',sender_agent_id='communication_correction',
            receiver_agent_id='independent_validation',correlation_id=verdict['verdict_id'],
            task_status=result.get('status'),summary='Artifact correction completed' if result.get('changed') else 'Artifact correction could not proceed',
            evidence_refs=[x for x in (result.get('original_artifact_id'),result.get('corrected_artifact_id')) if x])
        return {**result,'validation_cycle':next_cycle}

    def _learning(self,s):
        device=s.get('erp',{}).get('device')
        if not device:return {'recalled':[],'reason':'No verified equipment; reviewed memory cannot supply equipment authority'}
        result=self.flow.agent.toolbox.recall_history(SimpleNamespace(**device),s['body'])
        return {'tool':result.tool,'observation':result.summary,'evidence':result.data,
                'policy':'Reviewed, scoped advisory recall only; no automatic promotion'}

    def _await_review(self,s):
        from . import telemetry
        validation=s.get('validation') or {}
        if validation and not validation.get('passed'):
            # Unresolved validation (critical issue or correction exhausted) is a
            # visible handoff even when nothing is pending: the case goes to the
            # human review queue with the reason recorded (once per workflow).
            from .caseflow import HUMAN_REVIEW
            already=self.store.one("SELECT 1 FROM audit_events WHERE case_id=? AND event_type='validation_unresolved_handoff' AND detail LIKE ?",(s['cid'],'%'+str(s.get('workflow_id'))+'%'))
            if not already and self.store.case(s['cid'])['status'] not in ('Resolved','Closed'):
                self.store.update('cases','case_id',s['cid'],{'status':HUMAN_REVIEW})
                self.store.audit('validation_unresolved_handoff',actor='independent_validation',case_id=s['cid'],
                    summary='Validation unresolved after correction; routed to a person; release blocked',
                    detail={'workflow_id':s.get('workflow_id'),'cycle':s.get('validation_cycle'),
                            'human_escalation':bool(validation.get('human_escalation')),
                            'issues':(validation.get('issues') if isinstance(validation.get('issues'),list) else None)})
        # Resume is a request to re-check domain decisions, never permission.
        while True:
            if self.store.case(s['cid'])['status'] in ('Resolved','Closed'):
                from . import review_requests
                review_requests.settle(self.store,s['cid'])
                return {}
            pending=self.store.query("SELECT message_id AS id,'email' AS kind FROM messages WHERE case_id=? AND status='draft' AND direction='outbound' UNION ALL SELECT rma_id AS id,'movement' AS kind FROM rmas WHERE case_id=? AND status='proposed'",(s['cid'],s['cid']))
            if not pending and not s['result'].get('investigation_incomplete'):
                from . import review_requests
                review_requests.settle(self.store,s['cid'])
                return {}
            # rc.25 (A1): one stable request per pending set (not per resume);
            # closed by review_requests.settle when handled.
            from . import review_requests
            review_requests.request(self.store,s['cid'],s['workflow_id'],pending,
                reason='investigation incomplete' if not pending else 'named domain approvals required')
            interrupt({'case_id':s['cid'],'pending':pending,'reason':'Existing named domain approvals or recovery review required'})

    @staticmethod
    def _after_validation(s):
        value=s['validation']
        if value['passed']:
            return 'learning'
        if value.get('human_escalation') or int(s.get('validation_cycle',0)) >= 2:
            return 'human_review'
        return 'correct'

    def _graph(self,checkpointer):
        graph=StateGraph(State)
        definitions=[('interpret','ticket_interpretation','intake',self._interpret),('safety','safety_complaint','safety',self._safety),
            ('erp','erp_research','erp',self._erp),('qms','qms_research','qms',self._qms),
            ('document','documentation',None,self._document),('plan','resolution_planning','planning',self._plan),
            ('operate','operations','result',self._operate),('review','communication_review',None,self._review),
            ('validate','independent_validation','validation',self._validate),
            ('correct','communication_correction','correction',self._correct),
            ('learning','reviewed_learning',None,self._learning)]
        for name,role,key,fn in definitions:
            def node(s,role=role,key=key,fn=fn):
                value=self._finding(s,role,fn)
                if key=='correction':
                    return {key:value,'validation_cycle':value['validation_cycle']}
                return {key:value} if key else {}
            graph.add_node(name,node)
        def parallel_evidence(s):
            from .phase4_runtime import collect_initial_evidence
            concurrency=3 if self.execution_policy.get('parallel_initial_evidence',True) else 1
            return {'parallel_evidence':collect_initial_evidence(self.store,s,max_concurrency=concurrency)}
        graph.add_node('parallel_evidence',parallel_evidence)
        graph.add_node('human_review',self._await_review)
        graph.add_edge(START,'interpret');graph.add_edge('interpret','parallel_evidence')
        graph.add_edge('parallel_evidence','safety')
        graph.add_conditional_edges('safety',lambda s:'document' if s['intake']['outbound_message']['decision']!='draft_for_review' else 'erp')
        graph.add_edge('erp','qms');graph.add_edge('qms','document');graph.add_edge('document','plan')
        for a,b in [('plan','operate'),('operate','review'),('review','validate'),('correct','validate'),('learning','human_review')]:graph.add_edge(a,b)
        graph.add_conditional_edges('validate',self._after_validation)
        graph.add_edge('human_review',END)
        return graph.compile(checkpointer=checkpointer)

    def run(self,cid,resume=False):
        with self.repo.lease(cid),generation.case_budget(inference.current()['snapshot'].get('case_budget_seconds',300) if inference.current() else 240):
            last=self.store.one("SELECT * FROM messages WHERE case_id=? AND direction='inbound' ORDER BY created_at DESC LIMIT 1",(cid,))
            old=self.store.one('SELECT * FROM workflow_runs WHERE case_id=? AND message_id=? ORDER BY at DESC LIMIT 1',(cid,last['message_id']))
            continuing=bool(old and (old['status']=='running' or old['status']=='paused' and resume))
            wid=old['workflow_id'] if continuing else uid('GRAPH')
            if old and old['status']=='paused' and not resume:
                self.store.update('workflow_runs','workflow_id',old['workflow_id'],{'status':'superseded'})
                self.store.audit('workflow_reinvestigation_requested',actor='orchestrator',case_id=cid,
                    summary='Fresh investigation requested; prior paused checkpoint preserved',detail={'previous_workflow_id':old['workflow_id'],'new_workflow_id':wid})
            if not continuing:self.store.insert('workflow_runs',dict(workflow_id=wid,case_id=cid,message_id=last['message_id'],status='running',legacy_started=0,result=None,at=timestamp()))
            # Effective methodology for THIS pass (evaluator compares it with
            # the run's requested configuration, per investigation).
            from .candidate_execution import methodology_record
            activity.emit('methodology','Investigation under the selected methodology','running',
                          methodology_record(self.execution_policy,wid,self.execution_policy.get('_trigger','unspecified'),resume=continuing))
            from .lab_faults import crash_point
            crash_point('workflow_started')
            with SqliteSaver.from_conn_string(self.store.path) as saver:
                graph=self._graph(saver);config={'configurable':{'thread_id':wid},'recursion_limit':24}
                fresh={'cid':cid,'workflow_id':wid,'message_id':last['message_id'],'body':last['body'],'started':time.time()}
                if continuing:
                    argument=Command(resume={'request':'recheck_recorded_approvals'}) if resume or old['status']=='paused' else None
                    # Interrupted before the first checkpoint: nothing to
                    # resume, so this workflow starts from its input.
                    if argument is None and not graph.get_state(config).values:argument=fresh
                else:argument=fresh
                try:value=graph.invoke(argument,config)
                except Exception as exc:
                    self.store.update('workflow_runs','workflow_id',wid,{'status':'failed'})
                    activity.emit('coordination','Workflow stopped; preserved checkpoint and domain history','failed',{'workflow_id':wid,'error':str(exc)})
                    raise
                paused=bool(graph.get_state(config).interrupts)
                self.store.update('workflow_runs','workflow_id',wid,{'status':'paused' if paused else 'completed'})
                result=value.get('result') or json.loads((old or {}).get('result') or '{}')
                result['experimental_execution_policy']={
                    'force_tree':bool(self.execution_policy.get('force_tree')),
                    'max_depth':self.execution_policy.get('max_depth',planning.MAX_DEPTH),
                    'max_states':self.execution_policy.get('max_states',planning.MAX_EXPANDED_STATES),
                    # rc.27: the value actually used (topology C runs initial evidence sequentially), not a constant.
                    'parallel_initial_evidence':self.execution_policy.get('parallel_initial_evidence',True) is not False,
                    'adapter':self.execution_policy.get('adapter','candidate_a_baseline'),
                    'methodology_id':self.execution_policy.get('methodology_id')}
                from . import telemetry
                if not paused:
                    telemetry.emit(self.store,'workflow.completed',case_id=cid,workflow_id=wid,
                        agent_id='coordinator',task_status='completed',summary='Workflow completed')
                activity.emit('coordination','Workflow paused for domain review' if paused else 'Workflow completed','awaiting_human_review' if paused else 'completed',{'workflow_id':wid,'framework':'LangGraph','case_version':self.repo.version(cid)})
                return {**result,'workflow_id':wid,'paused_for_review':paused}


class ConversationCoordinator:
    """Live chat uses the same graph framework and original guarded responder.

    Case specialists take over at handoff. Pre-case turns retain the existing
    ChatSession memory and its separately validated public-manual workflow.
    """
    def __init__(self,bot):self.bot=bot;self.store=bot.store

    def run(self,session,text):
        cid=session.conversation_id;turn=uid('TURN-GRAPH');holder={}
        version=len(session.turns)
        def record(role,payload,elapsed,wall=None):
            calls=self.store.query('SELECT call_id,role,endpoint_id,model,status FROM inference_calls WHERE conversation_id=? AND at>=?',(cid,wall)) if wall is not None else []
            payload['inference_usage']={'llm_requested':bool(calls),'completed_calls':sum(c['status']=='completed' for c in calls),'calls':calls}
            self.store.insert('conversation_findings',dict(finding_id=uid('CHAT-FIND'),conversation_id=cid,turn_id=turn,
                role=role,based_on_version=version,payload=json.dumps(payload,default=str),at=timestamp()))
            activity.emit('specialist',role+' chat finding recorded','completed',{'role':role,'finding':payload,'turn_id':turn},elapsed)
        def intake(state):
            begin=time.perf_counter();finding=interpretation.interpret(text,'chat',turn)
            record('ticket_interpretation',finding,(time.perf_counter()-begin)*1000)
            return {'intake':finding}
        def respond(state):
            begin=time.perf_counter();wall=time.time();holder['reply']=self.bot._respond(session,text)
            record('chat_investigation',{'public_manual_and_react':'existing guarded shared support workflow',
                'case_id':session.handoff_case_id,'verified':session.verified,'source_ids':[s.get('procedure_id') for s in holder['reply'].sources]},(time.perf_counter()-begin)*1000,wall)
            return {'responded':True}
        def validate(state):
            reply=holder['reply']
            record('independent_validation',{'outcome':'Existing ChatBot._finish grounding, authorization and consistency checks completed',
                'delivered_text':reply.text,'handed_off':session.handed_off},0)
            return {}
        with CaseRepository(self.store).lease(cid),generation.case_budget(inference.current()['snapshot'].get('case_budget_seconds',300) if inference.current() else 240):
            with SqliteSaver.from_conn_string(self.store.path) as saver:
                graph=StateGraph(dict);graph.add_node('intake',intake);graph.add_node('guarded_chat',respond);graph.add_node('validation',validate)
                graph.add_edge(START,'intake');graph.add_edge('intake','guarded_chat');graph.add_edge('guarded_chat','validation');graph.add_edge('validation',END)
                graph.compile(checkpointer=saver).invoke({'conversation_id':cid,'turn_id':turn},{'configurable':{'thread_id':turn},'recursion_limit':8})
        return holder['reply']
