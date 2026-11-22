"""rc.25: every request body Capstone sends satisfies Gateway 0.4.6 request validation (§6).

Gateway 0.4.6 refuses with HTTP 400 before admission:

* ``tools`` that are not a list of at most 64 entries shaped exactly
  ``{"type": "function", "function": {"name": str, "description": str, "parameters": object}}``;
* ``images`` that are not a list of strings;
* options: ``temperature`` / ``top_p`` / ``repeat_penalty`` not finite numbers;
  ``seed`` / ``top_k`` (and the token counts) not whole numbers; ``stop`` not a
  list of at most 16 strings; booleans anywhere numeric.

The bodies are captured at the stand-in Gateway for the native, mediated and
delegated strategies, the reviewer, the methodology roles and the lab
qualification tool.
"""
import json
import math

import pytest

INT_OPTIONS = ('seed', 'top_k', 'num_ctx', 'num_predict')
FLOAT_OPTIONS = ('temperature', 'top_p', 'repeat_penalty')


def shape_errors(body):
    errors = []
    payload = body.get('payload') or body
    tools = payload.get('tools')
    if tools is not None:
        if not isinstance(tools, list) or len(tools) > 64:
            errors.append('tools must be a list of at most 64')
        for tool in tools or []:
            fn = tool.get('function') if isinstance(tool, dict) else None
            if not isinstance(tool, dict) or tool.get('type', 'function') != 'function' or not isinstance(fn, dict):
                errors.append(f'bad tool entry {tool!r}'[:120]);continue
            if not isinstance(fn.get('name'), str) or not isinstance(fn.get('description'), str) or not isinstance(fn.get('parameters'), dict):
                errors.append(f'bad tool function {fn!r}'[:120])
            if set(tool) - {'type', 'function'} or set(fn) - {'name', 'description', 'parameters'}:
                errors.append('unexpected tool keys')
    images = payload.get('images')
    if images is not None and (not isinstance(images, list) or not all(isinstance(i, str) for i in images)):
        errors.append('images must be a list of strings')
    for key, value in (payload.get('options') or {}).items():
        if isinstance(value, bool):
            errors.append(f'{key} is boolean')
        elif key in INT_OPTIONS and not (isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())):
            errors.append(f'{key} not a whole number: {value!r}')
        elif key in FLOAT_OPTIONS and not (isinstance(value, (int, float)) and math.isfinite(value)):
            errors.append(f'{key} not a finite number: {value!r}')
        elif key == 'stop' and not (isinstance(value, list) and len(value) <= 16 and all(isinstance(s, str) for s in value)):
            errors.append('stop must be a list of at most 16 strings')
    return errors


def test_validator_rejects_the_refused_shapes():
    assert shape_errors({'tools': [{'type': 'function', 'function': {'name': 'x', 'parameters': '{}'}}]})
    assert shape_errors({'options': {'seed': 7.5}}) and shape_errors({'options': {'top_k': True}}) and shape_errors({'options': {'temperature': float('inf')}})
    assert not shape_errors({'options': {'seed': 7.0, 'temperature': 0}})


@pytest.mark.parametrize('strategy,delegate', [('native', None), ('mediated', None), ('delegated', 'qwen3:8b')])
def test_every_submitted_body_passes_gateway_046_validation(monkeypatch, tmp_path, strategy, delegate):
    from test_agentic_testing import SMALL, LARGE, REVIEWER, _agentic_run
    from capstone_rag.agentic_methodologies import candidates
    _, _, _, gateway = _agentic_run(monkeypatch, tmp_path / strategy, 'agentic:missing-goggles:cooperative', models=(SMALL, LARGE, REVIEWER),
                                    strategy=strategy, delegate=delegate, architecture=candidates(['H'], ['4'])[0], keep_gateway=True)
    try:
        bodies = gateway.submissions
        assert bodies, 'no request reached the stand-in Gateway'
        assert any((b.get('payload') or {}).get('tools') for b in bodies) or strategy == 'mediated'
        bad = [(i, e) for i, b in enumerate(bodies) for e in shape_errors(b)]
        assert not bad, bad[:5]
    finally:
        gateway.close()


def test_qualification_tool_bodies_pass_gateway_046_validation(monkeypatch, tmp_path):
    from test_rc25_lab_qualification import MODELS
    from test_rc8_worker_gateway import GatewayStandIn, HEAD
    from capstone_rag import config, lab_qualification as lq
    from capstone_rag.ai_settings import Registry
    from capstone_rag.store import Store
    monkeypatch.setattr(lq, 'ROUTE_OVERRIDES', {'cluster_nodes': [HEAD]})
    gateway = GatewayStandIn(real_embeddings=True, models=MODELS, no_tools_models=('gemma3:4b',))
    try:
        token = tmp_path / 't';token.write_text('fixture-research-token', encoding='utf-8')
        client = tmp_path / 'c.json'
        client.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1', 'service_id': 'shared-inference',
                                      'url': gateway.url, 'application_id': 'capstone-research', 'token_file': str(token)}), encoding='utf-8')
        monkeypatch.setenv('CAPSTONE_INFERENCE_MODE', 'gateway')
        monkeypatch.setenv('CAPSTONE_GATEWAY_CLIENT_CONFIG', str(client));monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG', str(client))
        monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
        store = Store(str(tmp_path / 'q.db'));store.routing_enabled = True
        registry = Registry(tmp_path / 'r.db');store.inference_registry = registry
        lq.gemma_mediated(store, registry);lq.native_arguments(store, registry, 'qwen3:1.7b');lq.structured_budgets(store, registry, 'granite4:3b')
        lq.reviewer_calibration(store, registry)
        store.close()
        bad = [(i, e) for i, b in enumerate(gateway.submissions) for e in shape_errors(b)]
        assert gateway.submissions and not bad, bad[:5]
        gemma_tools = [b for b in gateway.submissions if b.get('model') == 'gemma3:4b' and (b.get('payload') or {}).get('tools')]
        assert not gemma_tools, 'native tools sent to gemma'
    finally:
        gateway.close()
