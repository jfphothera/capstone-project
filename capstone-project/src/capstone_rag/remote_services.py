"""Optional authenticated clients for independently hosted lab business systems."""
import json,os,time,urllib.request
from pathlib import Path

CONFIG_ENVS={'qms':'CAPSTONE_QMS_SERVICE_CONFIG','erp':'CAPSTONE_ERP_SERVICE_CONFIG'}
# One switch for where QMS/ERP data comes from. 'local' (default) reads the
# project's own data/qms, qms_compatibility.json and the operational SQLite
# ERP tables. 'remote' uses the authenticated head services named by the
# config files above. A config file alone no longer turns remote mode on.
MODE_ENV='CAPSTONE_BUSINESS_SERVICES'
def mode():return 'remote' if os.environ.get(MODE_ENV,'local').strip().lower()=='remote' else 'local'
def configured(kind):return mode()=='remote' and bool(os.environ.get(CONFIG_ENVS[kind]))
def _config(kind):
    path=Path(os.environ[CONFIG_ENVS[kind]]);value=json.loads(path.read_text(encoding='utf-8-sig'))
    token_path=Path(value['token_file']);token_path=token_path if token_path.is_absolute() else path.parent/token_path
    return value,token_path.read_text(encoding='utf-8').strip()
def get(kind,path):
    value,token=_config(kind);started=time.perf_counter();context={};store=None
    try:
        from . import activity,task_context,telemetry
        store=activity.active_store();context=task_context.current() or {}
        if store:telemetry.emit(store,'tool.started',workflow_id=context.get('workflow_id'),task_id=context.get('task_id'),agent_id=context.get('agent_id'),correlation_id=context.get('task_id'),execution_node=value['execution_node'],tool_node=value['execution_node'],task_status='running',summary=f'{kind.upper()} remote request started',detail={'service':kind,'path':path})
        request=urllib.request.Request(value['url'].rstrip('/')+path,headers={'Authorization':'Bearer '+token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=30) as response:result=json.load(response)
        if store:telemetry.emit(store,'tool.completed',workflow_id=context.get('workflow_id'),task_id=context.get('task_id'),agent_id=context.get('agent_id'),correlation_id=context.get('task_id'),execution_node=value['execution_node'],tool_node=value['execution_node'],task_status='completed',summary=f'{kind.upper()} remote request completed',detail={'service':kind,'path':path,'duration_ms':round((time.perf_counter()-started)*1000,3)})
        return result
    except Exception as exc:
        if store:telemetry.emit(store,'tool.failed',workflow_id=context.get('workflow_id'),task_id=context.get('task_id'),agent_id=context.get('agent_id'),correlation_id=context.get('task_id'),execution_node=value.get('execution_node'),tool_node=value.get('execution_node'),task_status='failed',summary=f'{kind.upper()} remote request failed',detail={'service':kind,'path':path,'error_class':type(exc).__name__})
        raise
