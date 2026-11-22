"""Verified Testing Lab adapters for immutable experimental architectures."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .coordination import uid,timestamp


def _settings(architecture,component):
    return dict((architecture.get('settings') or {}).get(component) or {})


def execution_plan(architecture):
    architecture=architecture or {'id':'candidate-a','components':[]}
    identifier=architecture.get('id')
    components=set(architecture.get('components') or [])
    methodology=architecture.get('methodology')
    if methodology:
        topology=dict(methodology['topology']);treatment=dict(methodology['treatment'])
        return {'adapter':topology['adapter'],'methodology_id':identifier,
            'topology_id':topology['id'],'treatment_id':treatment['id'],
            'reasoning':treatment['reasoning'],'retrieval':treatment['retrieval'],
            'arrangement':topology['agent_arrangement'],
            'communication':topology['communication'],'supervision':topology['supervision'],
            'max_agents':topology['max_agents'],
            'parallel_initial_evidence':bool(topology.get('parallel_initial_evidence')),
            'supervisor_checkpoints':bool(topology.get('supervisor_checkpoints')),
            'peer_exchange':bool(topology.get('peer_exchange')),
            'hierarchical_delegation':bool(topology.get('hierarchical_delegation')),
            'debate':bool(topology.get('debate')),'reflection':bool(treatment.get('reflection')),
            'repair_limit':max(0,min(2,int(treatment.get('repair_limit',2)))),
            'force_tree':bool(treatment.get('force_tree')),
            'max_depth':int(treatment.get('max_depth',3)),
            'max_states':int(treatment.get('max_states',6))}
    if identifier=='candidate-a':
        return {'adapter':'candidate_a_baseline','reasoning':'react_reflexion',
            'arrangement':'existing_langgraph_specialists','force_tree':False}
    if identifier=='candidate-b-direct':
        required={'reasoning.direct_guarded','agents.single_guarded','orchestration.deterministic',
            'validation.mandatory_multistage'}
        if not required<=components:raise ValueError('Candidate B execution components do not match its verified adapter')
        return {'adapter':'candidate_b_direct_v1','reasoning':'direct_guarded',
            'arrangement':'single_agent','force_tree':False,
            'repair_limit':int(_settings(architecture,'validation.mandatory_multistage').get('repair_limit',2))}
    if identifier=='candidate-c-tot':
        required={'reasoning.selective_bounded_tot','agents.parallel_specialists',
            'orchestration.langgraph','validation.mandatory_multistage'}
        if not required<=components:raise ValueError('Candidate C execution components do not match its verified adapter')
        configured=_settings(architecture,'reasoning.selective_bounded_tot')
        return {'adapter':'candidate_c_tot_v1','reasoning':'bounded_tree_of_thought',
            'arrangement':'parallel_initial_then_specialist_graph','force_tree':True,
            'max_depth':int(configured.get('max_depth',3)),
            'max_states':int(configured.get('max_states',6))}
    raise ValueError('Architecture has no verified Testing Lab execution adapter')


STAGE_KEYS=('supervisor_checkpoints','peer_exchange','hierarchical_delegation','debate','reflection','force_tree')


def methodology_record(policy,workflow_id,trigger,*,resume):
    """What a workflow pass actually executed with (one per investigation)."""
    policy=policy or {}
    return {'workflow_id':workflow_id,'trigger':trigger,'resume':bool(resume),
            'adapter':policy.get('adapter','candidate_a_baseline'),'methodology_id':policy.get('methodology_id'),
            'topology_id':policy.get('topology_id'),'treatment_id':policy.get('treatment_id'),
            'reasoning':policy.get('reasoning'),'retrieval':policy.get('retrieval'),
            **{k:bool(policy.get(k)) for k in STAGE_KEYS}}


def _event(store,event_type,cid,wid,architecture,plan,status,summary,detail=None):
    from . import telemetry
    return telemetry.emit(store,event_type,case_id=cid,workflow_id=wid,
        agent_id='coordinator',correlation_id=wid,task_status=status,summary=summary,
        detail={'candidate_architecture_id':architecture.get('id'),
            'candidate_configuration_hash':architecture.get('configuration_hash'),
            'execution_adapter':plan['adapter'],**(detail or {})})


def execute(flow,cid,run):
    """Execute a verified adapter. Candidate A behavior is deliberately untouched."""
    architecture=run.get('architecture') or {'id':'candidate-a'};plan=execution_plan(architecture)
    fault=dict((run.get('scenario') or {}).get('fault_policy') or {})
    if fault:
        delay=max(0,min(2000,int(fault.get('delay_ms',0))))
        if delay:time.sleep(delay/1000)
        failures=max(0,min(3,int(fault.get('preflight_failures',0))))
        retries=max(0,min(3,int(fault.get('max_retries',0))))
        state_path=Path(run['directory'])/'synthetic-fault-state.json'
        state=json.loads(state_path.read_text()) if state_path.exists() else {'attempts':0,'failures':0}
        while state['failures']<failures:
            state['attempts']+=1;state['failures']+=1
            temporary=state_path.with_suffix('.tmp');temporary.write_text(json.dumps(state),encoding='utf-8');os.replace(temporary,state_path)
            from . import telemetry
            telemetry.emit(flow.store,'tool.failed',case_id=cid,workflow_id=run['id'],
                agent_id='coordinator',correlation_id=run['id'],task_status='retrying',
                summary='Injected experiment-local preflight tool failure',
                detail={'synthetic':True,'physical_inference':False,'attempt':state['attempts'],
                    'failure_budget':failures,'retry_budget':retries})
            if state['failures']>retries:raise OSError('Injected experiment-local preflight failure exhausted retry budget')
        if failures:
            from . import telemetry
            telemetry.emit(flow.store,'tool.completed',case_id=cid,workflow_id=run['id'],
                agent_id='coordinator',correlation_id=run['id'],task_status='completed',
                summary='Experiment-local preflight tool recovered within retry budget',
                detail={'synthetic':True,'physical_inference':False,'failures':state['failures']})
    if plan['adapter'] in ('candidate_a_baseline','specialist_graph'):
        from .orchestration import CaseOrchestrator
        _event(flow.store,'experiment.candidate.started',cid,run['id'],architecture,plan,'running',
            'Candidate A baseline execution started',{'experiment_id':run.get('experiment_id'),
             'trial_id':run.get('id'),'scenario_id':(run.get('scenario') or {}).get('id')})
        result=CaseOrchestrator(flow,policy_for(architecture,'initial')[1]).run(cid)
        _event(flow.store,'experiment.candidate.completed',cid,result.get('workflow_id') or run['id'],architecture,plan,
            'completed','Candidate A baseline execution completed',{'experiment_id':run.get('experiment_id'),
             'trial_id':run.get('id'),'scenario_id':(run.get('scenario') or {}).get('id')})
        return result
    if plan['adapter']=='candidate_c_tot_v1':
        from .orchestration import CaseOrchestrator
        _event(flow.store,'experiment.candidate.started',cid,run['id'],architecture,plan,'running',
            'Candidate C bounded Tree-of-Thought execution started',{'experiment_id':run.get('experiment_id'),
             'trial_id':run.get('id'),'scenario_id':(run.get('scenario') or {}).get('id'),
             'max_depth':plan['max_depth'],'max_states':plan['max_states']})
        result=CaseOrchestrator(flow,{**plan,'_trigger':'initial'}).run(cid)
        _event(flow.store,'experiment.candidate.completed',cid,result.get('workflow_id') or run['id'],architecture,plan,
            'completed','Candidate C bounded Tree-of-Thought execution completed',
            {'experiment_id':run.get('experiment_id'),'trial_id':run.get('id'),
             'scenario_id':(run.get('scenario') or {}).get('id')})
        return result
    return _execute_direct(flow,cid,run,architecture,plan)


def _execute_direct(flow,cid,run,architecture,plan):
    from . import activity
    from .orchestration import CaseOrchestrator
    wid=uid('DIRECT');message=flow.store.one(
        "SELECT * FROM messages WHERE case_id=? AND direction='inbound' ORDER BY created_at DESC LIMIT 1",(cid,))
    flow.store.insert('workflow_runs',dict(workflow_id=wid,case_id=cid,
        message_id=message['message_id'],status='running',legacy_started=1,result=None,at=timestamp()))
    flow.agent.experimental_reasoning='direct_guarded'
    _event(flow.store,'experiment.candidate.started',cid,wid,architecture,plan,'running',
        'Direct guarded candidate execution started',{'reasoning_steps':1,'agent_count':1,
        'experiment_id':run.get('experiment_id'),'trial_id':run.get('id'),
        'scenario_id':(run.get('scenario') or {}).get('id')})
    with activity.investigation(flow.store,case_id=cid):
        activity.emit('methodology','Investigation under the selected methodology','running',
                      methodology_record(plan,wid,run.get('_trigger','initial'),resume=False))
        result=flow._pre_investigate(cid,run_id=run.get('id'),planning={
            'mode':'direct_linear','selected_action':'existing_workflow',
            'selected_plan_id':None,'plans':[],'model_proposal_used':False,
            'authorized_transition':'prepare_response','required_prerequisites':[]})
        validator=CaseOrchestrator(flow,plan)
        state={'cid':cid,'workflow_id':wid,'message_id':message['message_id'],
            'body':message['body'],'started':time.time(),'result':result,
            'planning':{'mode':'direct_linear'},'validation_cycle':0}
        if result.get('authorized_evidence'):
            # rc.25: validate against the evidence this direct pass authorized
            # (before: an empty package, so every cited chunk was "outside").
            state['qms']=result['authorized_evidence']
        validation=validator._validate(state);cycles=[]
        while not validation['passed'] and not validation.get('human_escalation') and len(cycles)<plan['repair_limit']:
            correction=validator._correct({**state,'validation':validation,
                'validation_cycle':len(cycles)})
            cycles.append(correction)
            validation=validator._validate({**state,'validation_cycle':len(cycles),
                'correction':correction})
        result['candidate_execution']={**plan,'workflow_id':wid,'validation':validation,
            'correction_cycles':cycles,'component_hashes':architecture.get('component_hashes',{}),
            'operational_activation_permitted':False}
        pending=bool(flow.store.one(
            "SELECT 1 FROM messages WHERE case_id=? AND status='draft' AND direction='outbound' UNION ALL SELECT 1 FROM rmas WHERE case_id=? AND status='proposed' LIMIT 1",(cid,cid)))
        status='paused' if pending or not validation['passed'] else 'completed'
        if not validation['passed']:
            flow.store.update('cases','case_id',cid,{'status':'Human Review Needed','ai_state':'completed'})
        flow.store.update('workflow_runs','workflow_id',wid,{'status':status,
            'result':json.dumps(result,default=str)})
        _event(flow.store,'experiment.candidate.completed' if validation['passed'] else
            'experiment.candidate.failed_safety_gate',cid,wid,architecture,plan,
            'waiting' if status=='paused' else 'completed',
            'Direct candidate passed independent validation' if validation['passed'] else
            'Direct candidate failed independent validation',
            {'validation_passed':validation['passed'],'correction_cycles':len(cycles),
             'synthetic_work_only':True})
        return {**result,'workflow_id':wid,'paused_for_review':status=='paused'}


def policy_for(architecture,trigger):
    """The execution policy for one investigation or continuation.

    The run's immutable architecture is authoritative for EVERY workflow
    pass (initial, customer follow-up, reviewer request, checkpoint resume,
    recovery). ``_trigger`` only labels why the pass ran."""
    architecture=architecture or {'id':'candidate-a'}
    plan=execution_plan(architecture)
    if architecture.get('methodology') or plan['adapter']=='candidate_c_tot_v1':
        return plan,{**plan,'_trigger':trigger}
    return plan,{'adapter':plan['adapter'],'_trigger':trigger}


def continue_investigation(flow,cid,architecture,*,trigger,run=None):
    """A new investigation of the latest customer message under the run's
    selected adapter (replaces the default-orchestrator pre_investigate)."""
    from . import activity
    from .caseflow import hand_off_failure
    plan,policy=policy_for(architecture,trigger)
    if plan['adapter'] in ('candidate_b_direct_v1','single_direct'):
        return _execute_direct(flow,cid,{**(run or {}),'_trigger':trigger},architecture or {},plan)
    from .orchestration import CaseOrchestrator
    try:
        with activity.investigation(flow.store,case_id=cid):
            return CaseOrchestrator(flow,policy).run(cid)
    except Exception as exc:
        hand_off_failure(flow.store,cid,'Investigation could not complete',exc)
        raise


def resume_checkpoint(flow,cid,architecture,trigger='checkpoint_resume'):
    plan,policy=policy_for(architecture,trigger)
    if plan['adapter'] in ('candidate_b_direct_v1','single_direct'):
        row=flow.store.one("SELECT workflow_id FROM workflow_runs WHERE case_id=? AND workflow_id LIKE 'DIRECT-%' ORDER BY at DESC LIMIT 1",(cid,))
        return {'ok':True,'resumed':bool(row),'workflow_id':row.get('workflow_id') if row else None,
            'reason':'Direct adapter has no hidden graph continuation; separately recorded reviewer decisions remain authoritative and no investigation was replayed.'}
    from .orchestration import CaseOrchestrator
    # Before rc.21 only the tree-of-thought adapter kept its policy here;
    # specialist-graph methodologies (D, F, G, H...) silently lost theirs.
    return CaseOrchestrator(flow,policy).run(cid,resume=True)
