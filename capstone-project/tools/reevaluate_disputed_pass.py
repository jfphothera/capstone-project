"""Versioned re-evaluation of a historical Agentic Testing result (rc.25, C6 / Phase 0).

Reads captured evidence READ-ONLY and writes a separate re-evaluation record.
The original score is never changed: the record links to it by path and
SHA-256 and states the rule set (version + hash) that produced the new view.

Usage:
    python tools/reevaluate_disputed_pass.py outputs/experiment-evaluation-f253/disputed-full-pass.json \
        docs/testing_lab/final-iteration/reevaluations
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main(source, target_dir):
    from capstone_rag import agentic_evaluator, lab_guards
    source = Path(source);target_dir = Path(target_dir);target_dir.mkdir(parents=True, exist_ok=True)
    raw = source.read_bytes()
    evidence = json.loads(raw)
    run = evidence['run']
    ledger = source.parent / 'trial-ledger-compact.json'
    if ledger.exists() and run.get('correct') is None:
        row = next((r for r in json.loads(ledger.read_text(encoding='utf-8')) if r.get('id') == run['id']), {})
        run = {**run, **{k: row.get(k) for k in ('correct', 'objective', 'terminal') if row.get(k) is not None}}
    inbound = [m for m in evidence['messages'] if m.get('direction') == 'inbound']
    latest = inbound[-1] if inbound else {}
    body = latest.get('body') or ''
    confirmed, contradicted = bool(lab_guards.CONFIRM.search(body)), bool(lab_guards.CONTRADICT.search(body))
    rules = inspect.getsource(lab_guards.confirmation_evidence) + inspect.getsource(agentic_evaluator.customer_confirmation)
    record = {
        'record_type': 'capstone.reevaluation/1',
        'reevaluation_id': 'REEVAL-' + run['id'],
        'original': {'trial_id': run['id'], 'experiment_id': 'EXP-f253c55fa55c4d9893c70bd14e41398d', 'source_revision': run.get('source_revision'),
                     'evidence_path': str(source.as_posix()), 'evidence_sha256': hashlib.sha256(raw).hexdigest(),
                     'recorded_terminal': run.get('terminal'), 'recorded_correct': run.get('correct'), 'recorded_objective': run.get('objective'),
                     'note': 'Historical score retained unchanged; this record is a separate, versioned view.'},
        'rules': {'evaluator_version': agentic_evaluator.EVALUATOR_VERSION, 'guard_version': lab_guards.GUARD_VERSION,
                  'rules_sha256': hashlib.sha256(rules.encode()).hexdigest()},
        'finding': {'latest_customer_message_id': latest.get('message_id'), 'confirms_fix': confirmed, 'reports_original_symptoms': contradicted,
                    'customer_confirmation': 'contradictory' if confirmed and contradicted else 'confirmed' if confirmed else 'not_confirmed'},
        'reevaluated': {'customer_confirmed': confirmed and not contradicted,
                        'objective_achieved': False if contradicted else None,
                        'complete_case_correct': False if contradicted else None,
                        'blocker': 'customer evidence contradicts the recorded fix' if contradicted else None},
        'caveat': ('The contradiction was produced by the pre-rc.25 customer simulator (fixed state set, then the original symptom '
                   'answers appended in the same turn). It is a harness defect, not a model or methodology result; '
                   'replicate under rc.25 before drawing any conclusion.'),
    }
    out = target_dir / f"{record['reevaluation_id']}.json"
    out.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'written': str(out), 'customer_confirmation': record['finding']['customer_confirmation']}))
    return record


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
