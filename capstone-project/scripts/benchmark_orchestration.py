"""Paired source-only inputs; baseline source runs only as an isolated evaluator."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--baseline',action='store_true');parser.add_argument('--real-model',action='store_true');parser.add_argument('--limit',type=int,default=8)
args=parser.parse_args()
sys.path.insert(0,str(root/('docs/evidence/multi_agent/baseline/source/src' if args.baseline else 'src')))
from capstone_rag import config
for name,relative in [('DATA_DIR','data'),('ERP_DIR','data/erp'),('QMS_DIR','data/qms'),('COMPATIBILITY_PATH','data/qms_compatibility.json'),('INDEX_PATH','data/index/qms_index.npz')]:setattr(config,name,str(root/relative))
mode='baseline' if args.baseline else 'coordinated'
identifier=mode+('-qwen-' if args.real_model else '-deterministic-')+uuid.uuid4().hex[:8]
directory=root/'docs/evidence/multi_agent/benchmarks'/identifier;directory.mkdir(parents=True)
db=directory/'operations.db'
source=sqlite3.connect(f'file:{(root/"docs/evidence/multi_agent/baseline/operations.db").as_posix()}?mode=ro',uri=True)
dest=sqlite3.connect(db);source.backup(dest);dest.close();source.close()
config.DB_PATH=str(db)
for name,file in [('TICKET_LOG_PATH','tickets.jsonl'),('ESCALATION_PATH','escalations.jsonl'),('RMA_PATH','approvals.jsonl'),('MEMORY_PATH','memory.jsonl'),('LABEL_DIR','labels')]:setattr(config,name,str(directory/file))
from capstone_rag import store,caseflow,agent
st=store.Store(str(db));ag=agent.Agent(prefer_llm=args.real_model);flow=caseflow.CaseFlow(st,ag)
audit=json.loads((root/'docs/evidence/multi_agent/reference/ticket_audit.json').read_text())
# Identical observed envelopes in both modes. Annotations remain in the scorer.
selected=[next(t for t in audit if t['text_derived_subtype']==subtype) for subtype in
    ['voicemail_without_transcript','callback_request','ivr_refill_selection','missing_accessory','manual_request','prior_authorization_status','device_symptom_report','return_or_repair_request'] if any(t['text_derived_subtype']==subtype for t in audit)]
if args.real_model:
    selected=[{'pool_id':'DEV-AMBIGUOUS-PART','subject':'Missing item','body':'My name is Ivan Nowak. Serial 98148469. An accessory is missing from the box. Which item should I check for?','outbound_policy':'candidate_reply'}]
report={'id':identifier,'mode':mode,'real_model_requested':args.real_model,'annotations':'review rule audit, not independent human gold','cases':[]}
for ticket in selected[:args.limit]:
    start=time.perf_counter();cid=flow.intake(ticket['subject'],ticket['body'],channel='unknown')
    try:
        result=flow.pre_investigate(cid)
        messages=st.messages(cid);outbound=[m for m in messages if m['direction']=='outbound' and m['channel']!='qa_notification']
        events=st.query('SELECT * FROM activity_events WHERE case_id=?',(cid,))
        model_calls=[e for e in events if e['category']=='model' and e['status']=='awaiting_model_response']
        record={'input_id':ticket['pool_id'],'input':{'subject':ticket['subject'],'body':ticket['body'],'channel':'unknown'},
            'case_id':cid,'latency_seconds':round(time.perf_counter()-start,3),'result':result,'drafts':[m['body'] for m in outbound],
            'expected_no_reply':ticket['outbound_policy']!='candidate_reply','actual_no_reply':not outbound,
            'rma_proposals':len(st.query('SELECT * FROM rmas WHERE case_id=?',(cid,))),
            'recorded_domain_tool_calls':len(st.query('SELECT * FROM tool_calls WHERE case_id=?',(cid,))),
            'model_calls':len(model_calls),'sources':st.query('SELECT * FROM retrieved_sources WHERE case_id=?',(cid,)),
            'trace':events,'response_quality':'review delivered drafts; no automatic prose-quality success claim'}
    except Exception as exc:record={'input_id':ticket['pool_id'],'case_id':cid,'error':str(exc),'latency_seconds':time.perf_counter()-start}
    report['cases'].append(record)
    (directory/'results.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    print(json.dumps({k:record.get(k) for k in ('input_id','case_id','actual_no_reply','model_calls','latency_seconds','error')}),flush=True)
report['metrics']={'cases':len(report['cases']),'errors':sum('error' in r for r in report['cases']),
    'no_reply_decision_correct':sum(r.get('actual_no_reply')==r.get('expected_no_reply') for r in report['cases'] if 'error' not in r),
    'no_reply_decision_denominator':sum('error' not in r for r in report['cases']),
    'rma_proposals':sum(r.get('rma_proposals',0) for r in report['cases']),
    'model_calls':sum(r.get('model_calls',0) for r in report['cases']),
    'latency_seconds_total':round(sum(r.get('latency_seconds',0) for r in report['cases']),3)}
(directory/'results.json').write_text(json.dumps(report,indent=2,default=str)+'\n');st.close()
print(json.dumps({'output':str(directory/'results.json'),'metrics':report['metrics']}),flush=True)
