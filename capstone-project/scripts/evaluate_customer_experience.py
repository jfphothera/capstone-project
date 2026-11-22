"""Two small real-backend development conversations; record actual reply paths."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import uuid

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url',default='http://127.0.0.1:8000')
parser.add_argument('--scenario',choices=['all','manual','vague'],default='all')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
identifier='customer-experience-'+uuid.uuid4().hex[:10]
path=root/'docs/evidence/customer_operations'/f'{identifier}.json'

def request(method,route,body=None):
    req=urllib.request.Request(args.url+route,data=json.dumps(body).encode() if body is not None else None,
        method=method,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=420) as response:
        return json.loads(response.read().decode())

report={'run_id':identifier,'scope':'two fictional development examples; no held-out tuning',
        'runtime':request('GET','/api/trace/runtime'),'cases':[]}
for label,text in [('manual','My P2400 powers on but all lamps remain dark. What should I check next?'),
                   ('vague',"My P2400 isn't working. Can you help me figure out what to tell you?")]:
    if args.scenario!='all' and args.scenario!=label:
        continue
    session=request('POST','/api/chat/sessions'); cid=session['conversation_id']; start=time.perf_counter()
    response=request('POST',f'/api/chat/sessions/{cid}/messages',{'text':text})
    trace=request('GET',f'/api/trace/conversation/{cid}')
    final=[e for e in trace['events'] if e['category']=='response'][-1]
    report['cases'].append({'scenario':label,'input':text,'conversation_id':cid,'response':response,
        'latency_seconds':time.perf_counter()-start,'generation':final['evidence'],'trace':trace,
        'checks':{'no_raw_qms_notes':not any(s in response['reply']['text'] for s in ('PROC-','QMS-DOC-','Scope and prerequisites')),
                  'no_premature_handoff':not response['handed_off'],'identity_not_required':not response['identification']['verified'],
                  'manual_sources_present':bool(response['reply']['sources']) if label=='manual' else None,
                  'language_quality':'review the actual delivered response; this is not an aggregate quality benchmark'}})
    modes=[c['generation']['generation_mode'] for c in report['cases']]
    report['final_reply_counts']={mode:modes.count(mode) for mode in sorted(set(modes))}
    report['measured_denominator']=len(report['cases'])
    path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'scenario':label,'conversation_id':cid,'mode':final['evidence']['generation_mode'],'output':str(path)}),flush=True)
print(json.dumps({'output':str(path),'counts':report['final_reply_counts']},indent=2),flush=True)
