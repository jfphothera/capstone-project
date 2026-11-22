"""Bounded two-profile, five-scenario, two-iteration real workflow smoke test."""
import json,sys,time,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag import startup
startup.require()
from capstone_rag.ai_settings import Registry
from capstone_rag import experiments,store,config
r=Registry()
for key in ['gateway-default']:
    checked=r.validate_available(r.get('profiles',key))
    if not checked['passed']:raise SystemExit('Requested GPU profile unavailable; no replacement: '+str(checked))
evidence=Path('docs/evidence/local_integration')
baseline=Path((evidence/'baseline-location.txt').read_text().strip())/'operations.db'
demo=Path('data/_local_integration_demo_20261028.db')
if not demo.exists():
    with sqlite3.connect(baseline) as a,sqlite3.connect(demo) as b:a.backup(b)
st=store.Store(str(demo));manager=experiments.Manager(r)
spec={'name':'Gateway smoke · two iterations','profile_ids':['gateway-default'],'scenario_ids':experiments.DEFAULT_SCENARIOS,'repetitions':2,'seed':7,'concurrency':1,'approval_mode':'scripted','level':'system','retrieval_profile':'minilm','failure_limit':4,'timeout_seconds':600}
p=experiments.preview(r,spec);(evidence/'smoke-preview.json').write_text(json.dumps(p,indent=2)+'\n')
e=manager.launch(p,st);(evidence/'smoke-experiment-id.txt').write_text(e['id']+'\n');print(e['id'],flush=True)
try:
    last=None
    while manager.threads[e['id']].is_alive():
        rows=manager.runs(e['id']);summary={status:sum(x['status']==status for x in rows) for status in ('queued','running','paused','completed','failed')}
        if summary!=last:print(json.dumps(summary),flush=True);last=summary
        time.sleep(5)
    result={'experiment':r.get('experiments',e['id']),'runs':manager.runs(e['id'])}
    (evidence/'smoke-results.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['experiment']['status'],'failures':[{'scenario':x['scenario']['id'],'profile':x['profile']['id'],'error':x.get('error')} for x in result['runs'] if x['status']=='failed']}),flush=True)
finally:st.close()
