"""Runnable Testing Lab categories and their release-availability contract."""
from contextlib import contextmanager
from pathlib import Path

CATEGORIES={
 'qualification':('Node/model connectivity and qualification','infrastructure',None),
 'performance':('Model inference performance','infrastructure',None),
 'specialist':('Specialist correctness','specialist',None),
 'case':('End-to-end synthetic cases','system',None),
 'concurrency':('Concurrent agent execution (local engineering)','engineering','tests/test_phase4_1_execution.py::test_two_and_three_tasks_overlap_under_resource_limit'),
 'dependency':('Dependencies and handoffs (local engineering)','engineering','tests/test_phase4_1_execution.py::test_task_transitions_dependencies_and_illegal_transition'),
 'switching':('Model loading/switching across selected profiles','infrastructure',None),
 'throughput':('Multi-node throughput across selected profiles','system',None),
 'recovery':('Failure and restart recovery (local engineering)','engineering','tests/test_phase4_1_execution.py::test_restart_recovery_and_duplicate_task_delivery_are_safe'),
 'qms_safety':('Safety and QMS revision traps (local engineering)','engineering','tests/test_qms_workflow_policy.py'),
 'validation':('Validator rejection/correction and case isolation','engineering','tests/test_phase4_1_graph_integration.py'),
 'hardware':('Hardware tiers across qualified cluster profiles','infrastructure',None),
}


def category_contract(project_root):
    """Return truthful execution availability for development and installed builds."""
    root=Path(project_root)
    rows=[]
    for key,(label,level,target) in CATEGORIES.items():
        available=True
        reason='Implemented by the isolated experiment worker.'
        if level=='engineering':
            available=bool(target and (root/target.split('::',1)[0]).is_file())
            reason=('Approved packaged engineering test target is present.' if available else
                    'Development-only: the approved engineering test target is not installed.')
        rows.append({'id':key,'label':label,'level':level,'implementation_status':
            'executable' if available else 'development_only','selectable':available,
            'runner':'pytest_isolated_subprocess' if level=='engineering' else 'isolated_experiment_worker',
            'target':target,'availability_reason':reason})
    return rows


# Telemetry identity of the READY probe (Agent Arena, rc.24): a Testing Lab
# participant, never one of the ticket workflow's agents (telemetry.AGENTS).
# Only telemetry uses it; the probe's request and result are unaffected.
PROBE_AGENT_ID='testing_lab_probe'
PROBE_AGENT_ROLE='lab-probe'
PROBE_DISPLAY_NAME='Testing Lab Probe'


def _register_probe(store):
    """Register the probe; a telemetry problem never stops the probe."""
    from . import telemetry
    try:
        telemetry.register(store,PROBE_AGENT_ID,PROBE_DISPLAY_NAME,PROBE_AGENT_ROLE,capabilities=('model.inference',),
            execution_constraints={'participant':'ready_probe','system_under_test':False})
    except Exception:
        pass


@contextmanager
def probe_identity(*,task_id,workflow_id,deadline_at=None):
    """Bind the probe's logical agent identity for its model call, as the
    reviewer does: inference.infer reads task_context, so the probe's
    inference.queued/started/heartbeat/completed/failed events carry agent_id
    testing_lab_probe (logical_agent_status 'known'), its task and workflow,
    replacing any enclosing context."""
    from . import task_context
    with task_context.bind(task_id=task_id,workflow_id=workflow_id,agent_id=PROBE_AGENT_ID,deadline_at=deadline_at):
        yield


def execute_probe(store,case_id,run,directory):
    import json,subprocess,sys,xml.etree.ElementTree as ET
    from pathlib import Path
    from . import config,inference
    from .ai_settings import http
    category=run.get('category','performance')
    if run['level']=='engineering':
        target=CATEGORIES[category][2]
        if not target:raise ValueError('Category has no approved engineering target')
        if not (Path(config.PROJECT_ROOT)/target.split('::',1)[0]).is_file():
            raise ValueError('Approved engineering target is not installed; this category is development-only')
        junit=directory/'engineering.xml'
        command=[sys.executable,'-m','pytest','-q',target,'--junitxml='+str(junit),'--basetemp='+str(directory/'test-temp')]
        process=subprocess.run(command,cwd=config.PROJECT_ROOT,capture_output=True,text=True,timeout=min(120,run['timeout_seconds']))
        (directory/'engineering.log').write_text(process.stdout+'\n'+process.stderr,encoding='utf-8')
        suite=ET.parse(junit).getroot().find('testsuite') if junit.exists() else None
        return {'structured_output_valid':suite is not None,'task_accuracy':process.returncode==0,
                'execution_class':'local_engineering','tests':dict(suite.attrib) if suite is not None else {},
                'command_target':target,'real_remote_inference':False}
    from . import telemetry
    import time
    agent=PROBE_AGENT_ID;workflow=run['experiment_id'];task=run['id']
    _register_probe(store)
    metadata={'task_source':'experiment_runs','category':category,'deadline_at':time.time()+run['timeout_seconds']}
    def event(kind,status):
        telemetry.emit(store,kind,case_id=case_id,workflow_id=workflow,task_id=task,agent_id=agent,
            correlation_id=task,task_status=status,summary='Testing Lab probe '+status,detail=metadata)
    event('task.created','queued');event('task.started','running')
    try:
        with probe_identity(task_id=task,workflow_id=workflow,deadline_at=metadata['deadline_at']),inference.session(store,run['id'],case_id=case_id,snapshot=run['profile']),inference.specialist('investigation'):
            response=inference.infer('/api/generate',{'prompt':'Return exactly the word READY.','think':False,
                'options':{'num_predict':64,'temperature':0,'num_ctx':1024}},run['timeout_seconds'])
    except Exception:
        event('task.failed','failed');raise
    else:event('task.completed','completed')
    actual=str(response.get('response','')).strip()
    return {'structured_output_valid':bool(response.get('done')),'task_accuracy':actual=='READY',
            'execution_class':'actual_remote_or_selected_endpoint_inference','expected':'READY','actual':actual,
            'model':response.get('model'),'timings':{k:v for k,v in response.items() if k.endswith('duration') or k.endswith('count')}}
