"""The 0.4.6 lab tools (lab-tools/*), run end to end against an in-process Gateway over HTTP with a live observation file.

Loopback only: the tools' head calls are replaced by a fake for the G4 direct step. The tools are rebuilt from
lab-tools/src first, so what is tested is what ships.
"""
import importlib.util
import io
import json
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from inference_gateway import recovery
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_gateway_server import Server
from inference_gateway.capacity_observe import snapshot

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('build_lab_tools', ROOT / 'scripts' / 'build_lab_tools.py')
builder = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(builder)
BUILT = builder.build()
HEADS = ['head-192-0-2-3', 'head-192-0-2-10']
MODELS = ['qwen3.5:4b', 'qwen3:4b', 'gemma3:4b']


def tool(name):
    script = Path(BUILT[name]['script'])
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), script)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Heads:
    def __init__(self):
        self.calls = []; self.fail_bodies = set()
    def __call__(self, url, operation, payload, timeout):
        if operation == '/api/version': return {'version': '0.40.15'}
        if operation == '/api/tags': return {'models': [{'name': m, 'digest': 'D-' + m, 'size': 3 * 1024 ** 3, 'details': {'family': 'x'}} for m in MODELS]}
        if operation == '/api/ps': return {'models': []}
        if operation == '/api/show': return {'capabilities': ['completion', 'tools']}
        if operation == '/api/generate': return {'done': True, 'model': payload['model'], 'response': 'READY', 'eval_count': 2}
        self.calls.append((url, payload['model']))
        text = json.dumps(payload.get('messages'))
        if any(marker in text for marker in self.fail_bodies):
            raise urllib.error.HTTPError(url + operation, 500, 'Internal Server Error', {}, io.BytesIO(b'{"error":"fake runner failure"}'))
        time.sleep(.05)
        fmt = payload.get('format')
        content = json.dumps({k: 'x' for k in (fmt.get('required') or [])}) if isinstance(fmt, dict) else 'ok'
        message = {'role': 'assistant', 'content': content}
        if payload.get('tools'): message['tool_calls'] = [{'function': {'name': 'lookup', 'arguments': {}}}]
        return {'done': True, 'model': payload['model'], 'message': message, 'eval_count': 5}


@pytest.fixture
def hub(tmp_path):
    heads = Heads(); gateway = Gateway(tmp_path / 'ledger.db', transport=heads); b = gateway.broker
    b.register_client('capstone-research', 'research-token-0123456789', models=MODELS, max_concurrency=24, queue_limit=64, priority=6)
    for rid in HEADS:
        b.authorize_resource(rid, endpoint='http://127.0.0.1:%d' % (21000 + int(rid.rsplit('-', 1)[1])), resource_type='shared_inference', owner='infrastructure',
                             applications=['capstone-research'], permitted_uses=['inference'], slots=4, memory_gb=31.4)
        for m in MODELS: assert gateway.qualify(rid, m, operator='ops', max_context=8192)['passed']
    with b.write('t') as db: recovery.store_policy(db, 'failure_policy', {'ps_settle_seconds': 0})
    gateway.refresh_health()
    server = Server(('127.0.0.1', 0), gateway); threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / 'research.token'; token.write_text('research-token-0123456789\n', encoding='utf-8')
    config = tmp_path / 'gateway-client.json'
    config.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1', 'service_id': 'shared-inference',
                                  'url': 'http://127.0.0.1:%d' % server.server_port, 'application_id': 'capstone-research', 'token_file': str(token)}), encoding='utf-8')
    observation = tmp_path / 'observation.json'; stop = threading.Event()
    def publish():
        while not stop.is_set():
            try:
                gateway.publish_counters(); doc = snapshot(tmp_path / 'ledger.db')
                tmp = observation.with_suffix('.tmp'); tmp.write_text(json.dumps(doc), encoding='utf-8'); tmp.replace(observation)
            except Exception: pass
            stop.wait(.5)
    threading.Thread(target=publish, daemon=True).start(); time.sleep(1)
    yield {'gateway': gateway, 'heads': heads, 'config': str(config), 'observation': str(observation), 'tmp': tmp_path}
    stop.set(); server.shutdown(); server.server_close()


def results(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'HERE', tmp_path / 'drive')
    return tmp_path / 'drive' / 'results'


def test_every_tool_refuses_while_the_gateway_has_work_and_says_why(hub, monkeypatch, capsys):
    g = hub['gateway']; heads = hub['heads']
    g.broker.submit('capstone-research', 'research-token-0123456789', 'experiment-1', 'qwen3.5:4b', ['@authorized'], time.time() + 60)
    time.sleep(1.2)  # the publisher shows it queued
    for name in BUILT:
        module = tool(name); out = results(module, hub['tmp'], monkeypatch)
        code = module.main(['--client-config', hub['config'], '--observation', hub['observation'], '--yes'])
        assert code == 1 and 'active work' in capsys.readouterr().out
        [note] = list(out.glob('*-%s-refused-*/refused.json' % name))
        assert 'queued' in json.loads(note.read_text())['reason']
    assert heads.calls == [], 'nothing was sent'


def test_a_stale_or_missing_observation_is_a_refusal(hub, monkeypatch, capsys, tmp_path):
    module = tool('launch-storm'); results(module, hub['tmp'], monkeypatch)
    assert module.main(['--client-config', hub['config'], '--observation', str(tmp_path / 'none.json'), '--yes']) == 1
    assert 'does not exist' in capsys.readouterr().out


def test_launch_storm_measures_both_sequences(hub, monkeypatch):
    module = tool('launch-storm'); out = results(module, hub['tmp'], monkeypatch)
    assert module.main(['--client-config', hub['config'], '--observation', hub['observation'], '--yes', '--levels', '3', '--rounds', '1']) == 0
    [report] = list(out.glob('*-launch-storm-*/report.json')); report = json.loads(report.read_text())
    by = {r['sequence']: r for r in report['rounds']}
    assert by['rc24']['api_calls_per_launch'] == 6 and by['scoped']['api_calls_per_launch'] == 2  # models, policy, 3 + 1 availability
    assert by['rc24']['errors'] == {} and by['scoped']['errors'] == {}
    assert by['rc24']['gateway']['routes']['GET /v1/policy']['count'] == 3
    assert 'research-token' not in json.dumps(report)
    assert hub['heads'].calls == [], 'discovery only'


def test_g6_capacity_check_runs_levels_and_reports_the_evidence(hub, monkeypatch):
    module = tool('g6-capacity-check'); out = results(module, hub['tmp'], monkeypatch)
    code = module.main(['--client-config', hub['config'], '--observation', hub['observation'], '--yes', '--levels', '2,3',
                        '--window-seconds', '10', '--warmup-seconds', '1', '--deadline-seconds', '30'])
    assert code == 0
    [report] = list(out.glob('*-g6-capacity-check-*/report.json')); report = json.loads(report.read_text())
    assert [L['level'] for L in report['levels']] == [2, 3]
    for L in report['levels']:
        assert L['all']['requests'] > 0 and L['all']['completed'] == L['all']['requests'] and L['all']['useful'] == L['all']['completed']
        assert L['contaminated_by_other_work'] == [] and L['per_head_overlap'] and 0 < L['reviewer_share']['calls'] < 1
        assert L['main']['queue_s']['p50'] is not None and L['reviewer']['execution_s']['p95'] is not None
        assert 'ledger' in L['gateway_overhead'] and L['gateway_overhead']['health_cycle_after'] is not None
    assert {m for _, m in hub['heads'].calls} == {'qwen3.5:4b', 'qwen3:4b'}, 'only the declared mix is loaded'
    assert 'research-token' not in json.dumps(report)
    with hub['gateway'].broker.connect() as db:
        keys = [r[0] for r in db.execute("SELECT client_key FROM capacity_requests WHERE app_id='capstone-research'")]
    assert keys and all(k.startswith('labtool-g6-') for k in keys)


LAUNCHER = ROOT / 'src' / 'inference_gateway' / 'service_launcher.py'


def identity_args(hub):
    """The hub paths, pointed at the test Gateway (the real tool reads them from the hub's deployment state)."""
    return ['--gateway-launcher', str(LAUNCHER), '--gateway-ledger', str(hub['tmp'] / 'ledger.db'), '--gateway-url', json.loads(Path(hub['config']).read_text())['url'],
            '--gateway-ca', '', '--identity-folder', str(hub['tmp'] / 'AgentArenaLab' / 'lab-capacity-check')]


def identity_state(hub):
    with hub['gateway'].broker.connect() as db:
        client = db.execute("SELECT enabled FROM capacity_clients WHERE app_id='lab-capacity-check'").fetchone()
        heads = {r[0]: json.loads(r[1]) for r in db.execute('SELECT resource_id,applications FROM capacity_resources')}
    folder = hub['tmp'] / 'AgentArenaLab' / 'lab-capacity-check'
    return (client[0] if client else None), heads, sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_g6_with_the_temporary_identity_issues_it_runs_and_revokes_it(hub, monkeypatch, capsys):
    module = tool('g6-capacity-check'); out = results(module, hub['tmp'], monkeypatch)
    code = module.main(['--observation', hub['observation'], '--yes', '--levels', '1,2', '--window-seconds', '10', '--warmup-seconds', '1',
                        '--deadline-seconds', '30', *identity_args(hub)])
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert 'Temporary identity lab-capacity-check issued' in printed and 'revoked; its token was deleted' in printed
    assert 'level 1/2' in printed and 'level 2/2 done' in printed
    enabled, heads, files = identity_state(hub)
    assert enabled == 0 and all('lab-capacity-check' not in apps for apps in heads.values()) and files == [], 'revoked, withdrawn, no token left'
    [report] = list(out.glob('*-g6-capacity-check-*/report.json')); report = json.loads(report.read_text())
    assert report['complete'] and [L['level'] for L in report['levels']] == [1, 2] and report['gateway']['application_id'] == 'lab-capacity-check'
    assert [s['verb'] for s in report['identity']['steps']][-2:] == ['revoke', 'withdraw-heads'] and all(s['ok'] for s in report['identity']['steps'][1:])
    assert all(0 < L['reviewer_share']['calls'] < 1 for L in report['levels']), 'even 1 caller sends the reviewer share'
    summary = (report_path := next(out.glob('*-g6-capacity-check-*'))) / 'summary.txt'
    assert 'Levels finished: 2 of 2.' in summary.read_text() and 'research-token' not in json.dumps(report)


def test_a_refusal_after_issuing_still_revokes_the_identity(hub, monkeypatch, capsys):
    module = tool('g6-capacity-check'); results(module, hub['tmp'], monkeypatch)
    code = module.main(['--observation', hub['observation'], '--yes', '--levels', '1', '--window-seconds', '10', '--main', 'gemma3:4b', *identity_args(hub)])
    assert code == 1 and 'not permitted' in capsys.readouterr().out
    enabled, heads, files = identity_state(hub)
    assert enabled == 0 and all('lab-capacity-check' not in apps for apps in heads.values()) and files == []


def test_a_crashed_run_is_cleaned_up_at_the_next_start(hub, monkeypatch, capsys):
    module = tool('launch-storm'); results(module, hub['tmp'], monkeypatch)
    a = module.argparse.ArgumentParser(); module.identity_args(a); identity = module.temporary_identity(a.parse_args(identity_args(hub)))
    identity.open()  # ... and the process "dies" here: enabled, on the heads, token on disk, state.json left
    enabled, heads, files = identity_state(hub)
    assert enabled == 1 and 'state.json' in files and 'identity.token' in files
    code = module.main(['--observation', hub['observation'], '--yes', '--levels', '2', '--rounds', '1', *identity_args(hub)])
    printed = capsys.readouterr().out
    assert code == 0 and 'An earlier run left lab-capacity-check behind' in printed
    enabled, heads, files = identity_state(hub)
    assert enabled == 0 and all('lab-capacity-check' not in apps for apps in heads.values()) and files == []


def test_a_token_folder_someone_else_may_use_is_refused_and_nothing_is_issued(hub, monkeypatch, capsys):
    import subprocess
    module = tool('launch-storm'); results(module, hub['tmp'], monkeypatch)
    folder = hub['tmp'] / 'AgentArenaLab' / 'lab-capacity-check'; folder.mkdir(parents=True)
    assert subprocess.run(['icacls', str(folder), '/grant', '*S-1-1-0:(OI)(CI)M'], capture_output=True).returncode == 0  # Everyone may modify
    code = module.main(['--observation', hub['observation'], '--yes', '--levels', '2', '--rounds', '1', *identity_args(hub)])
    assert code == 1 and 'not safe for the token' in capsys.readouterr().out
    enabled, heads, files = identity_state(hub)
    assert enabled is None and files == [], 'never issued, nothing written there'


def test_cleanup_mode_removes_an_interrupted_runs_identity(hub, monkeypatch, capsys):
    module = tool('g6-capacity-check'); results(module, hub['tmp'], monkeypatch)
    a = module.argparse.ArgumentParser(); module.identity_args(a); identity = module.temporary_identity(a.parse_args(identity_args(hub)))
    identity.open(expires_in=600)
    assert 'LAUNCH-STORM.cmd --cleanup' in identity.cleanup_hint('LAUNCH-STORM.cmd')
    assert module.main(['--cleanup', *identity_args(hub)]) == 0
    printed = capsys.readouterr().out
    assert 'revoked; its token was deleted' in printed and 'Cleanup done.' in printed
    enabled, heads, files = identity_state(hub)
    assert enabled == 0 and all('lab-capacity-check' not in apps for apps in heads.values()) and files == []
    assert module.main(['--cleanup', *identity_args(hub)]) == 0 and 'Nothing to clean up' in capsys.readouterr().out


def test_g6_stops_at_once_on_outside_queued_work_and_revokes_the_identity(hub, monkeypatch, capsys):
    g = hub['gateway']
    g.broker.register_client('outsider', 't-outsider-0123456789', models=['never-installed:1'], max_concurrency=1, queue_limit=4, priority=6)
    module = tool('g6-capacity-check'); out = results(module, hub['tmp'], monkeypatch)
    def outsider():
        # Once the first level is running (the tool's first request exists): a request that stays queued (its model is on no head).
        deadline = time.time() + 90
        while time.time() < deadline:
            with g.broker.connect() as db:
                if db.execute("SELECT 1 FROM capacity_requests WHERE app_id='lab-capacity-check'").fetchone():break
            time.sleep(.5)
        time.sleep(2)
        g.broker.submit('outsider', 't-outsider-0123456789', 'outside-1', 'never-installed:1', ['@authorized'], time.time() + 120)
    threading.Thread(target=outsider, daemon=True).start()
    began = time.time()
    code = module.main(['--observation', hub['observation'], '--yes', '--levels', '2,3', '--window-seconds', '60', '--warmup-seconds', '1',
                        '--deadline-seconds', '60', *identity_args(hub)])
    took = time.time() - began; printed = capsys.readouterr().out
    assert code == 0 and 'OUTSIDE WORK: 0 running and 1 queued requests of outsider' in printed
    assert took < 40, 'stopped at once, not after its 60 s window (took %.0f s)' % took
    [report] = list(out.glob('*-g6-capacity-check-*/report.json')); report = json.loads(report.read_text())
    assert [L['level'] for L in report['levels']] == [2] and report['levels'][0]['contaminated_by_other_work'][0]['queued'] == 1
    assert any('revoked at once' in n for n in report['notes'])
    enabled, heads, files = identity_state(hub)
    assert enabled == 0 and all('lab-capacity-check' not in apps for apps in heads.values()) and files == []
    with g.broker.connect() as db:
        assert db.execute("SELECT count(*) FROM capacity_requests WHERE app_id='lab-capacity-check' AND status='queued'").fetchone()[0] == 0


def test_g4_reproduction_replays_bodies_and_keeps_the_error(hub, monkeypatch):
    module = tool('g4-gemma500-repro'); out = results(module, hub['tmp'], monkeypatch)
    data = module.fixture(module.FIXTURE_BLOB)
    target = next(b for b in data['bodies'] if b['label'].startswith('TARGET'))
    marker = 'Noah Thomas'; assert marker in json.dumps(target['payload'])
    hub['heads'].fail_bodies.add(marker)
    seen = []
    def head_call(address, path, body=None, timeout=300):
        seen.append((address, path))
        if path == '/api/version': return {'status': 200, 'value': {'version': '0.40.15'}}
        if path == '/api/tags': return {'status': 200, 'value': {'models': [{'name': 'gemma3:4b', 'digest': module.C922_BUILD['digest']}]}}
        if path == '/api/ps': return {'status': 200, 'value': {'models': []}}
        failing = marker in json.dumps(body['messages']) and body.get('format') is not None and isinstance(body.get('format'), dict)
        if failing: return {'status': 500, 'error_body': '{"error":"fake: failed to sample token"}', 'error_message': 'fake: failed to sample token', 'seconds': .1}
        fmt = body.get('format'); content = json.dumps({'reply': 'x'}) if fmt else 'plain'
        return {'status': 200, 'seconds': .1, 'value': {'done': True, 'done_reason': 'stop', 'eval_count': 40, 'message': {'content': content}}}
    monkeypatch.setattr(module, 'head_call', head_call)
    code = module.main(['--client-config', hub['config'], '--observation', hub['observation'], '--yes', '--head', '192.0.2.10', '--repeats', '2'])
    assert code == 0
    [report] = list(out.glob('*-g4-gemma500-repro-*/report.json')); report = json.loads(report.read_text())
    assert report['head']['same_build_as_c922'] is True
    tally = report['tally']
    assert tally['%s / exact' % target['label']] == {'HTTP 500': 2}
    assert tally['%s / no_format' % target['label']] == {'HTTP 200': 1} and tally['%s / format_json' % target['label']] == {'HTTP 200': 1}
    assert report['distinct_error_messages'] == ['fake: failed to sample token']
    gw = {r['body']: r for r in report['gateway_replays']}
    assert gw[target['label']]['status'] == 'failed' and gw[target['label']]['upstream_errors'][0]['message'] == 'fake runner failure'
    assert gw[target['label']]['resource_id'] == 'head-192-0-2-10'
    assert all(a == '192.0.2.10' for a, _ in seen)
