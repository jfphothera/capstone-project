"""rc.25 lab qualification tool (mechanics against the stand-in Gateway; not model evidence)."""
import json
import sqlite3
import time

from capstone_rag import config, lab_qualification as lq
from capstone_rag.ai_settings import Registry
from capstone_rag.store import Store

MODELS = ('gemma3:4b', 'granite4:3b', 'qwen3.5:4b', 'qwen3:1.7b', 'llama3.2:3b', 'qwen3:4b')


def test_refuses_while_an_experiment_is_active(tmp_path):
    data = tmp_path / 'ProgramData';(data / 'data').mkdir(parents=True)
    with sqlite3.connect(data / 'data' / 'ai_configuration.db') as db:
        db.execute('CREATE TABLE experiments(id TEXT PRIMARY KEY, payload TEXT)')
        db.execute('INSERT INTO experiments VALUES (?,?)', ('EXP-1', json.dumps({'id': 'EXP-1', 'status': 'running'})))
    out = tmp_path / 'out'
    assert lq.main(['--data-root', str(data), '--install-root', str(tmp_path), '--out', str(out)]) == 2
    report = json.loads(next(out.glob('qualification-*.json')).read_text())
    assert report['status'] == 'refused' and report['experiment_launched'] is False


def test_live_worker_lease_also_refuses(tmp_path):
    data = tmp_path / 'ProgramData';run = data / 'data' / 'experiments' / 'EXP-2' / 'RUN-1';run.mkdir(parents=True)
    with sqlite3.connect(run / 'worker-lease.db') as db:
        db.execute('CREATE TABLE lease (id INTEGER PRIMARY KEY,owner TEXT,expires REAL)');db.execute('INSERT INTO lease VALUES(1,?,?)', ('w', time.time() + 60))
    assert lq.active_experiments(data)[0]['status'] == 'live worker lease'


def test_sections_run_against_the_stand_in_gateway(monkeypatch, tmp_path):
    from test_rc8_worker_gateway import GatewayStandIn, HEAD
    monkeypatch.setattr(lq, 'ROUTE_OVERRIDES', {'cluster_nodes': [HEAD]})
    gateway = GatewayStandIn(real_embeddings=True, models=MODELS, no_tools_models=('gemma3:4b',))
    try:
        token = tmp_path / 'research.token';token.write_text('fixture-research-token', encoding='utf-8')
        client = tmp_path / 'research-client.json'
        client.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1', 'service_id': 'shared-inference',
                                      'url': gateway.url, 'application_id': 'capstone-research', 'token_file': str(token)}), encoding='utf-8')
        monkeypatch.setenv('CAPSTONE_INFERENCE_MODE', 'gateway')
        monkeypatch.setenv('CAPSTONE_GATEWAY_CLIENT_CONFIG', str(client));monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG', str(client))
        monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
        store = Store(str(tmp_path / 'q.db'));store.routing_enabled = True
        registry = Registry(tmp_path / 'ai_configuration.db');store.inference_registry = registry
        gemma = lq.gemma_mediated(store, registry)
        assert gemma['status'] == 'completed' and gemma['results']['mediated_selection']['mechanism'] == 'mediated', gemma
        assert not any(call.get('native_tools_rejected') for call in gemma['calls'])          # no native tools sent to Gemma
        budgets = lq.structured_budgets(store, registry, 'granite4:3b')
        assert budgets['status'] == 'completed' and set(budgets['summary']) == {'qms_coverage_summary', 'peer_challenge', 'peer_reply'}
        native = lq.native_arguments(store, registry, 'qwen3:1.7b')
        assert native['status'] == 'completed' and native['contract_valid'] is True and native['model_arguments_executed'] is False
        calibration = lq.reviewer_calibration(store, registry)
        assert calibration['status'] == 'completed' and len(calibration['results']) == 2
        skipped = lq._skip('mistral:7b')
        assert skipped and skipped['status'] == 'skipped' and 'mistral:7b' in skipped['reason']
        store.close()
    finally:
        gateway.close()


def test_missing_configuration_is_an_installation_problem(tmp_path):
    data = tmp_path / 'ProgramData';(data / 'data').mkdir(parents=True)
    assert lq.main(['--data-root', str(data), '--install-root', str(tmp_path), '--out', str(tmp_path / 'out')]) == 3
