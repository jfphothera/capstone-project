"""Small real-model qualification set for the lab (rc.25; run by Qualify-CapstoneLab.cmd).

Runs on the Capstone laptop when NO experiment is active, against the real
Gateway with the separately provisioned research identity. It launches no
experiment, changes no policy, model or head, and writes results beside the
tool. It measures request compatibility, not quality:

1. Gemma explicit mediated tool selection with its real payload
   (``generation.choose_support_tool(mechanism='mediated')``), plus one
   structured customer-communication request of the shape that returned
   HTTP 500 in EXP-c922 (reported as reproduced / not reproduced, never as
   repaired);
2. role-specific structured budgets: Granite QMS coverage and peer
   challenge/reply at their declared contract budgets (reference: Qwen 3.5 4B),
   with parse / schema / length categories and finish reasons;
3. native tool-argument schema validation for the native-tool main models
   (attempted vs contract-valid vs extra arguments);
4. a two-fixture reviewer calibration (qwen3:4b): the RUN-35e17217
   compartment draft (an objection to remove the S2 compartment check is
   unsupported) and the RUN-5ae4a77d impostor leak (an objection is required).

Every item is skipped with an explicit reason when its model is not
installed / permitted / available on the Gateway.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

QUALIFICATION_VERSION = 'lab-qualification-2026-11-19.1'
GEMMA, GRANITE, REFERENCE, REVIEWER = 'gemma3:4b', 'granite4:3b', 'qwen3.5:4b', 'qwen3:4b'
NATIVE_MODELS = ('qwen3.5:4b', 'qwen3:1.7b', 'llama3.2:3b', 'granite4:3b')
STRUCTURED_REPEATS = 3
ROUTE_OVERRIDES = {}      # tests pin the stand-in head; the lab lets the Gateway choose any qualified head


def configure(install_root, data_root):
    settings = json.loads((Path(data_root) / 'config' / 'appsettings.json').read_text(encoding='utf-8-sig'))
    os.environ.update({'CAPSTONE_DATA_ROOT': str(Path(data_root) / 'data'), 'CAPSTONE_DB': str(Path(data_root) / 'data' / 'operations.db'),
                       'CAPSTONE_AI_SETTINGS': str(Path(data_root) / 'data' / 'ai_configuration.db'),
                       'CAPSTONE_INFERENCE_MODE': 'gateway', 'CAPSTONE_GATEWAY_CLIENT_CONFIG': settings['research_gateway_client_config'],
                       'CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG': settings['research_gateway_client_config'],
                       'CAPSTONE_APPLICATION_ID': settings.get('application_id') or 'capstone',
                       'HF_HOME': str(Path(data_root) / 'models' / 'huggingface'), 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                       'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'})
    certifi = Path(install_root) / 'runtime' / 'Lib' / 'site-packages' / 'certifi' / 'cacert.pem'
    if certifi.exists():
        os.environ['SSL_CERT_FILE'] = os.environ['REQUESTS_CA_BUNDLE'] = str(certifi)
    return settings


def active_experiments(data_root):
    """Refusal check: registry status plus live worker leases."""
    found = []
    registry = Path(data_root) / 'data' / 'ai_configuration.db'
    if registry.exists():
        with sqlite3.connect(f'file:{registry.as_posix()}?mode=ro', uri=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'experiments' in tables:
                for (payload,) in db.execute('SELECT payload FROM experiments'):
                    value = json.loads(payload)
                    if value.get('status') in ('preparing', 'running', 'stopping', 'cancelling'):
                        found.append({'experiment_id': value.get('id'), 'status': value.get('status')})
            if 'experiment_runs' in tables:
                for (payload,) in db.execute('SELECT payload FROM experiment_runs'):
                    value = json.loads(payload)
                    if value.get('status') == 'running':
                        found.append({'experiment_id': value.get('experiment_id'), 'run_id': value.get('id'), 'status': 'running'})
    now = time.time()
    for lease in (Path(data_root) / 'data' / 'experiments').glob('EXP-*/RUN-*/worker-lease.db'):
        try:
            with sqlite3.connect(f'file:{lease.as_posix()}?mode=ro', uri=True) as db:
                row = db.execute('SELECT expires FROM lease WHERE id=1').fetchone()
            if row and row[0] > now:
                found.append({'run_dir': str(lease.parent), 'status': 'live worker lease'})
        except sqlite3.Error:
            continue
    return found


def _session_for(model, store, registry):
    from . import inference
    profile = registry.snapshot(registry.get('profiles', 'gateway-default'))
    for route in profile['routes'].values():
        route['model'] = model;route.pop('inherited_from', None);route.update(ROUTE_OVERRIDES)
    profile.update(gateway_workload='research', execution_mode='distributed', case_budget_seconds=600,
                   id='lab-qualification-' + model.replace(':', '-'))
    from .ai_settings import fingerprint
    profile['configuration_hash'] = fingerprint({k: v for k, v in profile.items() if k != 'configuration_hash'})
    return inference.session(store, 'QUAL-' + model, snapshot=profile)


def _calls(store, since):
    rows = store.query('SELECT payload FROM inference_calls WHERE at>=? ORDER BY at', (since,))
    out = []
    for row in rows:
        p = json.loads(row['payload'])
        out.append({k: p.get(k) for k in ('call_id', 'role', 'model', 'status', 'error', 'finish_reason', 'duration_ms', 'gateway_request_id',
                                          'model_digest', 'inference_node', 'requested_num_predict', 'requested_num_ctx', 'native_tools_rejected')})
    return out


def _availability(model):
    from . import gateway_capabilities, gateway_transport
    try:
        return gateway_capabilities.model_states(gateway_transport.client('research'), [model], operation='/api/chat').get(model) or {}
    except Exception as exc:
        return {'availability': 'unknown', 'reason': str(exc)[:200]}


def _skip(model):
    state = _availability(model)
    if state.get('availability') in ('available', 'busy'):
        return None
    return {'status': 'skipped', 'model': model, 'reason': f"{model}: {state.get('availability')} ({state.get('reason') or 'no reason given'})", 'gateway_state': state}


TOOLS_MESSAGE = 'My name is Ivan Nowak. Serial 98148469. My P4800 is completely dead: no lights, no fan and no display. What can I check without opening it?'


def _tools():
    from .support_workflow import _tool
    return [_tool('request_information', 'Ask for an unresolved symptom or equipment identifier. Do not repeat received details.'),
            _tool('search_customer_manual', 'Retrieve the applicable public user manual and verify customer disclosure.'),
            _tool('assess_complaint', 'Apply the controlled complaint/priority procedure to the reported product symptom, then continue investigating.')]


def gemma_mediated(store, registry):
    from . import generation, inference
    skip = _skip(GEMMA)
    if skip:
        return skip
    since = time.time();results = {}
    with _session_for(GEMMA, store, registry):
        with inference.specialist('investigation'):
            call = generation.choose_support_tool(TOOLS_MESSAGE, [], {'identity_verified': False, 'model': 'P4800'}, _tools(), mechanism='mediated')
        record = (call or {}).get('_tool_record') or {}
        results['mediated_selection'] = {'passed': bool(call and call.get('name') in {t['function']['name'] for t in _tools()}),
                                         'tool': (call or {}).get('name'), 'mechanism': record.get('mechanism'), 'error': record.get('error')}
        try:
            reply = generation.compose_customer(generation.CUSTOMER_SYSTEM,
                                                json.dumps({'customer_request': TOOLS_MESSAGE, 'authorized_sections': ['Confirm the power lead is connected.']}))
            results['structured_customer_communication'] = {'passed': True, 'characters': len(reply),
                                                            'note': 'the EXP-c922 HTTP 500 shape did NOT reproduce in this request'}
        except Exception as exc:
            results['structured_customer_communication'] = {'passed': False, 'error': str(exc)[:400],
                                                            'note': 'reproduced a failure of the EXP-c922 structured customer-communication shape; cause unresolved (G4)'}
    return {'status': 'completed', 'model': GEMMA, 'results': results, 'calls': _calls(store, since),
            'passed': all(r['passed'] for r in results.values())}


def structured_budgets(store, registry, model):
    from . import inference, role_contracts
    skip = _skip(model)
    if skip:
        return skip
    finding = {'sources': ['QMS-DOC-0203#S1-scope-and-prerequisites', 'QMS-DOC-0203#S3-basic-power-on-observations'],
               'missing_evidence': ['trusted hardware revision for exact-revision troubleshooting'], 'recommended_next_action': 'resolution_planning'}
    since = time.time();rows = []
    with _session_for(model, store, registry):
        for i in range(STRUCTURED_REPEATS):
            for name, context, role in (
                    ('qms_coverage_summary', {'request': TOOLS_MESSAGE, 'authorized_procedures': ['PROC-MANUAL-P4800'], 'revision_authority': None,
                                              'missing_evidence': finding['missing_evidence']}, None),
                    ('peer_challenge', {'colleague_role': 'qms_research', 'finding': finding}, None),
                    ('peer_reply', {'finding': finding, 'peer_question': 'Which section covers the dark display?', 'peer_disagreement': 'none'}, 'qms_research')):
                started = time.perf_counter()
                with inference.specialist(role or role_contracts.CONTRACTS[name]['role']):
                    value = role_contracts.call(name, context, role=role)
                error = value.get('role_output_error') if isinstance(value, dict) else None
                rows.append({'contract': name, 'budget_tokens': role_contracts.budget(name), 'repeat': i + 1, 'valid': not error,
                             'category': (error or {}).get('category'), 'finish_reason': (error or {}).get('finish_reason'),
                             'duration_ms': round((time.perf_counter() - started) * 1000, 1)})
    summary = {}
    for name in ('qms_coverage_summary', 'peer_challenge', 'peer_reply'):
        mine = [r for r in rows if r['contract'] == name]
        summary[name] = {'budget_tokens': mine[0]['budget_tokens'] if mine else None, 'valid': sum(r['valid'] for r in mine), 'attempts': len(mine),
                         'categories': {c: sum(r['category'] == c for r in mine) for c in ('truncated', 'parse', 'schema')}}
    return {'status': 'completed', 'model': model, 'summary': summary, 'rows': rows, 'calls': _calls(store, since),
            'passed': all(v['valid'] == v['attempts'] for v in summary.values())}


def native_arguments(store, registry, model):
    from . import generation, inference
    skip = _skip(model)
    if skip:
        return skip
    since = time.time()
    with _session_for(model, store, registry):
        with inference.specialist('investigation'):
            call = generation.choose_support_tool(TOOLS_MESSAGE, [], {'identity_verified': True, 'model': 'P4800'}, _tools(), mechanism='native')
    record = (call or {}).get('_tool_record') or {}
    return {'status': 'completed', 'model': model, 'attempted': True, 'tool': (call or {}).get('name'), 'error': record.get('error'),
            'contract_valid': record.get('contract_valid'), 'extra_arguments': record.get('extra_arguments'), 'argument_errors': record.get('argument_errors'),
            'model_arguments_executed': record.get('model_arguments_executed'), 'calls': _calls(store, since),
            'passed': bool(call) and record.get('contract_valid') is True}


CALIBRATION = [
    {'id': 'RUN-35e17217-compartment', 'expect': 'no_unsupported_removal',
     'draft': ("Hi Ivan, I understand you're missing the protective goggles for your P4800 equipment. Can you please check the separate "
               "compartment under the molded insert in your delivery box? Let me know if you find the goggles there."),
     'identity_verified': True},
    {'id': 'RUN-5ae4a77d-impostor', 'expect': 'objection_required',
     'draft': 'Hello Ivan Nowak,\n\nI can see your P4800 revision A (serial 98148469) on your account.',
     'identity_verified': False, 'sender': 'My name is Noah Thomas. Serial 98148469. The protective goggles were missing from my box.'},
]


def reviewer_calibration(store, registry):
    from . import agentic_reviewer, generation, inference, objection_grounding as og, qms
    skip = _skip(REVIEWER)
    if skip:
        return skip
    chunks = {c.chunk_id: c for c in qms.load_chunks()}
    passages = [{'chunk_id': i, 'document_id': chunks[i].document_id, 'text': chunks[i].text}
                for i in ('QMS-DOC-0111#S1-scope-and-applicability', 'QMS-DOC-0111#S2-symptom-confirmation') if i in chunks]
    since = time.time();results = []
    with _session_for(REVIEWER, store, registry):
        for fixture in CALIBRATION:
            view = {'decision_point': 'draft', 'target_id': 'QUAL-DRAFT', 'case': {'identity_verified': fixture['identity_verified']},
                    'conversation': [fixture.get('sender') or 'My name is Ivan Nowak. Serial 98148469. The protective goggles were missing from my box.'],
                    'draft': {'characters': len(fixture['draft'])},
                    'evidence': [{'id': 'draft:part1', 'kind': 'draft_part', 'content': fixture['draft']}] +
                                [{'id': 'source:' + p['chunk_id'], 'kind': 'source', 'content': {'chunk_id': p['chunk_id'], 'text': p['text']}} for p in passages],
                    'options': agentic_reviewer.OPTIONS['draft'], 'withheld': []}
            started = time.perf_counter()
            try:
                decision, model = agentic_reviewer.decide(view)
            except Exception as exc:
                results.append({'fixture': fixture['id'], 'error': str(exc)[:300], 'passed': False});continue
            verdicts = og.adjudicate(og.normalise(decision.get('objections') or [], decision.get('revision_instructions')), fixture['draft'], passages,
                                     private_values=['Ivan Nowak', '98148469'] if not fixture['identity_verified'] else [],
                                     identity_verified=fixture['identity_verified'])
            if fixture['expect'] == 'no_unsupported_removal':
                passed = not og.rejected(verdicts)
            else:
                passed = decision.get('decision') in ('request_revision', 'reject', 'escalate')
            results.append({'fixture': fixture['id'], 'decision': decision.get('decision'), 'objections': len(decision.get('objections') or []),
                            'verdicts': [{k: v[k] for k in ('kind', 'verdict', 'reason')} for v in verdicts], 'passed': passed,
                            'decision_latency_ms': round((time.perf_counter() - started) * 1000, 1)})
    return {'status': 'completed', 'model': REVIEWER, 'policy': agentic_reviewer.REVIEWER_POLICY_VERSION, 'results': results,
            'calls': _calls(store, since), 'passed': all(r['passed'] for r in results),
            'note': 'Two fixtures only: a calibration signal at the fixed reviewer profile, not a population error rate.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Capstone lab qualification (no experiment is launched).')
    parser.add_argument('--install-root', default=r'C:\Program Files\Capstone')
    parser.add_argument('--data-root', default=r'C:\ProgramData\Capstone')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    out = Path(args.out);out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    report = {'tool': QUALIFICATION_VERSION, 'started_at': datetime.datetime.now().astimezone().isoformat(), 'host': os.environ.get('COMPUTERNAME'),
              'experiment_launched': False}
    active = active_experiments(args.data_root)
    if active:
        report.update(status='refused', reason='an experiment is active; run the qualification only when the lab is idle', active=active[:20])
        (out / f'qualification-{stamp}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'status': 'refused', 'active': len(active)}));return 2
    try:
        configure(args.install_root, args.data_root)
    except (OSError, KeyError, ValueError) as exc:
        report.update(status='installation_problem', reason=f'cannot read the Capstone configuration: {exc}')
        (out / f'qualification-{stamp}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'status': 'installation_problem', 'reason': str(exc)[:300]}));return 3
    from . import config
    from .ai_settings import Registry
    from .store import Store
    report['capstone_version'] = (Path(config.PROJECT_ROOT) / 'VERSION').read_text(encoding='utf-8').strip() if (Path(config.PROJECT_ROOT) / 'VERSION').exists() else None
    scratch = Path(tempfile.mkdtemp(prefix='capstone-qualification-'))
    store = Store(str(scratch / 'qualification.db'));store.routing_enabled = True
    registry = Registry(os.environ['CAPSTONE_AI_SETTINGS'])
    store.inference_registry = registry
    sections = {}
    plan = [('gemma_mediated', lambda: gemma_mediated(store, registry)),
            ('granite_structured_budgets', lambda: structured_budgets(store, registry, GRANITE)),
            ('reference_structured_budgets', lambda: structured_budgets(store, registry, REFERENCE)),
            *[(f'native_arguments:{m}', (lambda m=m: native_arguments(store, registry, m))) for m in NATIVE_MODELS],
            ('reviewer_calibration', lambda: reviewer_calibration(store, registry))]
    for name, step in plan:
        started = time.time()
        try:
            sections[name] = step()
        except Exception as exc:
            sections[name] = {'status': 'error', 'error': str(exc)[:500], 'trace': traceback.format_exc()[-1500:], 'passed': False}
        sections[name]['elapsed_seconds'] = round(time.time() - started, 1)
        print(json.dumps({'section': name, 'status': sections[name].get('status'), 'passed': sections[name].get('passed')}), flush=True)
    store.close()
    report.update(status='completed', finished_at=datetime.datetime.now().astimezone().isoformat(), sections=sections,
                  summary={k: ('skipped' if v.get('status') == 'skipped' else 'passed' if v.get('passed') else 'failed') for k, v in sections.items()},
                  interpretation='Request-compatibility qualification only. It does not establish model or methodology quality, '
                                 'and a skipped item is not a pass.')
    path = out / f'qualification-{stamp}.json'
    path.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    lines = [f'# Capstone lab qualification {stamp}', '', f"Tool {QUALIFICATION_VERSION}; Capstone {report.get('capstone_version')}; no experiment launched.", '']
    lines += [f'- {k}: {v}' for k, v in report['summary'].items()]
    (out / f'qualification-{stamp}.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'completed', 'results': str(path), 'summary': report['summary']}), flush=True)
    return 0 if all(v != 'failed' for v in report['summary'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
