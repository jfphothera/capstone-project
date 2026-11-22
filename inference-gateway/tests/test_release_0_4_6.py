"""Release 0.4.6: the final corrective iteration's Gateway items (docs/RELEASE-NOTES-0.4.6.md).

G1 cheap policy reads, one read snapshot for discovery, an application-scoped fleet snapshot; dispatch still checks.
G2 /v1/models capacity map no longer overwritten by a qualification document (two heads, limit 4, both orders).
G3 placement planned outside the write lock; a local ledger failure in a health cycle never marks a head offline.
G4 the head's HTTP error body is kept (bounded, redacted, protected) and the failure class is unchanged.
G5 installed, qualified and available are separate states in /v1/fleet.
Phase 4: structured_chat (chat + JSON schema) is qualified as its own shape.
"""
import importlib.util
import io
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import shared_inference_client
from shared_inference_client import Client, GatewayFeatureUnavailable
from inference_gateway import capacity_lifecycle, model_capacity, placement, recovery
from inference_gateway.capacity_broker import LedgerBusy
from inference_gateway.capacity_gateway import Gateway, http_error_body, redact, request_shape
from inference_gateway.capacity_gateway_server import Server
from inference_gateway.capacity_observe import snapshot

_spec = importlib.util.spec_from_file_location('fp_helpers', Path(__file__).with_name('test_failures_and_placement.py'))
fp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fp)
_spec4 = importlib.util.spec_from_file_location('r044', Path(__file__).with_name('test_release_0_4_4.py'))
r044 = importlib.util.module_from_spec(_spec4); _spec4.loader.exec_module(r044)

MAIN = 'qwen3.5:4b'


def declared(n=4):
    return {MAIN: {'parallel': n, 'digest': 'DIGEST-' + MAIN, 'runtime_version': '0.40.15', 'num_ctx': 8192, 'evidence': 'lab matrix'}}


def two_heads(tmp_path, order, *, unmeasured=1, extra_model='m:1'):
    """Two 8-slot heads, qwen3.5:4b declared 4 on both, enrolled in `order`; a second qualified model on each."""
    transport = r044.UpgradedLab(); gateway = Gateway(tmp_path / 'ledger.db', transport=transport); b = gateway.broker
    b.register_client('capstone', 't-capstone', models=[MAIN, extra_model], max_concurrency=32, queue_limit=64, priority=6)
    for rid in order:
        b.authorize_resource(rid, endpoint=fp.endpoint(rid), resource_type='shared_inference', owner='infrastructure', applications=['capstone'],
                             permitted_uses=['inference'], slots=8, memory_gb=31.4, model_capacity=declared())
        for model in (MAIN, extra_model):
            assert gateway.qualify(rid, model, operator='ops', max_context=8192)['passed']
    with b.write('test') as db:
        recovery.store_policy(db, 'failure_policy', {'ps_settle_seconds': 0})
        recovery.store_policy(db, 'placement_policy', {'unmeasured_model_limit': unmeasured})
    transport.calls.clear(); gateway.refresh_health()
    return gateway, transport


# ---------------------------------------------------------------- G2

@pytest.mark.parametrize('order', [('head-192-0-2-7', 'head-192-0-2-8'), ('head-192-0-2-8', 'head-192-0-2-7')])
def test_g2_every_head_reports_its_declared_limit_and_models_observation_and_scheduler_agree(tmp_path, order):
    gateway, transport = two_heads(tmp_path, order)
    reported = {r['resource_id']: r['model_details'][MAIN]['parallel'] for r in gateway.models('capstone', 't-capstone')['resources']}
    assert set(reported) == set(order)
    for rid in order:
        assert (reported[rid]['limit'], reported[rid]['source']) == (4, 'declared'), \
            '%s: 0.4.5 gave the later head the unmeasured default (1) because the capacity map was overwritten' % rid
    observed = {r['resource_id']: {l['model']: l for l in r['capacity']['model_limits']} for r in snapshot(tmp_path / 'ledger.db')['resources']}
    with gateway.broker.connect() as db:
        resources = [dict(r) for r in db.execute('SELECT * FROM capacity_resources')]
        view = placement.View(db, time.time(), resources, recovery.load_policy(db)['placement'], recovery.active(db))
        for rid in order:
            assert observed[rid][MAIN]['limit'] == reported[rid]['limit'] == view.model_limit(rid, MAIN) == 4
            # The other model has no evidence on either head: the same default everywhere.
            assert observed[rid]['m:1']['limit'] == view.effective_limit(rid, 'm:1')['limit'] == 1
    # And the scheduler really runs 4 of the model at once per head (8 calls, both heads busy with 4).
    transport.hold = True
    threads = fp.background(gateway, ['k%d' % i for i in range(9)], model=MAIN, prompt='hold')
    assert transport.wait_inflight(8)
    time.sleep(.5)
    assert transport.inflight == 8 and len(fp.leases(gateway)) == 8, 'the ninth call waits: 4 per head'
    transport.release.set()
    for t in threads:
        t.join(10)


def test_g2_head_limits_refuses_a_qualification_document_in_place_of_the_capacity_map(tmp_path):
    gateway, _ = two_heads(tmp_path, ('head-192-0-2-7',))
    with gateway.broker.connect() as db, pytest.raises(TypeError):
        model_capacity.head_limits(db, 'head-192-0-2-7', [MAIN], recovery.load_policy(db)['placement'], evidence={'purpose': 'completion', 'metadata': {}})


# ---------------------------------------------------------------- G1

def fleet_lab(tmp_path, models=('m:1', 'other:1', 'all-minilm:latest'), heads=('head-192-0-2-3', 'head-192-0-2-4', 'head-192-0-2-5')):
    gateway, transport = fp.lab(tmp_path, list(heads), models=models, clients={'capstone': (8, 6), 'coder': (2, 5)})
    return gateway, transport


def test_g1_policy_reads_one_snapshot_and_policy_only_reads_no_head(tmp_path, monkeypatch):
    many = ['m:1', 'other:1', 'all-minilm:latest'] + ['extra-%d:1' % i for i in range(33)]  # 36 authorized models, as in c922
    gateway, _ = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4'], models=('m:1', 'other:1', 'all-minilm:latest'))
    gateway.broker.update_client_policy('capstone', models=many, max_concurrency=5, queue_limit=32, priority=6)
    calls = {'context': 0, 'reason': 0}
    real_context, real_reason = Gateway._context, Gateway._reason_in
    monkeypatch.setattr(Gateway, '_context', lambda self, *a: (calls.__setitem__('context', calls['context'] + 1), real_context(self, *a))[1])
    monkeypatch.setattr(Gateway, '_reason_in', lambda self, *a: (calls.__setitem__('reason', calls['reason'] + 1), real_reason(self, *a))[1])
    full = gateway.policy('capstone', 't-capstone')
    assert calls['context'] == 1, '0.4.5: 72 complete availability() calculations, each re-reading every head'
    assert len(full['models']) == 36 and full['availability_included'] is True
    by = {m['model']: m['operations'] for m in full['models']}
    assert by['m:1'] == {'/api/generate': 'available', '/api/embed': 'qualified_other_operation'}
    assert by['all-minilm:latest']['/api/embed'] == 'available' and by['extra-0:1']['/api/generate'] == 'not_installed'
    calls.update(context=0, reason=0)
    cheap = gateway.policy('capstone', 't-capstone', include_availability=False)
    assert calls == {'context': 0, 'reason': 0} and cheap['availability_included'] is False
    assert cheap['policy'] == full['policy'] and [m['model'] for m in cheap['models']] == many
    # The same answers as the single-model availability endpoint.
    for model in ('m:1', 'all-minilm:latest', 'extra-0:1'):
        for op in ('/api/generate', '/api/embed'):
            assert gateway.availability('capstone', 't-capstone', model, op)['status'] == by[model][op]


def test_g1_qualification_facts_are_rebuilt_when_a_new_test_is_recorded(tmp_path):
    gateway, transport = fleet_lab(tmp_path)
    gateway.models('capstone', 't-capstone'); built = gateway.counters.get('facts_cache_builds', 0)
    gateway.models('capstone', 't-capstone')
    assert gateway.counters.get('facts_cache_builds', 0) == built and gateway.counters['facts_cache_hits'] > 0
    # A capability test fails on head 3 (the fake answers chat without a message): its facts are rebuilt at once
    # and the result shows there only.
    assert gateway.qualify('head-192-0-2-3', 'm:1', operator='ops', purpose='chat')['passed'] is False
    detail = {r['resource_id']: r['model_details']['m:1'] for r in gateway.models('capstone', 't-capstone')['resources']}
    assert detail['head-192-0-2-3']['qualifications']['chat']['state'] == 'failed'
    assert detail['head-192-0-2-4']['qualifications']['chat']['state'] == 'not_tested'
    assert gateway.counters['facts_cache_builds'] == built + 1


def test_g1_fleet_snapshot_is_shared_scoped_and_never_stale_on_revocation_suspension_or_digest_change(tmp_path):
    gateway, transport = fleet_lab(tmp_path)
    first = gateway.fleet('capstone', 't-capstone', models=['m:1'])
    second = gateway.fleet('capstone', 't-capstone', models=['m:1'])
    assert second['snapshot']['served_from_cache'] is True and second['snapshot']['snapshot_id'] == first['snapshot']['snapshot_id']
    assert first['snapshot']['valid_until'] - first['snapshot']['generated_at'] == 5 and first['snapshot']['authoritative'] is False
    # Another application's snapshot is its own (different identity, different scope).
    other = gateway.fleet('coder', 't-coder', models=['m:1'])
    assert other['application_id'] == 'coder' and other['snapshot']['snapshot_id'] != first['snapshot']['snapshot_id']
    # Suspension: the inputs changed, so the next snapshot is new and says why.
    with gateway.broker.write('test') as db:
        recovery.suspend_model_on_head(gateway.broker, db, 'head-192-0-2-3', 'm:1', 'DIGEST-m:1', 'completion', request_id=None, source='test')
    third = gateway.fleet('capstone', 't-capstone', models=['m:1'])
    assert third['snapshot']['served_from_cache'] is False and third['snapshot']['snapshot_id'] != first['snapshot']['snapshot_id']
    assert third['models'][0]['resources']['head-192-0-2-3']['availability']['/api/generate'] == 'model_suspended_on_resource'
    # Digest change on head 4: the snapshot reports it at once.
    real = transport.__call__
    def changed(url, operation, payload, timeout):
        reply = real(url, operation, payload, timeout)
        if operation == '/api/tags' and url == fp.endpoint('head-192-0-2-4'):
            reply = {'models': [dict(m, digest='NEW-' + m['name']) if m['name'] == 'm:1' else m for m in reply['models']]}
        return reply
    gateway.transport = changed; gateway.refresh_health()
    fourth = gateway.fleet('capstone', 't-capstone', models=['m:1'])
    assert fourth['models'][0]['resources']['head-192-0-2-4']['availability']['/api/generate'] in ('digest_changed', 'not_qualified')
    # Revocation: no snapshot, cached or not, and no request.
    gateway.broker.revoke('capstone')
    with pytest.raises(PermissionError):
        gateway.fleet('capstone', 't-capstone', models=['m:1'])
    with pytest.raises(PermissionError):
        fp.run(gateway, 'after-revoke')


def test_g1_a_cached_discovery_never_authorizes_dispatch_after_permission_withdrawal(tmp_path):
    gateway, transport = fleet_lab(tmp_path)
    assert gateway.fleet('capstone', 't-capstone', models=['m:1'])['models'][0]['available']['any']
    gateway.broker.update_client_policy('capstone', models=['other:1'], max_concurrency=8, queue_limit=32, priority=6)
    with pytest.raises(PermissionError):
        fp.run(gateway, 'withdrawn')
    assert gateway.fleet('capstone', 't-capstone', models=['m:1'])['models'][0] == {'model': 'm:1', 'authorized': False, 'status': 'not_permitted'}


# ---------------------------------------------------------------- G5

def test_g5_installed_qualified_and_available_are_separate_states(tmp_path):
    gateway, transport = fleet_lab(tmp_path)
    gateway.broker.update_client_policy('capstone', models=['m:1', 'other:1', 'all-minilm:latest', 'absent:1'], max_concurrency=8, queue_limit=32, priority=6)
    with gateway.broker.write('test') as db:  # m:1 suspended on every head (a cascade)
        for rid in ('head-192-0-2-3', 'head-192-0-2-4'):
            recovery.suspend_model_on_head(gateway.broker, db, rid, 'm:1', 'DIGEST-m:1', 'completion', request_id=None, source='test')
    out = {m['model']: m for m in gateway.fleet('capstone', 't-capstone', models=['m:1', 'absent:1', 'not-mine:1', 'all-minilm:latest'])['models']}
    suspended = out['m:1']
    assert suspended['installed']['state'] == 'installed' and set(suspended['installed']['resources']) == {'head-192-0-2-3', 'head-192-0-2-4', 'head-192-0-2-5'}
    assert suspended['qualified']['state'] == 'qualified'
    assert suspended['available']['status']['/api/generate'] == 'model_suspended' and suspended['available']['any'] is False
    assert {h['availability']['/api/generate'] for h in suspended['resources'].values()} == {'model_suspended'}
    # /v1/models still drops it (0.2.0 shape): the fleet is where "installed but suspended" is visible.
    assert all('m:1' not in r['models'] for r in gateway.models('capstone', 't-capstone')['resources'])
    assert out['absent:1']['installed']['state'] == 'not_installed' and out['absent:1']['qualified']['state'] == 'not_applicable'
    assert out['absent:1']['available']['status']['/api/generate'] == 'not_installed'
    assert out['not-mine:1'] == {'model': 'not-mine:1', 'authorized': False, 'status': 'not_permitted'}, 'nothing about heads for a model not permitted'
    assert out['all-minilm:latest']['available']['status'] == {'/api/generate': 'qualified_other_operation', '/api/embed': 'available'}
    # Busy is occupancy only, and stale inventory is its own state.
    transport.hold = True
    threads = fp.background(gateway, ['private-key-%d' % i for i in range(3)], model='other:1', prompt='hold')
    assert transport.wait_inflight(3)
    busy = gateway.fleet('capstone', 't-capstone', models=['other:1'])['models'][0]
    assert busy['available']['status']['/api/generate'] == 'busy' and 'private-key' not in json.dumps(busy)
    transport.release.set()
    for t in threads:
        t.join(10)
    with gateway.broker.write('test') as db:
        db.execute('UPDATE capacity_inventory SET observed_at=observed_at-600')
    stale = gateway.fleet('capstone', 't-capstone', models=['other:1'])['models'][0]
    assert stale['installed']['inventory_fresh'] is False and stale['available']['status']['/api/generate'] == 'inventory_stale'


# ---------------------------------------------------------------- G3

def test_g3_a_full_fleet_pass_takes_no_write_lock_and_order_and_limits_hold(tmp_path):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4'], clients={'capstone': (2, 6), 'coder': (8, 5)})
    transport.hold = True
    threads = fp.background(gateway, ['c1', 'c2', 'c3', 'c4'], prompt='hold')  # capstone: 2 run, 2 wait (its own limit)
    assert transport.wait_inflight(2); time.sleep(.3)
    b = gateway.broker
    before = dict(b.place_stats); writes = b.ledger_stats()['labels'].get('place', {}).get('count', 0)
    for _ in range(20):
        assert b.place() == []
    after = b.place_stats
    assert after['read_only_passes'] - before['read_only_passes'] >= 20 # (the scheduler's own passes may add more)
    assert b.ledger_stats()['labels'].get('place', {}).get('count', 0) == writes, 'nothing to grant: no write transaction'
    assert len(fp.leases(gateway)) == 2, 'the per-application limit still holds'
    transport.release.set()
    for t in threads:
        t.join(15)
    with gateway.broker.connect() as db:
        assert {r[0] for r in db.execute("SELECT status FROM capacity_requests WHERE app_id='capstone'")} == {'completed'}, 'all four ran, in turn'


def test_g3_unplaceable_signatures_are_skipped_within_a_pass_but_other_models_still_place(tmp_path):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'], models=('m:1', 'other:1'), clients={'capstone': (16, 6)}, slots={'head-192-0-2-3': 2})
    with gateway.broker.write('test') as db:
        recovery.store_policy(db, 'placement_policy', {'model_parallel': {'m:1': 1}})
    b = gateway.broker; deadline = time.time() + 60
    ids = [b.submit('capstone', 't-capstone', 'q%d' % i, 'm:1', ['head-192-0-2-3'], deadline, operation='completion')['request_id'] for i in range(6)]
    late = b.submit('capstone', 't-capstone', 'late', 'other:1', ['head-192-0-2-3'], deadline, operation='completion')['request_id']
    changed = b.place()
    assert ids[0] in changed and late in changed and not set(ids[1:]) & set(changed), 'm:1 at its limit of 1; other:1 takes the second slot'
    assert b.place_stats['memo_skips'] >= 4, 'the four other m:1 requests were not evaluated again in that pass'


def test_g3_a_local_ledger_failure_in_a_health_cycle_never_marks_the_head_offline(tmp_path, monkeypatch):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4'])
    logged = []; gateway.emit = lambda event, **fields: logged.append((event, fields))
    real = gateway._inventory_in
    def busy(db, resource, version, installed):
        if resource['resource_id'] == 'head-192-0-2-3':
            raise LedgerBusy('health', 30.0, {'label': 'place', 'held_ms': 2277})
        return real(db, resource, version, installed)
    monkeypatch.setattr(gateway, '_inventory_in', busy)
    cycle = gateway.refresh_health()
    assert fp.health(gateway, 'head-192-0-2-3') == 'healthy', '0.4.5 marked this answering head offline'
    assert fp.health(gateway, 'head-192-0-2-4') == 'healthy', 'and the remaining heads are still recorded'
    assert cycle['head_failures'] == [] and cycle['local_failures'] == ['head_record']
    [(event, fields)] = [x for x in logged if x[0] == 'health.local_failure']
    assert fields['resource_id'] == 'head-192-0-2-3' and fields['ledger_busy'] is True and fields['stage'] == 'head_record'
    assert gateway.counters['health_local_failures'] == 1 and 'health_head_failures' not in gateway.counters
    # A real head failure is still offline, with its stage and cause in the event.
    transport.down.add(fp.endpoint('head-192-0-2-4'))
    cycle = gateway.refresh_health()
    assert fp.health(gateway, 'head-192-0-2-4') == 'offline' and cycle['head_failures'] == ['head-192-0-2-4']
    change = [p for e, p in fp.events(gateway) if e == 'resource.health_changed' and p['resource_id'] == 'head-192-0-2-4'][-1]
    assert change['to'] == 'offline' and change['stage'] == 'version' and 'URLError' in change['cause']
    assert [x for x in logged if x[0] == 'health.head_failed'][-1][1]['stage'] == 'version'


def test_g3_one_write_transaction_per_head_per_health_cycle(tmp_path):
    gateway, _ = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4', 'head-192-0-2-5'])
    labels = lambda: {k: v['count'] for k, v in gateway.broker.ledger_stats()['labels'].items()}
    before = labels(); gateway.refresh_health(); after = labels()
    assert after.get('health', 0) - before.get('health', 0) == 3
    assert after.get('record_inventory', 0) == before.get('record_inventory', 0) and after.get('record_runtime', 0) == before.get('record_runtime', 0)


def test_a_background_result_counts_as_collected_only_when_the_caller_reads_it(tmp_path):
    """Found while verifying 0.4.6: the background execution's own final read marked every /v1/submit result as read,
    so a graceful stop never waited for its caller and request.unclaimed never fired for submitted requests."""
    gateway, _ = fp.lab(tmp_path, ['head-192-0-2-3'])
    rid = gateway.execute('capstone', 't-capstone', fp.body('async'), asynchronous=True)['request']['request_id']
    deadline = time.time() + 10
    while gateway.result('capstone', 't-capstone', rid, claim=False)['status'] != 'completed' and time.time() < deadline:
        time.sleep(.05)
    assert gateway.unread_results() == 1, 'finished, not yet collected by its caller'
    assert [u['request_id'] for u in gateway.sweep_unclaimed(now=time.time() + 121)] == [rid]
    rid2 = gateway.execute('capstone', 't-capstone', fp.body('async-2'), asynchronous=True)['request']['request_id']
    while gateway.result('capstone', 't-capstone', rid2, claim=False)['status'] != 'completed' and time.time() < deadline:
        time.sleep(.05)
    gateway.result('capstone', 't-capstone', rid2)
    assert gateway.unread_results() == 0


# ---------------------------------------------------------------- G4

def test_g4_a_500_keeps_its_body_shape_and_class_and_the_application_sees_its_own_message(tmp_path):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4'])
    def failing(url, operation, payload, timeout):
        if operation == '/api/generate' and payload.get('prompt') != 'Return exactly the word READY.' and url == fp.endpoint('head-192-0-2-3'):
            body = json.dumps({'error': 'llama runner process has terminated: GGML_ASSERT failed; token=t-capstone'}).encode()
            raise urllib.error.HTTPError(url + operation, 500, 'Internal Server Error', {}, io.BytesIO(body))
        return fp.Lab.__call__(transport, url, operation, payload, timeout)
    gateway.transport = failing
    body = fp.body('k500', nodes=['head-192-0-2-3'], prompt='structured')
    body['payload'].update(format={'type': 'object', 'properties': {'reply': {'type': 'string'}}, 'required': ['reply'], 'additionalProperties': False},
                           options={'num_ctx': 1024, 'num_predict': 320, 'temperature': 0, 'seed': 7})
    out = gateway.execute('capstone', 't-capstone', body)
    assert out['status'] == 'failed' and out['error'] == 'HTTP Error 500: Internal Server Error', 'the 0.4.5 error string is unchanged'
    [error] = out['upstream_errors']
    assert error['status'] == 500 and error['failure_class'] == 'model_on_head' and error['resource_id'] == 'head-192-0-2-3'
    assert 'GGML_ASSERT' in error['message'] and 't-capstone' not in error['message'] and error['digest'] == 'DIGEST-m:1'
    assert error['runtime_version'] == '0.40.13'
    assert [s['scope'] for s in fp.suspensions(gateway)] == ['resource_model'], 'the class and its action are unchanged'
    rows = capacity_lifecycle.upstream_errors(tmp_path / 'ledger.db', request_id=out['request']['request_id'])['errors']
    assert rows[0]['shape']['format'] == 'schema' and rows[0]['shape']['format_properties'] == ['reply'] and rows[0]['shape']['options']['num_predict'] == 320
    assert 'structured' not in json.dumps(rows[0]['shape']), 'no prompt content in the shape'
    observed = snapshot(tmp_path / 'ledger.db')
    assert observed['last_hour']['upstream_http_errors'][0]['status'] == 500
    assert 'GGML_ASSERT' not in json.dumps(observed), 'bodies never reach the observation file'


def test_g4_a_400_stays_a_request_fault_and_an_undecodable_or_missing_body_is_bounded(tmp_path):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    transport.behaviour[(None, 'm:1')] = '400'
    out = fp.run(gateway, 'k400')
    assert out['status'] == 'failed' and out['upstream_errors'][0]['failure_class'] == 'request_fault' and fp.suspensions(gateway) == []
    raw = urllib.error.HTTPError('u', 500, 'x', {}, io.BytesIO(b'\xff\xfe' + b'A' * 10000))
    text, truncated, bad = http_error_body(raw)
    assert truncated and len(text.encode('utf-8', 'replace')) <= 4096 + 8 and bad >= 1
    assert http_error_body(raw) == (text, truncated, bad), 'read once, kept on the exception'
    assert recovery.classify(raw, 'dispatch') == recovery.MODEL_ON_HEAD
    assert http_error_body(urllib.error.HTTPError('u', 502, 'x', {}, None))[0] == ''
    cleaned = redact('Authorization: Bearer abc.def-123 token=xyz789 password: hunter2 known-secret-value\x00', ('known-secret-value',))
    assert not any(s in cleaned for s in ('abc.def-123', 'xyz789', 'hunter2', 'known-secret-value', '\x00')), cleaned


def test_g4_the_real_upstream_path_reads_the_body_once_and_reraises_the_same_httperror(tmp_path):
    class Head(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            data = b'{"error":"failed to load model vocabulary"}'
            self.send_response(500); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Head); threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        gateway = Gateway(tmp_path / 'ledger.db')
        with pytest.raises(urllib.error.HTTPError) as caught:
            gateway.upstream('http://127.0.0.1:%d' % server.server_port, '/api/chat', {'model': 'x'}, 5)
        assert caught.value.code == 500 and 'vocabulary' in caught.value.gateway_error_body[0]
        assert recovery.classify(caught.value, 'dispatch') == recovery.MODEL_ON_HEAD
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.parametrize('options', [{'temperature': 'hot'}, {'seed': 7.5}, {'top_k': True}, {'top_p': float('nan')}, {'stop': 'END'}, {'stop': [1]}])
def test_g4_a_wrongly_typed_option_is_refused_before_dispatch_and_suspends_nothing(tmp_path, options):
    """Ollama 0.40.16 answers 'option "temperature" must be of type float32' with HTTP 500 (verification/live-ollama-0.4.6.json):
    sent, it would suspend a healthy model on the head. It is a request fault, refused before any request exists."""
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    body = fp.body('typed'); body['payload']['options'] = {'num_predict': 8, **options}
    with pytest.raises(ValueError):
        gateway.execute('capstone', 't-capstone', body)
    assert fp.suspensions(gateway) == [] and transport.generations() == []
    good = fp.body('typed-ok'); good['payload']['options'] = {'num_predict': 8, 'temperature': 0, 'seed': 7, 'top_p': .9, 'top_k': 40, 'stop': ['END']}
    assert gateway.execute('capstone', 't-capstone', good)['status'] == 'completed'


# ---- review of 2026-11-19: a malformed `tools` entry quarantined a healthy head (HIGH), and the LOW items

MALFORMED_TOOLS = [[{'type': 'function', 'function': 'record_value'}], [{'type': 'function', 'function': 7}], [{'type': 'function', 'function': ['x']}],
                   ['record_value'], [{'type': 'function', 'function': {'name': 5}}], [{'type': 'other', 'function': {'name': 'x'}}],
                   {'name': 'x'}, [{'type': 'function', 'function': {'name': 'x', 'parameters': 'object'}}]]


def assert_settled_as_request_fault(gateway, request_id, rid='head-192-0-2-3'):
    with gateway.broker.connect() as db:
        row = dict(db.execute('SELECT status,executing FROM capacity_requests WHERE request_id=?', (request_id,)).fetchone())
        state = db.execute('SELECT state FROM capacity_invocations WHERE request_id=?', (request_id,)).fetchone()[0]
        attempts = db.execute('SELECT failure_class FROM capacity_request_attempts WHERE request_id=?', (request_id,)).fetchall()
    assert row['executing'] == 0 and row['status'] in ('cancelled', 'completed') and state == 'failed'
    assert [a[0] for a in attempts] == ['request_fault']
    # Even with every lease forced past its time, nothing is held: the head stays in service.
    with gateway.broker.write('test') as db:
        db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?', (time.time() - 1, request_id))
    gateway.broker.reconcile_expired()
    assert fp.health(gateway, rid) == 'healthy' and fp.suspensions(gateway) == []


@pytest.mark.parametrize('tools', MALFORMED_TOOLS)
def test_review_malformed_tools_are_refused_before_any_request_exists(tmp_path, tools):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    body = fp.body('bad-tools', operation='/api/chat'); body['payload'] = {'messages': [{'role': 'user', 'content': 'x'}], 'tools': tools}
    with pytest.raises(ValueError):
        gateway.execute('capstone', 't-capstone', body)
    with gateway.broker.connect() as db:
        assert db.execute("SELECT count(*) FROM capacity_requests WHERE client_key='bad-tools'").fetchone()[0] == 0
    assert transport.generations() == [] and fp.health(gateway, 'head-192-0-2-3') == 'healthy'


def test_review_the_reported_scenario_cannot_hold_a_head_even_if_malformed_tools_reach_the_head(tmp_path, monkeypatch):
    """The reviewer's repro (validate() of the first 0.4.6 build let the tool through; the head answered 400; the
    shape capture raised; the lease stayed executing; its expiry quarantined the head). Here validate() is made
    lenient again so the malformed tool reaches the capture path: the failure must still settle as a request fault."""
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    transport.behaviour[(None, 'm:1')] = '400'
    strict = Gateway.validate
    def lenient(body):
        tools = body['payload'].pop('tools', None)
        operation, payload, canonical = strict(body)
        payload['tools'] = tools
        return operation, payload, canonical
    monkeypatch.setattr(Gateway, 'validate', staticmethod(lenient))
    body = fp.body('ktools', operation='/api/chat'); body['payload'] = {'messages': [{'role': 'user', 'content': 'x'}], 'tools': MALFORMED_TOOLS[0]}
    out = gateway.execute('capstone', 't-capstone', body)
    assert out['status'] == 'failed' and out['upstream_errors'][0]['status'] == 400
    rows = capacity_lifecycle.upstream_errors(tmp_path / 'ledger.db', request_id=out['request']['request_id'])['errors']
    assert rows[0]['shape']['tool_names'] == ['<str>'], 'the shape says what was malformed'
    assert_settled_as_request_fault(gateway, out['request']['request_id'])


@pytest.mark.parametrize('payload', [None, [], 'x', {'messages': 'x'}, {'messages': [None, 3, {'role': 7, 'content': ['a']}]},
                                     {'tools': [None, {'function': None}, {'function': {'name': None}}]}, {'options': {'stop': 'x', 'num_ctx': 'y'}},
                                     {'format': {'properties': ['a']}}, {'format': {'properties': {1: 2}}}, {'images': 'x'}, {'prompt': 5}])
def test_review_request_shape_never_raises(payload):
    shape = request_shape('/api/chat', payload)
    assert isinstance(shape, dict) and json.dumps(shape, default=str)


def test_review_a_failing_capture_or_record_never_stops_the_settlement(tmp_path, monkeypatch):
    from inference_gateway import capacity_gateway as module
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    transport.behaviour[(None, 'm:1')] = '400'
    monkeypatch.setattr(module, 'request_shape', lambda *a: (_ for _ in ()).throw(RuntimeError('capture bug')))
    out = fp.run(gateway, 'capture-bug')
    assert out['status'] == 'failed' and gateway.counters['upstream_capture_failures'] == 1
    assert_settled_as_request_fault(gateway, out['request']['request_id'])
    monkeypatch.undo()
    # The diagnostic insert fails (table gone): its savepoint is rolled back, the settlement and the class's action stay.
    (tmp_path / 'b').mkdir()
    gateway2, transport2 = fp.lab(tmp_path / 'b', ['head-192-0-2-3', 'head-192-0-2-4'])
    transport2.behaviour[(fp.endpoint('head-192-0-2-3'), 'm:1')] = '500'
    with gateway2.broker.write('test') as db:
        db.execute('DROP TABLE capacity_upstream_errors')
    out = fp.run(gateway2, 'record-bug', nodes=['head-192-0-2-3'])
    assert out['status'] == 'failed' and gateway2.counters['upstream_capture_failures'] == 1
    assert [s['scope'] for s in fp.suspensions(gateway2)] == ['resource_model'], 'the 5xx action still applied'
    with gateway2.broker.connect() as db:
        assert db.execute('SELECT executing FROM capacity_requests WHERE request_id=?', (out['request']['request_id'],)).fetchone()[0] == 0


def test_review_if_settle_itself_raises_the_fallback_settles_without_holding_the_head(tmp_path, monkeypatch):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    transport.behaviour[(None, 'm:1')] = '400'
    monkeypatch.setattr(gateway, '_settle', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('unexpected')))
    out = fp.run(gateway, 'fallback')
    assert out['status'] == 'failed' and gateway.counters['settle_fallbacks'] == 1
    assert_settled_as_request_fault(gateway, out['request']['request_id'])


def test_rereview_a_fallback_after_a_recorded_failure_writes_no_second_failed_event(tmp_path, monkeypatch):
    """_settle recorded the failure, then raised in finish: the fallback must not count the failure twice."""
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    transport.behaviour[(None, 'm:1')] = '400'
    real = gateway.broker.finish
    def finish(*a, **k):
        if k.get('cancel') and k.get('execution_owner'):raise RuntimeError('finish failed after the failure was recorded')
        return real(*a, **k)
    monkeypatch.setattr(gateway.broker, 'finish', finish)
    out = fp.run(gateway, 'twice')
    rid = out['request']['request_id']
    assert out['status'] == 'failed' and gateway.counters['settle_fallbacks'] == 1
    assert [e for e, _ in fp.events(gateway, rid)].count('invocation.failed') == 1
    assert_settled_as_request_fault(gateway, rid)


def test_rereview_an_uncertain_failure_in_the_fallback_holds_the_head_at_once_like_the_normal_path(tmp_path, monkeypatch):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    with gateway.broker.write('test') as db:
        recovery.store_policy(db, 'failure_policy', {'ps_settle_seconds': 0, 'deadline_liveness_seconds': 0})
    transport.behaviour[(None, 'm:1')] = 'timeout' # a raw TimeoutError after the request was sent: uncertain execution
    monkeypatch.setattr(gateway, '_settle', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('unexpected')))
    out = fp.run(gateway, 'uncertain')
    assert gateway.counters['settle_fallbacks'] == 1
    holds = [(s['scope'], s['state'], s['request_id']) for s in fp.suspensions(gateway)]
    assert holds == [('resource', 'held', out['request']['request_id'])], 'held now, not only when the lease expires'
    assert fp.health(gateway, 'head-192-0-2-3') == 'quarantined'
    with gateway.broker.connect() as db:
        row = db.execute('SELECT status,executing FROM capacity_requests WHERE request_id=?', (out['request']['request_id'],)).fetchone()
    assert (row[0], row[1]) == ('cancel_requested', 1), 'still executing as far as the Gateway knows: never replayed'


def test_rereview_a_failed_retention_prune_appears_in_the_health_cycle_summary(tmp_path, monkeypatch):
    gateway, _ = fp.lab(tmp_path, ['head-192-0-2-3'])
    monkeypatch.setattr(gateway, 'prune_upstream_errors', lambda **k: (_ for _ in ()).throw(LedgerBusy('upstream_error_retention', 30.0, 'external')))
    cycle = gateway.refresh_health()
    assert cycle['local_failures'] == ['upstream_error_retention'] and fp.health(gateway, 'head-192-0-2-3') == 'healthy'


def ledger_rows(gateway):
    with gateway.broker.connect() as db:
        return {'resources': [dict(r) for r in db.execute('SELECT * FROM capacity_resources ORDER BY resource_id')],
                'capacity': [dict(r) for r in db.execute('SELECT resource_id,slots,memory_gb FROM capacity_resource_capacity ORDER BY resource_id')],
                'model_capacity': [dict(r) for r in db.execute('SELECT * FROM capacity_model_capacity ORDER BY resource_id,model,source')],
                'policy': [dict(r) for r in db.execute("SELECT * FROM capacity_service_policy WHERE key IN ('failure_policy','placement_policy','admission_open')")],
                'clients': [dict(r) for r in db.execute('SELECT * FROM capacity_clients ORDER BY app_id')],
                'limits': [dict(r) for r in db.execute('SELECT * FROM capacity_client_limits ORDER BY app_id')],
                'qualifications': [dict(r) for r in db.execute('SELECT * FROM capacity_qualification_evidence ORDER BY evidence_id')],
                'suspensions': [dict(r) for r in db.execute('SELECT * FROM capacity_suspensions ORDER BY suspension_id')],
                'inventory': [dict(r) for r in db.execute('SELECT * FROM capacity_inventory ORDER BY resource_id')]}


def test_temporary_identity_verbs_change_only_its_own_row_and_the_named_heads(tmp_path):
    from inference_gateway import credentials
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4', 'head-192-0-2-5'], models=('qwen3.5:4b', 'm:1'),
                                clients={'capstone-research': (16, 6), 'coder': (2, 5)})
    with gateway.broker.write('test') as db:  # coder only on head 5; the template (research) on 3 and 4
        db.execute("UPDATE capacity_resources SET applications=? WHERE resource_id='head-192-0-2-5'", (json.dumps(['coder']),))
        db.execute("UPDATE capacity_resources SET applications=? WHERE resource_id IN ('head-192-0-2-3','head-192-0-2-4')", (json.dumps(['capstone-research', 'coder']),))
        recovery.store_policy(db, 'placement_policy', {'unmeasured_model_limit': 1})
    before = ledger_rows(gateway); db_path = str(tmp_path / 'ledger.db')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1', 'clients': [
        {'app_id': 'lab-capacity-check', 'models': ['qwen3.5:4b'], 'max_concurrency': 24, 'queue_limit': 48, 'priority': 0,
         'operations': ['/api/chat', '/api/generate'], 'max_context': 8192}]}), encoding='utf-8')
    out = credentials.reissue(db_path, policy, 'lab-capacity-check', tmp_path / 'one.token')
    assert out['action'] == 'issued' and out['previous'] is None
    grant = credentials.grant_heads(db_path, 'lab-capacity-check', 'capstone-research')
    assert grant['granted'] == ['head-192-0-2-3', 'head-192-0-2-4'] and credentials.grant_heads(db_path, 'lab-capacity-check', 'capstone-research')['already'] == grant['granted']
    after = ledger_rows(gateway)
    for key in ('capacity', 'model_capacity', 'policy', 'qualifications', 'suspensions', 'inventory'):
        assert after[key] == before[key], key + ' unchanged'
    assert [c for c in after['clients'] if c['app_id'] != 'lab-capacity-check'] == before['clients'], 'other clients untouched'
    assert [r for r in after['limits'] if r['app_id'] != 'lab-capacity-check'] == before['limits']
    for old, new in zip(before['resources'], after['resources']):
        diff = {k for k in old if old[k] != new[k]}
        assert diff <= {'applications'} and (not diff or json.loads(new['applications']) == json.loads(old['applications']) + ['lab-capacity-check'])
    token = (tmp_path / 'one.token').read_text().strip()
    body = fp.body('lab', model='qwen3.5:4b'); assert gateway.execute('lab-capacity-check', token, body)['status'] == 'completed'
    credentials.revoke(db_path, 'lab-capacity-check')
    assert credentials.withdraw_heads(db_path, 'lab-capacity-check')['withdrawn'] == ['head-192-0-2-3', 'head-192-0-2-4']
    final = ledger_rows(gateway)
    assert final['resources'] == before['resources'], 'heads exactly as before'
    for key in ('capacity', 'model_capacity', 'policy', 'qualifications', 'suspensions', 'inventory'):
        assert final[key] == before[key], key + ' unchanged after revoke and withdraw'
    [row] = [c for c in final['clients'] if c['app_id'] == 'lab-capacity-check']
    assert row['enabled'] == 0, 'revoking leaves a disabled row (history)'
    with pytest.raises(PermissionError):
        gateway.execute('lab-capacity-check', token, fp.body('after'))
    again = credentials.reissue(db_path, policy, 'lab-capacity-check', tmp_path / 'two.token')
    assert again['action'] == 'reissued' and again['previous'] == 'disabled'
    with pytest.raises(PermissionError):
        gateway.broker.authenticate('lab-capacity-check', token)  # the old token no longer works
    # Refusals: grant never widens beyond the template; an enabled application with work cannot be withdrawn.
    with gateway.broker.write('test') as db:
        db.execute("UPDATE capacity_resources SET applications=? WHERE resource_id='head-192-0-2-5'", (json.dumps(['coder', 'lab-capacity-check']),))
    with pytest.raises(ValueError):
        credentials.grant_heads(db_path, 'lab-capacity-check', 'capstone-research')
    with pytest.raises(PermissionError):
        credentials.grant_heads(db_path, 'nobody', 'capstone-research')


def guard_lab(tmp_path):
    from inference_gateway import credentials
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4'], models=('qwen3.5:4b',),
                                clients={'capstone': (8, 6), 'capstone-research': (16, 6), 'capstone-researchx': (2, 5), 'lab-traffic': (4, 1)})
    def policy(entries, **extra):
        f = tmp_path / ('policy-%d.json' % len(list(tmp_path.glob('policy-*.json'))))
        f.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1', 'clients': entries, **extra}), encoding='utf-8'); return f
    entry = lambda app: {'app_id': app, 'models': ['qwen3.5:4b'], 'max_concurrency': 24, 'queue_limit': 24, 'priority': 0, 'operations': ['/api/chat', '/api/generate'], 'max_context': 8192}
    return gateway, credentials, str(tmp_path / 'ledger.db'), policy, entry


@pytest.mark.parametrize('app', ['capstone', 'capstone-research', 'capstone-researchx', 'lab-traffic', 'Lab-capacity-check', 'lab_capacity', 'xlab-capacity', 'lab-'])
def test_round3_the_temporary_verbs_refuse_every_real_or_lookalike_application_and_change_nothing(tmp_path, app):
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    tokens = {a: gateway.broker.authenticate(a, 't-' + a) for a in ('capstone', 'capstone-research', 'capstone-researchx', 'lab-traffic')}
    before = ledger_rows(gateway)
    for call in (lambda: credentials.reissue(db, policy([entry(app)]), app, tmp_path / 'x.token'),
                 lambda: credentials.grant_heads(db, app, 'capstone'),
                 lambda: credentials.withdraw_heads(db, app)):
        with pytest.raises(PermissionError) as refused:
            call()
        assert 'temporary' in str(refused.value)
    assert ledger_rows(gateway) == before, 'nothing changed'
    for a in tokens:
        gateway.broker.authenticate(a, 't-' + a)  # every real token still works
    assert not (tmp_path / 'x.token').exists()


def test_round3_reissue_refuses_the_infrastructure_policy_and_any_file_with_more_than_its_own_entry(tmp_path):
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    before = ledger_rows(gateway)
    for bad in (policy([entry('lab-capacity-check'), entry('capstone')]),                        # declares a real application too
                policy([entry('lab-capacity-check')], resources=[{'resource_id': 'head-192-0-2-3'}]),  # an infrastructure policy
                policy([{**entry('lab-capacity-check'), 'credential': 'provisioned'}]),
                policy([entry('lab-other')])):
        with pytest.raises(PermissionError):
            credentials.reissue(db, bad, 'lab-capacity-check', tmp_path / 'x.token')
    assert ledger_rows(gateway) == before


def test_round3_a_temporary_identity_expires_and_the_health_cycle_revokes_and_withdraws_it(tmp_path):
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    out = credentials.reissue(db, policy([entry('lab-capacity-check')]), 'lab-capacity-check', tmp_path / 't.token', expires_in=120)
    assert out['expires_at'] > time.time() + 100
    credentials.grant_heads(db, 'lab-capacity-check', 'capstone-research')
    token = (tmp_path / 't.token').read_text().strip()
    gateway.broker.authenticate('lab-capacity-check', token)
    with pytest.raises(ValueError):
        credentials.reissue(db, policy([entry('lab-capacity-check')]), 'lab-capacity-check', tmp_path / 'u.token', expires_in=10 ** 6)
    with gateway.broker.write('test') as db_:  # the run was killed and its expiry has passed
        gateway.broker.mark_temporary(db_, 'lab-capacity-check', expires_at=time.time() - 1, by='test')
    with pytest.raises(PermissionError, match='expired'):
        gateway.broker.authenticate('lab-capacity-check', token)
    gateway.refresh_health()
    with gateway.broker.connect() as d:
        assert d.execute("SELECT enabled FROM capacity_clients WHERE app_id='lab-capacity-check'").fetchone()[0] == 0
        assert all('lab-capacity-check' not in json.loads(r[0]) for r in d.execute('SELECT applications FROM capacity_resources'))
    assert 'temporary_client.expired' in [e for e, _ in fp.events(gateway)]


def test_round3_a_temporary_identity_never_outranks_a_real_application_however_long_it_waits(tmp_path):
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    credentials.reissue(db, policy([entry('lab-capacity-check')]), 'lab-capacity-check', tmp_path / 't.token')
    credentials.grant_heads(db, 'lab-capacity-check', 'capstone-research')
    token = (tmp_path / 't.token').read_text().strip(); b = gateway.broker; deadline = time.time() + 600
    with b.write('test') as d:  # both heads full
        for i, rid in enumerate(('head-192-0-2-3', 'head-192-0-2-4')):
            d.execute("INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing,node_policy) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ('REQ-busy%d' % i, 'capstone', 'busy%d' % i, 'qwen3.5:4b', '[]', 'leased', time.time(), deadline, 'L%d' % i, rid, deadline, 1, 'authorized_dynamic'))
    heads = ['head-192-0-2-3', 'head-192-0-2-4']
    lab = b.submit('lab-capacity-check', token, 'lab-old', 'qwen3.5:4b', heads, deadline)['request_id']
    with b.write('test') as d:
        d.execute('UPDATE capacity_requests SET created=? WHERE request_id=?', (time.time() - 600, lab))  # waited 10 minutes: +120 if it aged
    real = b.submit('capstone-researchx', 't-capstone-researchx', 'real-new', 'qwen3.5:4b', heads, deadline)['request_id']
    with b.write('test') as d:
        d.execute("UPDATE capacity_requests SET status='completed',executing=0 WHERE request_id='REQ-busy0'")  # one slot frees
    assert b.place() == [real], 'the real application (priority 5, new) goes first; the temporary one waits'


def temporary_names(gateway):
    return sorted(gateway.broker.temporary_clients())


def test_round4_a_failed_reissue_leaves_no_mark_and_changes_nothing(tmp_path):
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    existing = tmp_path / 'exists.token'; existing.write_text('x', encoding='utf-8')
    before = ledger_rows(gateway)
    with pytest.raises(FileExistsError):
        credentials.reissue(db, policy([entry('lab-new')]), 'lab-new', existing)
    assert temporary_names(gateway) == [] and ledger_rows(gateway) == before and existing.read_text() == 'x'
    # The reviewer's sequence: the failed reissue, then an ordinary issue of the same name, then an old expiry: nothing.
    with gateway.broker.write('test') as d:
        gateway.broker._store_temporary(d, {})
    out = credentials.issue(db, policy([entry('lab-new')]), 'lab-new', tmp_path / 'real.token')
    assert out['action'] == 'issued' and temporary_names(gateway) == []
    assert gateway.broker.expire_temporary_clients(now=time.time() + 10 ** 6) == []


def test_round4_an_ordinary_issue_rotate_or_bootstrap_clears_a_stale_mark(tmp_path):
    from inference_gateway import capacity_bootstrap
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    b = gateway.broker
    for app in ('lab-one', 'lab-two', 'lab-traffic'):
        with b.write('test') as d:  # a stale mark from an older build (or a crash between steps)
            b.mark_temporary(d, app, expires_at=time.time() - 5, by='stale')
    credentials.issue(db, policy([entry('lab-one')]), 'lab-one', tmp_path / 'one.token')                     # credentials issue
    credentials.rotate(db, 'lab-traffic', tmp_path / 'traffic.token')                                     # credentials rotate
    boot = tmp_path / 'boot.json'
    boot.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1',
                                'clients': [{**entry('lab-two'), 'token_environment': 'LAB_TWO_TOKEN'}]}), encoding='utf-8')
    import os
    os.environ['LAB_TWO_TOKEN'] = 'x' * 40
    try:capacity_bootstrap.apply(boot, db)                                                                 # bootstrap
    finally:del os.environ['LAB_TWO_TOKEN']
    assert temporary_names(gateway) == []
    assert gateway.broker.expire_temporary_clients(now=time.time() + 10 ** 6) == []
    with b.connect() as d:
        assert {r[0]: r[1] for r in d.execute("SELECT app_id,enabled FROM capacity_clients WHERE app_id IN ('lab-one','lab-two','lab-traffic')")} == \
               {'lab-one': 1, 'lab-two': 1, 'lab-traffic': 1}, 'none revoked by an old expiry'
    # And they are real applications now: the temporary verbs refuse them.
    with pytest.raises(PermissionError):
        credentials.withdraw_heads(db, 'lab-one')


def test_round4_an_expired_then_reissued_identity_is_not_revoked_by_the_old_expiry(tmp_path):
    gateway, credentials, db, policy, entry = guard_lab(tmp_path)
    b = gateway.broker; old_now = time.time() + 200
    credentials.reissue(db, policy([entry('lab-capacity-check')]), 'lab-capacity-check', tmp_path / 'a.token', expires_in=100)
    # The health cycle saw it due at old_now and is about to revoke ... but the tool reissues it first.
    credentials.reissue(db, policy([entry('lab-capacity-check')]), 'lab-capacity-check', tmp_path / 'b.token', expires_in=3600)
    assert b.expire_temporary_clients(now=old_now) == [], 'the fresh expiry is checked inside the write'
    token = (tmp_path / 'b.token').read_text().strip(); b.authenticate('lab-capacity-check', token)
    # When it really is past its expiry it is cleaned once, kept as temporary (so the tool can reissue it), never twice.
    later = time.time() + 7200
    assert b.expire_temporary_clients(now=later) == ['lab-capacity-check']
    assert b.expire_temporary_clients(now=later + 1) == []
    assert temporary_names(gateway) == ['lab-capacity-check'] and b.temporary_clients()['lab-capacity-check']['cleaned_at']
    again = credentials.reissue(db, policy([entry('lab-capacity-check')]), 'lab-capacity-check', tmp_path / 'c.token', expires_in=600)
    assert again['previous'] == 'disabled' and 'cleaned_at' not in b.temporary_clients()['lab-capacity-check']


def test_review_whole_number_floats_are_accepted_for_integer_options(tmp_path):
    gateway, _ = fp.lab(tmp_path, ['head-192-0-2-3'])
    body = fp.body('floats'); body['payload']['options'] = {'num_predict': 8, 'seed': 7.0, 'top_k': 40.0, 'temperature': 0}
    assert gateway.execute('capstone', 't-capstone', body)['status'] == 'completed'
    for bad in ({'seed': 7.5}, {'top_k': True}, {'seed': float('inf')}):
        b = fp.body('bad-%s' % list(bad)[0] + str(list(bad.values())[0])); b['payload']['options'] = {'num_predict': 8, **bad}
        with pytest.raises(ValueError):
            gateway.execute('capstone', 't-capstone', b)


def test_review_upstream_error_rows_are_bounded_by_age_and_count(tmp_path):
    gateway, _ = fp.lab(tmp_path, ['head-192-0-2-3'])
    now = time.time()
    with gateway.broker.write('test') as db:
        db.executemany('INSERT INTO capacity_upstream_errors(request_id,app_id,at) VALUES(?,?,?)',
                       [('old-%d' % i, 'capstone', now - 30 * 86400) for i in range(5)] + [('new-%d' % i, 'capstone', now - i) for i in range(30)])
    gateway.UPSTREAM_ERROR_ROWS = 20
    assert gateway.prune_upstream_errors(now=now, force=True) == {'aged': 5, 'excess': 10}
    with gateway.broker.connect() as db:
        kept = [r[0] for r in db.execute('SELECT request_id FROM capacity_upstream_errors ORDER BY error_id')]
    assert len(kept) == 20 and kept[0] == 'new-10' and kept[-1] == 'new-29', 'the newest rows are kept'
    assert gateway.prune_upstream_errors(now=now) is None, 'at most every UPSTREAM_PRUNE_SECONDS'


def test_review_an_http_error_during_checks_is_recorded_under_the_call_that_failed(tmp_path):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3', 'head-192-0-2-4'])
    def show_fails(url, operation, payload, timeout):
        if operation == '/api/show' and url == fp.endpoint('head-192-0-2-3'):
            raise urllib.error.HTTPError(url + operation, 500, 'x', {}, io.BytesIO(b'{"error":"show failed"}'))
        return fp.Lab.__call__(transport, url, operation, payload, timeout)
    gateway.transport = show_fails
    out = fp.run(gateway, 'show', nodes=['head-192-0-2-3'])
    [row] = capacity_lifecycle.upstream_errors(tmp_path / 'ledger.db', request_id=out['request']['request_id'])['errors']
    assert row['operation'] == '/api/show' and row['phase'] == 'checks' and row['shape']['operation'] == '/api/generate'


def test_review_fleet_cached_keeps_a_snapshot_only_for_the_rest_of_its_validity(monkeypatch):
    client = Client('http://127.0.0.1:1', 'capstone', 't')
    reply = {'snapshot': {'generated_at': 1000.0, 'served_at': 1004.0, 'valid_until': 1005.0, 'max_age_seconds': 5}}
    monkeypatch.setattr(Client, 'fleet', lambda self, *a, **k: reply)
    before = time.monotonic(); client.fleet_cached(['m'])
    [(until, _)] = client._fleet_cache.values()
    assert until - before <= 1.05, 'served 4 s into its 5 s: reused here for about 1 s, not 5'


def test_g4_request_shape_has_no_content():
    shape = request_shape('/api/chat', {'messages': [{'role': 'system', 'content': 'SECRET-SYSTEM'}, {'role': 'user', 'content': 'SECRET-USER'}],
                                        'format': {'type': 'object', 'properties': {'reply': {'type': 'string'}}},
                                        'tools': [{'type': 'function', 'function': {'name': 'lookup'}}], 'options': {'num_ctx': 8192, 'stop': ['SECRET-STOP']},
                                        'model': 'm', 'keep_alive': '2m', 'stream': False})
    text = json.dumps(shape)
    assert 'SECRET' not in text and shape['roles'] == {'system': 1, 'user': 1} and shape['message_chars'] == 24
    assert shape['tools'] == 1 and shape['tool_names'] == ['lookup'] and shape['options'] == {'num_ctx': 8192, 'stop_count': 1}
    assert 'model' not in shape['fields'] and shape['format'] == 'schema'


# ---------------------------------------------------------------- Phase 4: structured_chat

def test_structured_chat_is_qualified_on_its_own_shape_and_named_in_provenance(tmp_path):
    gateway, transport = fp.lab(tmp_path, ['head-192-0-2-3'])
    replies = {'ok': '{"reply": "READY"}'}
    def head(url, operation, payload, timeout):
        if operation == '/api/chat':
            return {'done': True, 'model': payload['model'], 'message': {'role': 'assistant', 'content': replies['ok']}}
        return fp.Lab.__call__(transport, url, operation, payload, timeout)
    gateway.transport = head
    evidence = gateway.qualify('head-192-0-2-3', 'm:1', operator='ops', purpose='structured_chat')
    assert evidence['passed'] and evidence['fixture']['id'] == 'structured_chat/1'
    [resource] = gateway.models('capstone', 't-capstone')['resources']
    assert 'structured_chat' in resource['model_details']['m:1']['qualified_capabilities']
    body = fp.body('sc', operation='/api/chat'); body['payload'] = {'messages': [{'role': 'user', 'content': 'x'}], 'format': {'type': 'object'}}
    body['required_capabilities'] = ['structured_chat']
    out = gateway.execute('capstone', 't-capstone', body)
    assert out['status'] == 'completed'
    assert out['result']['provenance']['capabilities_verified'] == ['structured_chat']
    replies['ok'] = '{"reply": "READY", "extra": 1}'
    assert gateway.qualify('head-192-0-2-3', 'm:1', operator='ops', purpose='structured_chat')['passed'] is False


# ---------------------------------------------------------------- HTTP and SDK 0.4.2

@pytest.fixture
def http(tmp_path):
    gateway, transport = fleet_lab(tmp_path)
    server = Server(('127.0.0.1', 0), gateway); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield gateway, Client('http://127.0.0.1:%d' % server.server_port, 'capstone', 't-capstone')
    server.shutdown(); server.server_close(); thread.join(5)


def test_sdk_0_4_2_policy_fleet_and_gateway_info_over_http(http):
    gateway, client = http
    assert shared_inference_client.__version__ == '0.4.2'
    info = client.gateway_info()
    assert info['release'] == '0.4.6' and 'fleet_snapshot' in info['features']
    cheap = client.policy(availability=False)
    assert cheap['availability_included'] is False and 'operations' not in cheap['models'][0]
    full = client.policy()
    assert full['models'][0]['operations']['/api/generate'] == 'available'
    fleet = client.fleet(['m:1'], operations=['/api/chat'], capabilities=['tools'])
    assert fleet['request'] == {'models': ['m:1'], 'operations': ['/api/chat'], 'num_ctx': None, 'capabilities': ['tools']}
    assert fleet['models'][0]['available']['status']['/api/chat'] == 'capability_not_qualified'
    again = client.fleet_cached(['m:1']); cached = client.fleet_cached(['m:1'])
    assert cached is again, 'reused in this process until valid_until'
    with pytest.raises(urllib.error.HTTPError) as bad:
        client.request('/v1/fleet?bogus=1')
    assert bad.value.code == 400
    time.sleep(.3) # the handler counts the route after its answer is sent
    routes = gateway.route_stats()
    assert routes['GET /v1/fleet']['count'] >= 2 and routes['GET /v1/policy']['count'] == 2 and routes['GET /health']['count'] == 1


def test_sdk_0_4_2_falls_back_on_an_older_gateway():
    class Old(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path == '/v1/policy':
                data = json.dumps({'contract': 'shared.inference/1', 'policy': {}, 'models': [{'model': 'm', 'operations': {}}]}).encode(); code = 200
            elif self.path == '/health':
                data = json.dumps({'ok': True, 'contract': 'shared.inference/1', 'service_id': 'shared-inference'}).encode(); code = 200
            else:
                data = b'{"error":"Unknown shared-inference route"}'; code = 404
            self.send_response(code); self.send_header('X-Inference-Service-ID', 'shared-inference'); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Old); threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Client('http://127.0.0.1:%d' % server.server_port, 'capstone', 't')
        assert client.policy(availability=False)['models'][0]['operations'] == {}, 'the full 0.4.1 policy'
        assert client.gateway_info()['release'] is None
        with pytest.raises(GatewayFeatureUnavailable):
            client.fleet(['m'])
    finally:
        server.shutdown(); server.server_close()
