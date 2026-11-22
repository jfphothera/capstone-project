"""Real physical inference consumed by the existing isolated case workflow."""
import concurrent.futures
import os
import json
import shutil
import tempfile
from pathlib import Path

from phase4_2_cluster_audit import audit
from capstone_rag import agent,caseflow,config,store
from capstone_rag.ai_settings import Registry
from capstone_rag.cluster_runtime import ClusterService


def main():
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    out=Path('docs/phase4_2');out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='phase42-case-') as temp:
        path=Path(temp)/'operations.db';shutil.copy2(config.DB_PATH,path)
        st=store.Store(str(path));service=ClusterService(st);st.cluster_service=service
        st.inference_registry=Registry(str(Path(temp)/'settings.db'))
        addresses=['192.0.2.3','192.0.2.6','192.0.2.7']
        service.configure({'addresses':addresses})
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            evidence=list(pool.map(lambda a:audit(a,90,'gemma3:270m'),addresses))
        workers=[service.enrollment.ingest(e) for e in evidence]
        routes={role:{'model':'gemma3:270m','nodes':[worker['node_id']]}
                for role,worker in zip(('erp_research','ticket_interpretation','independent_validation'),workers)}
        service.configure({'addresses':addresses,'workflow_routes':routes,'interval_seconds':60})
        flow=caseflow.CaseFlow(st,agent.Agent(prefer_llm=False))
        cid=flow.intake('Distributed synthetic support acceptance',
             'My name is Ivan Nowak. Serial 98148469. My device lamps are dark. Please help.')
        result=flow.pre_investigate(cid)
        tasks=st.query("SELECT * FROM execution_tasks WHERE task_type LIKE 'cluster.%'")
        calls=st.query('SELECT * FROM inference_calls WHERE case_id=?',(cid,))
        trace={'synthetic_only':True,'isolated_database':True,'qualification':evidence,
            'workflow_result':result,'remote_tasks':tasks,'model_calls':calls,
            'decisions':st.query('SELECT * FROM scheduler_decisions'),
            'arena_events':st.query('SELECT * FROM arena_events ORDER BY sequence'),
            'case':st.case(cid),'mcp_invocations':st.query('SELECT * FROM mcp_tool_invocations')}
        (out/'distributed_case_trace.json').write_text(json.dumps(trace,indent=2,default=str))
        print('Remote task states:',[(t['logical_agent_id'],t['state'],t['assigned_worker_id']) for t in tasks])
        print('Physical endpoints:',[json.loads(c['payload'])['endpoint_url'] for c in calls])
        assert len(tasks)==3 and all(t['state']=='completed' for t in tasks)
        assert len({json.loads(c['payload'])['endpoint_url'] for c in calls})==3
        assert all(c['status']=='completed' for c in calls)
        st.close()


if __name__=='__main__':main()
