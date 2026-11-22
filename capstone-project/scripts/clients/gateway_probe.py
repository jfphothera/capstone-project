"""Independent synthetic client example. Only the neutral stdlib SDK is needed."""
import argparse,json,sys,time,uuid
from pathlib import Path
from shared_inference_client import Client

def main():
    p=argparse.ArgumentParser();p.add_argument('--bootstrap',required=True);p.add_argument('--resource',required=True)
    p.add_argument('--model',required=True);p.add_argument('--output',required=True);p.add_argument('--start-at',type=float,default=0)
    a=p.parse_args();client=Client.from_config(a.bootstrap)
    while time.time()<a.start_at:time.sleep(.01)
    began=time.time()
    result=client.infer(a.model,[a.resource],'/api/generate',{'prompt':'Return exactly the word READY.','think':False,
        'options':{'num_ctx':1024,'num_predict':24,'temperature':0,'seed':42}},client_key=uuid.uuid4().hex,timeout=60)
    evidence={'synthetic_application':True,'started_at':began,'finished_at':time.time(),'result':result,
              'imported_capstone_modules':[name for name in sys.modules if name.startswith('capstone_rag')]}
    Path(a.output).write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    return 0 if result['status']=='completed' else 1

if __name__=='__main__':raise SystemExit(main())
