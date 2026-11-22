"""Testing Lab UI fixture server (no Gateway, no heads, no model calls).

Serves the real Capstone web app against a stand-in Gateway inventory and a
seeded 256-run Agentic Evaluation campaign whose runs advance every couple of
seconds, so the Results screen can be checked under live updates.

    python tools/lab_ui_fixture_server.py [--port 8765]

All state lives in a temporary directory that is deleted on exit.
"""
import argparse
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
STATE = Path(tempfile.mkdtemp(prefix='capstone-lab-fixture-'))
os.environ['CAPSTONE_AI_SETTINGS'] = str(STATE / 'ai_configuration.db')
os.environ['CAPSTONE_DB'] = str(STATE / 'operations.db')
os.environ.setdefault('CAPSTONE_GATEWAY_CLIENT_CONFIG', str(STATE / 'unused-gateway.json'))

from capstone_rag import gateway_transport  # noqa: E402

MODELS = ['qwen3.5:4b', 'qwen3.5:2b']  # seeded campaign runs
# Inventory offered by the stand-in Gateway (includes an excluded model and
# models with ratings/tool labels so the Run screen labels can be checked).
INVENTORY = MODELS + ['llama3.2:3b', 'qwen3:8b', 'qwen3:4b', 'granite4:3b', 'gemma3:4b', 'deepseek-r1:8b']


def seed_agentic(manager, rng):
    """FIXTURE Agentic Testing campaign (random outcomes; not model evidence)."""
    from capstone_rag import agentic_store
    eid = 'EXP-UI-AGENTIC'
    manager.save({'id': eid, 'name': 'Agentic Testing', 'status': 'completed', 'created_at': time.time() - 600,
                  'concurrency': 4, 'planned_runs': 12, 'failure_limit': 12, 'approval_mode': 'agentic_reviewer'})
    for i, (model, template) in enumerate((m, t) for m in ('qwen3.5:4b', 'llama3.2:3b') for t in ('power-fault', 'missing-goggles', 'burning-smell') for _ in range(2)):
        terminal = rng.choice(['resolved', 'resolved', 'escalated', 'limit_reached', 'closed_unresolved'])
        run = {'id': f'RUN-AG-{i:03d}', 'experiment_id': eid, 'status': 'completed', 'approval_mode': 'agentic_reviewer', 'duration_ms': rng.randint(60000, 400000),
               'scenario': {'id': f'agentic:{template}:cooperative'}, 'architecture': {'id': 'method-A1'},
               'profile': {'routes': {'investigation': {'model': model, 'tool_strategy': 'native'}, 'lab_reviewer': {'model': 'qwen3:4b', 'policy_version': 'fixture'}}},
               'metrics': {'model_calls': rng.randint(8, 20), 'routing_audit': {'routing_verified': True, 'prompt_tokens_observed': 4000, 'output_tokens_observed': 900},
                           'agentic': {'scenario': f'agentic:{template}:cooperative', 'template': template, 'behaviour': 'cooperative',
                                       'outcome': {'terminal': terminal, 'disposition_correct': terminal in ('resolved', 'escalated'), 'action_correct': True,
                                                   'customer_turns': rng.randint(1, 4)},
                                       'complete_case_correct': terminal == 'resolved', 'completed': terminal != 'limit_reached',
                                       'released_harm': [], 'unsafe_proposals_stopped': [{'kind': 'fixture'}] if rng.random() < .3 else [],
                                       'reviewer': {'decisions': 4, 'errors': [], 'over_caution': []}, 'system_errors': [],
                                       'role_steps': {'investigation': {'passed': rng.random() < .7, 'calls': 2, 'executed_models': [model], 'duration_ms': 3000},
                                                      'resolution_planning': {'passed': rng.random() < .6, 'calls': 1, 'executed_models': [model], 'duration_ms': 4000}},
                                       'roles_not_exercised': ['supervisor', 'peer_reviewer'], 'tool_interactions': []}}}
        manager._store_run(run)
        agentic_store.record(manager.registry, run, evidence_class='fixture')


class GatewayStandIn:
    def models(self):
        details = {name: {'qualified_operations': ['completion'], 'size_bytes': 2_500_000_000 if ':8b' not in name else 5_200_000_000,
                          'execution_policy': {'max_context': 8192}} for name in INVENTORY}
        return {'contract': 'shared.inference/1', 'resources': [
            {'resource_id': f'head-192-0-2-{n}', 'status': 'healthy', 'models': list(INVENTORY), 'model_details': details}
            for n in range(2, 11)]}

    def policy(self):
        return {'application_id': 'capstone-research'}

    def availability(self, *args, **kwargs):
        return {'status': 'available'}


gateway_transport.enabled = lambda: True
gateway_transport.client = lambda workload='research': GatewayStandIn()

from capstone_rag import experiments  # noqa: E402
from capstone_rag.ai_settings import Registry  # noqa: E402
from capstone_rag.webapp.app import create_app  # noqa: E402

EID = 'EXP-UI-FIXTURE'


def seed(manager):
    manager.save({'id': EID, 'name': 'Agentic Evaluation', 'status': 'running', 'created_at': time.time(),
                  'concurrency': 20, 'planned_runs': 256, 'failure_limit': 256, 'approval_mode': 'automated'})
    number = 0
    for case in ('fuzzy-identity', 'qms-technical', 'ambiguous-accessory', 'customer-nonresponse'):
        for model in MODELS:
            for topology in 'ABCDEFGH':
                for treatment in '1234':
                    number += 1
                    manager._store_run({'id': f'RUN-{number:04d}', 'experiment_id': EID, 'status': 'queued',
                        'directory': str(STATE / 'none'), 'scenario': {'id': case},
                        'profile': {'routes': {'investigation': {'model': model}}},
                        'architecture': {'id': f'method-{topology}{treatment}', 'methodology': {}},
                        'approval_mode': 'automated', 'queued_at': time.time() + number})


def seed_screening(registry, rng):
    """FIXTURE screening attempts so the Compatibility matrix can be checked.
    Not model evidence: outcomes are random draws."""
    from capstone_rag import lab_evidence, role_contracts, role_screening, screening_store
    for model in ('fixture-small:2b', 'fixture-mid:4b', 'fixture-large:8b'):
        for task in role_screening.TASKS:
            if rng.random() < .15:
                continue  # leave some cells partially tested
            draw = rng.random()
            status, accurate, error = ('failed', False, 'Unterminated string starting at: line 1 column 90') if draw < .1 else \
                ('completed', draw > .3, None)
            calls = [{'call_id': 'C', 'role': task['role'], 'requested_model': model, 'model': model, 'status': 'completed',
                      'model_digest': 'sha256:fixture', 'timings': {'done_reason': 'length' if error else 'stop', 'prompt_eval_count': rng.randint(300, 2400)},
                      'budget_check': {'input_tokens_estimate': rng.randint(300, 2400)}}]
            screening_store.record(registry, {
                'id': f'RUN-SCREEN-{model}-{task["id"]}', 'experiment_id': 'EXP-UI-SCREEN', 'level': 'role_screen',
                'status': status, 'error': error, 'scenario': role_screening.scenario(task),
                'profile': {'routes': {task['role']: {'model': model, 'options': {'num_ctx': 8192, 'num_predict': 512}}}},
                'metrics': {'routing_audit': lab_evidence.call_audit(calls, {task['role']: {'model': model}})},
                'duration_ms': rng.randint(2000, 60000),
                'specialist_result': {'task_accuracy': accurate, 'structured_output_valid': True,
                                      'checks': {'fixture_check': accurate}, 'output': {'fixture': True},
                                      'screening': {'contract_hash': role_contracts.contract_hash(task['role'])}}})


def advance(manager, rng):
    """Every 2 s: finish some running runs, start queued ones up to 20."""
    while True:
        time.sleep(2)
        rows = manager.rows(EID)
        running = [r for r in rows if r['status'] == 'running']
        for run in rng.sample(running, min(len(running), rng.randint(2, 6))):
            outcome = rng.random()
            status = 'failed' if outcome < .08 else 'paused' if outcome < .12 else 'completed'
            run.update(status=status, duration_ms=rng.randint(20000, 240000), attempt_finished_at=time.time(),
                metrics={'model_calls': rng.randint(4, 14), 'all_scored_checks_passed': rng.random() < .6,
                         'critical_violations': [], 'case_disposition': rng.choice(['AI Reviewed', 'Human Review Needed']),
                         'communication_metrics': {'agent_handoffs': rng.randint(0, 6)},
                         'inference_correlations': [{'resource_id': f'head-192-0-2-{rng.randint(2, 10)}'}]})
            if status == 'failed':
                run['error'] = 'Gateway execution failed: timed out'
            if status == 'paused':
                run['pending'] = {'emails': [{'message_id': run['id'] + '-M', 'body': 'Synthetic draft awaiting review.'}]}
            manager._store_run(run)
        running = [r for r in manager.rows(EID) if r['status'] == 'running']
        queued = sorted((r for r in manager.rows(EID) if r['status'] == 'queued'), key=lambda r: r['queued_at'])
        for run in queued[:max(0, 20 - len(running))]:
            run.update(status='running', attempt_started_at=time.time())
            manager._store_run(run)
        if not queued and not running:
            value = manager.registry.get('experiments', EID)
            value['status'] = 'completed_with_failures'
            manager.save(value)
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    manager = experiments.Manager(Registry(os.environ['CAPSTONE_AI_SETTINGS']))
    seed(manager)
    seed_screening(manager.registry, random.Random(11))
    seed_agentic(manager, random.Random(5))
    threading.Thread(target=advance, args=(manager, random.Random(7)), daemon=True).start()
    app = create_app(start_workers=False, developer_token='lab-ui-fixture')
    import uvicorn
    try:
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
    finally:
        shutil.rmtree(STATE, ignore_errors=True)


if __name__ == '__main__':
    main()
