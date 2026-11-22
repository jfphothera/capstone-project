"""Run one isolated, model-backed Phase 2 collaboration trace."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from capstone_rag import agent, caseflow, config, store
from capstone_rag.ai_settings import Registry
from capstone_rag.collaboration import Coordinator


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'phase2' / 'model_smoke_trace.json'


def decoded(row):
    value = dict(row)
    for key in ('payload', 'result', 'missing_evidence', 'conflicts',
                'evidence_ids', 'permitted_tools', 'required_inputs',
                'authorization_context', 'dependency_ids', 'detail'):
        if key in value and isinstance(value[key], str):
            try:
                value[key] = json.loads(value[key])
            except ValueError:
                pass
    return value


def main():
    with tempfile.TemporaryDirectory(prefix='capstone-phase2-smoke-') as directory:
        root = Path(directory)
        db = root / 'operations.db'
        shutil.copy2(config.DB_PATH, db)
        registry = Registry(root / 'ai_configuration.db')
        registry.activate('gateway-default')
        operational = store.Store(str(db))
        operational.routing_enabled = True
        operational.inference_registry = registry
        specialist = agent.Agent(prefer_llm=True)
        flow = caseflow.CaseFlow(operational, specialist)
        case_id = flow.intake(
            'P4800 Rev B power collaboration check',
            'My name is Mary Cohen. Serial 68177-DV5. My P4800 will not power on.')
        result = flow.pre_investigate(case_id)
        run = operational.one(
            'SELECT * FROM workflow_runs WHERE case_id=? ORDER BY at DESC LIMIT 1',
            (case_id,))
        findings = [decoded(row) for row in operational.query(
            'SELECT * FROM specialist_findings WHERE case_id=? ORDER BY at', (case_id,))]
        payload = {
            'case_id': case_id, 'workflow_id': run['workflow_id'],
            'scenario': 'verified customer/device with inferred P4800 Rev B; family guidance exists but exact power coverage is absent',
            'result': result,
            'delegation': {key: [decoded(row) for row in rows]
                           for key, rows in Coordinator(operational, run['workflow_id']).trace(case_id).items()},
            'findings': findings,
            'inference_calls': [decoded(row) for row in operational.query(
                'SELECT * FROM inference_calls WHERE case_id=? ORDER BY at', (case_id,))],
            'retrieved_sources': operational.query(
                'SELECT * FROM retrieved_sources WHERE case_id=? ORDER BY at,rank', (case_id,)),
            'messages': operational.messages(case_id),
            'authority_note': 'Model outputs are advisory. Identity, revision, QMS applicability, movement, and approval remained deterministic.',
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2, default=str) + '\n', encoding='utf-8')
        print(json.dumps({
            'output': str(OUT),
            'tasks': len(payload['delegation']['tasks']),
            'findings': len(findings),
            'model_calls': len(payload['inference_calls']),
            'completed_model_calls': sum(call['status'] == 'completed' for call in payload['inference_calls']),
            'paused_for_review': result.get('paused_for_review'),
        }, indent=2))
        operational.close()


if __name__ == '__main__':
    main()
