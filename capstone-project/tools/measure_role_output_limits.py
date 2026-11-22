"""Measure structured role outputs against their output budgets (rc.25, T2).

Reads the three captured exports READ-ONLY (selective parsing; nothing under
outputs\\ is written):

* outputs/experiment-evaluation-297fb3ed/trial-cache/*.json  (baseline, rc.22)
* outputs/experiment-evaluation-c922/calls.json               (partial, 4 models)
* outputs/experiment-evaluation-f253/trial-cache/*.json        (rc.24)

Per export x contract x model it counts: calls; finished by the output limit
(done_reason/finish_reason 'length' = TRUNCATED); not JSON; JSON missing
required fields; output tokens p50/p95/max against the requested budget; and
for every string field the p50/p95/p99/max length of valid outputs. Writes
JSON and Markdown to the output
directory given (default docs/testing_lab/final-iteration/measurements).
"""
from __future__ import annotations

import glob
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
FIRST_KEY = re.compile(r'^\s*\{\s*"([A-Za-z_]+)"')


def _json(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def contract_signatures():
    from capstone_rag import role_contracts
    sigs = {}
    for name, c in role_contracts.CONTRACTS.items():
        if c.get('schema'):
            sigs[frozenset(c['schema']['properties'])] = name
    sigs[frozenset({'preferred_action', 'plans'})] = 'compare_plans'
    return sigs


LEGACY_EXTRA = {frozenset({'proceed', 'weakness', 'recommended_check'}): 'reflect_on_plan',
                frozenset({'issues', 'strongest_objection'}): 'critique_draft'}
FIRST_KEY_CONTRACT = {'relevance': 'qms_coverage_summary', 'query': 'qms_search_query', 'question': 'peer_challenge', 'answer': 'peer_reply',
                      'continue': 'supervise_finding', 'proceed': 'reflect_on_plan', 'issues': 'critique_draft', 'proceed_to_validator': 'judge_debate',
                      'clear': 'review_clarity', 'passed': 'validate_draft', 'intent': 'interpret_request', 'suggested_account_id': 'erp_identity_advisory',
                      'preferred_action': 'compare_plans', 'plans': 'compare_plans', 'reply': 'customer_composition', 'name': 'tool_selection',
                      'decision': 'lab_reviewer'}


def classify(call, sigs):
    payload = _json(call.get('payload')) or {}
    request = payload.get('request') or {}
    response = payload.get('response') if payload else call.get('response')
    response = _json(response) if not isinstance(response, dict) else response
    content = ((response or {}).get('message') or {}).get('content') if isinstance(response, dict) else None
    if content is None and isinstance(call.get('response'), str):
        content = call['response']
    finish = (payload.get('finish_reason') or (response or {}).get('done_reason') if isinstance(response, dict) else None) or call.get('finish_reason')
    tokens = (payload.get('timings') or {}).get('eval_count') if payload else call.get('output_tokens')
    if tokens is None and isinstance(response, dict):
        tokens = response.get('eval_count')
    budget = payload.get('requested_num_predict') or ((call.get('options') or {}).get('num_predict') if isinstance(call.get('options'), dict) else None)
    fmt = request.get('format') if isinstance(request, dict) else None
    contract = None
    if isinstance(fmt, dict) and fmt.get('properties'):
        props = frozenset(fmt['properties'])
        contract = sigs.get(props) or LEGACY_EXTRA.get(props) or ('lab_reviewer' if 'decision' in props else 'schema:' + ','.join(sorted(props))[:60])
    parsed = _json(content) if isinstance(content, str) else None
    if contract is None and isinstance(parsed, dict) and parsed:
        props = frozenset(parsed)
        contract = sigs.get(props) or LEGACY_EXTRA.get(props) or FIRST_KEY_CONTRACT.get(next(iter(parsed)))
    if contract is None and isinstance(content, str):
        m = FIRST_KEY.match(content)
        contract = FIRST_KEY_CONTRACT.get(m.group(1)) if m else None
    if contract is None:
        return None
    required = (fmt or {}).get('required') if isinstance(fmt, dict) else None
    return {'contract': contract, 'role': call.get('role'), 'model': call.get('model'), 'finish': finish, 'tokens': tokens, 'budget': budget,
            'parsed': parsed if isinstance(parsed, dict) else None, 'content': content or '', 'required': required,
            'status': call.get('status'), 'error': payload.get('error') if payload else call.get('error')}


def calls_from(export):
    base = ROOT / 'outputs' / export
    if export == 'experiment-evaluation-c922':
        for call in json.loads((base / 'calls.json').read_text(encoding='utf-8')):
            yield call
        return
    for path in sorted(glob.glob(str(base / 'trial-cache' / '*.json'))):
        for call in (json.loads(Path(path).read_text(encoding='utf-8')).get('calls') or []):
            yield call


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def main(out_dir=None):
    from capstone_rag import role_contracts
    sigs = contract_signatures()
    rows = defaultdict(lambda: {'calls': 0, 'truncated': 0, 'not_json': 0, 'missing_required': 0, 'tokens': [], 'budgets': set(), 'fields': defaultdict(list)})
    for export in ('experiment-evaluation-297fb3ed', 'experiment-evaluation-c922', 'experiment-evaluation-f253'):
        for call in calls_from(export):
            row = classify(call, sigs)
            if not row:
                continue
            cell = rows[(export, row['contract'], row['model'])]
            cell['calls'] += 1
            if row['finish'] == 'length':
                cell['truncated'] += 1
            if row['parsed'] is None and row['content'].strip():
                cell['not_json'] += 1
            elif row['parsed'] is not None and row['required'] and not set(row['required']) <= set(row['parsed']):
                cell['missing_required'] += 1
            if isinstance(row['tokens'], int):
                cell['tokens'].append(row['tokens'])
            if row['budget']:
                cell['budgets'].add(int(row['budget']))
            if row['parsed'] is not None and row['finish'] != 'length':
                for key, value in row['parsed'].items():
                    if isinstance(value, str):
                        cell['fields'][key].append(len(value))
                    elif isinstance(value, list):
                        cell['fields'][key + '[]'].extend(len(v) for v in value if isinstance(v, str))
    report = []
    for (export, contract, model), cell in sorted(rows.items()):
        report.append({'export': export, 'contract': contract, 'model': model, 'calls': cell['calls'], 'truncated': cell['truncated'],
                       'not_json': cell['not_json'], 'missing_required': cell['missing_required'],
                       'truncated_rate': round(cell['truncated'] / cell['calls'], 3) if cell['calls'] else None,
                       'budgets': sorted(cell['budgets']), 'output_tokens': {'p50': pct(cell['tokens'], .5), 'p95': pct(cell['tokens'], .95), 'max': max(cell['tokens']) if cell['tokens'] else None},
                       'fields': {k: {'n': len(v), 'p50': pct(v, .5), 'p95': pct(v, .95), 'p99': pct(v, .99), 'max': max(v)}
                                  for k, v in cell['fields'].items() if v}})
    out = Path(out_dir or ROOT / 'docs' / 'testing_lab' / 'final-iteration' / 'measurements')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'role-output-limits.json').write_text(json.dumps(report, indent=1) + '\n', encoding='utf-8')
    by_contract = defaultdict(lambda: [0, 0, 0, 0])
    for r in report:
        c = by_contract[(r['contract'], r['model'])]
        c[0] += r['calls'];c[1] += r['truncated'];c[2] += r['not_json'];c[3] += r['missing_required']
    lines = ['# Structured role outputs vs output budgets (three exports, read-only)', '',
             '| Contract | Model | Calls | Truncated (length) | Not JSON | Missing required |', '|---|---|---:|---:|---:|---:|']
    for (contract, model), (n, t, j, m) in sorted(by_contract.items()):
        lines.append(f'| {contract} | {model} | {n} | {t} ({t / n:.0%}) | {j} | {m} |')
    lines += ['', '## rc.25 output budgets (tokens) set from these counts', '', '| Contract | rc.24 | rc.25 | Evidence |', '|---|---:|---:|---|']
    for name, (old, new, why) in role_contracts.BUDGET_EVIDENCE.items():
        lines.append(f'| {name} | {old} | {new} | {why} |')
    lines += ['', 'All other contracts keep their rc.24 budgets: none of their calls finished at the limit.',
              'Prose fields no longer carry a hard maxLength: the grammar cut text mid-sentence at the cap (rc.24 reviewer',
              'rationales end at exactly 400 characters). A cut-off answer is the category `truncated`, never a wrong decision.',
              'Counts are per classified structured call, including the one bounded repair retry, so they differ slightly from',
              'per-role counts in the evaluation documents. rc.25 numbers do not exist yet: the lab qualification tool measures',
              'Granite and Qwen QMS and peer contracts at the new budgets.']
    (out / 'role-output-limits.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'cells': len(report), 'written': str(out)}))
    return report


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
