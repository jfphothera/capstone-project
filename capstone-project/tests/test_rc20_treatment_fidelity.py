"""rc.20 treatment fidelity: each retrieval treatment ranks its own way, but all
are gated on the same semantic similarity (treatments 3/4 used to abstain 100%)."""
import pytest

from capstone_rag import agentic_methodologies, caseflow, config

QUERY = 'My P4800 is completely dead: no lights, no fan, and no display. What can I check without opening the enclosure?'


@pytest.fixture(scope='module')
def agent():
    return caseflow.make_agent(prefer_llm=False)


@pytest.mark.parametrize('treatment', ['1', '2', '3', '4'])
def test_every_treatment_clears_the_similarity_gate_for_the_applicable_procedure(agent, treatment):
    methodology = next(c['methodology'] for c in agentic_methodologies.candidates(['A'], [treatment]))
    base = agent.index.base if isinstance(agent.index, agentic_methodologies.RetrievalTreatmentIndex) else agent.index
    wrapper = agentic_methodologies.RetrievalTreatmentIndex(base, methodology)
    hits = wrapper.search_procedures(QUERY, k=5)
    by_procedure = {hit.chunk.procedure_id: hit for hit in hits}
    assert 'PROC-P4800-PWR-A' in by_procedure, (treatment, list(by_procedure))
    # The gate sees real cosine similarity, never a fusion or rescaled lexical score.
    dense = {hit.chunk.procedure_id: hit.score for hit in base.search_procedures(QUERY, k=len(base.chunks))}
    for procedure, hit in by_procedure.items():
        assert hit.score == pytest.approx(dense[procedure])
    assert by_procedure['PROC-P4800-PWR-A'].score >= config.MIN_SIMILARITY
    assert wrapper.last_trace['mode'] == methodology['treatment']['retrieval']


def test_treatments_still_rank_differently(agent):
    base = agent.index
    orders = {}
    for treatment in '1234':
        methodology = next(c['methodology'] for c in agentic_methodologies.candidates(['A'], [treatment]))
        wrapper = agentic_methodologies.RetrievalTreatmentIndex(base, methodology)
        wrapper.search_procedures('accessory missing goggles box carton', k=6)
        orders[treatment] = (wrapper.last_trace['mode'], tuple(wrapper.last_trace['selected_procedures']))
    assert len({mode for mode, _ in orders.values()}) == 4
    assert orders['1'][1] != orders['2'][1] or orders['2'][1] != orders['3'][1]
