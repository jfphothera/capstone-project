"""Gateway-aware model inventory, suitability evidence and fair exploration plans."""
from __future__ import annotations
import copy,hashlib,json,time,uuid
from .ai_settings import canonical

FORMAT='capstone-model-research-v1'
CLASSIFICATIONS=('compatible','verified','promising_unverified','incompatible','unknown')
ROLE_REQUIREMENTS={
 'customer_response':{'operations':{'completion'}},
 'structured_reasoning':{'operations':{'completion'},'capabilities':{'structured_output'}},
 'tool_use':{'operations':{'completion'},'capabilities':{'tool_calling'}},
 'retrieval_query':{'operations':{'completion'}},
 'document_classification':{'operations':{'completion'}},
 'embedding':{'operations':{'embedding'}},
 'reranking':{'operations':{'reranking'}},
 'planning':{'operations':{'completion'},'capabilities':{'structured_output'}},
 'independent_verification':{'operations':{'completion'},'capabilities':{'structured_output'}},
 'coding_source_analysis':{'operations':{'completion'}},
 'document_curation':{'operations':{'completion'},'capabilities':{'structured_output'}},
 'synthetic_interaction':{'operations':{'completion'}},
}


def _hash(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalize_inventory(payload):
    """Normalize only facts actually supplied by shared.inference/1."""
    resources=payload.get('resources') or [];models={}
    for resource in resources:
        rid=resource.get('resource_id')
        detail_map=resource.get('model_details') or {}
        for identifier in resource.get('models') or []:
            detail=detail_map.get(identifier) or {}
            row=models.setdefault(identifier,{'model_id':identifier,'qualified_resources':[],
                'qualified_operations':set(),'capabilities':set(),'runtime_declared_capabilities':set(),'digests':set(),
                'context_windows':set(),'quantizations':set(),'resource_observations':[]})
            row['qualified_resources'].append(rid)
            row['qualified_operations'].update(detail.get('qualified_operations') or [])
            row['capabilities'].update(detail.get('qualified_capabilities') or [])
            row['runtime_declared_capabilities'].update(detail.get('capabilities') or [])
            if detail.get('digest'):row['digests'].add(detail['digest'])
            descriptor=detail.get('descriptor') or {}
            policy=detail.get('execution_policy') or {}
            if policy.get('max_context') is not None:row['context_windows'].add(policy['max_context'])
            quantization=descriptor.get('quantization_level') or detail.get('quantization')
            if quantization:row['quantizations'].add(quantization)
            row['resource_observations'].append({'resource_id':rid,
                'status':resource.get('status'),'healthy':resource.get('healthy'),
                'available':resource.get('available'),'detail':{k:detail.get(k) for k in
                    ('family','version','digest','quantization') if detail.get(k) is not None}})
            observation=row['resource_observations'][-1]['detail']
            for key in ('family','runtime_version'):
                if descriptor.get(key) is not None:observation['version' if key=='runtime_version' else key]=descriptor[key]
            if quantization is not None:observation['quantization']=quantization
    result=[]
    for row in models.values():
        for key in ('qualified_operations','capabilities','runtime_declared_capabilities','digests','context_windows','quantizations'):
            row[key]=sorted(row[key])
        observations=row['resource_observations']
        explicit=[x['available'] for x in observations if isinstance(x.get('available'),bool)]
        row['currently_available']=any(explicit) if explicit else None
        row['availability_status']='observed' if explicit else 'not_exposed'
        missing=[]
        for field,present in (
            ('family',any(x['detail'].get('family') for x in observations)),
            ('version',any(x['detail'].get('version') for x in observations)),
            ('quantization',bool(row['quantizations'])),('context_window',bool(row['context_windows'])),
            ('tool_calling_or_structured_output',bool(set(row['capabilities'])&{'tool_calling','structured_output'})),
            ('current_availability',bool(explicit))):
            if not present:missing.append(field)
        row['missing_gateway_fields']=missing
        row['inventory_hash']=_hash(row);result.append(row)
    return {'format':FORMAT,'contract':payload.get('contract','shared.inference/1'),
        'source':'authenticated_gateway','models':sorted(result,key=lambda row:row['model_id']),
        'resource_count':len(resources),'missing_contract_fields':sorted({field for row in result for field in row['missing_gateway_fields']}),
        'note':'Measured qualification, runtime-declared capabilities, experimental suitability, and current availability are independent.'}


def discover(client):
    """Use Gateway 0.3 discovery without manufacturing inventory while offline."""
    try:inventory=normalize_inventory(client.models())
    except Exception as exc:
        return {'format':FORMAT,'contract':'shared.inference/1','source':'authenticated_gateway',
            'gateway_status':'offline_unverified','models':[],'resource_count':0,
            'policy':None,'policy_verified':False,'availability_verified':False,
            'error_class':type(exc).__name__,
            'note':'Gateway inventory could not be authenticated; no available models were inferred.'}
    try:
        policy=client.policy();inventory['policy']=policy;inventory['policy_verified']=True
    except Exception as exc:
        inventory.update(policy=None,policy_verified=False,policy_error_class=type(exc).__name__)
    availability_verified=True
    for model in inventory['models']:
        operations=[]
        if 'completion' in model['qualified_operations']:operations.append('/api/generate')
        if 'embedding' in model['qualified_operations']:operations.append('/api/embed')
        observations=[]
        for operation in operations:
            try:
                value=client.availability(model['model_id'],operation,
                    num_ctx=max(model['context_windows']) if model['context_windows'] else None)
                observations.append({'operation':operation,'status':value.get('status'),
                    'resources':value.get('resources',[]),'contract':value.get('contract')})
            except Exception as exc:
                availability_verified=False
                observations.append({'operation':operation,'status':'unverified',
                    'error_class':type(exc).__name__})
        model['availability']=observations
        states={row['status'] for row in observations}
        model['currently_available']=True if 'available' in states else False if observations and states<= {
            'not_permitted','operation_not_permitted','context_exceeds_policy','context_exceeds_qualification',
            'capability_not_qualified','not_installed','not_qualified','qualified_other_operation',
            'digest_changed','runtime_changed','resource_unhealthy','resource_stale','inventory_stale',
            'no_authorized_resource'} else None
        model['availability_status']='gateway_0.3_preflight' if observations else 'no_qualified_operation'
    inventory.update(gateway_status='authenticated',availability_verified=availability_verified)
    return inventory


class SuitabilityStore:
    def __init__(self,registry):self.registry=registry
    def ensure(self):
        with self.registry.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS model_suitability_evidence (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    def list(self):
        self.ensure()
        with self.registry.connect() as db:return [json.loads(row[0]) for row in db.execute('SELECT payload FROM model_suitability_evidence ORDER BY id')]
    def record(self,body,authorizer):
        if not str(authorizer).strip():raise ValueError('Named reviewer required')
        if body.get('role') not in ROLE_REQUIREMENTS:raise ValueError('Unknown suitability role')
        if body.get('classification') not in CLASSIFICATIONS:raise ValueError('Unknown suitability classification')
        if not str(body.get('model_id','')).strip() or not body.get('evidence_refs') or not str(body.get('rationale','')).strip():
            raise ValueError('Model, rationale and evidence references are required')
        value={'id':'SUIT-'+uuid.uuid4().hex,'model_id':body['model_id'],'role':body['role'],
            'classification':body['classification'],'evidence_refs':list(body['evidence_refs']),
            'rationale':body['rationale'],'reviewed_by':str(authorizer).strip(),'at':time.time(),
            'operational_activation_permitted':False}
        value['evidence_hash']=_hash(value);self.ensure()
        with self.registry.connect() as db:db.execute('INSERT INTO model_suitability_evidence VALUES (?,?)',(value['id'],canonical(value)))
        return value


def suitability(inventory,evidence=()):
    reviewed={(row['model_id'],row['role']):row for row in evidence}
    rows=[]
    for model in inventory.get('models',[]):
        operations=set(model.get('qualified_operations') or [])
        capabilities=set(model.get('capabilities') or [])
        for role,required in ROLE_REQUIREMENTS.items():
            record=reviewed.get((model['model_id'],role))
            missing_ops=required.get('operations',set())-operations
            missing_caps=required.get('capabilities',set())-capabilities
            if record:classification=record['classification'];basis='reviewed_experiment_evidence'
            elif missing_ops:classification='incompatible';basis='missing_qualified_operation'
            elif missing_caps:classification='unknown';basis='Gateway capability field absent or insufficient'
            else:classification='compatible';basis='Gateway contract compatibility only; experimental quality unverified'
            rows.append({'model_id':model['model_id'],'role':role,'classification':classification,
                'basis':basis,'missing_operations':sorted(missing_ops),'missing_capabilities':sorted(missing_caps),
                'evidence_refs':record.get('evidence_refs',[]) if record else [],
                'currently_available':model.get('currently_available'),
                'inventory_hash':model.get('inventory_hash')})
    return {'format':FORMAT,'roles':list(ROLE_REQUIREMENTS),'suitability':rows,
        'note':'Compatible is not verified. Model names are never used as suitability evidence.'}


def exploration_plan(inventory,suitability_report,policy):
    allow=set(policy.get('allowlist') or [row['model_id'] for row in inventory.get('models',[])])
    excluded=set(policy.get('excluded_models') or [])
    roles=policy.get('roles') or ['customer_response']
    unknown=set(roles)-set(ROLE_REQUIREMENTS)
    if unknown:raise ValueError('Unknown exploration roles: '+', '.join(sorted(unknown)))
    budget=int(policy.get('exploration_budget',0));repetitions=int(policy.get('repetitions',2))
    minimum=int(policy.get('minimum_useful_comparisons',2));seeds=list(policy.get('seeds') or range(7,7+repetitions))
    if not 1<=budget<=10000 or not 1<=repetitions<=100 or len(seeds)<repetitions:
        raise ValueError('Bounded exploration budget, repetitions and seeds are required')
    baseline=policy.get('baseline_model')
    records={(row['model_id'],row['role']):row for row in suitability_report.get('suitability',[])}
    assignments=[];not_tested=[]
    for role in roles:
        candidates=[]
        for model in inventory.get('models',[]):
            mid=model['model_id'];record=records.get((mid,role),{})
            reason=None
            if mid not in allow:reason='not in operator allowlist'
            elif mid in excluded:reason='operator excluded'
            elif record.get('classification') in ('incompatible','unknown'):reason=record.get('basis') or record.get('classification')
            elif model.get('currently_available') is False:reason='Gateway explicitly reports unavailable'
            if reason:not_tested.append({'model_id':mid,'role':role,'reason':reason})
            else:candidates.append(mid)
        candidates=sorted(set(candidates),key=lambda value:(value!=baseline,value))
        if len(candidates)<minimum:raise ValueError(f'Role {role} has fewer than the minimum useful compatible comparisons')
        for repeat in range(repetitions):
            for mid in candidates:
                if len(assignments)>=budget:break
                assignments.append({'role':role,'model_id':mid,'seed':seeds[repeat],
                    'repetition':repeat+1,'selection_reason':'baseline_control' if mid==baseline else 'deterministic_round_robin_exploration'})
            if len(assignments)>=budget:break
    plan={'format':FORMAT,'selection_policy':'deterministic_baseline_first_round_robin',
        'authorized_policy':copy.deepcopy(policy),'assignments':assignments,'not_tested':not_tested,
        'budget_used':len(assignments),'budget_limit':budget,'gateway_allocation_authority':'external',
        'automatic_permission_changes':False}
    plan['plan_hash']=_hash(plan);return plan
