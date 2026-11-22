"""Read-only scoring of recorded runs, plus advisory identity safety probes."""
import json
from pathlib import Path
import sqlite3
import sys
import threading
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from capstone_rag import identity,erp_research,qms,qms_policy,response_policy

class ReadOnlyRecords:
    def __init__(self,path):
        self.connection=sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)
        self.connection.row_factory=sqlite3.Row
    def query(self,sql,args=()):return [dict(r) for r in self.connection.execute(sql,args)]
    def one(self,sql,args=()):return next(iter(self.query(sql,args)),None)
    def customer(self,account_id):return self.one('SELECT * FROM customers WHERE account_id=?',(account_id,))
    def device(self,serial):return self.one('SELECT * FROM devices WHERE serial_number=?',(serial,))

reports=[];chunks={c.chunk_id:c for c in qms.load_chunks()}
for path in sorted((root/'docs/evidence/multi_agent/benchmarks').glob('*/results.json')):
    report=json.loads(path.read_text());records=ReadOnlyRecords(path.parent/'operations.db')
    metrics=[]
    for row in report['cases']:
        if 'error' in row:continue
        case=records.one('SELECT * FROM cases WHERE case_id=?',(row['case_id'],));device=records.device(case['serial_number']) if case['serial_number'] else None
        context={'model':case['model'],'revision':case['revision'],'revision_confirmed':bool(device and identity.assess_revision(device).usable_for_troubleshooting),
            'compatibility':qms.load_compatibility(),'safety_hold':bool(case['is_adverse_event'])}
        used=[r for r in row['sources'] if r['status']=='USED']
        denied=[r['chunk_id'] for r in used if r['chunk_id'] not in chunks or not qms_policy.authorize(chunks[r['chunk_id']],context=context).allowed]
        messages=records.query("SELECT * FROM messages WHERE case_id=? AND direction='outbound' AND channel!='qa_notification'",(row['case_id'],))
        quality=[]
        for message in messages:
            metadata=json.loads(message.get('metadata') or '{}')
            quality.append({'message_id':message['message_id'],'generation_mode':metadata.get('generation_mode'),
                'recorded_grounded':metadata.get('grounded'),'word_count':len(message['body'].split()),
                'release_language_checks':response_policy.validate(message['body'],verified=bool(case['identity_verified']),name=case['customer_name'],serial=case['serial_number'],device=device,
                    revision_confirmed=context['revision_confirmed'])})
        metrics.append({'input_id':row['input_id'],'used_source_denominator':len(used),'unauthorized_sources':denied,
            'response_checks':quality,'domain_tool_calls':row['recorded_domain_tool_calls'],
            'model_request_events':row['model_calls'],'latency_seconds':row['latency_seconds']})
    records.connection.close()
    reports.append({'run_id':report['id'],'metrics':report.get('metrics'),'case_scores':metrics})

records=ReadOnlyRecords(root/'docs/evidence/multi_agent/baseline/operations.db');probes=[]
for name in ('ivan nowka','ivna nowak','ivan nowa','nowak ivan','ivan nówak'):
    for serial in ('98148468','98148460','98148479','98148496'):
        finding=erp_research.candidates(records,name,serial)
        verdict=identity.verify(records,identity.Claim(name=name,serial=serial))
        probes.append({'name':name,'serial':serial,'top_candidate':finding['candidates'][0]['account_id'] if finding['candidates'] else None,
            'authoritatively_verified':verdict.verified,'points':verdict.points_matched})
records.connection.close()
result={'scope':'Source authorization and elementary language checks, not independent clinical or prose-quality gold',
    'runs':reports,'identity_safety':{'denominator':len(probes),'false_verifications':sum(r['authoritatively_verified'] for r in probes),
        'correct_account_ranked_first':sum(r['top_candidate']=='ACC-00010' for r in probes),'probes':probes}}
out=root/'docs/evidence/multi_agent/benchmark-summary.json';out.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'output':str(out),'identity_safety':{k:v for k,v in result['identity_safety'].items() if k!='probes'},
    'runs':[{'id':r['run_id'],**(r['metrics'] or {})} for r in reports]},indent=2))
