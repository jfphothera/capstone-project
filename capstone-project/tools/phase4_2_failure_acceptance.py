"""Controlled coordinator fault injection, followed by real same-model failover.

This does not unplug a machine or claim a physical network interruption.
"""
import concurrent.futures
import json
import shutil
import tempfile
from pathlib import Path
from phase4_2_cluster_audit import audit,request
from capstone_rag import config,store,caseflow,agent
from capstone_rag.cluster_runtime import ClusterService
from capstone_rag.execution import ConcurrentExecutionEngine,TaskSpec,stable_task_key


def main():
    with tempfile.TemporaryDirectory(prefix='phase42-failover-') as temp:
        path=Path(temp)/'operations.db';shutil.copy2(config.DB_PATH,path)
        st=store.Store(str(path));service=ClusterService(st)
        service.configure({'addresses':['192.0.2.3','192.0.2.7']})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            observations=list(pool.map(lambda ip:audit(ip,90,'gemma3:270m'),['192.0.2.3','192.0.2.7']))
        workers=[service.enrollment.ingest(e) for e in observations]
        cid=caseflow.CaseFlow(st,agent.Agent(prefer_llm=False)).intake('Synthetic transport failover','My device serial is 98148469.')
        engine=ConcurrentExecutionEngine(st,max_concurrency=1,queue_capacity=4)
        task=engine.repo.create(TaskSpec('WF-FAILOVER',cid,'cluster_probe','inference','model.inference',
            'WF-FAILOVER',stable_task_key(cid,'failover'),required_model='gemma3:270m',
            model_eligibility=tuple(w['node_id'] for w in workers),timeout_seconds=90))
        attempts=[]
        def handler(current):
            worker=engine.scheduler.registry.get(current['assigned_worker_id'])
            attempts.append(worker['worker_id'])
            if current['attempt_count']==1:raise ConnectionError('Explicit coordinator fault injection before transport; physical node remains online')
            result=request(worker['endpoint'],'/api/generate',{'model':'gemma3:270m','prompt':'Return exactly READY.',
                'stream':False,'options':{'temperature':0,'num_predict':24,'num_ctx':1024}},90)
            return {'actual_remote_response':result,'endpoint':worker['endpoint'],'injected_failure':False}
        engine.run([task['task_id']],{'inference':handler})
        engine.run([task['task_id']],{'inference':handler})
        retired=workers[0]['node_id'];service.enrollment.set_state(retired,'retired')
        service.enrollment.reenable(retired)
        refreshed=service.enrollment.ingest(audit('192.0.2.3',90,'gemma3:270m'))
        trace={'fault_is_injected':True,'physical_disconnect_tested':False,'attempt_workers':attempts,
            'task':engine.repo.get(task['task_id']),'result':engine.repo.result(task['task_id']),
            'transitions':st.query('SELECT * FROM execution_task_transitions WHERE task_id=?',(task['task_id'],)),
            'attempts':st.query('SELECT * FROM execution_attempts WHERE task_id=?',(task['task_id'],)),
            'reenrolled_worker':refreshed}
        Path('docs/phase4_2/failure_recovery_trace.json').write_text(json.dumps(trace,indent=2,default=str))
        assert trace['task']['state']=='completed' and len(set(attempts))==2
        assert refreshed['available_models']==['gemma3:270m']
        print('Injected transport failure reassigned to a distinct qualified head; real generation completed; retired head requalified.')
        st.close()


if __name__=='__main__':main()
