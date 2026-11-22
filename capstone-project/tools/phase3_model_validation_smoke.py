"""Run two small local-model critique cases against an isolated DB copy."""
import json,shutil,tempfile,time
from pathlib import Path
from capstone_rag import config,store as store_mod,agent,caseflow,inference,generation,independent_review
from capstone_rag.ai_settings import Registry
from capstone_rag.collaboration import Coordinator

out=Path('docs/phase3/model_validation_smoke_trace.json')
with tempfile.TemporaryDirectory(prefix='phase3-smoke-') as td:
    db=Path(td)/'operations.db';shutil.copy2(config.DB_PATH,db)
    st=store_mod.Store(str(db));st.routing_enabled=True;st.inference_registry=Registry()
    flow=caseflow.CaseFlow(st,agent.Agent(prefer_llm=True));cases=[]
    for index,bad in enumerate(('681:77-DV5','981:48-469'),1):
        device=st.one("SELECT * FROM devices WHERE serial_number IN ('68177-DV5','98148469') ORDER BY serial_number LIMIT 1 OFFSET ?",(index-1,))
        cid=flow.intake(f'Phase 3 critique {index}',f"Serial {device['serial_number']}. Please confirm the next supported step.")
        st.update('cases','case_id',cid,{'serial_number':device['serial_number'],'model':device['model'],'revision':device['revision']})
        mid=f'PHASE3-SMOKE-DRAFT-{index}'
        body=f'We confirmed that your device serial is {bad}, and the requested action is approved.'
        st.insert('messages',dict(message_id=mid,case_id=cid,run_id=f'SMOKE-{index}',created_at=f'2026-10-29T00:00:0{index}Z',direction='outbound',channel='email',author='ai',subject='Draft',body=body,status='draft',approved_by=None,approved_at=None,parent_message_id=None,metadata='{}'))
        with inference.session(st,f'PHASE3-MODEL-{index}',case_id=cid),inference.specialist('independent_validation'):
            semantic=generation.compose_structured('Independently critique this proposed customer draft. Report unsupported promises or identifier inconsistency. You cannot grant approval or manufacture evidence.',{'authoritative_serial':device['serial_number'],'draft':body},{'type':'object','properties':{'passed':{'type':'boolean'},'issues':{'type':'array','maxItems':3,'items':{'type':'string'}}},'required':['passed','issues'],'additionalProperties':False},purpose='independent_validation',max_tokens=180,timeout=60)
        state={'qms':{'sources':[],'evidence_contract':{'mandatory_context_chunk_ids':[],'procedure_ids':[]}},'planning':{'selected_action':'existing_workflow'},'result':{}}
        package_row,package=independent_review.freeze(st,cid,f'SMOKE-{index}',state,0)
        verdict=independent_review.evaluate(st,package_row,package,generation.review_conclusion(semantic))
        correction=independent_review.correct(st,package,verdict,1)
        task=Coordinator(st,f'SMOKE-{index}').delegate(case_id=cid,issuer='independent_validation',recipient='communication_review',purpose='Review corrected draft',based_on_version=st.case(cid)['case_version'],evidence_ids=[verdict['verdict_id']])
        Coordinator(st,f'SMOKE-{index}').start(task['task_id'],'communication_review')
        cases.append({'case_id':cid,'authoritative_serial':device['serial_number'],'original_draft':body,
            'model_review':semantic,'verdict':{**verdict,'issues':json.loads(verdict['issues'])},
            'correction':correction,'corrected_draft':st.one('SELECT body FROM messages WHERE message_id=?',(correction.get('corrected_artifact_id'),)),
            'original_status':st.one('SELECT status FROM messages WHERE message_id=?',(mid,))['status']})
    trace={'generated_at':time.time(),'isolated_database':True,'cases':cases,
        'inference_calls':[dict(x) for x in st.query('SELECT call_id,role,endpoint_id,model,status,payload FROM inference_calls WHERE investigation_id LIKE ? ORDER BY at',('PHASE3-MODEL-%',))],
        'arena_events':st.query("SELECT * FROM arena_events WHERE workflow_id LIKE 'SMOKE-%' ORDER BY sequence")}
    out.write_text(json.dumps(trace,indent=2,default=str),encoding='utf-8');st.close()
print(out)
