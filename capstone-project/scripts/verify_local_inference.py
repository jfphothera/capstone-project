"""Small real local endpoint probes, with isolated durable evidence."""
import json,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag import startup
startup.require()
from capstone_rag.ai_settings import Registry
from capstone_rag import inference,activity,store,generation,config
r=Registry();report=[]
for endpoint,model in [('local','qwen3:4b'),('gpu','qwen3:8b')]:
    p=r.active();p.update(id='probe-'+uuid.uuid4().hex,name='Explicit real endpoint probe')
    for route in p['routes'].values():route.update(endpoint=endpoint,model=model)
    directory=Path(config.DATA_DIR)/'ai_probes'/p['id'];directory.mkdir(parents=True)
    st=store.Store(str(directory/'probe.db'));st.inference_registry=r
    try:
        with activity.investigation(st),inference.session(st,p['id'],snapshot=r.snapshot(p)):
            response=generation.compose_structured('Return ok=true to confirm this private local inference request.',{}, {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']},purpose='investigation',max_tokens=64,timeout=120)
        calls=[{**x,'payload':json.loads(x['payload'])} for x in st.query('SELECT * FROM inference_calls')]
        report.append({'endpoint':endpoint,'requested_model':model,'response':response,'calls':calls,'evidence_path':str(directory)})
    except Exception as exc:report.append({'endpoint':endpoint,'requested_model':model,'error':str(exc)})
    finally:st.close()
    print(json.dumps(report[-1],default=str),flush=True)
Path('docs/evidence/local_integration/real-endpoint-probes.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
raise SystemExit(any('error' in x or not x.get('calls') for x in report))
