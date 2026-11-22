"""Read-only private-LAN endpoint inventory and minimal generation proof."""
import json,socket,time
from datetime import datetime,timezone
from pathlib import Path
from capstone_rag.ai_settings import Registry,http

registry=Registry();active=registry.active();result={'checked_at':datetime.now(timezone.utc).isoformat(),
    'registry_path':registry.path,'active_profile':active,'profiles':registry.list('profiles'),
    'endpoints':[],'active_profile_changed':False}
for endpoint in registry.list('endpoints'):
    item={'endpoint':endpoint,'tcp_11434':False,'tags_response':None,'loaded_response':None,
          'generation':None,'error':None};started=time.perf_counter()
    try:
        host=endpoint['url'].split('//',1)[1].split(':',1)[0]
        with socket.create_connection((host,11434),timeout=8):item['tcp_11434']=True
        tags=http(endpoint['url'],'/api/tags',timeout=20);item['tags_response']=tags
        item['loaded_response']=http(endpoint['url'],'/api/ps',timeout=20)
        installed={m['name'] for m in tags.get('models',[])}
        model='qwen3:4b' if 'qwen3:4b' in installed else None
        if model:
            t=time.perf_counter();response=http(endpoint['url'],'/api/generate',{
                'model':model,'prompt':'Return only the token GPU_OK if this request reached you.',
                'stream':False,'think':False,'keep_alive':'1m','options':{'temperature':0,'num_predict':12}},timeout=120)
            item['generation']={'requested_model':model,'returned_model':response.get('model'),
                'response':response.get('response'),'done':response.get('done'),
                'done_reason':response.get('done_reason'),'wall_ms':round((time.perf_counter()-t)*1000,3),
                'total_duration_ns':response.get('total_duration'),'load_duration_ns':response.get('load_duration'),
                'eval_duration_ns':response.get('eval_duration'),'eval_count':response.get('eval_count'),
                'identity_match':response.get('model')==model}
        else:item['error']='qwen3:4b is not installed; generation proof not attempted'
    except Exception as exc:item['error']=f'{type(exc).__name__}: {exc}'
    item['audit_wall_ms']=round((time.perf_counter()-started)*1000,3);result['endpoints'].append(item)
result['active_after']=registry.active();result['active_profile_changed']=result['active_after']['id']!=active['id']
Path('docs/phase3/gpu_endpoint_audit.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
print(json.dumps({'active':active['id'],'endpoints':[{'id':x['endpoint']['id'],'tcp':x['tcp_11434'],
    'models':[m.get('name') for m in (x['tags_response'] or {}).get('models',[])],
    'generation':x['generation'],'error':x['error']} for x in result['endpoints']]},indent=2))
