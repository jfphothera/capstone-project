"""Immutable Agentic Evaluation matrix and experiment-only retrieval adapters."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import asdict, is_dataclass

from .index import Hit


TOPOLOGIES = (
    {'id':'A','name':'Current baseline','agent_arrangement':'sequential_specialists',
     'supervision':'coordinator','communication':'structured_handoffs','max_agents':8,
     'parallel_initial_evidence':True,'adapter':'candidate_a_baseline'},
    {'id':'B','name':'Single guarded agent','agent_arrangement':'single_agent',
     'supervision':'none','communication':'none','max_agents':1,
     'parallel_initial_evidence':False,'adapter':'single_direct'},
    {'id':'C','name':'Fixed linear team','agent_arrangement':'fixed_linear',
     'supervision':'mechanical_scheduler','communication':'fixed_handoff','max_agents':5,
     'parallel_initial_evidence':False,'adapter':'specialist_graph'},
    {'id':'D','name':'Supervisor over linear team','agent_arrangement':'supervised_linear',
     'supervision':'model_supervisor','communication':'supervisor_mediated','max_agents':6,
     'parallel_initial_evidence':False,'adapter':'specialist_graph','supervisor_checkpoints':True},
    {'id':'E','name':'Parallel specialists and join','agent_arrangement':'parallel_join',
     'supervision':'deterministic_join','communication':'join_only','max_agents':5,
     'parallel_initial_evidence':True,'adapter':'specialist_graph'},
    {'id':'F','name':'Peer communication','agent_arrangement':'peer_network',
     'supervision':'coordinator_bounds_only','communication':'peer_exchange','max_agents':5,
     'parallel_initial_evidence':True,'adapter':'specialist_graph','peer_exchange':True},
    {'id':'G','name':'Hierarchical delegation','agent_arrangement':'hierarchical',
     'supervision':'model_supervisor','communication':'bounded_delegation','max_agents':8,
     'parallel_initial_evidence':False,'adapter':'specialist_graph','supervisor_checkpoints':True,
     'hierarchical_delegation':True},
    {'id':'H','name':'Producer, critic and judge','agent_arrangement':'debate_critic',
     'supervision':'independent_judge','communication':'proposal_critique_rebuttal','max_agents':4,
     'parallel_initial_evidence':True,'adapter':'specialist_graph','debate':True},
)

TREATMENTS = (
    {'id':'1','name':'ReAct + dense retrieval','reasoning':'react_reflexion',
     'retrieval':'dense','chunk_view':'heading_sections','retrieval_timing':'before_planning'},
    {'id':'2','name':'Direct + lexical retrieval','reasoning':'direct_guarded',
     'retrieval':'sparse','chunk_view':'lexical_sections','retrieval_timing':'before_planning'},
    {'id':'3','name':'Bounded ToT + hybrid retrieval','reasoning':'bounded_tree_of_thought',
     'retrieval':'hybrid','chunk_view':'dense_and_lexical','retrieval_timing':'before_planning',
     'force_tree':True,'max_depth':3,'max_states':6},
    {'id':'4','name':'Plan/reflection + dual-view retrieval','reasoning':'plan_execute_reflect',
     'retrieval':'dual_view','chunk_view':'independent_dense_and_lexical',
     'retrieval_timing':'interpretation_and_validation','reflection':True},
)

# --- method registry versions (rc.25, Phase 0) -------------------------------------
# agentic-matrix/1 is the definition every experiment up to rc.24 ran; it stays
# readable unchanged (TOPOLOGIES / TREATMENTS above are its literal values and
# experiment manifests keep their own copy). agentic-matrix/2 records what the
# rc.25 implementation actually executes. Each cell's configuration hash covers
# its definition, so v1 and v2 cells never share an identity.
REGISTRY_V1 = 'agentic-matrix/1'
REGISTRY_V2 = 'agentic-matrix/2'
CURRENT_REGISTRY = REGISTRY_V2
IMPLEMENTATION = {
    # topology id: (implementation version, implementation-qualified, honest description)
    'A': ('A-2026-11-19.1', True, 'Specialist graph; initial independent evidence TOOLS run in parallel (tool parallelism, not LLM specialist parallelism).'),
    'B': ('B-2026-11-19.1', True, 'Direct guarded single pass with the same identity, authorization, safety and evidence capture; ignores tree and reflection stages (reported as not supported).'),
    'C': ('C-2026-11-19.1', True, 'Fixed linear specialist graph.'),
    'D': ('D-2026-11-19.1', True, 'Supervisor decisions now have a bounded effect: a stop is surfaced to the reviewer as an unresolved supervisor concern; a valid requested specialist is recorded (dispatched only under G).'),
    'E': ('E-2026-11-19.1', False, 'NOT implementation-qualified as parallel specialists: specialist LLM steps run sequentially; only the initial tool evidence is parallel (same as A).'),
    'F': ('F-2026-11-19.1', True, 'Peers receive the authorized passage text; an unresolved peer question triggers one bounded authorized evidence lookup or is recorded unresolved.'),
    'G': ('G-2026-11-19.1', True, 'Supervisor-requested specialist tasks are dispatched (bounded: depth 2, two tasks), executed, completed and reintegrated into planning.'),
    'H': ('H-2026-11-19.1', True, 'Critic and judge receive the authorized passages; a sourced critic objection the judge upholds causes one bounded explicit revision or an explicit unresolved decision before independent validation.'),
}
TREATMENT_IMPLEMENTATION = {
    '1': ('1-2026-11-19.1', True, 'ReAct writer with dense ranking.'),
    '2': ('2-2026-11-19.1', True, 'Direct planning with lexical ranking; the common applicability gate still uses dense similarity (not embedding-free).'),
    '3': ('3-2026-11-19.1', True, 'Bounded tree planning: the model planner runs within a reserved stage budget or is recorded budget-exhausted (no elapsed-time cut-off).'),
    '4': ('4-2026-11-19.1', True, 'Plan/reflection with an evidence-grounded bounded consequence. Retrieval "dual_view" is an ALIAS of the hybrid reciprocal-rank fusion of treatment 3; no independent second view is executed.'),
}
# Advisory recommendation state (U1). Never part of a configuration hash and
# never used to disable, hide or deselect an option.
ADVISORY = {
    'topologies': {'A': ('normal', 'retained'), 'B': ('normal', 'retained'), 'C': ('normal', 'retained'),
                   'G': ('normal', 'retained: repaired bounded delegation; next-experiment candidate by operator decision'),
                   'D': ('gray', 'discouraged: supervisor output previously unused; broad always-on supervision added work without demonstrated benefit'),
                   'E': ('gray', 'discouraged: duplicate of A tool parallelism; distinct parallel specialist execution not implemented'),
                   'F': ('gray', 'discouraged: whole peer-network sweep added work; 170/219 exchanges self-reported unresolved'),
                   'H': ('gray', 'discouraged: judge outputs did not control revision before rc.25; no demonstrated benefit on this case bank')},
    'treatments': {'1': ('normal', 'retained'), '2': ('normal', 'retained'),
                   '3': ('gray', 'discouraged: model-driven tree never ran under the old 120 s gate; repaired, unmeasured'),
                   '4': ('gray', 'discouraged: dual-view is an alias of hybrid; blanket reflection blocked without recorded gaps')},
}


def _versioned(row, table, registry):
    value = copy.deepcopy(row)
    if registry == REGISTRY_V2:
        version, qualified, note = table[row['id']]
        value.update(implementation_version=version, implementation_qualified=qualified, implementation_note=note)
        if row.get('retrieval') == 'dual_view':
            value['retrieval_alias_of'] = 'hybrid'
    return value


def definitions(registry=CURRENT_REGISTRY):
    if registry not in (REGISTRY_V1, REGISTRY_V2):
        raise ValueError('Unknown method registry version: ' + str(registry))
    return ([_versioned(r, IMPLEMENTATION, registry) for r in TOPOLOGIES],
            [_versioned(r, TREATMENT_IMPLEMENTATION, registry) for r in TREATMENTS])


def stage_coverage(topology, treatment):
    """What a cell actually executes (shown before launch; M2)."""
    rows = []
    direct = topology.get('adapter') == 'single_direct'
    if treatment.get('force_tree'):
        rows.append({'stage': 'bounded_tree_planning', 'status': 'not_supported_by_adapter' if direct else 'executes_within_stage_budget',
                     'reason': 'the direct adapter plans directly' if direct else 'model planner runs if its reserved budget remains, else recorded budget_exhausted'})
    if treatment.get('reflection'):
        rows.append({'stage': 'plan_reflection', 'status': 'not_supported_by_adapter' if direct else 'executes',
                     'reason': 'the direct adapter has no specialist planning stage' if direct else 'bounded evidence-grounded consequence'})
    if treatment.get('reasoning') == 'react_reflexion' and direct:
        rows.append({'stage': 'react_writer', 'status': 'not_supported_by_adapter', 'reason': 'the direct adapter uses the direct guarded writer'})
    if treatment.get('retrieval') == 'dual_view':
        rows.append({'stage': 'dual_view_retrieval', 'status': 'alias', 'reason': 'executes the hybrid fusion ranking; no independent second view'})
    if topology.get('id') == 'E':
        rows.append({'stage': 'parallel_specialists', 'status': 'not_implementation_qualified',
                     'reason': 'specialist LLM steps are sequential; only the initial tools are parallel'})
    return rows


def catalogue(registry=CURRENT_REGISTRY):
    topologies, treatments = definitions(registry)
    for row in topologies:
        row['advisory'] = dict(zip(('appearance', 'reason'), ADVISORY['topologies'][row['id']]))
    for row in treatments:
        row['advisory'] = dict(zip(('appearance', 'reason'), ADVISORY['treatments'][row['id']]))
    return {'registry_version':registry,'topologies':topologies,'treatments':treatments,
            'legacy_registry_versions':[REGISTRY_V1],
            'advisory_policy':'Advisory only: gray options remain selectable, submitted and executed.',
            'default_topology_ids':[row['id'] for row in TOPOLOGIES],
            'default_treatment_ids':[row['id'] for row in TREATMENTS]}


def candidates(topology_ids=None,treatment_ids=None,registry=CURRENT_REGISTRY):
    topology_ids=topology_ids or [row['id'] for row in TOPOLOGIES]
    treatment_ids=treatment_ids or [row['id'] for row in TREATMENTS]
    defined_topologies,defined_treatments=definitions(registry)
    topologies={row['id']:row for row in defined_topologies};treatments={row['id']:row for row in defined_treatments}
    unknown=(set(topology_ids)-set(topologies)) | (set(treatment_ids)-set(treatments))
    if unknown:raise ValueError('Unknown Agentic Evaluation matrix cell: '+', '.join(sorted(unknown)))
    result=[]
    for topology_id in topology_ids:
        for treatment_id in treatment_ids:
            methodology={'schema':'capstone.agentic-methodology/1',
                'topology':copy.deepcopy(topologies[topology_id]),
                'treatment':copy.deepcopy(treatments[treatment_id])}
            payload=json.dumps(methodology,sort_keys=True,separators=(',',':'))
            methodology['configuration_hash']=hashlib.sha256(payload.encode()).hexdigest()
            result.append({'id':f'method-{topology_id}{treatment_id}',
                'name':f"{topology_id}{treatment_id} · {topologies[topology_id]['name']} · {treatments[treatment_id]['name']}",
                'baseline':topology_id=='A' and treatment_id=='1','registry_version':registry,
                'stage_coverage':stage_coverage(topologies[topology_id],treatments[treatment_id]),
                'advisory':{'topology':ADVISORY['topologies'][topology_id][0],'treatment':ADVISORY['treatments'][treatment_id][0]},
                'components':[],'settings':{},'execution_adapter':'agentic_matrix_v1',
                'execution_ready':True,'operational_activation_permitted':False,
                'source_authority':'immutable_snapshot_only','errors':[],'warnings':[],
                'valid':True,'methodology':methodology,
                'configuration_hash':methodology['configuration_hash']})
    return result


def _tokens(value):
    return re.findall(r'[a-z0-9]+',str(value).lower())


class RetrievalTreatmentIndex:
    """Use the same controlled chunks with genuinely different rankers.

    Authorization still occurs in Toolbox.search_qms after ranking.  This
    wrapper changes retrieval only and never changes QMS authority.
    """
    def __init__(self,base,methodology):
        self.base=base;self.chunks=base.chunks
        self.minimum_similarity=getattr(base,'minimum_similarity',0.0)
        if hasattr(base,'public_manual_similarity'):self.public_manual_similarity=base.public_manual_similarity
        self.methodology=copy.deepcopy(methodology);self.last_trace={}

    def _dense(self,query):
        hits=self.base.search_procedures(query,k=len(self.chunks))
        return {hit.chunk.procedure_id:hit for hit in hits}

    def _sparse(self,query):
        query_terms=set(_tokens(query));rows=[]
        for chunk in self.chunks:
            terms=_tokens(' '.join((chunk.title,chunk.section_path,chunk.text)))
            if not terms:continue
            overlap=sum(1+math.log1p(terms.count(term)) for term in query_terms if term in terms)
            rows.append((overlap/ math.sqrt(len(terms)),chunk))
        rows.sort(key=lambda item:(-item[0],item[1].chunk_id));maximum=max([row[0] for row in rows] or [1]) or 1;seen=set();hits=[]
        for score,chunk in rows:
            if chunk.procedure_id in seen:continue
            seen.add(chunk.procedure_id);hits.append(Hit(chunk,float(score/maximum),len(hits)+1))
        return {hit.chunk.procedure_id:hit for hit in hits}

    def _sparse_chunks(self,query):
        query_terms=set(_tokens(query));rows=[]
        for chunk in self.chunks:
            terms=_tokens(' '.join((chunk.title,chunk.section_path,chunk.text)))
            if not terms:continue
            overlap=sum(1+math.log1p(terms.count(term)) for term in query_terms if term in terms)
            rows.append((overlap/math.sqrt(len(terms)),chunk))
        rows.sort(key=lambda item:(-item[0],item[1].chunk_id));maximum=max([row[0] for row in rows] or [1]) or 1
        return {chunk.chunk_id:Hit(chunk,float(score/maximum),rank) for rank,(score,chunk) in enumerate(rows,1)}

    @staticmethod
    def _rrf(*rankings):
        scores={};representatives={}
        for ranking in rankings:
            for rank,(procedure,hit) in enumerate(ranking.items(),1):
                scores[procedure]=scores.get(procedure,0)+1/(60+rank)
                representatives.setdefault(procedure,hit)
        ordered=sorted(scores,key=lambda key:(-scores[key],key))
        return [Hit(representatives[key].chunk,float(scores[key]),rank)
                for rank,key in enumerate(ordered,1)]

    # Treatment fidelity (rc.20): each treatment keeps its OWN ranking order
    # (dense, lexical, or fused), but every hit carries the dense cosine
    # similarity of that procedure/chunk as its score. The applicability gate
    # (validation.validate_chunk MIN_SIMILARITY, public-manual threshold) is a
    # similarity threshold; before rc.20 the fused treatments passed RRF scores
    # (~0.03) to it, so every source was rejected and treatments 3/4 always
    # abstained, while the lexical treatment passed a rescaled overlap score
    # (top hit = 1.0) that always cleared it. Now all four are gated the same.
    def search_procedures(self,query,k=5):
        mode=self.methodology['treatment']['retrieval'];dense=self._dense(query);sparse=self._sparse(query)
        if mode=='dense':ranked=list(dense.values())
        elif mode=='sparse':ranked=list(sparse.values())
        else:ranked=self._rrf(dense,sparse)
        ranked=ranked[:max(1,int(k))]
        similarity=lambda hit:dense[hit.chunk.procedure_id].score if hit.chunk.procedure_id in dense else 0.0
        self.last_trace={'mode':mode,'query':query,'dense_procedures':list(dense),
            'sparse_procedures':list(sparse),'selected_procedures':[hit.chunk.procedure_id for hit in ranked],
            'ranking_scores':{hit.chunk.procedure_id:round(float(hit.score),6) for hit in ranked},
            'gate_similarity':{hit.chunk.procedure_id:round(float(similarity(hit)),6) for hit in ranked},
            'independent_views':2 if mode=='dual_view' else 1}
        return [Hit(hit.chunk,float(similarity(hit)),rank) for rank,hit in enumerate(ranked,1)]

    def search(self,query,k=5):
        mode=self.methodology['treatment']['retrieval']
        dense_hits=self.base.search(query,k=len(self.chunks));dense={hit.chunk.chunk_id:hit for hit in dense_hits}
        sparse=self._sparse_chunks(query)
        if mode=='dense':ranked=list(dense.values())
        elif mode=='sparse':ranked=list(sparse.values())
        else:ranked=self._rrf(dense,sparse)
        similarity=lambda hit:dense[hit.chunk.chunk_id].score if hit.chunk.chunk_id in dense else 0.0
        return [Hit(hit.chunk,float(similarity(hit)),rank) for rank,hit in enumerate(ranked[:max(1,int(k))],1)]


def apply_retrieval(agent,methodology):
    if not methodology:return None
    wrapper=RetrievalTreatmentIndex(agent.index,methodology)
    agent.index=wrapper;agent.toolbox.index=wrapper
    return wrapper
