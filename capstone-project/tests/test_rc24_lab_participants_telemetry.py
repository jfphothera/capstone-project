"""6.0.0-rc.24: the Testing Lab participants that act during a run have their own
telemetry identities (Agent Arena figures).

Before rc.24 the simulated customer (actor lab-customer-simulator) and the
environment simulator (lab-environment-simulator) emitted no telemetry at all,
and the READY probe registered itself as "Testing Lab probe" with role
infrastructure_evaluation (registration not guarded). Now:

* each customer turn is one task: task.created -> task.started ->
  task.completed (replied / did not reply) or task.failed (reply not recorded);
* each carrier / receiving event is one task with the same lifecycle
  (task.failed when the application blocked it);
* the probe registers as "Testing Lab Probe" (role lab-probe) and its model
  call is bound to agent testing_lab_probe the way the reviewer's calls are.

Telemetry only. The byte-identity tests below hash the simulators' outputs for a
fixed input; the golden values were recorded from the unmodified rc.23 source.
"""
import copy
import hashlib
import json
import os
import random
import shutil
import sqlite3
from pathlib import Path

import pytest

from capstone_rag import agentic_customer, agentic_scenarios, experiment_catalogue, gateway_transport, telemetry
from capstone_rag.agentic_customer import (CUSTOMER_ACTOR, ENVIRONMENT_ACTOR, CustomerSimulator, EnvironmentSimulator,
                                           ParticipantTelemetry)
from capstone_rag.ai_settings import Registry
from capstone_rag.store import Store

RUN = 'RUN-RC24-0000000000000000000000000001'
HEAD = 'head-192-0-2-5'
LAB_IDS = {CUSTOMER_ACTOR, ENVIRONMENT_ACTOR, 'testing_lab_probe'}

# Recorded with the rc.23 source (scenario set agentic-2026-11-16.2) before any
# rc.24 change; the harness below is unchanged since.
# rc.25 (C6) intentionally changed the customer simulator: a turn that reports a
# fix no longer re-appends the original symptoms. Against the rc.23/rc.24
# simulator exactly 6 of the 160 scripted turns differ - the power-fault turn-1
# replies of six behaviours, all fixed-state turns, each losing only the
# contradictory "There are no lights at all ..." sentence. Environment and probe
# hashes are unchanged. rc.23 golden: b3e9619909071faa8ae14adf055963e901ffd9cb72c42075217f370f743f6d78
GOLDEN_CUSTOMER = '3a8472648f396981af3ef4dec4712dc38376bac7a1b75bbdf178854a7aa7a12b'
GOLDEN_ENVIRONMENT = '838f8529924db894b8301f43951734125c7885d687a7a82166415297f3a9ee21'
GOLDEN_PROBE_REQUEST = '627e9b61e4aeecdc18b57704f38cb2e148ace75948b7ed70a73de4bc173d1895'


# --- fixed-input harness ---------------------------------------------------------

CUSTOMER_SCRIPT = [
    ('Thank you for contacting us. Could you confirm your full name, the serial number and your date of birth?', {}),
    ('Thanks. Please check that the power cord is pushed firmly into the inlet and let me know what you observe. Are the lights on?', {}),
    ('Do you notice any smell, noise or an error code on the display? When did it start? Are you still using it?', {}),
    ('Which item was missing from the box? Has it arrived yet?', {}),
    ('Your case has been updated.', {}),
    ('We shipped the missing item. Has it arrived?', {'delivered_to_customer': True}),
    ('Is there anything else we can help with?', {'delivered_to_customer': True}),
    ('Is there anything else?', {'delivered_to_customer': True}),
]
ENVIRONMENT_POLICIES = [{'carrier': 'normal', 'original_returned': False}, {'carrier': 'delayed', 'original_returned': True},
                        {'carrier': 'lost', 'original_returned': False}, {'carrier': 'normal', 'original_returned': True}]


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def customer_transcript(scenarios, respond=None):
    """Every persona answers the same fixed messages; returns outputs and states."""
    out = []
    for key in scenarios.keys():
        persona = scenarios.build(key)['customer']
        sim, state = CustomerSimulator(copy.deepcopy(persona)), {}
        for i, (body, environment) in enumerate(CUSTOMER_SCRIPT):
            env = dict(environment)
            reply = (respond or (lambda s, b, st, e, i, k: s.respond(b, st, e)))(sim, body, state, env, i, key)
            out.append({'key': key, 'turn': i, 'reply': reply, 'state': copy.deepcopy(state), 'environment': env})
    return out


def environment_fixture(store, cid):
    store.insert('cases', {'case_id': cid, 'created_at': '2026-11-18T00:00:00', 'updated_at': '2026-11-18T00:00:00', 'status': 'Open'})
    rows = [('R-RC24-1', 'inbound', 'device_return'), ('R-RC24-2', 'outbound', 'part_outbound')]
    for i, (rma, direction, movement) in enumerate(rows):
        store.insert('rmas', {'rma_id': rma, 'case_id': cid, 'created_at': f'2026-11-18T00:00:0{i}', 'movement_type': movement,
                              'direction': direction, 'status': 'executed'})
        store.insert('sim_shipments', {'shipment_id': f'SHP-RC24-{i}', 'rma_id': rma, 'case_id': cid, 'created_at': f'2026-11-18T00:00:0{i}',
                                       'direction': direction, 'tracking_number': f'SIMFX00000002{i}', 'status': 'label_created'})


def environment_transcript(store_factory, step=None, cycles=6):
    """Each policy steps the environment over the same fixture; an event's effect
    is applied as a direct status change (not through the ERP) so only the
    simulator's own output is measured."""
    out = []
    for p, policy in enumerate(ENVIRONMENT_POLICIES):
        store, cid = store_factory(p), f'CASE-RC24-{p}'
        environment_fixture(store, cid)
        sim, state = EnvironmentSimulator(copy.deepcopy(policy)), {}
        for cycle in range(cycles):
            events = (step or (lambda s, st, c, sta, cy: s.step(st, None, c, sta)))(sim, store, cid, state, cycle)
            listed = [{k: v for k, v in e.items() if k != 'apply'} for e in events]
            out.append({'policy': p, 'cycle': cycle, 'events': listed, 'state': copy.deepcopy(state)})
            for e in listed:
                if e['kind'] == 'carrier':
                    store.execute('UPDATE sim_shipments SET status=? WHERE tracking_number=?', (e['to'], e['tracking_number']))
                else:
                    store.execute("UPDATE rmas SET received_at='2026-11-18T01:00:00' WHERE rma_id=?", (e['rma_id'],))
        store.close()
    return out


def _rows(st, agent_id=None):
    sql, args = 'SELECT * FROM arena_events', ()
    if agent_id:
        sql, args = sql + ' WHERE agent_id=?', (agent_id,)
    rows = st.query(sql + ' ORDER BY sequence', args)
    for row in rows:
        row['detail'] = json.loads(row['detail'])
    return rows


# --- 1. byte identity of the simulators ------------------------------------------

def test_simulator_outputs_match_the_rc23_golden_hashes_without_telemetry(tmp_path):
    before = random.getstate()
    assert agentic_scenarios.SCENARIO_SET_VERSION == 'agentic-2026-11-19.1', 'scenario set changed: re-record the golden hashes'
    assert canonical_hash(customer_transcript(agentic_scenarios)) == GOLDEN_CUSTOMER
    assert canonical_hash(environment_transcript(lambda p: Store(str(tmp_path / f'env-{p}.db')))) == GOLDEN_ENVIRONMENT
    assert random.getstate() == before


def test_simulator_outputs_are_byte_identical_with_participant_telemetry(tmp_path):
    """The coordinator's telemetry calls, interleaved exactly as in run_lifecycle,
    on a real store: same replies, events, participant state, RNG state."""
    st = Store(str(tmp_path / 'telemetry.db'))
    lab = ParticipantTelemetry(st, 'CASE-RC24', RUN)

    def respond(sim, body, state, env, i, key):
        task = 'customer:MSG-' + key.split(':', 1)[1].replace(':', '-') + f'-{i}'
        lab.customer_received(task, task[9:])
        reply = sim.respond(body, state, env)
        lab.customer_done(task, task[9:], 'applied' if reply['reply'] else 'no_reply', bool(reply['reply']))
        return reply

    def step(sim, store, cid, state, cycle):
        events = sim.step(store, None, cid, state)
        for e in events:
            listed = {k: v for k, v in e.items() if k != 'apply'}
            lab.environment_started(f'environment:{cycle}', listed)
            lab.environment_done(f'environment:{cycle}', listed, 'applied')
        return events

    before = random.getstate()
    assert canonical_hash(customer_transcript(agentic_scenarios, respond)) == GOLDEN_CUSTOMER
    assert canonical_hash(environment_transcript(lambda p: Store(str(tmp_path / f'env-{p}.db')), step)) == GOLDEN_ENVIRONMENT
    assert random.getstate() == before
    # ...and the telemetry really was written meanwhile.
    assert len(_rows(st, CUSTOMER_ACTOR)) == 1 + 3 * 20 * len(CUSTOMER_SCRIPT)          # one registration, three rows per turn
    assert len(_rows(st, ENVIRONMENT_ACTOR)) == 1 + 3 * 18
    assert 'inference' not in agentic_customer.__dict__                                  # no model calls added


# --- 2. participant rows ------------------------------------------------------------

def test_participant_rows_registration_and_fields(tmp_path):
    st = Store(str(tmp_path / 'operations.db'))
    lab = ParticipantTelemetry(st, 'CASE-RC24', RUN)
    lab.customer_received('customer:MSG-1', 'MSG-1')
    lab.customer_done('customer:MSG-1', 'MSG-1', 'applied', True)
    lab.customer_received('customer:MSG-2', 'MSG-2')
    lab.customer_done('customer:MSG-2', 'MSG-2', 'no_reply', False)
    carrier = {'kind': 'carrier', 'tracking_number': 'SIMFX0000000021', 'from': 'in_transit', 'to': 'delivered', 'direction': 'outbound'}
    key = 'environment:carrier:SIMFX0000000021:delivered'
    lab.environment_started(key, carrier);lab.environment_done(key, carrier, 'applied')
    receiving = {'kind': 'receiving', 'rma_id': 'R-RC24-1'}
    lab.environment_started('environment:receiving:R-RC24-1:received', receiving)
    lab.environment_done('environment:receiving:R-RC24-1:received', receiving, 'blocked')

    customer = _rows(st, CUSTOMER_ACTOR)
    assert [r['event_type'] for r in customer] == ['agent.registered', 'task.created', 'task.started', 'task.completed',
                                                  'task.created', 'task.started', 'task.completed']
    assert [r['summary'] for r in customer[3:7:3]] == ['Testing Lab customer replied', 'Testing Lab customer did not reply']
    for r in customer[1:]:
        assert (r['case_id'], r['workflow_id'], r['task_id'][:12]) == ('CASE-RC24', RUN, 'customer:MSG')
        assert r['correlation_id'] == r['task_id'][9:]
        assert r['detail']['participant'] == 'simulated_customer' and r['detail']['system_under_test'] is False
        assert r['model_id'] is None and r['inference_node'] is None
    environment = _rows(st, ENVIRONMENT_ACTOR)
    assert [r['event_type'] for r in environment] == ['agent.registered', 'task.created', 'task.started', 'task.completed',
                                                     'task.created', 'task.started', 'task.failed']
    assert environment[3]['summary'] == 'Testing Lab environment carrier scan in_transit to delivered applied'
    assert environment[3]['detail']['to_status'] == 'delivered' and environment[3]['correlation_id'] == 'SIMFX0000000021'
    assert environment[6]['summary'] == 'Testing Lab environment service-centre receiving blocked'
    assert environment[6]['task_status'] == 'failed' and environment[6]['correlation_id'] == 'R-RC24-1'
    registry = {r['agent_id']: r for r in telemetry.registry(st)}
    assert (registry[CUSTOMER_ACTOR]['display_name'], registry[CUSTOMER_ACTOR]['role']) == ('Testing Lab Customer', 'lab-customer')
    assert (registry[ENVIRONMENT_ACTOR]['display_name'], registry[ENVIRONMENT_ACTOR]['role']) == ('Testing Lab Environment', 'lab-environment')
    assert registry[CUSTOMER_ACTOR]['capabilities'] == ['simulated_customer']
    assert registry[ENVIRONMENT_ACTOR]['execution_constraints'] == {'participant': 'simulated_environment', 'system_under_test': False}
    assert not LAB_IDS & {a[0] for a in telemetry.AGENTS}                                  # never ticket-workflow agents
    # No message text, persona or scenario fact in any participant row.
    text = json.dumps(customer + environment, default=str)
    for secret in ('intents', 'behaviour', 'serial_number', 'date_of_birth', 'customer_state'):
        assert secret not in text


def test_telemetry_failure_never_affects_a_participant(tmp_path, monkeypatch):
    st = Store(str(tmp_path / 'operations.db'))

    def broken(*args, **kwargs):
        raise RuntimeError('telemetry unavailable')
    monkeypatch.setattr(telemetry, 'register', broken)
    lab = ParticipantTelemetry(st, 'CASE-RC24', RUN)
    lab.customer_received('customer:MSG-1', 'MSG-1')                    # registration failed: rows still carry the identity
    assert {r['agent_id'] for r in _rows(st) if r['event_type'].startswith('task.')} == {CUSTOMER_ACTOR}
    monkeypatch.setattr(telemetry, 'emit', broken)
    lab.customer_done('customer:MSG-1', 'MSG-1', 'applied', True)       # does not raise
    ParticipantTelemetry(None, 'CASE-RC24', RUN).environment_done('k', {'kind': 'carrier'}, 'applied')


# --- 3. the READY probe ------------------------------------------------------------

class ProbeGateway:
    def __init__(self):
        self.calls = []

    def availability(self, *args, **kwargs):
        return {'status': 'available'}

    def infer(self, model, nodes, operation, payload, *, client_key=None, timeout=None, progress=None,
              expected_digest=None, required_capabilities=None):
        self.calls.append(json.dumps({'model': model, 'nodes': nodes, 'operation': operation, 'payload': payload,
                                      'expected_digest': expected_digest, 'required_capabilities': required_capabilities},
                                     sort_keys=True, separators=(',', ':')))
        request = {'request_id': 'REQ-' + client_key[5:], 'lease_id': 'LEASE-RC24', 'resource_id': HEAD, 'status': 'leased', 'executing': 1}
        progress({'status': 'active', 'request': request, 'execution_started_at': 1795013730.5})
        return {'status': 'completed', 'request': request, 'result': {'resource_id': HEAD, 'request_id': request['request_id'], 'model': model,
                'response': {'model': model, 'response': 'READY', 'done': True, 'done_reason': 'stop', 'eval_count': 2}}}


def _probe(tmp_path, monkeypatch):
    gateway = ProbeGateway()
    monkeypatch.setattr(gateway_transport, 'client', lambda workload='operational': gateway)
    monkeypatch.setenv('CAPSTONE_INFERENCE_MODE', 'gateway')
    tmp_path.mkdir(parents=True, exist_ok=True)
    st = Store(str(tmp_path / 'operations.db'));st.inference_registry = Registry(tmp_path / 'settings.db')
    profile = {'id': 'rc24-probe', 'configuration_hash': 'sha256-rc24', 'execution_mode': 'distributed', 'gateway_workload': 'research',
               'capacity_preflight_verified': True, 'case_budget_seconds': 300,
               'endpoints': {'gateway': {'id': 'gateway', 'url': 'https://192.0.2.254:8443', 'transport': 'gateway', 'timeout': 300}},
               'routes': {'investigation': {'endpoint': 'gateway', 'model': 'qwen3.5:4b', 'enabled': True, 'options': {}}}}
    run = {'id': 'RUN-RC24-PROBE', 'experiment_id': 'EXP-RC24', 'level': 'infrastructure', 'category': 'performance',
           'timeout_seconds': 120, 'profile': profile}
    result = experiment_catalogue.execute_probe(st, 'CASE-RC24', run, tmp_path)
    return st, gateway, result


def test_probe_inference_events_carry_the_probe_identity_and_request_is_unchanged(tmp_path, monkeypatch):
    st, gateway, result = _probe(tmp_path, monkeypatch)
    assert result['task_accuracy'] is True and result['actual'] == 'READY'
    assert len(gateway.calls) == 1 and hashlib.sha256(gateway.calls[0].encode()).hexdigest() == GOLDEN_PROBE_REQUEST
    rows = _rows(st, 'testing_lab_probe')
    assert [r['event_type'] for r in rows] == ['agent.registered', 'task.created', 'task.started', 'inference.queued',
                                              'inference.started', 'inference.completed', 'task.completed']
    for r in rows[3:6]:
        assert r['detail']['logical_agent_status'] == 'known'
        assert (r['task_id'], r['workflow_id']) == ('RUN-RC24-PROBE', 'EXP-RC24')
    assert rows[4]['inference_node'] == HEAD
    registered = {r['agent_id']: r for r in telemetry.registry(st)}['testing_lab_probe']
    assert (registered['display_name'], registered['role']) == ('Testing Lab Probe', 'lab-probe')
    assert registered['execution_constraints']['system_under_test'] is False


def test_probe_registration_failure_never_stops_the_probe(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('registry unavailable')
    monkeypatch.setattr(telemetry, 'register', broken)
    st, gateway, result = _probe(tmp_path, monkeypatch)
    assert result['task_accuracy'] is True and len(gateway.calls) == 1
    assert {r['agent_id'] for r in _rows(st) if r['event_type'].startswith('inference.')} == {'testing_lab_probe'}


# --- 4. a complete agentic run (real worker subprocess, stand-in Gateway) ----------------

def _translator(monkeypatch):
    """Agent Arena's translator, read-only: a standalone file before Arena 2.4.0,
    a module of the standard-library-only arena_service package since."""
    path = Path(os.environ.get('CAPSTONE_ARENA_TRANSLATOR', Path(__file__).resolve().parents[2] / 'AgentArena/arena_service/adapters/capstone/translate.py'))
    if not path.exists():
        return None
    source = path.read_text(encoding='utf-8')
    if 'from ...' not in source:
        namespace = {}
        exec(compile(source, str(path), 'exec'), namespace)
        return namespace['Translator']
    import importlib
    import sys
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)          # never write into the independent project
    monkeypatch.syspath_prepend(str(path.parents[3]))
    for name in [m for m in sys.modules if m == 'arena_service' or m.startswith('arena_service.')]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module('arena_service.adapters.capstone.translate').Translator


def _comparable(metrics):
    timing = ('_ms', 'duration')
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if not any(k.endswith(t) for t in timing)}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(metrics)


def test_complete_run_shows_customer_and_environment_and_metrics_are_unchanged(monkeypatch, tmp_path):
    from test_agentic_testing import _agentic_run
    from capstone_rag import experiment_worker
    result, run_dir, log, _ = _agentic_run(monkeypatch, tmp_path, 'agentic:missing-goggles:cooperative')
    assert result['status'] == 'completed', (result.get('error'), log[-3000:])
    journal = result['lifecycle']['journal']
    customer_steps = [r for r in journal if r.get('kind') == 'customer' and r.get('status') == 'applied' and '#' not in r['key']]
    environment_steps = [r for r in journal if r.get('kind') == 'environment' and r.get('status') == 'applied']
    assert customer_steps and environment_steps

    st = Store(str(run_dir / 'operations.db'))
    try:
        cid = result['case_id']
        customer = _rows(st, CUSTOMER_ACTOR)
        environment = _rows(st, ENVIRONMENT_ACTOR)
        # One task per journaled step, in order, each created -> started -> terminal.
        for rows, steps in ((customer, customer_steps), (environment, environment_steps)):
            assert sum(r['event_type'] == 'agent.registered' for r in rows) == 1
            tasks = [r for r in rows if r['event_type'].startswith('task.')]
            assert [r['task_id'] for r in tasks if r['event_type'] == 'task.created'] == [s['key'] for s in steps]
            for step in steps:
                kinds = [r['event_type'] for r in tasks if r['task_id'] == step['key']]
                assert kinds[:2] == ['task.created', 'task.started'] and len(kinds) == 3
                assert kinds[2] == ('task.failed' if step.get('outcome') in ('failed', 'blocked') else 'task.completed')
            assert {(r['case_id'], r['workflow_id']) for r in tasks} == {(cid, 'RUN-RC8')}
        replied = [r for r in customer if r['event_type'] == 'task.completed' and r['detail']['replied']]
        assert len(replied) == sum(1 for s in customer_steps if s.get('reply'))
        assert {r['correlation_id'] for r in customer} - {None} <= {s['target_id'] for s in customer_steps} | {CUSTOMER_ACTOR}
        assert [r['detail']['to_status'] for r in environment if r['event_type'] == 'task.completed'][:2] == ['in_transit', 'delivered']

        # Mirrored for Agent Arena with the run id, like every other lab event,
        # and imported into the live database at run end.
        files = sorted(run_dir.glob('arena-mirror*.jsonl'))
        mirrored = [json.loads(line) for f in files for line in f.read_text(encoding='utf-8').splitlines() if line.strip()]
        lab_mirrored = [m for m in mirrored if m['agent_id'] in (CUSTOMER_ACTOR, ENVIRONMENT_ACTOR)]
        assert len(lab_mirrored) == len(customer) + len(environment)
        assert all(m['workflow_id'] == 'RUN-RC8' and m['summary'].startswith('EXPERIMENT Testing Lab') for m in lab_mirrored
                   if m['event_type'].startswith('task.'))
        with sqlite3.connect(tmp_path / 'live.db') as live:
            # rc.25 A1 closure rows (ARENA-RECON-*) are coordinator facts, not mirrored rows.
            imported = live.execute("SELECT COUNT(*) FROM arena_events WHERE agent_id IN (?,?) AND workflow_id='RUN-RC8' AND event_id NOT LIKE 'ARENA-RECON-%'",
                                    (CUSTOMER_ACTOR, ENVIRONMENT_ACTOR)).fetchone()[0]
        assert imported == len(lab_mirrored)

        # Not workflow agents, no model calls of their own.
        communication = result['metrics']['communication_metrics']
        assert not LAB_IDS & set(communication['logical_agent_roles']) and not LAB_IDS & set(communication['model_backed_roles'])
        assert not {r['role'] for r in st.query('SELECT role FROM inference_calls')} & {'lab_customer', 'lab_environment', CUSTOMER_ACTOR, ENVIRONMENT_ACTOR}

        # The metrics do not depend on the new rows: score the run with and without them.
        manifest = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
        scored = {}
        for name, strip in (('with', False), ('without', True)):
            copy_path = tmp_path / f'score-{name}.db'
            shutil.copyfile(run_dir / 'operations.db', copy_path)
            if strip:
                with sqlite3.connect(copy_path) as db:
                    removed = db.execute('DELETE FROM arena_events WHERE agent_id IN (?,?)', (CUSTOMER_ACTOR, ENVIRONMENT_ACTOR)).rowcount
                assert removed == len(customer) + len(environment)
            copy_store = Store(str(copy_path))
            try:
                scored[name] = _comparable(experiment_worker.score(copy_store, cid, manifest, 0.0))
            finally:
                copy_store.close()
        assert scored['with'] == scored['without']
        assert scored['with']['communication_metrics']['logical_agent_count'] == communication['logical_agent_count']
        assert scored['with']['communication_metrics']['model_backed_agent_count'] == communication['model_backed_agent_count']

        # Agent Arena's own (read-only) translator turns them into figures with tasks.
        Translator = _translator(monkeypatch)
        if Translator is None:
            pytest.skip('Independent Arena translator not available; translator check pending')
        translator = Translator({r['agent_id']: r for r in telemetry.registry(st)})
        mapped = [out for row in customer + environment for out in translator.translate(row)]
        for agent, role in ((CUSTOMER_ACTOR, 'lab-customer'), (ENVIRONMENT_ACTOR, 'lab-environment')):
            registered = [e for e in mapped if e['type'] == 'agent.registered' and e['agent_id'] == agent]
            assert registered and registered[-1]['role'] == role
            created = [e for e in mapped if e['type'] == 'assignment.created' and e['agent_id'] == agent]
            assert len(created) == len(customer_steps if agent == CUSTOMER_ACTOR else environment_steps)
            keys = {e['assignment_id'] for e in created}
            finished = {e['assignment_id'] for e in mapped if e['type'] == 'assignment.state' and e.get('state') in ('completed', 'failed')
                        and e.get('assignment_id') in keys}
            assert finished == keys
        assert not any(k.startswith('task.') for k in translator.skipped), translator.skipped
    finally:
        st.close()
