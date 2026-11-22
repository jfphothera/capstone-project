"""Three-call installed Gateway self-test; prints no credentials or prompts."""
import argparse,json,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from shared_inference_client import Client

def choose(client,operation):
    qualified='embedding' if operation=='/api/embed' else 'completion'
    inventory=client.models();client.policy()
    names=sorted({name for resource in inventory.get('resources',[]) for name in resource.get('models',[])
        if qualified in ((resource.get('model_details') or {}).get(name,{}) .get('qualified_operations') or [])})
    for name in names:
        check=client.availability(name,operation,num_ctx=512 if operation!='/api/embed' else None)
        if check.get('status') in {'available','busy'}:return name,check
    raise PermissionError(f'no {qualified} model is allowed for {client.app} in the Gateway; the administrator allows one in the admin hub')

def run(client,operation):
    model,availability=choose(client,operation);key='capstone-selftest-'+uuid.uuid4().hex
    payload={'input':['Capstone embedding self-test']} if operation=='/api/embed' else {'prompt':'Return exactly READY.','options':{'num_ctx':512,'num_predict':8,'temperature':0}}
    envelope=client.infer(model,None,operation,payload,client_key=key,timeout=180)
    if envelope.get('status')!='completed':raise RuntimeError('Gateway self-test ended '+str(envelope.get('status')))
    result=envelope.get('result') or {};provenance=result.get('provenance') or {}
    return {'status':'completed','application_id':client.app,'operation':operation,
        'gateway_request_id':result.get('request_id') or (envelope.get('request') or {}).get('request_id'),
        'client_key':key,'resource_id':result.get('resource_id'),'model':result.get('model') or result.get('executed_model') or model,
        'digest':result.get('model_digest') or provenance.get('executed_digest'),
        'queue_ms':result.get('queue_ms'),'duration_ms':result.get('duration_ms'),'availability':availability.get('status')}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--operational',required=True);parser.add_argument('--research',required=True);args=parser.parse_args()
    output={'format':'capstone-end-to-end-v1','passed':False,'calls':[]}
    try:
        operational=Client.from_config(args.operational);research=Client.from_config(args.research)
        output['calls']=[run(operational,'/api/generate'),run(operational,'/api/embed'),run(research,'/api/generate')];output['passed']=True
    except Exception as exc:output.update(error_class=type(exc).__name__,error=str(exc))
    print(json.dumps(output,indent=2));return 0 if output['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
