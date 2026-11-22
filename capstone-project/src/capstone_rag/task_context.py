"""Evidence-backed task correlation shared by orchestration and inference."""
import contextvars
from contextlib import contextmanager

_TASK=contextvars.ContextVar('capstone_execution_task',default=None)

def current():return _TASK.get()

@contextmanager
def bind(*,task_id,workflow_id,agent_id,deadline_at=None,attempt=None):
    token=_TASK.set({'task_id':task_id,'workflow_id':workflow_id,'agent_id':agent_id,
                     'deadline_at':deadline_at,'attempt':attempt})
    try:yield current()
    finally:_TASK.reset(token)

@contextmanager
def delegated_liveness(store,task_id,workflow_id,agent_id):
    import threading
    from . import telemetry
    stop=threading.Event()
    def heartbeat():
        while not stop.wait(30):
            with store.transaction():
                row=store.one('SELECT status FROM delegated_tasks WHERE task_id=?',(task_id,))
                if not row or row['status']!='running':return
                telemetry.emit(store,'task.heartbeat',task_id=task_id,workflow_id=workflow_id,agent_id=agent_id,
                    correlation_id=task_id,task_status='running',summary='Specialist worker remains active',
                    detail={'lease_seconds':60,'liveness_source':'executing_specialist_process'})
    thread=threading.Thread(target=heartbeat,name='specialist-liveness',daemon=True);thread.start()
    try:yield
    finally:stop.set()
