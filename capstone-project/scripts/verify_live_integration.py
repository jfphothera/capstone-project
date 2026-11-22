"""Exercise the normal app API, pinning, optional specialists and public chat."""
import json,time,urllib.request
from pathlib import Path
BASE='http://127.0.0.1:8000'
def api(path,body=None,timeout=90):
    request=urllib.request.Request(BASE+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=timeout) as response:return json.load(response)
evidence=Path('docs/evidence/local_integration');result={}
initial=api('/api/ai/configuration');original=initial['active']['id']
custom=next(p for p in initial['profiles'] if p['name']=='Integration proof · GPU specialists')
try:
    api('/api/ai/profiles/'+custom['id']+'/activate',{'authorizer':'synthetic.integration.reviewer'})
    created=api('/api/cases',{'subject':'Live integration proof — missing accessory clarification','body':'My name is ivan nowak. Serial 98148469. An accessory is missing from my box, but I am unsure which item. Please help me work out the next step.','channel':'email','author':'Synthetic integration customer'})
    cid=created['case_id'];print('Normal app case',cid,flush=True);result['case_id']=cid
    for attempt in range(240):
        pinned=api('/api/ai/cases/'+cid+'/configuration')
        if pinned['snapshots']:break
        time.sleep(.5)
    else:raise RuntimeError('Case never established a pinned snapshot')
    result['pinned_before_active_change']=pinned['snapshots']
    api('/api/ai/profiles/'+original+'/activate',{'authorizer':'synthetic.integration.reviewer'})
    result['active_changed_during_case']=api('/api/ai/configuration')['active']['id']
    for attempt in range(240):
        workspace=api('/api/cases/'+cid)
        workflows=workspace.get('workflow_runs',[])
        if workflows and all(w['status']!='running' for w in workflows):break
        time.sleep(2)
    result['workspace']=workspace;result['configuration']=api('/api/ai/cases/'+cid+'/configuration')
    result['all_calls_remained_on_pinned_gpu']=bool(result['configuration']['calls']) and all(x['endpoint_id']=='gpu' for x in result['configuration']['calls'])
    result['actual_model_roles']=sorted({x['role'] for x in result['configuration']['calls']})
finally:
    if api('/api/ai/configuration')['active']['id']!=original:api('/api/ai/profiles/'+original+'/activate',{'authorizer':'synthetic.integration.reviewer'})
    (evidence/'live-case-proof.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
print(json.dumps({'case':result['case_id'],'roles':result.get('actual_model_roles'),'pinned_gpu':result.get('all_calls_remained_on_pinned_gpu'),'state':result.get('workspace',{}).get('case',{}).get('ai_state')}),flush=True)
session=api('/api/chat/sessions',{});conversation=session['conversation_id']
reply=api('/api/chat/sessions/'+conversation+'/messages',{'text':'I have a P4800 revision A. How should I read its indicators, according to the user manual?'},timeout=360)
(evidence/'live-chat-proof.json').write_text(json.dumps(reply,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'conversation_id':conversation,'reply':reply.get('reply'),'identification':reply.get('identification')}),flush=True)
