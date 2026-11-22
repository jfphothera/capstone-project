"""Compare one identical manual query through the two actual isolated indexes."""
import json,sys,uuid,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag import startup
startup.require()
from capstone_rag import activity,inference,store,caseflow,config,support_workflow
from capstone_rag.ai_settings import Registry
r=Registry();original=r.get('retrieval_profiles','qwen3-experimental');report=[]
baseline=Path(config.INDEX_PATH);before=hashlib.sha256(baseline.read_bytes()).hexdigest()
directory=Path('data/retrieval_probes')/uuid.uuid4().hex;directory.mkdir(parents=True)
st=store.Store(str(directory/'probe.db'));st.inference_registry=r;agent=caseflow.make_agent(prefer_llm=False,store=st)
try:
    r.put('retrieval_profiles',{**original,'enabled':True})
    for selected in ['minilm','qwen3-experimental']:
        p=r.active();p.update(id='retrieval-probe-'+uuid.uuid4().hex,name='Explicit index-selection probe',retrieval_profile=selected)
        query='How do I read the indicators and find the user guide for my P4800?'
        with activity.investigation(st),inference.session(st,p['id'],snapshot=r.snapshot(p)):
            manual=support_workflow.customer_manual_search(agent.toolbox,query,model='P4800',revision='A',revision_confirmed=True)
            generation,grounding=support_workflow.write_public_reply(agent,query,{'manual':manual,'model':'P4800','device':{'revision':'A'}})
        report.append({'retrieval_profile':selected,'selected_procedure':manual['procedure'],'selected_chunks':[c.chunk_id for c in manual['chunks']],'unauthorized_candidates_rejected':sum(not c['authorization']['allowed'] for c in manual['evidence']['candidates']),'public_generation_mode':generation.mode if generation else None,'grounding':vars(grounding) if grounding else None,'text':generation.text if generation else None})
finally:
    r.put('retrieval_profiles',original)
    result={'results':report,'minilm_unchanged':before==hashlib.sha256(baseline.read_bytes()).hexdigest(),'experimental_enabled_after_probe':r.get('retrieval_profiles','qwen3-experimental')['enabled'],'evidence_database':st.path}
    Path('docs/evidence/local_integration/retrieval-selection-proof.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8');st.close()
print(json.dumps(result,default=str),flush=True)
