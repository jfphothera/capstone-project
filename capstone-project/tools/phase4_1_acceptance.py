"""Generate isolated Phase 4.1 integration, correction, and inference evidence."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import time
from pathlib import Path

from capstone_rag import (agent, caseflow, config, independent_review, inference,
                          store as store_mod)
from capstone_rag.ai_settings import Registry
from capstone_rag.execution import ConcurrentExecutionEngine, TaskSpec, stable_task_key
from capstone_rag.scheduler import probe_configured_ollama_workers


OUT = Path('docs/phase4_1/phase4_1_acceptance_trace.json')


def decoded_verdict(row):
    result = dict(row)
    result['issues'] = json.loads(result['issues'])
    return result


with tempfile.TemporaryDirectory(prefix='phase4-1-acceptance-') as temp:
    db_path = Path(temp) / 'operations.db'
    shutil.copy2(config.DB_PATH, db_path)
    st = store_mod.Store(str(db_path))
    flow = caseflow.CaseFlow(st, agent.Agent(prefer_llm=False))
    case_id = flow.intake('Phase 4.1 synthetic acceptance',
        'My name is Ivan Nowak. Serial 98148469. The unit powers on but all lamps remain dark.')
    workflow = flow.pre_investigate(case_id)
    workflow_id = workflow['workflow_id']

    # Deliberate defect and genuine immutable successor correction.
    bad_id = 'PHASE41-BAD-DRAFT'
    st.insert('messages', {'message_id': bad_id, 'case_id': case_id,
        'run_id': workflow_id, 'created_at': '2099-01-01T00:00:00+00:00',
        'direction': 'outbound', 'channel': 'email', 'author': 'ai',
        'subject': 'Deliberately defective acceptance draft',
        'body': ('We confirmed that your device serial is 981:48-469, and the '
                 'requested replacement is approved and shipped.'),
        'status': 'draft', 'metadata': '{}'})
    state = {'qms': {'sources': [], 'evidence_contract': {
        'mandatory_context_chunk_ids': [], 'procedure_ids': []}},
        'planning': {'selected_action': 'human_review'}, 'result': {}}
    package_row, package = independent_review.freeze(st, case_id, workflow_id + '-CORRECTION', state, 0)
    rejected = independent_review.evaluate(st, package_row, package)
    correction = independent_review.correct(st, package, rejected, 1)
    package_row_2, package_2 = independent_review.freeze(
        st, case_id, workflow_id + '-CORRECTION', state, 1)
    corrected = independent_review.evaluate(st, package_row_2, package_2)

    registry = Registry()
    snapshot = registry.snapshot()
    workers = probe_configured_ollama_workers(st, snapshot, timeout=4)
    gpu = next((row for row in workers if row['node_id'] == 'gpu'), None)
    gpu_evidence = {'status': 'unavailable', 'reason': 'GPU endpoint was not healthy at execution time'}
    if gpu and gpu['health_state'] == 'healthy' and 'qwen3:4b' in gpu['available_models']:
        engine = ConcurrentExecutionEngine(st, max_concurrency=1, queue_capacity=4)
        task = engine.repo.create(TaskSpec(
            workflow_id + '-GPU', case_id, 'independent_validation', 'model.inference',
            'model.inference', workflow_id + '-GPU', stable_task_key(workflow_id, 'gpu-proof'),
            required_model='qwen3:4b', model_eligibility=('gpu',), timeout_seconds=90))
        gpu_snapshot = copy.deepcopy(snapshot)
        gpu_snapshot['id'] = 'phase4-1-gpu-proof'
        gpu_snapshot['name'] = 'Phase 4.1 GPU proof'
        gpu_snapshot['routes']['investigation'] = {
            'enabled': True, 'endpoint': 'gpu', 'model': 'qwen3:4b',
            'options': {'temperature': 0, 'num_ctx': 2048, 'num_predict': 24}}
        gpu_snapshot['configuration_hash'] = inference.fingerprint(gpu_snapshot)

        def gpu_handler(active_task):
            started = time.perf_counter()
            with inference.session(st, workflow_id + '-GPU', case_id=case_id,
                                   snapshot=gpu_snapshot), inference.specialist('investigation'):
                response = inference.infer('/api/generate', {
                    'prompt': 'Reply with exactly: PHASE41_GPU_OK', 'stream': False,
                    'think': False,
                    'options': {'temperature': 0, 'num_predict': 24}}, timeout=75)
            return {'model': response.get('model'),
                    'response': str(response.get('response') or '').strip()[:120],
                    'duration_ms': round((time.perf_counter() - started) * 1000, 3)}

        engine.run([task['task_id']], {'model.inference': gpu_handler})
        completed = engine.repo.get(task['task_id'])
        gpu_evidence = {'status': completed['state'], 'task': completed,
                        'result': engine.repo.result(task['task_id'])}

    execution_tasks = st.query(
        'SELECT * FROM execution_tasks WHERE workflow_id=? ORDER BY created_at', (workflow_id,))
    attempts = st.query(
        'SELECT a.* FROM execution_attempts a JOIN execution_tasks t ON t.task_id=a.task_id '
        'WHERE t.workflow_id=? ORDER BY a.started_at', (workflow_id,))
    initial_attempts = [row for row in attempts if st.one(
        'SELECT task_type FROM execution_tasks WHERE task_id=?', (row['task_id'],))['task_type'] != 'initial.join']
    intervals = [(row['started_at'], row['ended_at']) for row in initial_attempts]
    trace = {
        'generated_at': store_mod.now(), 'isolated_database': True,
        'synthetic_data_only': True, 'active_profile_unchanged': registry.active()['id'],
        'case_id': case_id, 'workflow_id': workflow_id,
        'workflow_result': workflow, 'execution_tasks': execution_tasks,
        'execution_attempts': attempts, 'initial_task_intervals': intervals,
        'scheduler_decisions': st.query(
            'SELECT * FROM scheduler_decisions WHERE workflow_id=? ORDER BY at', (workflow_id,)),
        'mcp_invocations': st.query(
            'SELECT * FROM mcp_tool_invocations WHERE workflow_id=? ORDER BY started_at', (workflow_id,)),
        'arena_events': st.query(
            'SELECT * FROM arena_events WHERE workflow_id=? ORDER BY sequence', (workflow_id,)),
        'correction': {
            'original_message_id': bad_id, 'rejected_verdict': decoded_verdict(rejected),
            'correction': correction,
            'corrected_message': st.one('SELECT * FROM messages WHERE message_id=?',
                                        (correction['corrected_artifact_id'],)),
            'corrected_verdict': decoded_verdict(corrected),
            'validation_records': st.query(
                'SELECT * FROM artifact_validation_records WHERE workflow_id=? ORDER BY cycle',
                (workflow_id + '-CORRECTION',)),
        },
        'workers': workers, 'gpu_inference': gpu_evidence,
        'limitations': ['Physical CPU nodes .3-.7 were not contacted or claimed as deployed.'],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(trace, indent=2, default=str), encoding='utf-8')
    st.close()

print(OUT)
