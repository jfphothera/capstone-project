"""Selective bounded operational Tree-of-Thought, with persisted hard pruning."""
import hashlib
import json
import re
import time
from . import activity, config, generation
from .coordination import uid,timestamp

MAX_DEPTH=3
MAX_EXPANDED_STATES=6

SCHEMA='''
CREATE TABLE IF NOT EXISTS candidate_plans (
 plan_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,run_id TEXT NOT NULL,parent_plan_id TEXT,
 based_on_version INTEGER NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_evaluations (
 evaluation_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,case_id TEXT NOT NULL,
 verdict TEXT NOT NULL,checks TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_runs (
 workflow_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,message_id TEXT NOT NULL,
 status TEXT NOT NULL,legacy_started INTEGER NOT NULL DEFAULT 0,result TEXT,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_findings (
 finding_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,turn_id TEXT NOT NULL,
 role TEXT NOT NULL,based_on_version INTEGER NOT NULL,payload TEXT NOT NULL,at TEXT NOT NULL);
'''

def complex_case(text,intake):
    if intake['outbound_message']['decision']!='draft_for_review': return False
    return bool(re.search(r'\b(?:missing|accessor\w*|replacement|exchange|code|refund)\b|not included|wasn.t included|weren.t included',text,re.I)
                or len(intake['intents'])>1 and re.search(r'return|repair',text,re.I))

TOT_SESSION_ENTRY_CAP=3  # tree-of-thought entries allowed per case (session = the case's lifetime)
CANDIDATE_ACTIONS=('public_manual','part_shipment','device_return','replacement')

def tot_entry(store,cid,text,intake,facts,*,force_tree=False,force_direct=False):
    """Documented, testable Tree-of-Thought entry rule (replaces keyword-only entry).

    Order: (1) hard safety/authorization checks come first: a safety hold or
    a configured direct path never branches; (2) the session budget: at most
    TOT_SESSION_ENTRY_CAP entries per case, after which the safe default is
    human review; (3) branch only when there is real ambiguity:
      * two or more substantive actions pass every hard gate (competing
        feasible actions), or
      * exactly one passes while evidence another action needs is unresolved
        AND the request itself asks for a consequential outcome.
    A request-type signal alone never enters the tree. Returns a record with
    the entry decision, reason, candidates, unresolved evidence and budget.
    """
    feasible=[a for a in CANDIDATE_ACTIONS if not evaluate(a,facts)]
    # Core ambiguities a branch could resolve (not every missing prerequisite).
    unresolved=[name for name,ok in (('exact_identity',facts.get('verified')),('identified_part',facts.get('identified_part'))) if not ok]
    signal=bool(complex_case(text,intake))
    used=store.one('SELECT COUNT(DISTINCT run_id) AS n FROM candidate_plans WHERE case_id=? AND parent_plan_id IS NULL',(cid,))['n']
    record={'feasible_candidates':feasible,'unresolved_evidence':unresolved,'consequential_request_signal':signal,
            'session_entries_used':used,'session_entry_cap':TOT_SESSION_ENTRY_CAP,'max_depth':MAX_DEPTH,'max_expanded_states':MAX_EXPANDED_STATES}
    if facts.get('safety_hold'):return {**record,'enter':False,'reason':'hard check: safety hold blocks branching'}
    if force_direct:return {**record,'enter':False,'reason':'configured direct guarded path'}
    if used>=TOT_SESSION_ENTRY_CAP:return {**record,'enter':False,'budget_exhausted':True,'reason':'session tree-of-thought budget exhausted; safe default is human review'}
    if force_tree:return {**record,'enter':True,'reason':'configured to branch (methodology treatment)'}
    if intake['outbound_message']['decision']!='draft_for_review':return {**record,'enter':False,'reason':'no customer reply is drafted for this record'}
    if len(feasible)>=2:return {**record,'enter':True,'reason':'two or more feasible actions compete'}
    if feasible and unresolved and signal:return {**record,'enter':True,'reason':'one feasible action while evidence for a requested consequential action is unresolved'}
    if not feasible and signal and unresolved:return {**record,'enter':True,'reason':'requested consequential action blocked by unresolved evidence; compare clarification against review'}
    return {**record,'enter':False,'reason':'single feasible path under current evidence'}

def evaluate(action,facts):
    gates=[]
    if action in ('part_shipment','device_return','replacement'):
        if facts.get('operational_qms_missing'):gates.append('Controlled operational QMS support missing')
        if not facts['verified']:gates.append('Exact account/equipment verification missing')
        if action=='part_shipment' and not facts['identified_part']:gates.append('Specific replacement item not established')
        if action=='device_return' and not facts['field_completion']:gates.append('No customer-confirmed approved field completion or reviewed disposition')
        if action=='replacement' and not facts['received_original']:gates.append('Original device not physically received and accepted')
    if facts['safety_hold'] and action not in ('human_review','safety_review'):gates.append('Safety hold blocks ordinary troubleshooting')
    if action=='public_manual' and not facts['public_sources']:gates.append('No applicable customer-disclosable manual retrieved')
    return gates

def plan(store,cid,run_id,text,intake,facts,prefer_llm=False,based_on_version=None,
         *,force_tree=False,force_direct=False,max_depth=MAX_DEPTH,max_expanded_states=MAX_EXPANDED_STATES):
    max_depth=max(1,min(MAX_DEPTH,int(max_depth)))
    max_expanded_states=max(2,min(MAX_EXPANDED_STATES,int(max_expanded_states)))
    version=store.case(cid)['case_version'] if based_on_version is None else based_on_version
    simple_part=bool(facts['verified'] and facts['identified_part'] and not re.search(r'exchange|refund|replace the whole',text,re.I))
    entry=tot_entry(store,cid,text,intake,facts,force_tree=force_tree,force_direct=force_direct)
    if simple_part and not force_tree and entry['enter'] and not facts['safety_hold']:
        entry={**entry,'enter':False,'reason':'verified identity and an exactly identified part: direct guarded path'}
    activity.emit('planning','Tree-of-thought entry: '+('branch' if entry['enter'] else 'single path'),'completed',entry)
    if not entry['enter']:
        action=('safety_review' if facts['safety_hold'] else
                'human_review' if entry.get('budget_exhausted') else
                'part_shipment' if simple_part else 'existing_workflow')
        return {'mode':'single_path','selected_action':action,'plans':[],
                'selected_plan_id':None,'model_proposal_used':False,'entry':entry,
                'exit_reason':entry['reason'],
                'reason':'Configured direct guarded path under current evidence' if force_direct else entry['reason']}
    start=time.perf_counter();actions=['clarify','human_review']
    if re.search(r'missing|accessor|goggle|part',text,re.I):actions+=['part_shipment','device_return']
    elif re.search(r'exchange|replacement',text,re.I):actions+=['replacement','device_return']
    else:actions+=['public_manual','device_return']
    rationales={a:'Evaluate the next bounded investigative/action path against current evidence' for a in actions}
    model_used=False
    preferred=None
    if prefer_llm and generation.llm_available():
        from . import role_contracts
        try:
            model_facts={k:v for k,v in facts.items() if k!='received_original'}
            model_facts['returned_device_received_and_accepted_by_service_center']=facts['received_original']
            proposal=role_contracts.call('compare_plans',{'customer_request':text,'facts':model_facts,'allowed_actions':actions},schema=role_contracts.plan_schema(actions))
            preferred=proposal.get('preferred_action') if proposal.get('preferred_action') in actions else None
            for p in proposal.get('plans',[])[:4]:
                if p.get('action') in actions and isinstance(p.get('rationale'),str):rationales[p['action']]=p['rationale'][:500];model_used=True
        except Exception as exc:activity.emit('planning','Model planner failed; evaluate controlled alternatives','failed',{'error':str(exc)})
    candidates=[]
    sequences={'clarify':['request_missing_fact','reassess_authority'],
        'human_review':['assemble_evidence','request_named_review'],
        'part_shipment':['identify_exact_part','verify_movement_prerequisites','propose_part_shipment'],
        'device_return':['verify_return_prerequisites','propose_return','await_named_approval'],
        'replacement':['confirm_received_original','propose_replacement','await_named_approval'],
        'public_manual':['retrieve_authorized_guidance','prepare_grounded_response']}
    for action in actions[:max(1,min(4,max_expanded_states-1))]:
        pid=uid('PLAN');gates=evaluate(action,facts)
        if store.case(cid)['case_version']!=version:gates.append('Stale case version; proposal must be re-evaluated')
        record={'plan_id':pid,'action':action,'action_sequence':sequences.get(action,[action])[:MAX_DEPTH],
            'evidence_ids':facts.get('evidence_ids',[]),'required_approvals':['inventory_movement'] if action in ('part_shipment','device_return','replacement') else [],
            'prerequisites':list(gates),'feasible':not gates,'rejection_reasons':list(gates),
            'selection_score':None if gates else (2 if action=='human_review' else 1 if action=='clarify' else 0),
            'hard_gate_verdicts':gates,'rationale':rationales[action],'authority':'proposal_only','depth':1,
            'estimated_tool_calls':1 if action=='public_manual' else 0,'model_proposal_used':model_used,
            'configured_max_depth':max_depth,'configured_max_expanded_states':max_expanded_states}
        store.insert('candidate_plans',dict(plan_id=pid,case_id=cid,run_id=run_id,parent_plan_id=None,
            based_on_version=version,payload=json.dumps(record),status='pruned' if gates else 'feasible',at=timestamp()))
        store.insert('plan_evaluations',dict(evaluation_id=uid('EVAL'),plan_id=pid,case_id=cid,verdict='pruned' if gates else 'feasible',checks=json.dumps(gates),at=timestamp()))
        activity.emit('planning',f'Candidate {action}: '+('pruned' if gates else 'feasible'),'blocked' if gates else 'completed',record)
        candidates.append(record)
    feasible=[p for p in candidates if not p['hard_gate_verdicts']]
    # Hard gates run first. No utility score can buy permission to ship.
    priority={'public_manual':0,'part_shipment':0,'device_return':0,'replacement':0,'clarify':1,'human_review':2}
    selected=min(feasible,key=lambda p:priority.get(p['action'],9)) if feasible else None
    if preferred:
        supported=next((p for p in feasible if p['action']==preferred),None)
        if supported:selected=supported
        else:activity.emit('planning','Model-preferred branch rejected by independent constraints','blocked',{'preferred_action':preferred})
    if selected:
        store.execute("UPDATE candidate_plans SET status='selected' WHERE plan_id=?",(selected['plan_id'],))
        # Gather evidence for the chosen branch and persist a depth-two child.
        child=uid('PLAN');refined={**selected,'plan_id':child,'depth':2,'parent_plan_id':selected['plan_id'],
            'rationale':'Re-evaluated selected branch with exact verification, manual applicability and recorded receiving evidence'}
        receiving=store.query('SELECT rma_id,received_at,received_by FROM rmas WHERE case_id=?',(cid,))
        refreshed={**facts,'received_original':any(r['received_at'] and r['received_by'] for r in receiving),
                   'safety_hold':bool(facts['safety_hold'] or store.case(cid).get('safety_hold'))}
        activity.emit('tool','ToT branch evidence refresh: actual receiving and safety records','completed',{'tool':'read_case_receiving','record_count':len(receiving)})
        gates=evaluate(selected['action'],refreshed)
        refined['hard_gate_verdicts']=gates
        refined['evidence_gathering']={'receiving_records':receiving,'safety_hold':refreshed['safety_hold']}
        store.insert('candidate_plans',dict(plan_id=child,case_id=cid,run_id=run_id,parent_plan_id=selected['plan_id'],
            based_on_version=version,payload=json.dumps(refined),status='selected' if not gates else 'pruned',at=timestamp()))
        store.insert('plan_evaluations',dict(evaluation_id=uid('EVAL'),plan_id=child,case_id=cid,verdict='selected' if not gates else 'pruned',checks=json.dumps(gates),at=timestamp()))
        if gates:selected=None
        else:selected=refined
    result={'mode':'tree_of_thought','selected_action':selected['action'] if selected else 'human_review',
        'selected_plan_id':selected['plan_id'] if selected else None,'plans':candidates,'model_proposal_used':model_used,
        'model_preferred_action':preferred,'shared_note_ids':[n['note_id'] for n in facts.get('advisory_case_notes',[])],
        'latency_ms':round((time.perf_counter()-start)*1000),
        'expanded_states':min(max_expanded_states,len(candidates)+bool(selected)),
        'max_depth':max_depth,'max_expanded_states':max_expanded_states,
        'forced_tree':bool(force_tree),'entry':entry,
        'budget_consumed':{'expanded_states':min(max_expanded_states,len(candidates)+bool(selected)),'max_expanded_states':max_expanded_states,
                           'session_entries_used':entry['session_entries_used']+1,'session_entry_cap':entry['session_entry_cap']},
        'exit_reason':('selected '+selected['action']) if selected else 'no feasible branch survived hard gates; human review',
        'selected_action_sequence':selected.get('action_sequence',[]) if selected else ['request_named_review']}
    activity.emit('planning','Selected bounded next-step branch','completed',result)
    return result
