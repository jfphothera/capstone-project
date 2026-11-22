"""6.0.0-rc.23: the agentic Testing Lab reviewer has its own telemetry identity.

Before rc.23 the reviewer's model calls (route lab_reviewer, qwen3:4b in
EXP-8608251d...) ran with no task context, so every inference event carried
agent_id NULL / logical_agent_status 'unavailable' and Agent Arena's
translator dropped them: no reviewer assignment and no failure for the calls
the Gateway timed out. These tests drive the real inference path in Gateway
mode with a stand-in Gateway client (no physical head is claimed).
"""
import json
import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from capstone_rag import agentic_review_session as ars
from capstone_rag import agentic_reviewer as reviewer
from capstone_rag import gateway_transport, inference, task_context, telemetry
from capstone_rag.ai_settings import Registry
from capstone_rag.store import Store

HEAD = 'head-192-0-2-5'
RUN = 'RUN-9276628e56ae48b0bc1cc35a0d82655f'
STEP = 'draft:MSG-RC23'


def _fill(schema):
    return {k: (s['enum'][0] if 'enum' in s else [] if s.get('type') == 'array' else 'fixture') for k, s in schema['properties'].items()}


class StandInGateway:
    """Shaped like shared_inference_client.Client.infer: progress on dispatch, then a result,
    a failed envelope (the Gateway's own 'timed out') or the client deadline."""
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def availability(self, *args, **kwargs):
        return {'status': 'available'}

    def infer(self, model, nodes, operation, payload, *, client_key=None, timeout=None, progress=None,
              expected_digest=None, required_capabilities=None):
        self.calls.append(json.dumps({'model': model, 'nodes': nodes, 'operation': operation, 'payload': payload,
                                      'expected_digest': expected_digest, 'required_capabilities': required_capabilities},
                                     sort_keys=True, separators=(',', ':')))
        request = {'request_id': 'REQ-' + client_key[5:], 'lease_id': 'LEASE-RC23', 'resource_id': HEAD, 'status': 'leased', 'executing': 1}
        progress({'status': 'active', 'request': request, 'execution_started_at': 1795013730.5})
        if self.outcome == 'deadline':
            raise TimeoutError('Gateway inference deadline exceeded; query this request before retrying')
        if self.outcome == 'failed':
            return {'status': 'failed', 'error': 'timed out', 'request': {**request, 'status': 'cancelled'}}
        content = json.dumps(_fill(payload['format']))
        return {'status': 'completed', 'request': request, 'result': {'resource_id': HEAD, 'request_id': request['request_id'], 'model': model,
                'response': {'model': model, 'message': {'role': 'assistant', 'content': content}, 'done': True, 'done_reason': 'stop', 'eval_count': 40}}}


def _snapshot():
    return {'id': 'rc23-fixture', 'configuration_hash': 'sha256-rc23', 'execution_mode': 'distributed', 'gateway_workload': 'research',
            'capacity_preflight_verified': True, 'case_budget_seconds': 300,
            'endpoints': {'gateway': {'id': 'gateway', 'url': 'https://192.0.2.254:8443', 'transport': 'gateway', 'timeout': 300}},
            'routes': {'investigation': {'endpoint': 'gateway', 'model': 'qwen3.5:4b', 'enabled': True, 'options': {}},
                       'lab_reviewer': {'endpoint': 'gateway', 'model': 'qwen3:4b', 'enabled': True, 'participant': 'simulated_human_reviewer',
                                        'policy_version': reviewer.REVIEWER_POLICY_VERSION,
                                        'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 1024}}}}


VIEW = {'decision_point': 'draft', 'target_id': 'MSG-RC23', 'previous_attempt': None,
        'case': {'case_id': 'CASE-RC23', 'subject': 'Synthetic reviewer fixture', 'status': 'Awaiting review'},
        'draft': {'message_id': 'MSG-RC23', 'characters': 21, 'parts': 1, 'validation_disposition': 'passed'},
        'options': list(reviewer.OPTIONS['draft']),
        'evidence': [{'id': 'draft:part1', 'kind': 'draft_part', 'content': 'Synthetic draft text.'}], 'withheld': []}


def _review(tmp_path, monkeypatch, outcome, *, enclosing=None, step=STEP):
    gateway = StandInGateway(outcome)
    monkeypatch.setattr(gateway_transport, 'client', lambda workload='operational': gateway)
    st = Store(str(tmp_path / 'operations.db'))
    st.inference_registry = Registry(tmp_path / 'settings.db')
    error = None
    with inference.session(st, RUN, case_id='CASE-RC23', snapshot=_snapshot()), (enclosing or nullcontext()):
        try:
            with (reviewer.review_step(step) if step else nullcontext()):
                ars._model_call(VIEW, 'final')
        except Exception as exc:
            error = exc
    return st, gateway, error, telemetry.replay(st, 0, 1000)


@pytest.mark.parametrize('outcome,terminal', [('completed', 'inference.completed'), ('failed', 'inference.failed'),
                                              ('deadline', 'inference.failed')])
def test_queued_started_and_finished_reviewer_calls_carry_the_reviewer_identity(tmp_path, monkeypatch, outcome, terminal):
    st, gateway, error, events = _review(tmp_path, monkeypatch, outcome)
    assert (error is None) == (outcome == 'completed')
    calls = [e for e in events if e['event_type'].startswith('inference.')]
    assert [e['event_type'] for e in calls] == ['inference.queued', 'inference.started', terminal]
    call_id = calls[0]['correlation_id']
    for event in calls:
        assert event['agent_id'] == reviewer.REVIEWER_AGENT_ID == 'lab_reviewer'
        assert event['agent_id'] not in {a[0] for a in telemetry.AGENTS}       # not a ticket-workflow agent
        assert event['detail']['logical_agent_status'] == 'known'
        assert event['detail']['route_role'] == 'lab_reviewer'
        assert event['model_id'] == 'qwen3:4b'
        assert event['case_id'] == 'CASE-RC23'
        assert event['workflow_id'] == event['detail']['workflow_id'] == RUN
        assert event['task_id'] == event['detail']['task_id'] == STEP             # the lifecycle journal key
        assert event['correlation_id'] == event['detail']['request_id'] == call_id
    assert calls[0]['inference_node'] is None                                    # the Gateway has not chosen a head yet
    assert calls[1]['inference_node'] == calls[2]['inference_node'] == HEAD
    assert calls[1]['detail']['gateway_request_id'] == 'REQ-' + call_id[5:]
    assert calls[2]['task_status'] == ('completed' if outcome == 'completed' else 'failed')
    registered = [e for e in events if e['event_type'] == 'agent.registered' and e['agent_id'] == 'lab_reviewer']
    assert len(registered) == 1 and registered[0]['detail']['role'] == reviewer.REVIEWER_AGENT_ROLE == 'lab-reviewer'
    row = st.one("SELECT payload FROM inference_calls WHERE call_id=?", (call_id,))
    record = json.loads(row['payload'])
    assert (record['logical_agent_id'], record['logical_agent_status'], record['session_scope_id']) == ('lab_reviewer', 'known', RUN)


def test_reviewer_call_inside_a_workflow_agent_context_is_still_the_reviewer(tmp_path, monkeypatch):
    enclosing = task_context.bind(task_id='EXEC-TASK-SUT', workflow_id='GRAPH-SUT', agent_id='supervisor')
    st, gateway, error, events = _review(tmp_path, monkeypatch, 'completed', enclosing=enclosing, step=None)
    calls = [e for e in events if e['event_type'].startswith('inference.')]
    assert {e['agent_id'] for e in calls} == {'lab_reviewer'}
    assert {e['task_id'] for e in calls} == {'draft:MSG-RC23'}                   # derived from the view without a step key
    assert {e['workflow_id'] for e in calls} == {RUN}


def test_reviewer_request_is_byte_identical_with_and_without_the_identity(tmp_path, monkeypatch):
    _, with_identity, _, _ = _review(tmp_path / 'a', monkeypatch, 'completed')
    monkeypatch.setattr(reviewer, 'telemetry_identity', lambda view=None: nullcontext())
    monkeypatch.setattr(reviewer, 'review_step', lambda key: nullcontext())
    _, without, _, events = _review(tmp_path / 'b', monkeypatch, 'completed')
    assert with_identity.calls == without.calls and len(without.calls) == 1
    sent = json.loads(without.calls[0])
    assert sent['model'] == 'qwen3:4b'
    assert sent['payload']['options'] == {'temperature': 0, 'num_ctx': 8192, 'num_predict': 1024}
    assert sent['payload']['messages'][0]['content'].startswith(reviewer.REVIEWER_SYSTEM)
    assert {e['agent_id'] for e in events if e['event_type'].startswith('inference.')} == {None}   # the pre-rc.23 shape


def test_telemetry_registration_failure_never_stops_a_review(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('registry unavailable')
    monkeypatch.setattr(telemetry, 'register', broken)
    st, gateway, error, events = _review(tmp_path, monkeypatch, 'completed')
    assert error is None and len(gateway.calls) == 1
    assert {e['agent_id'] for e in events if e['event_type'].startswith('inference.')} == {'lab_reviewer'}


def test_independent_arena_translator_shows_the_reviewer_and_its_failure(tmp_path, monkeypatch):
    # Arena 2.4.0 made the translator a module of its arena_service package
    # (relative imports); the rc.24 loader handles both shapes, read-only.
    from test_rc24_lab_participants_telemetry import _translator
    Translator = _translator(monkeypatch)
    if Translator is None:
        pytest.skip('Independent Arena translator not available; contract integration pending')
    st, gateway, error, events = _review(tmp_path, monkeypatch, 'failed')
    translator = Translator({r['agent_id']: r for r in telemetry.registry(st)})
    mapped = [out for event in events for out in translator.translate(event)]
    assert not any(k.startswith('inference.') for k in translator.skipped)
    created = [e for e in mapped if e['type'] == 'assignment.created' and e['agent_id'] == 'lab_reviewer']
    assert len(created) == 1 and created[0]['kind'] == 'inference' and created[0]['task_id'] == STEP
    assert created[0]['workflow_id'] == RUN + '/lab_reviewer'                   # outside the run's workflow agents
    key = created[0]['assignment_id']
    assert any(e.get('assignment_id') == key and e.get('to_location') == 'head:' + HEAD and e.get('model_id') == 'qwen3:4b' for e in mapped)
    assert any(e.get('assignment_id') == key and e['type'] == 'assignment.state' and e.get('state') == 'failed' for e in mapped)
    role = [e for e in mapped if e['type'] == 'agent.registered' and e['agent_id'] == 'lab_reviewer']
    assert role and role[-1]['role'] == 'lab-reviewer'
