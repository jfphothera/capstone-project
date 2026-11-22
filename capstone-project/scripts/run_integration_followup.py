"""Targeted repair verification plus small equivalent specialist tasks."""
import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag import startup
startup.require()
from capstone_rag import experiments,store
from capstone_rag.ai_settings import Registry
r=Registry();manager=experiments.Manager(r);st=store.Store('data/_local_integration_demo_20261028.db')
base={'profile_ids':['gateway-default'],'repetitions':2,'seed':7,'concurrency':1,'approval_mode':'scripted','retrieval_profile':'minilm','failure_limit':3,'timeout_seconds':600}
specs=[{**base,'name':'Targeted component workflow repair · two iterations','scenario_ids':['component-approval'],'level':'system'},
       {**base,'name':'Specialist intake phone-log proof · two iterations','scenario_ids':['phone-log'],'level':'specialist','specialist_roles':['ticket_interpretation']},
       {**base,'name':'Specialist unsafe-draft review proof · two iterations','scenario_ids':['safety-complaint'],'level':'specialist','specialist_roles':['independent_validation']}]
summary=[]
try:
    for spec in specs:
        value=manager.launch(experiments.preview(r,spec),st);eid=value['id'];print(eid,spec['name'],flush=True)
        last=None
        while manager.threads[eid].is_alive():
            rows=manager.runs(eid);counts={s:sum(x['status']==s for x in rows) for s in ('queued','running','completed','paused','failed')}
            if counts!=last:print(json.dumps(counts),flush=True);last=counts
            time.sleep(3)
        summary.append({'experiment':r.get('experiments',eid),'runs':manager.runs(eid)})
        Path('docs/evidence/local_integration/targeted-followup-results.json').write_text(json.dumps(summary,indent=2,default=str)+'\n',encoding='utf-8')
finally:st.close()
