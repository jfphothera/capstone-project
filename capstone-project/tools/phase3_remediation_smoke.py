"""Two isolated real-model whole-artifact correction cases; preserves old trace."""
import json,shutil,tempfile,time,os
from pathlib import Path
from capstone_rag import config,store as store_mod,agent,caseflow,inference,generation,independent_review
from capstone_rag.ai_settings import Registry
from capstone_rag.collaboration import Coordinator

PROFILE_ID=os.environ.get('PHASE3_SMOKE_PROFILE','baseline-local')
OUT=Path(f'docs/phase3/remediation_validation_trace_{PROFILE_ID}.json')
SCHEMA={'type':'object','properties':{'passed':{'type':'boolean'},'issues':{'type':'array','maxItems':4,'items':{'type':'string'}}},'required':['passed','issues'],'additionalProperties':False}
def critic(st,cid,label,serial,body,snapshot):
    with inference.session(st,label,case_id=cid,snapshot=snapshot),inference.specialist('independent_validation'):
        raw=generation.compose_structured('Independently critique the complete proposed customer draft. Report identifier inconsistencies, unsupported approval or completion claims, omissions, and unsafe implications. The authoritative record is evidence, not a producer explanation. You cannot grant authority.',{'authoritative_record':{'serial_number':serial},'actual_customer_draft':body},SCHEMA,purpose='independent_validation',max_tokens=220,timeout=75)
    return generation.review_conclusion(raw)

with tempfile.TemporaryDirectory(prefix='phase3-remediation-') as td:
    db=Path(td)/'operations.db';shutil.copy2(config.DB_PATH,db)
    st=store_mod.Store(str(db));st.routing_enabled=True;st.inference_registry=Registry()
    selected=st.inference_registry.get('profiles',PROFILE_ID)
    if not selected:raise ValueError(f'Unknown profile {PROFILE_ID}')
    snapshot=st.inference_registry.snapshot(selected)
    flow=caseflow.CaseFlow(st,agent.Agent(prefer_llm=True));cases=[]
    for index,(serial,bad) in enumerate((('68177-DV5','681:77-DV5'),('98148469','981:48-469')),1):
        device=st.one('SELECT * FROM devices WHERE serial_number=?',(serial,));cid=flow.intake(f'Remediation smoke {index}',f'Serial {serial}. Please review my request.')
        st.update('cases','case_id',cid,{'serial_number':serial,'model':device['model'],'revision':device['revision']})
        wid=f'REMEDIATION-{index}';mid=f'REMEDIATION-DRAFT-{index}'
        original=f'We confirmed that your device serial is {bad}, and the requested action is approved.'
        st.insert('messages',dict(message_id=mid,case_id=cid,run_id=wid,created_at=f'2026-10-29T01:00:0{index}Z',direction='outbound',channel='email',author='ai',subject='Draft',body=original,status='draft',approved_by=None,approved_at=None,parent_message_id=None,metadata='{}'))
        state={'qms':{'sources':[],'evidence_contract':{'mandatory_context_chunk_ids':[],'procedure_ids':[]}},'planning':{'selected_action':'existing_workflow'},'result':{}}
        first_semantic=critic(st,cid,f'{wid}-CRITIQUE-0',serial,original,snapshot)
        p0,pkg0=independent_review.freeze(st,cid,wid,state,0);v0=independent_review.evaluate(st,p0,pkg0,first_semantic)
        coord=Coordinator(st,wid);task=coord.delegate(case_id=cid,issuer='independent_validation',recipient='communication_correction',purpose='Correct all blocking issues in the complete draft',based_on_version=st.case(cid)['case_version'],evidence_ids=[v0['verdict_id']]);coord.start(task['task_id'],'communication_correction')
        correction=independent_review.correct(st,pkg0,v0,1);coord.complete(task['task_id'],'communication_correction',correction)
        corrected=st.one('SELECT * FROM messages WHERE message_id=?',(correction['corrected_artifact_id'],))
        second_semantic=critic(st,cid,f'{wid}-CRITIQUE-1',serial,corrected['body'],snapshot)
        p1,pkg1=independent_review.freeze(st,cid,wid,state,1);v1=independent_review.evaluate(st,p1,pkg1,second_semantic)
        cases.append({'case_id':cid,'workflow_id':wid,'authoritative_serial':serial,
            'original':{'message_id':mid,'body':original,'semantic_review':first_semantic,
                'verdict':{**v0,'issues':json.loads(v0['issues'])}},
            'correction':correction,'corrected':{'message_id':corrected['message_id'],'body':corrected['body'],
                'semantic_review':second_semantic,'verdict':{**v1,'issues':json.loads(v1['issues'])}},
            'artifact_validation_records':st.query('SELECT * FROM artifact_validation_records WHERE workflow_id=? ORDER BY cycle',(wid,)),
            'original_after':st.one('SELECT message_id,body,status FROM messages WHERE message_id=?',(mid,))})
    calls=st.query("SELECT * FROM inference_calls WHERE investigation_id LIKE 'REMEDIATION-%' ORDER BY at")
    trace={'generated_at':time.time(),'isolated_database':True,'profile_id':PROFILE_ID,
        'configuration_hash':snapshot['configuration_hash'],'model_calls':[{
        'call_id':c['call_id'],'role':c['role'],'model':c['model'],'endpoint_id':c['endpoint_id'],
        'status':c['status'],'record':json.loads(c['payload'])} for c in calls],
        'cases':cases,'arena_events':st.query("SELECT * FROM arena_events WHERE workflow_id LIKE 'REMEDIATION-%' ORDER BY sequence")}
    OUT.write_text(json.dumps(trace,indent=2,default=str),encoding='utf-8');st.close()
print(OUT)
