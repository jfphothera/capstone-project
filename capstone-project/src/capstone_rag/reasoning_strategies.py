"""Bounded observable reasoning strategies; no hidden chain-of-thought is stored."""
from __future__ import annotations
import time


MAX_QMS_ROUNDS = 2
MAX_TOT_DEPTH = 3
MAX_TOT_STATES = 6


def observation_driven_qms(search, query, *, model=None, revision=None,
                           exact_revision=False, device=None, revision_context=None):
    """Search once, and refine once only when the first observation warrants it."""
    rounds = []
    current = query.strip()
    started=time.perf_counter()
    try:
        result = search(current, model, revision, exact_revision,
                        device=device, revision_context=revision_context)
    except Exception as exc:
        return {'chunks':[],'procedures':[],'contract':{},'evidence':{},'tool_error':str(exc)}, {
            'rounds':[{'round':1,'tool':'customer_manual_search','query':current,
                'observation':{'tool_error':str(exc),'source_count':0,'source_ids':[]},
                'duration_ms':round((time.perf_counter()-started)*1000,3),'retry_reason':None}],
            'max_rounds':MAX_QMS_ROUNDS,'termination':'tool_error'}
    source_ids = [getattr(c, 'chunk_id', str(c)) for c in result.get('chunks', [])]
    observation = {'source_count': len(source_ids), 'source_ids': source_ids,
                   'procedure_count': len(result.get('procedures', [])),
                   'has_contract': bool(result.get('contract'))}
    rounds.append({'round': 1, 'tool': 'customer_manual_search', 'query': current,
                   'observation': observation,'duration_ms':round((time.perf_counter()-started)*1000,3),
                   'retry_reason': None})
    # The retry decision is based on the real result, not an advance plan.
    if not source_ids and model:
        refined = f'{model} customer manual {current}'.strip()
        if refined != current:
            rounds[0]['retry_reason'] = 'No authorized sources in first observation; refine with established model'
            started=time.perf_counter()
            result = search(refined, model, revision, exact_revision,
                            device=device, revision_context=revision_context)
            ids = [getattr(c, 'chunk_id', str(c)) for c in result.get('chunks', [])]
            rounds.append({'round': 2, 'tool': 'customer_manual_search', 'query': refined,
                           'observation': {'source_count': len(ids), 'source_ids': ids,
                               'procedure_count': len(result.get('procedures', [])),
                               'has_contract': bool(result.get('contract'))},
                           'duration_ms':round((time.perf_counter()-started)*1000,3),
                           'retry_reason': 'refined from first tool observation'})
    return result, {'rounds': rounds, 'max_rounds': MAX_QMS_ROUNDS,
                    'termination': 'authorized_evidence_returned' if result.get('chunks')
                    else 'no_applicable_authorized_evidence'}
