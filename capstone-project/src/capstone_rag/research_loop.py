"""Durable, bounded Phase 6 research planning; approval never means promotion."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from .ai_settings import canonical

FORMAT='capstone-research-loop-v1'


def _hash(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


class ResearchLedger:
    def __init__(self,registry):self.registry=registry
    def ensure(self):
        with self.registry.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS research_iterations (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    def list(self):
        self.ensure()
        with self.registry.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM research_iterations ORDER BY id')]
    def get(self,identifier):return next((row for row in self.list() if row['id']==identifier),None)
    def _put(self,value):
        self.ensure()
        with self.registry.connect() as db:
            db.execute('INSERT OR REPLACE INTO research_iterations VALUES (?,?)',(value['id'],canonical(value)))
        return value
    def propose(self,evidence,policy,authorizer,_inherited=False):
        if evidence.get('format')!='capstone-phase6-comparison-v1':
            raise ValueError('Phase 6 comparison evidence is required')
        if not str(authorizer).strip():raise ValueError('Named research operator required')
        limits={'max_iterations':int(policy.get('max_iterations',5)),
            'max_trials_per_iteration':int(policy.get('max_trials_per_iteration',20)),
            'max_concurrency':int(policy.get('max_concurrency',2)),
            'max_model_calls':int(policy.get('max_model_calls',200)),
            'max_tokens':int(policy.get('max_tokens',200000))}
        if not 1<=limits['max_iterations']<=50 or not 2<=limits['max_trials_per_iteration']<=1000 or not 1<=limits['max_concurrency']<=1024 or not 1<=limits['max_model_calls']<=100000 or not 1<=limits['max_tokens']<=100000000:
            raise ValueError('Research-loop resource limits must be positive and bounded')
        prior=self.list()
        if not _inherited and len(prior)>=limits['max_iterations']:raise ValueError('Authorized research iteration budget exhausted')
        unsafe=[row for row in evidence.get('candidates',[]) if not row.get('safety_eligible')]
        classes={item for row in evidence.get('candidates',[]) for item in row.get('execution_classifications',[])}
        paired=sum(row.get('paired_samples',0) for row in evidence.get('paired_comparisons',[]))
        if unsafe:
            hypothesis={'weakness':'critical safety or authority failures',
                'hypothesis':'A validation or evidence-authority correction can remove the observed severe failure without weakening deterministic gates.',
                'component_family':'validation','experiment':'reproduce failing cases after a reviewable engineering correction'}
            priority='safety_blocker'
        elif 'physical_gateway_inference_observed' not in classes:
            hypothesis={'weakness':'no physical Gateway inference evidence',
                'hypothesis':'The eligible candidate remains stable under authenticated Gateway execution with preserved request/resource correlation.',
                'component_family':'model_assignment','experiment':'paired Gateway acceptance campaign'}
            priority='integration_evidence'
        elif paired<2:
            hypothesis={'weakness':'insufficient paired evidence',
                'hypothesis':'Repeated paired trials on one holdout snapshot will reduce outcome uncertainty.',
                'component_family':'evaluation','experiment':'paired repeated holdout comparison'}
            priority='measurement_quality'
        else:
            hypothesis={'weakness':'remaining performance uncertainty',
                'hypothesis':'Varying one approved component while holding the source snapshot and model assignments fixed will identify a causal candidate for validation.',
                'component_family':'single_component_variation','experiment':'one-variable candidate comparison'}
            priority='controlled_exploration'
        identity={'evidence_hash':_hash(evidence),'hypothesis':hypothesis,'limits':limits}
        hypothesis_hash=_hash(identity)
        duplicate=next((row for row in prior if row.get('hypothesis_hash')==hypothesis_hash),None)
        if duplicate and not str(policy.get('repeat_reason','')).strip():
            raise ValueError('An identical research iteration already exists; an explicit repeat reason is required')
        value={'format':FORMAT,'id':'ITER-'+uuid.uuid4().hex,'status':'proposed',
            'created_at':time.time(),'created_by':str(authorizer).strip(),'priority':priority,
            'hypothesis':hypothesis,'hypothesis_hash':hypothesis_hash,
            'evidence_hash':identity['evidence_hash'],'evidence_refs':policy.get('evidence_refs') or [],
            'resource_limits':limits,'repeat_of':duplicate['id'] if duplicate else None,
            'repeat_reason':policy.get('repeat_reason'),'exploration_reason':policy.get('exploration_reason') or priority,
            'proposed_campaign':{'baseline_hash':evidence.get('baseline_hash'),
                'candidate_hashes':evidence.get('performance_ranking_eligible_only',[])[:4],
                'allowed_architecture_ids':sorted(set(policy.get('allowed_architecture_ids') or ['candidate-a'])),
                'allowed_profile_ids':sorted(set(policy.get('allowed_profile_ids') or [])),
                'allowed_scenario_ids':sorted(set(policy.get('allowed_scenario_ids') or [])),
                'paired':True,'holdout_required':True,'minimum_repetitions':2,
                'max_trials':limits['max_trials_per_iteration'],'concurrency':limits['max_concurrency'],
                'model_call_budget':limits['max_model_calls'],'total_token_budget':limits['max_tokens']},
            'operator_approval_required':True,'automatic_execution_permitted':False,
            'operational_activation_permitted':False,'source_modification_permitted':False,
            'qms_modification_permitted':False,'gateway_policy_change_permitted':False,
            'customer_or_business_action_permitted':False}
        value['iteration_hash']=_hash(value);return self._put(value)
    def approve(self,identifier,authorizer,rationale):
        value=self.get(identifier)
        if not value:raise ValueError('Unknown research iteration')
        if value['status']!='proposed':raise ValueError('Only proposed iterations may be approved')
        if not str(authorizer).strip() or not str(rationale).strip():
            raise ValueError('Named approver and approval rationale required')
        value=copy.deepcopy(value);value.update(status='approved_for_testing_lab',
            approved_at=time.time(),approved_by=str(authorizer).strip(),approval_rationale=str(rationale).strip(),
            automatic_execution_permitted=True,execution_scope='isolated_testing_lab_campaign_only')
        value['authorization_envelope']={**value['resource_limits'],
            'allowed_architecture_ids':value['proposed_campaign']['allowed_architecture_ids'],
            'allowed_profile_ids':value['proposed_campaign']['allowed_profile_ids'],
            'allowed_scenario_ids':value['proposed_campaign']['allowed_scenario_ids'],
            'synthetic_actions_only':True,'operational_actions':False}
        value['approval_hash']=_hash(value)
        return self._put(value)
    def dispatch(self,identifier,planned,manager,source_store,authorizer,rationale):
        value=self.get(identifier)
        if not value:raise ValueError('Unknown research iteration')
        if value['status'] not in ('approved_for_testing_lab','dispatched'):
            raise ValueError('Research iteration requires operator approval before dispatch')
        if not str(authorizer).strip() or not str(rationale).strip():
            raise ValueError('Named dispatcher and dispatch rationale required')
        spec=planned.get('spec') or {};envelope=value['authorization_envelope']
        if spec.get('research_iteration_id')!=identifier:raise ValueError('Preview is not pinned to this research iteration')
        if spec.get('approval_mode')!='scripted':raise ValueError('Approved research dispatch requires experiment-local scripted decisions')
        if int(planned.get('planned_runs',0))>envelope['max_trials_per_iteration']:
            raise ValueError('Planned trials exceed the approved research envelope')
        if int(spec.get('concurrency',1))>envelope['max_concurrency']:
            raise ValueError('Concurrency exceeds the approved research envelope')
        if not spec.get('model_call_budget') or not spec.get('total_token_budget'):
            raise ValueError('Research dispatch requires explicit model-call and token budgets')
        if int(spec['model_call_budget'])>envelope['max_model_calls'] or int(spec['total_token_budget'])>envelope['max_tokens']:
            raise ValueError('Model-call or token budget exceeds the approved research envelope')
        architectures=set(spec.get('architecture_ids') or ([spec.get('architecture_id')] if spec.get('architecture_id') else []))
        if not architectures or not architectures<=set(envelope['allowed_architecture_ids']):
            raise ValueError('Candidate architecture is outside the approved research envelope')
        if value['proposed_campaign'].get('paired') and ('candidate-a' not in architectures or len(architectures)<2):
            raise ValueError('Paired research dispatch requires Candidate A and at least one candidate')
        profiles=set(spec.get('profile_ids') or [])
        if envelope['allowed_profile_ids'] and not profiles<=set(envelope['allowed_profile_ids']):
            raise ValueError('Model profile is outside the approved research envelope')
        scenarios=set(spec.get('scenario_ids') or [])
        if envelope['allowed_scenario_ids'] and not scenarios<=set(envelope['allowed_scenario_ids']):
            raise ValueError('Scenario is outside the approved research envelope')
        if value['proposed_campaign'].get('holdout_required') and any(
                row.get('evaluation_split')!='holdout' for row in planned.get('scenarios',[])):
            raise ValueError('Research dispatch requires pinned holdout scenarios')
        dispatches=list(value.get('dispatches') or [])
        if len(dispatches)>=envelope['max_iterations']:raise ValueError('Approved dispatch iteration limit exhausted')
        experiment=manager.launch(planned,source_store)
        from . import telemetry
        telemetry.emit(source_store,'research.iteration.dispatched',workflow_id=experiment['id'],
            agent_id='coordinator',correlation_id=identifier,task_status='running',
            summary='Approved bounded research iteration dispatched',detail={
                'research_iteration_id':identifier,'experiment_id':experiment['id'],
                'approval_hash':value.get('approval_hash'),'architecture_ids':sorted(architectures),
                'planned_runs':planned.get('planned_runs'),'synthetic':True,
                'operational_activation_permitted':False})
        record={'experiment_id':experiment['id'],'preview_hash':planned.get('preview_hash'),
            'dispatched_at':time.time(),'dispatched_by':str(authorizer).strip(),
            'rationale':str(rationale).strip(),'architecture_ids':sorted(architectures),
            'profile_ids':sorted(profiles),'scenario_ids':sorted(scenarios),
            'planned_runs':planned.get('planned_runs')}
        dispatches.append(record);value=copy.deepcopy(value);value.update(status='dispatched',dispatches=dispatches)
        value['last_dispatch_hash']=_hash(record);self._put(value)
        return {'iteration':value,'experiment':experiment}
    def propose_followup(self,identifier,evidence,policy=None):
        parent=self.get(identifier)
        if not parent or parent['status'] not in ('approved_for_testing_lab','dispatched'):
            raise ValueError('An approved research iteration is required for a follow-up')
        envelope=parent['authorization_envelope'];root=parent.get('authorization_root') or parent['id']
        descendants=[row for row in self.list() if row.get('authorization_root')==root]
        if len(descendants)+1>=envelope['max_iterations']:
            raise ValueError('Approved research iteration limit exhausted')
        policy={**(policy or {}),'max_iterations':envelope['max_iterations'],
            'max_trials_per_iteration':envelope['max_trials_per_iteration'],
            'max_concurrency':envelope['max_concurrency'],'max_model_calls':envelope['max_model_calls'],
            'max_tokens':envelope['max_tokens'],'allowed_architecture_ids':envelope['allowed_architecture_ids'],
            'allowed_profile_ids':envelope['allowed_profile_ids'],'allowed_scenario_ids':envelope['allowed_scenario_ids'],
            'evidence_refs':(policy or {}).get('evidence_refs') or [d['experiment_id'] for d in parent.get('dispatches',[])]}
        followup=self.propose(evidence,policy,'bounded-research-loop:'+root,True)
        followup=copy.deepcopy(followup);followup.update(status='approved_for_testing_lab',
            approved_at=time.time(),approved_by=parent['approved_by'],
            approval_rationale='Covered follow-up within '+parent['approval_hash'],
            approval_hash=parent['approval_hash'],authorization_root=root,
            authorization_envelope=copy.deepcopy(envelope),automatic_execution_permitted=True,
            execution_scope='isolated_testing_lab_campaign_only',parent_iteration_id=parent['id'])
        return self._put(followup)
