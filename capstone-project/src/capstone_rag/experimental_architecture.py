"""Versioned, authority-safe construction of Testing Lab architectures.

The registry describes Capstone-owned logical architecture only.  It never
allocates a physical inference resource, changes operational workflow/QMS
authority, or activates a candidate.  Only templates whose executor mapping is
already implemented are marked execution-ready.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from .ai_settings import canonical

REGISTRY_VERSION='6.4.0'
CATEGORIES=('document_ingestion','chunking','embedding_indexing','retrieval',
            'evidence_assembly','reasoning','agent_arrangement','orchestration',
            'memory','validation')


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _component(identifier,category,inputs,outputs,*,parameters=None,requires=(),
               capabilities=(),compatible=('*',),resources=None,constraints=(),
               methods=(),executor=None,status='executable',version='1.0.0',
               introduced='6.1.0'):
    value={'id':identifier,'version':version,'category':category,
        'inputs':list(inputs),'outputs':list(outputs),
        'parameters':copy.deepcopy(parameters or {}),'required_tools':list(requires),
        'required_model_capabilities':list(capabilities),
        'compatible_components':list(compatible),
        'resource_requirements':copy.deepcopy(resources or {}),
        'safety_authority_constraints':list(constraints) or [
            'experiment_isolation_required','operational_authority_never_granted'],
        'supported_execution_evaluation_methods':list(methods),
        'executor':executor,'implementation_status':status,
        'provenance':{'owner':'Capstone Testing Lab','registry_version':introduced}}
    value['configuration_hash']=_hash(value)
    return value


_COMPONENTS=(
 _component('ingest.controlled_markdown','document_ingestion',['immutable_source_snapshot'],['controlled_documents'],
    parameters={'preserve_tables':True,'preserve_warnings':True},executor='capstone_rag.qms.load_chunks',methods=('source_hash','lineage_check')),
 _component('chunk.heading_sections','chunking',['controlled_documents'],['chunks'],
    parameters={'heading_levels':[2,3]},executor='capstone_rag.qms._split_sections',methods=('boundary_trace','warning_preservation')),
 _component('chunk.fixed_window','chunking',['controlled_documents'],['chunks'],
    parameters={'size':{'default':800,'min':200,'max':2400},'overlap':{'default':100,'min':0,'max':600}},
    executor='capstone_rag.document_curator.build_chunks',methods=('boundary_trace','source_span_check'),
    version='1.1.0',introduced='6.3.0'),
 _component('chunk.semantic_lexical','chunking',['controlled_documents'],['chunks'],
    parameters={'size':{'default':800,'min':200,'max':2400}},
    executor='capstone_rag.document_curator.build_chunks',methods=('boundary_trace','lexical_cohesion','source_span_check'),
    introduced='6.3.0'),
 _component('chunk.parent_child','chunking',['controlled_documents'],['chunks'],
    parameters={'size':{'default':500,'min':200,'max':1200},'overlap':{'default':50,'min':0,'max':300}},
    executor='capstone_rag.document_curator.build_chunks',methods=('parent_child_trace','source_span_check'),
    introduced='6.3.0'),
 _component('chunk.document_type','chunking',['controlled_documents'],['chunks'],
    parameters={'size':{'default':800,'min':200,'max':2400},'overlap':{'default':100,'min':0,'max':600}},
    executor='capstone_rag.document_curator.build_chunks',methods=('document_type_routing','source_span_check'),
    introduced='6.3.0'),
 _component('embed.minilm_l6_v2','embedding_indexing',['chunks'],['vector_index'],
    parameters={'dimensions':384,'normalize':True},capabilities=('embedding',),resources={'cpu_allowed':True},
    executor='capstone_rag.index.VectorIndex',methods=('reproducible_index','cosine_similarity')),
 _component('index.sparse_lexical','embedding_indexing',['chunks'],['sparse_index'],
    parameters={'normalize':False},resources={'cpu_allowed':True},
    executor='capstone_rag.document_curator.build_index',methods=('reproducible_index','term_postings'),
    introduced='6.3.0'),
 _component('index.hybrid_minilm_sparse','embedding_indexing',['chunks'],['vector_index','sparse_index'],
    parameters={'dimensions':384,'normalize':True},capabilities=('embedding',),resources={'cpu_allowed':True},
    executor='capstone_rag.document_curator.build_index',methods=('reproducible_index','cosine_similarity','term_postings'),
    introduced='6.4.0'),
 _component('retrieve.dense_authority_filtered','retrieval',['vector_index','query','authority_context'],['retrieved_evidence'],
    parameters={'top_k':{'default':20,'min':1,'max':100}},requires=('search_qms',),
    executor='capstone_rag.tools.Toolbox.search_qms',methods=('recall','wrong_revision_rate')),
 _component('retrieve.hybrid_rrf_authority_filtered','retrieval',['vector_index','sparse_index','query','authority_context'],['retrieved_evidence'],
    parameters={'top_k':{'default':20,'min':1,'max':100},'rrf_k':{'default':60,'min':1,'max':200}},
    requires=('search_qms',),executor='capstone_rag.document_curator.build_index',
    methods=('recall','wrong_revision_rate','reciprocal_rank_fusion'),introduced='6.3.0'),
 _component('retrieve.sparse_authority_filtered','retrieval',['sparse_index','query','authority_context'],['retrieved_evidence'],
    parameters={'top_k':{'default':20,'min':1,'max':100}},requires=('search_qms',),
    executor='capstone_rag.document_curator.CuratorIndex',methods=('recall','wrong_revision_rate','bm25'),
    introduced='6.4.0'),
 _component('evidence.deterministic_authority','evidence_assembly',['retrieved_evidence','authority_context'],['authorized_evidence'],
    executor='capstone_rag.validation.validate_chunk',methods=('disclosure','applicability','deduplication'),
    constraints=('deterministic_qms_policy_mandatory','restricted_text_not_customer_visible')),
 _component('reasoning.react_reflexion','reasoning',['authorized_evidence','case_context'],['proposed_response','observable_steps'],
    parameters={'max_unproductive':{'default':3,'min':1,'max':5}},capabilities=('completion',),
    executor='capstone_rag.react.ReasoningLoop',methods=('groundedness','tool_efficiency')),
 _component('reasoning.selective_bounded_tot','reasoning',['authorized_evidence','case_context'],['proposed_plan','observable_branches'],
    parameters={'max_depth':{'default':3,'min':1,'max':3},'max_states':{'default':6,'min':2,'max':6}},
    capabilities=('structured_output',),executor='capstone_rag.planning.plan',methods=('branch_cost','plan_safety')),
 _component('reasoning.direct_guarded','reasoning',['authorized_evidence','case_context'],['proposed_response'],
    parameters={'max_steps':1},executor='capstone_rag.generation.generate_grounded',methods=('groundedness','latency')),
 _component('agents.single_guarded','agent_arrangement',['case_context'],['agent_findings'],
    parameters={'max_agents':1},executor='capstone_rag.agent.Agent',methods=('task_completion',)),
 _component('agents.sequential_specialists','agent_arrangement',['case_context'],['agent_findings'],
    parameters={'max_agents':5},executor='capstone_rag.orchestration.CaseOrchestrator',methods=('handoff_quality','task_completion')),
 _component('agents.parallel_specialists','agent_arrangement',['case_context'],['agent_findings'],
    parameters={'max_agents':3},executor='capstone_rag.phase4_runtime.collect_initial_evidence',methods=('overlap','handoff_quality')),
 _component('orchestration.langgraph','orchestration',['agent_findings'],['workflow_outcome'],
    parameters={'recursion_limit':{'default':24,'min':8,'max':32}},executor='capstone_rag.orchestration.CaseOrchestrator',methods=('checkpoint_recovery','termination')),
 _component('orchestration.deterministic','orchestration',['agent_findings'],['workflow_outcome'],
    parameters={'bounded':True},executor='capstone_rag.agent.Agent',methods=('termination','task_completion')),
 _component('memory.stateless','memory',['case_context'],['memory_context'],parameters={'enabled':False},executor='builtins.none',methods=('control_condition',)),
 _component('memory.session','memory',['case_context'],['memory_context'],parameters={'scope':'conversation'},executor='capstone_rag.agent.Conversation',methods=('context_retention',)),
 _component('memory.human_reviewed','memory',['case_context'],['memory_context'],parameters={'review_required':True},executor='capstone_rag.memory.ReviewedMemory',methods=('review_enforcement','applicability')),
 _component('validation.mandatory_multistage','validation',['proposed_response','authorized_evidence'],['validated_outcome'],
    parameters={'repair_limit':{'default':2,'min':0,'max':2}},executor='capstone_rag.independent_review.evaluate',
    methods=('safety','grounding','contradiction','authority'),constraints=(
        'deterministic_qms_policy_mandatory','human_approval_boundaries_mandatory','unsafe_candidate_ineligible')),
)
COMPONENTS={row['id']:row for row in _COMPONENTS}

TEMPLATES={
 'candidate-a':{'id':'candidate-a','name':'Candidate A · immutable operational baseline','baseline':True,
    'registry_version':'6.1.0',
    'components':['ingest.controlled_markdown','chunk.heading_sections','embed.minilm_l6_v2',
      'retrieve.dense_authority_filtered','evidence.deterministic_authority',
      'reasoning.react_reflexion','agents.sequential_specialists','orchestration.langgraph',
      'memory.human_reviewed','validation.mandatory_multistage'],
    'execution_adapter':'existing_capstone_baseline','execution_ready':True},
 'candidate-b-direct':{'id':'candidate-b-direct','name':'Candidate B · guarded direct single agent','baseline':False,
    'components':['ingest.controlled_markdown','chunk.fixed_window','index.sparse_lexical',
      'retrieve.sparse_authority_filtered','evidence.deterministic_authority',
      'reasoning.direct_guarded','agents.single_guarded','orchestration.deterministic',
      'memory.session','validation.mandatory_multistage'],
    'execution_adapter':'candidate_b_direct_v1','execution_ready':True},
 'candidate-c-tot':{'id':'candidate-c-tot','name':'Candidate C · bounded ToT with parallel evidence','baseline':False,
    'components':['ingest.controlled_markdown','chunk.parent_child','index.hybrid_minilm_sparse',
      'retrieve.hybrid_rrf_authority_filtered','evidence.deterministic_authority',
      'reasoning.selective_bounded_tot','agents.parallel_specialists','orchestration.langgraph',
      'memory.human_reviewed','validation.mandatory_multistage'],
    'execution_adapter':'candidate_c_tot_v1','execution_ready':True},
}


def registry():
    return {'registry_version':REGISTRY_VERSION,'components':copy.deepcopy(list(_COMPONENTS)),
            'categories':list(CATEGORIES),'registry_sha256':_hash(list(_COMPONENTS))}


def template(identifier):
    if identifier not in TEMPLATES:raise ValueError('Unknown architecture template')
    return assemble(copy.deepcopy(TEMPLATES[identifier]))


def assemble(spec):
    value=copy.deepcopy(spec);ids=value.get('components') or []
    errors=[];warnings=[]
    if len(ids)!=len(set(ids)):errors.append('Duplicate component selection')
    unknown=[key for key in ids if key not in COMPONENTS]
    if unknown:errors.append('Unknown components: '+', '.join(unknown))
    chosen=[COMPONENTS[key] for key in ids if key in COMPONENTS]
    by_category={category:[row for row in chosen if row['category']==category] for category in CATEGORIES}
    for category,rows in by_category.items():
        if len(rows)!=1:errors.append(f'Exactly one {category} component is required; found {len(rows)}')
    settings=value.get('settings') or {}
    if set(settings)-set(ids):errors.append('Settings supplied for an unselected component')
    for key,configured in settings.items():
        if not isinstance(configured,dict):errors.append('Component settings must be objects: '+key);continue
        declared=COMPONENTS.get(key,{}).get('parameters',{})
        for name,selected in configured.items():
            if name not in declared:errors.append(f'Unknown parameter {key}.{name}');continue
            rule=declared[name]
            if isinstance(rule,dict) and ('min' in rule or 'max' in rule):
                if isinstance(selected,bool) or not isinstance(selected,(int,float)):
                    errors.append(f'Numeric parameter required: {key}.{name}')
                elif selected<rule.get('min',selected) or selected>rule.get('max',selected):
                    errors.append(f'Parameter outside allowed range: {key}.{name}')
    validation={row['id'] for row in by_category.get('validation',[])}
    evidence={row['id'] for row in by_category.get('evidence_assembly',[])}
    if 'validation.mandatory_multistage' not in validation:errors.append('Mandatory safety/authority validation cannot be removed')
    if 'evidence.deterministic_authority' not in evidence:errors.append('Deterministic document authority filtering cannot be removed')
    selected=set(ids)
    if 'orchestration.deterministic' in selected and 'agents.single_guarded' not in selected:
        errors.append('Deterministic orchestration currently requires the single guarded agent')
    if 'orchestration.langgraph' in selected and not selected.intersection({'agents.sequential_specialists','agents.parallel_specialists'}):
        errors.append('LangGraph execution requires a verified specialist arrangement')
    if 'reasoning.selective_bounded_tot' in selected and 'orchestration.langgraph' not in selected:
        errors.append('Bounded Tree-of-Thought requires the verified LangGraph adapter')
    retrieval_requirements={
        'retrieve.dense_authority_filtered':'embed.minilm_l6_v2',
        'retrieve.sparse_authority_filtered':'index.sparse_lexical',
        'retrieve.hybrid_rrf_authority_filtered':'index.hybrid_minilm_sparse'}
    for retrieval,indexing in retrieval_requirements.items():
        if retrieval in selected and indexing not in selected:
            errors.append(f'{retrieval} requires {indexing}')
    for row in chosen:
        if row['implementation_status']!='executable':warnings.append(row['id']+' is registered but not executable')
    ready=not errors and not warnings and bool(value.get('execution_ready',False))
    normalized={'registry_version':value.get('registry_version',REGISTRY_VERSION),'id':value.get('id') or 'ARCH-'+uuid.uuid4().hex,
        'name':value.get('name') or 'Experimental architecture','baseline':bool(value.get('baseline')),
        'components':ids,'settings':settings,'execution_adapter':value.get('execution_adapter'),
        'execution_ready':ready,'operational_activation_permitted':False,
        'source_authority':'immutable_snapshot_only','errors':errors,'warnings':warnings}
    normalized['component_hashes']={row['id']:row['configuration_hash'] for row in chosen}
    normalized['configuration_hash']=_hash({k:v for k,v in normalized.items() if k not in ('errors','warnings')})
    normalized['valid']=not errors
    return normalized


def vary(base,category,replacement,*,identifier=None,name=None):
    value=template(base) if base in TEMPLATES else assemble(base)
    if category not in CATEGORIES or replacement not in COMPONENTS or COMPONENTS[replacement]['category']!=category:
        raise ValueError('Replacement component does not match the requested category')
    ids=[replacement if COMPONENTS[key]['category']==category else key for key in value['components']]
    return assemble({**value,'id':identifier or 'ARCH-'+uuid.uuid4().hex,
        'name':name or value['name']+' · '+replacement,'baseline':False,'components':ids,
        'registry_version':REGISTRY_VERSION,'execution_ready':False,
        'execution_adapter':'pending_testing_lab_adapter'})


class ArchitectureStore:
    """Immutable candidate versions in the existing AI settings database."""
    def __init__(self,settings_registry):self.registry=settings_registry
    def ensure(self):
        with self.registry.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS architecture_candidates (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    def list(self):
        self.ensure()
        with self.registry.connect() as db:return [json.loads(row[0]) for row in db.execute('SELECT payload FROM architecture_candidates ORDER BY id')]
    def get(self,identifier):return next((row for row in self.list() if row['id']==identifier),None)
    def save(self,spec,authorizer):
        if not str(authorizer).strip():raise ValueError('Named operator required')
        value=assemble(spec)
        if not value['valid']:raise ValueError('; '.join(value['errors']))
        value.update(saved_at=time.time(),saved_by=str(authorizer).strip(),immutable=True)
        self.ensure()
        with self.registry.connect() as db:
            if db.execute('SELECT 1 FROM architecture_candidates WHERE id=?',(value['id'],)).fetchone():
                raise ValueError('Architecture versions are immutable; save a new identifier')
            db.execute('INSERT INTO architecture_candidates VALUES (?,?)',(value['id'],canonical(value)))
        return value
