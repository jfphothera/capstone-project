"""rc.19 Tree-of-Thought entry rule: hard checks first, real ambiguity, session budget."""
import json

from capstone_rag import planning, store as store_mod

DRAFT = {'outbound_message': {'decision': 'draft_for_review'}, 'intents': ['support']}
BASE = {'verified': True, 'identified_part': False, 'field_completion': False, 'received_original': False,
        'safety_hold': False, 'public_sources': True, 'operational_qms_missing': False, 'evidence_ids': []}


def _store(tmp_path):
    st = store_mod.Store(str(tmp_path / 'ops.db'))
    st.insert('cases', {'case_id': 'CASE-T', 'created_at': store_mod.now(), 'updated_at': store_mod.now(), 'status': 'New', 'channel': 'email'})
    return st


def test_safety_hold_never_branches_even_when_forced(tmp_path):
    st = _store(tmp_path)
    entry = planning.tot_entry(st, 'CASE-T', 'My goggles are missing', DRAFT, {**BASE, 'safety_hold': True}, force_tree=True)
    assert entry['enter'] is False and 'safety hold' in entry['reason']


def test_keyword_alone_does_not_enter(tmp_path):
    st = _store(tmp_path)
    # "missing" is a request signal, but with only public guidance feasible and
    # nothing unresolved there is no real ambiguity.
    entry = planning.tot_entry(st, 'CASE-T', 'Which accessory is missing?', DRAFT,
                               {**BASE, 'identified_part': True, 'public_sources': False})
    assert entry['enter'] is False
    assert entry['consequential_request_signal'] is True


def test_competing_feasible_actions_enter(tmp_path):
    st = _store(tmp_path)
    facts = {**BASE, 'identified_part': True, 'field_completion': True, 'received_original': True}
    entry = planning.tot_entry(st, 'CASE-T', 'The unit is broken', DRAFT, facts)
    assert entry['enter'] is True and len(entry['feasible_candidates']) >= 2
    assert entry['reason'] == 'two or more feasible actions compete'


def test_unresolved_evidence_for_requested_action_enters_and_is_recorded(tmp_path):
    st = _store(tmp_path)
    entry = planning.tot_entry(st, 'CASE-T', 'The goggles were missing, please send a replacement part', DRAFT, BASE)
    assert entry['enter'] is True and 'identified_part' in entry['unresolved_evidence']


def test_session_budget_exhaustion_defaults_to_human_review(tmp_path):
    st = _store(tmp_path)
    for n in range(planning.TOT_SESSION_ENTRY_CAP):
        st.insert('candidate_plans', {'plan_id': f'PLAN-{n}', 'case_id': 'CASE-T', 'run_id': f'RUN-{n}', 'parent_plan_id': None,
                                      'based_on_version': 0, 'payload': json.dumps({}), 'status': 'feasible', 'at': store_mod.now()})
    facts = {**BASE, 'identified_part': True, 'field_completion': True, 'received_original': True}
    result = planning.plan(st, 'CASE-T', 'RUN-NEXT', 'The unit is broken', DRAFT, facts)
    assert result['mode'] == 'single_path' and result['selected_action'] == 'human_review'
    assert result['entry']['budget_exhausted'] is True and 'budget exhausted' in result['exit_reason']
