"""Four same-input reviewer tasks after the explicit draft-pass contract fix."""
import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag import startup
startup.require()
from capstone_rag import experiments,store
from capstone_rag.ai_settings import Registry
r=Registry();manager=experiments.Manager(r);st=store.Store('data/_local_integration_demo_20261028.db')
spec={'name':'Reviewer contract repair · same unsafe draft · two iterations','profile_ids':['gateway-default'],'scenario_ids':['safety-complaint'],'repetitions':2,'seed':7,'concurrency':1,'approval_mode':'interactive','level':'specialist','specialist_roles':['independent_validation'],'retrieval_profile':'minilm','failure_limit':2,'timeout_seconds':300}
try:
    value=manager.launch(experiments.preview(r,spec),st);eid=value['id'];print(eid,flush=True)
    while manager.threads[eid].is_alive():time.sleep(2)
    rows=manager.runs(eid);result={'experiment':r.get('experiments',eid),'runs':rows}
    Path('docs/evidence/local_integration/reviewer-contract-repair-results.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps([{'profile':x['profile']['id'],'state':x['status'],'raw_accuracy':x.get('metrics',{}).get('specialist_accuracy'),'effective_gate_correct':x.get('specialist_result',{}).get('effective_gate_correct'),'error':x.get('error')} for x in rows]),flush=True)
finally:st.close()
