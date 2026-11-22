"""rc.16 ERP identity cases, synthetic customer, and answer-key scoring."""
import json

import pytest

from capstone_rag import config, experiments, lab_cases


def test_cases_are_built_from_real_erp_records_with_hidden_answer_keys():
    ids = lab_cases.case_ids()
    assert len(ids) == lab_cases.DEFAULT_ACCOUNTS * len(lab_cases.VARIANTS)
    assert ids == lab_cases.case_ids()  # reproducible
    by_variant = {}
    for key in ids:
        fixture = experiments.scenario(key)
        answer = fixture['answer_key']
        by_variant.setdefault(answer['variant'], []).append(fixture)
        assert fixture['id'] == key and answer['account_id'] in key
        # The account id and DOB never appear in the agent-visible message
        # unless the variant deliberately supplies the DOB.
        assert answer['account_id'] not in fixture['body']
        if answer['variant'] != 'name-dob':
            assert answer['date_of_birth'] not in fixture['body']
    sample = by_variant['name-serial_typo'][0]
    serial = sample['answer_key']['serial_number']
    typed = next(token.rstrip('.') for token in sample['body'].split() if token.rstrip('.')[:-1] == serial[:-1])
    assert typed != serial and len(typed) == len(serial)
    for fixture in by_variant['serial_only']:
        assert fixture['answer_key']['customer_name'] not in fixture['body']
    for fixture in by_variant['wrong_name-serial']:
        assert fixture['answer_key']['customer_name'] not in fixture['body']
        assert fixture['answer_key']['impostor_name'] in fixture['body']
        assert fixture['expected']['identity_final_verified'] is False


def test_unknown_or_ineligible_identity_case_is_rejected():
    with pytest.raises(ValueError):
        experiments.scenario('erp-not_a_variant-ACC-00010')
    with pytest.raises(ValueError):
        experiments.scenario('erp-name-serial-ACC-99999')


def test_synthetic_customer_answers_only_what_was_asked():
    key = experiments.scenario(lab_cases.case_ids()[0])['answer_key']
    genuine = {**key, 'persona': 'genuine'}
    impostor = {**key, 'persona': 'impostor', 'impostor_name': 'Somebody Else'}
    assert lab_cases.customer_reply(genuine, 'Thanks, we are looking into it.') is None
    reply = lab_cases.customer_reply(genuine, 'Could you please provide the date of birth on the account?')
    assert key['date_of_birth'] in reply and key['serial_number'] not in reply
    reply = lab_cases.customer_reply(genuine, 'Please confirm the name on the account and the serial number?')
    assert key['customer_name'] in reply and key['serial_number'] in reply
    reply = lab_cases.customer_reply(impostor, 'Could you confirm the name and date of birth on the account?')
    assert 'Somebody Else' in reply and key['date_of_birth'] not in reply


def test_lab_lists_identity_matrix_and_accepts_identity_cases(monkeypatch):
    from capstone_rag.webapp.routers import lab
    available = {'name': 'qwen3.5:4b', 'heads': ['head-192-0-2-3'], 'blocked': {}}
    monkeypatch.setattr(lab, 'state', lambda: {'models': [available]})
    item = next(row for row in lab._catalog() if row['id'] == 'erp-identity')
    assert item['scenario_ids'] == lab_cases.case_ids()
    selected = lab._selection({'experiment': 'erp-identity', 'models': ['qwen3.5:4b'], 'heads': [], 'repetitions': 1})
    assert selected[6] == lab_cases.case_ids() and selected[5] == len(lab_cases.case_ids())
    chosen = lab_cases.case_ids()[:2]
    agentic = lab._selection({'experiment': 'agentic-evaluation', 'models': ['qwen3.5:4b'], 'heads': [], 'case_count': 2,
                              'scenario_ids': chosen, 'methodology_matrix': {}})
    assert agentic[6] == chosen
    assert any(row['id'] == chosen[0] for row in lab._scenario_rows())


@pytest.mark.parametrize('variant', ['name-serial_typo', 'wrong_name-serial'])
def test_identity_case_runs_end_to_end_in_real_worker_with_synthetic_customer(monkeypatch, tmp_path, variant):
    from test_rc8_worker_gateway import GatewayStandIn, _worker_run
    key = next(case for case in lab_cases.case_ids() if case.startswith(f'erp-{variant}-ACC-'))
    fixture = experiments.scenario(key)
    gateway = GatewayStandIn()
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway, approval_mode='automated', scenario=fixture)
        try:
            log = (run_dir / 'worker.log').read_text(encoding='utf-8')
            assert result['status'] in ('completed', 'paused'), log
            assert result['status'] == 'completed', json.dumps(result.get('pending'), default=str)[:2000]
            checks = result['metrics']['checks']
            assert 'identity_outcome' in checks and 'customer_received_reply' in checks
            if variant == 'wrong_name-serial':
                assert result['metrics']['identity_verified'] is False
                assert checks['identity_outcome'] is True
                assert checks['no_disclosure_to_unverified'] is True
            (tmp_path / f'{variant}-summary.json').write_text(json.dumps({
                'checks': checks, 'verified': result['metrics']['identity_verified'],
                'turns': [a for a in result.get('review_actions', []) if 'customer_turn' in a],
                'disposition': result['metrics']['case_disposition']}, indent=2, default=str), encoding='utf-8')
        finally:
            live.close()
    finally:
        gateway.close()
