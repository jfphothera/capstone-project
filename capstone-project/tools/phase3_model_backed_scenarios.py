"""Small isolated GPU-backed case set for collaboration, ToT, QMS gap and no-reply evidence."""
import json,shutil,tempfile,time
from pathlib import Path
from capstone_rag import config,store as store_mod,agent,caseflow,inference
from capstone_rag.ai_settings import Registry

SCENARIOS=[
 ('normal-revb','P4800 Rev B power','My name is Mary Cohen. Serial 68177-DV5. My P4800 will not power on.','email'),
 ('complex-replacement','Replacement comparison','My name is Ivan Nowak. Serial 98148469. The P4800 will not power on and I need an exchange or replacement.','email'),
 ('no-reply','Missed call callback request','Automated call-back request logged from inbound IVR session. No conversation transcript is available.','phone_ai')]
with tempfile.TemporaryDirectory(prefix='phase3-gpu-cases-') as td:
    path=Path(td)/'operations.db';shutil.copy2(config.DB_PATH,path);st=store_mod.Store(str(path))
    registry=Registry();profile=registry.get('profiles','baseline-gpu');snapshot=registry.snapshot(profile)
    st.routing_enabled=True;st.inference_registry=registry;flow=caseflow.CaseFlow(st,agent.Agent(prefer_llm=True));runs=[]
    for sid,subject,body,channel in SCENARIOS:
        cid=flow.intake(subject,body,channel=channel);start=time.perf_counter();error=None;result={}
        try:
            with inference.session(st,'GPU-SCENARIO-'+sid,case_id=cid,snapshot=snapshot):result=flow.pre_investigate(cid)
        except Exception as exc:error=f'{type(exc).__name__}: {exc}'
        wall=round((time.perf_counter()-start)*1000,3);calls=[]
        for row in st.query('SELECT * FROM inference_calls WHERE case_id=? ORDER BY at',(cid,)):
            payload=json.loads(row['payload']);calls.append({'call_id':row['call_id'],'role':row['role'],
                'endpoint_id':row['endpoint_id'],'model':row['model'],'status':row['status'],
                'duration_ms':payload.get('duration_ms'),'slot_wait_ms':payload.get('slot_wait_ms'),
                'timings':payload.get('timings',{})})
        planning=st.one("SELECT payload FROM specialist_findings WHERE case_id=? AND role='resolution_planning' ORDER BY at DESC LIMIT 1",(cid,))
        runs.append({'scenario_id':sid,'case_id':cid,'wall_ms':wall,'error':error,'result':result,
            'case':st.case(cid),'model_calls':calls,'model_call_count':len(calls),
            'planning':json.loads(planning['payload']) if planning else None,
            'delegated_tasks':st.query('SELECT task_id,issuer,recipient,status,attempt FROM delegated_tasks WHERE case_id=? ORDER BY created_at',(cid,)),
            'outbound_message_count':st.one("SELECT count(*) AS n FROM messages WHERE case_id=? AND direction='outbound'",(cid,))['n'],
            'arena_events':st.query('SELECT * FROM arena_events WHERE case_id=? ORDER BY sequence',(cid,))})
    output={'generated_at':time.time(),'isolated_database':True,'profile_id':'baseline-gpu',
        'active_profile_unchanged':registry.active()['id'],'configuration_hash':snapshot['configuration_hash'],
        'runs':runs};Path('docs/phase3/gpu_model_backed_scenarios.json').write_text(json.dumps(output,indent=2,default=str),encoding='utf-8');st.close()
print('docs/phase3/gpu_model_backed_scenarios.json')
