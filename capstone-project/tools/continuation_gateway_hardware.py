"""Bounded physical-head contention through ONE staged capacity authority.

Independent synthetic applications run in separate processes over the public
HTTP contract. This proves gateway serialization, not raw-network bypass control.
"""
import argparse,json,os,secrets,subprocess,sys,tempfile,threading,time,sqlite3
from contextlib import closing
from pathlib import Path
from capstone_rag.capacity_gateway import Gateway
from capstone_rag.capacity_gateway_server import Server

def main():
    p=argparse.ArgumentParser();p.add_argument('--endpoint',required=True);p.add_argument('--resource-id',required=True)
    p.add_argument('--model',required=True);p.add_argument('--embedding-model');p.add_argument('--case-workflow',action='store_true');a=p.parse_args()
    root=Path(__file__).resolve().parents[1];out=root/'docs/phase4_2_continuation';out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gateway-hardware-') as temp:
        folder=Path(temp);gateway=Gateway(folder/'capacity.db')
        apps=['test-ticketing','test-coder','test-third']+(['capstone'] if a.case_workflow else []);tokens={app:secrets.token_urlsafe(32) for app in apps}
        for app in apps:gateway.broker.register_client(app,tokens[app],models=[a.model]+([a.embedding_model] if a.embedding_model else []),max_concurrency=1)
        gateway.broker.authorize_resource(a.resource_id,endpoint=a.endpoint,resource_type='shared_inference',owner='explicit hardware acceptance',
            applications=apps,permitted_uses=['inference'])
        qualifications=[gateway.qualify(a.resource_id,a.model,operator='authorized acceptance',max_context=2048)]
        if a.embedding_model:qualifications.append(gateway.qualify(a.resource_id,a.embedding_model,operator='authorized acceptance',purpose='embedding'))
        if not all(q['passed'] for q in qualifications):
            (out/'physical_gateway_trace.json').write_text(json.dumps({'qualification':qualifications,'acceptance':'qualification failed'},indent=2))
            raise RuntimeError('Physical model qualification failed; no client work was admitted')
        server=Server(('127.0.0.1',0),gateway);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        processes=[];start=time.time()+3
        try:
            for app in apps[:3]:
                bootstrap=folder/(app+'.json');output=folder/(app+'-result.json')
                bootstrap.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1','service_id':'shared-inference',
                    'url':'http://127.0.0.1:'+str(server.server_port),'application_id':app,'token_environment':'ACCEPTANCE_CLIENT_TOKEN'}))
                env=os.environ.copy();env['ACCEPTANCE_CLIENT_TOKEN']=tokens[app]
                env['PYTHONPATH']=str(root/'src');env['HF_HUB_OFFLINE']='1';env['TRANSFORMERS_OFFLINE']='1'
                child=subprocess.Popen([sys.executable,str(root/'scripts/clients/gateway_probe.py'),'--bootstrap',str(bootstrap),
                    '--model',a.model,'--resource',a.resource_id,'--output',str(output),'--start-at',str(start)],
                    env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                processes.append((child,output))
            clients=[]
            for child,output in processes:
                stdout,stderr=child.communicate(timeout=90)
                if child.returncode:raise RuntimeError('Independent acceptance client failed: '+stderr.decode(errors='replace')[:500])
                clients.append(json.loads(output.read_text()))
            with gateway.broker.connect() as db:
                requests=[dict(r) for r in db.execute('SELECT * FROM capacity_requests')]
                events=[dict(r) for r in db.execute('SELECT * FROM capacity_events ORDER BY event_id')]
            intervals=sorted((c['result']['result']['started_at'],c['result']['result']['finished_at']) for c in clients)
            serialized=all(intervals[i][1]<=intervals[i+1][0] for i in range(len(intervals)-1))
            demand_overlap=max(c['started_at'] for c in clients)<min(c['finished_at'] for c in clients)
            trace={'physical_ollama':True,'independent_synthetic_client_processes':True,'qualification':qualifications,
                'clients':clients,'requests':requests,'events':events,'serialized_model_intervals':serialized,
                'overlapping_client_demand':demand_overlap,'raw_ollama_bypass_prevented':False,
                'limitations':['Test service bound only to loopback; no gateway/head firewall or TLS deployment changed.',
                    'No actual Coder application or Arena service was imported, started or modified.']}
            (out/'physical_gateway_trace.json').write_text(json.dumps(trace,indent=2),encoding='utf-8')
            assert serialized and demand_overlap and all(not c['imported_capstone_modules'] for c in clients)
            assert all(c['result']['status']=='completed' for c in clients)
            if a.case_workflow:
                from capstone_rag import agent,caseflow,store
                from capstone_rag.ai_settings import Registry
                from capstone_rag.cluster_runtime import ClusterService
                database=folder/'operations.db'
                with closing(sqlite3.connect('file:'+(root/'data/operations.db').as_posix()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(database)) as target:source.backup(target)
                os.environ.update(CAPSTONE_INFERENCE_MODE='gateway',CAPSTONE_GATEWAY_URL='http://127.0.0.1:'+str(server.server_port),
                    CAPSTONE_APPLICATION_ID='capstone',CAPSTONE_INFRASTRUCTURE_TOKEN=tokens['capstone'],HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
                st=store.Store(str(database))
                try:
                    st.inference_registry=Registry(folder/'case-settings.db');st.routing_enabled=True
                    service=ClusterService(st);st.cluster_service=service;service.heartbeat()
                    service.configure({'addresses':[],'workflow_routes':{role:{'model':a.model,'nodes':['@authorized']} for role in ('erp_research','ticket_interpretation','independent_validation')}})
                    flow=caseflow.CaseFlow(st,agent.Agent(prefer_llm=False))
                    cid=flow.intake('Gateway synthetic support acceptance','My name is Ivan Nowak. Serial 98148469. My device lamps are dark. Please help.')
                    result=flow.pre_investigate(cid)
                    tasks=st.query("SELECT * FROM execution_tasks WHERE case_id=? AND task_type LIKE 'cluster.%'",(cid,))
                    calls=st.query('SELECT * FROM inference_calls WHERE case_id=?',(cid,))
                    events=st.query('SELECT * FROM arena_events WHERE case_id=? ORDER BY sequence',(cid,))
                    trace_case={'physical_ollama':True,'synthetic_case':True,'copied_state':True,'workflow':result,'tasks':tasks,'calls':calls,'events':events,
                        'sent_messages':st.query("SELECT message_id FROM messages WHERE case_id=? AND status='sent'",(cid,)),
                        'executed_rmas':st.query("SELECT rma_id FROM rmas WHERE case_id=? AND status='executed'",(cid,))}
                    (out/'physical_case_trace.json').write_text(json.dumps(trace_case,indent=2,default=str),encoding='utf-8')
                    assert len(tasks)==3 and all(t['state']=='completed' for t in tasks)
                    assert len(calls)==3 and all(c['status']=='completed' for c in calls)
                    assert all(json.loads(c['payload'])['gateway']['resource_id']==a.resource_id for c in calls)
                    model_events=[e for e in events if e['event_type'].startswith('inference.')]
                    assert all(e['task_id'] and e['workflow_id']==result['workflow_id'] for e in model_events)
                    assert all(e['execution_node']=='orch:capstone/main' for e in model_events)
                    assert result['paused_for_review'] and not trace_case['sent_messages'] and not trace_case['executed_rmas']
                    from capstone_rag import config,experiments
                    old_data=config.DATA_DIR # Keep the worker's managed-root security check intact.
                    try:
                        registry=st.inference_registry
                        endpoint=registry.put('endpoints',{'id':'gateway-lab','name':'Authorized gateway resource','url':os.environ['CAPSTONE_GATEWAY_URL'],
                            'resource_id':'@authorized','transport':'gateway','timeout':90})
                        profile=registry.active();profile.update(id='gateway-lab',name='Bounded synthetic gateway performance',default_endpoint=endpoint['id'])
                        for role,route in profile['routes'].items():
                            route.update(endpoint=endpoint['id'],model=a.model,enabled=role=='investigation',options={'num_ctx':1024,'temperature':0,'num_predict':24})
                        registry.put('profiles',profile)
                        planned=experiments.preview(registry,{'name':'Gateway Lab acceptance','profile_ids':[profile['id']],'scenario_ids':['phone-log'],
                            'repetitions':1,'concurrency':1,'max_trials':1,'token_limit':24,'timeout_seconds':90,'failure_limit':1,
                            'approval_mode':'interactive','seed':42,'level':'infrastructure','category':'performance','model_call_budget':1,'total_token_budget':24})
                        manager=experiments.Manager(registry);launched=manager.launch(planned,st);manager.threads[launched['id']].join(120)
                        assert not manager.threads[launched['id']].is_alive(),'Lab acceptance worker exceeded deadline'
                        reopened=experiments.Manager(Registry(registry.path));runs=reopened.runs(launched['id'])
                        traces=[json.loads((Path(run['directory'])/'trace.json').read_text()) if (Path(run['directory'])/'trace.json').exists() else
                            {'worker_log':(Path(run['directory'])/'worker.log').read_text(encoding='utf-8',errors='replace')} for run in runs]
                        evidence={'physical_ollama':True,'copied_state':True,'browser_required':False,'fresh_manager_reopened':True,
                            'runs':runs,'traces':traces,'experiment':reopened.registry.get('experiments',launched['id'])}
                        (out/'physical_lab_trace.json').write_text(json.dumps(evidence,indent=2,default=str),encoding='utf-8')
                        assert len(runs)==1 and runs[0]['status']=='completed'
                        assert traces[0]['model_calls'] and all(c['status']=='completed' and json.loads(c['payload'])['gateway']['resource_id']==a.resource_id for c in traces[0]['model_calls'])
                    finally:config.DATA_DIR=old_data
                finally:st.close()
            print(json.dumps({'clients':len(clients),'resource_id':a.resource_id,'physical_generation_completed':True,
                'serialized':serialized,'overlapping_demand':demand_overlap,'raw_bypass_prevention':'not deployed'}))
        finally:
            for child,_ in processes:
                if child.poll() is None:child.terminate();child.wait(timeout=10)
            server.shutdown();server.server_close();thread.join(5)

if __name__=='__main__':main()
