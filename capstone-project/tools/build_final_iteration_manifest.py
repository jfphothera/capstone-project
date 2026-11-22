"""Write docs/testing_lab/final-iteration/addendum-manifest.json (rc.25, U2).

Links the rc.25 addendum, ledger and re-evaluations to the ORIGINAL evidence by
path and SHA-256. Reads outputs\\ read-only; never writes there.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
ORIGINALS = ['outputs/model-and-methodology-evaluation/Capstone model and methodology evaluation.md',
             'outputs/model-and-methodology-evaluation/experiment-evidence-manifest.json',
             'outputs/experiment-evaluation-f253/Four model evaluation and revision loop findings.md',
             'outputs/experiment-evaluation-f253/Problems ownership and experiment decisions.md',
             'outputs/experiment-evaluation-f253/disputed-full-pass.json',
             'outputs/experiment-evaluation-f253/compartment-review-disputes.json',
             'outputs/experiment-evaluation-f253/revision-case-studies.json',
             'outputs/experiment-evaluation-f253/experiment-manifest.json',
             'outputs/experiment-evaluation-c922/Confirmed tool routing and preflight findings.md']


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def main(package=None):
    from capstone_rag import agentic_evaluator, agentic_methodologies, agentic_reviewer, agentic_scenarios, experiments, revision
    folder = ROOT / 'docs' / 'testing_lab' / 'final-iteration'
    value = {'manifest': 'capstone.final-iteration-addendum/1', 'release': (ROOT / 'VERSION').read_text().strip(),
             'source_fingerprint': experiments.source_revision(),
             'observed_source_before': 'e98e7b993b7809c97610cad30b436032ca6f6230fdadf0ebd4c3bc739e103aac',
             'versions': {'method_registry': agentic_methodologies.CURRENT_REGISTRY, 'legacy_method_registry': agentic_methodologies.REGISTRY_V1,
                          'evaluator': agentic_evaluator.EVALUATOR_VERSION, 'reviewer_policy': agentic_reviewer.REVIEWER_POLICY_VERSION,
                          'scenario_set': agentic_scenarios.SCENARIO_SET_VERSION, 'revision': revision.REVISION_VERSION},
             'originals_unchanged': [{'path': p, 'sha256': sha(ROOT / p)} for p in ORIGINALS if (ROOT / p).exists()],
             'addendum_documents': [{'path': p.relative_to(ROOT).as_posix(), 'sha256': sha(p)} for p in sorted(folder.rglob('*'))
                                    if p.is_file() and p.name != 'addendum-manifest.json'],
             'supersession': {'recommendations': 'EVALUATION-ADDENDUM-rc25.md (2026-11-19) supersedes the earlier exclusion wording; the earlier text is retained in the original living record',
                              'scores': 'no historical score changed; re-evaluations are separate records under reevaluations/'}}
    if package:
        value['release_package'] = {'path': str(package), 'sha256': sha(package), 'bytes': Path(package).stat().st_size}
    (folder / 'addendum-manifest.json').write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'written': str(folder / 'addendum-manifest.json'), 'source_fingerprint': value['source_fingerprint']}))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
