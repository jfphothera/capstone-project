"""rc.25 field defect (2026-11-19): every model the Gateway had qualified only for "completion" was refused with
"no native tool calling (gateway qualified capabilities)". A qualified list without "tools" means the Gateway's tools
test was never run, not that the model lacks tools. Tool support now comes from the Gateway's tools test, then the
runtime-declared capabilities (Ollama /api/show), and a model without confirmed native tools delegates its tool calls
by default to a tool-capable model."""
import pytest

from capstone_rag import model_evidence as me


def detail(declared, tools_state='not_tested', qualified=('completion',)):
    return {'capabilities': list(declared), 'declared_capabilities': {'source': 'runtime_declared', 'values': list(declared)},
            'qualifications': {'completion': {'state': 'passed'}, 'tools': {'state': tools_state}},
            'qualified_capabilities': list(qualified)}


FIELD_MODELS = ('qwen3.5:4b', 'qwen3.5:2b', 'llama3.2:1b', 'qwen3:8b')


@pytest.mark.parametrize('model', FIELD_MODELS)
def test_completion_only_list_is_not_refused(model):
    # Exactly the field shape: Gateway 0.4.6 reports ["completion"] only.
    r = me.resolve_tool_strategy(model, None, ['completion'])
    assert r['strategy'] == 'native'


@pytest.mark.parametrize('model', FIELD_MODELS)
def test_declared_tools_resolve_native(model):
    facts = me.tool_facts([detail(['completion', 'tools', 'thinking'])])
    native, basis = me.native_tools_evidence(model, facts)
    assert native is True and 'declares' in basis
    assert me.resolve_tool_strategy(model, None, facts)['strategy'] == 'native'


def test_gateway_tools_test_wins_over_declared():
    assert me.native_tools_evidence('x', me.tool_facts([detail(['completion'], 'passed', ('completion', 'tools'))]))[0] is True
    assert me.native_tools_evidence('x', me.tool_facts([detail(['completion', 'tools'], 'failed')]))[0] is False


def test_any_head_passing_counts():
    facts = me.tool_facts([detail(['completion', 'tools'], 'failed'), detail(['completion', 'tools'], 'passed')])
    assert facts['tools_state'] == 'passed'


def test_model_without_tools_delegates_to_preferred_tool_model():
    pool = {'qwen3.5:4b': me.tool_facts([detail(['completion', 'tools'])]),
            'llama3.2:3b': me.tool_facts([detail(['completion', 'tools'])]),
            'gemma3:4b': me.tool_facts([detail(['completion', 'vision'])])}
    r = me.resolve_tool_strategy('gemma3:4b', None, pool['gemma3:4b'], delegates=pool)
    assert r['strategy'] == 'delegated' and r['delegate_model'] == 'qwen3.5:4b' and r['default'] is True
    assert 'qwen3.5:4b' in r['basis']


def test_unknown_model_delegates_when_a_tool_model_exists():
    pool = {'qwen3:8b': me.tool_facts([detail(['completion', 'tools'])])}
    r = me.resolve_tool_strategy('mystery:1b', None, None, delegates=pool)
    assert r['strategy'] == 'delegated' and r['delegate_model'] == 'qwen3:8b'


def test_never_delegates_to_itself_or_unconfirmed():
    pool = {'gemma3:4b': me.tool_facts([detail(['completion'])]), 'mystery:1b': None}
    with pytest.raises(me.ToolStrategyIncompatible):
        me.resolve_tool_strategy('gemma3:4b', None, pool['gemma3:4b'], delegates=pool)


def test_explicit_choice_is_kept():
    pool = {'qwen3.5:4b': me.tool_facts([detail(['completion', 'tools'])])}
    r = me.resolve_tool_strategy('gemma3:4b', {'strategy': 'mediated'}, me.tool_facts([detail(['completion'])]), delegates=pool)
    assert r['strategy'] == 'mediated'


def test_native_model_is_not_delegated():
    pool = {'qwen3.5:4b': me.tool_facts([detail(['completion', 'tools'])]), 'qwen3:8b': me.tool_facts([detail(['completion', 'tools'])])}
    assert me.resolve_tool_strategy('qwen3:8b', None, pool['qwen3:8b'], delegates=pool)['strategy'] == 'native'


def test_lab_state_and_launch_defaults(monkeypatch):
    from capstone_rag.webapp.routers import lab
    def row(name, declared):
        notes = lab.model_notes(name, {**me.tool_facts([detail(declared)]), 'digest': 'sha256:x'})
        return {'name': name, 'heads': ['head-192-0-2-2'], 'blocked': {}, **notes}
    current = {'models': [row('qwen3.5:4b', ['completion', 'tools']), row('qwen3.5:2b', ['completion', 'tools']),
                          row('llama3.2:1b', ['completion', 'tools']), row('qwen3:8b', ['completion', 'tools']),
                          row('gemma3:4b', ['completion', 'vision'])]}
    for m in current['models']:
        if m['name'] != 'gemma3:4b':
            assert m['native_tools'] is True and not m['tool_strategy_default'].get('incompatible'), m
    chosen = lab._default_tool_strategies(current, {'models': [m['name'] for m in current['models']]},
                                          [m['name'] for m in current['models']])
    assert chosen == {'gemma3:4b': {'strategy': 'delegated', 'delegate_model': 'qwen3.5:4b'}}
    # A delegate must be usable on every chosen head.
    assert lab._delegate_candidates(current, ['head-192-0-2-9']) == {}
