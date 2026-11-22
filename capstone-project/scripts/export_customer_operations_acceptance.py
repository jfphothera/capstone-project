"""Read-only acceptance snapshot of the fictional browser demonstration."""
import json
from pathlib import Path
import sqlite3

root=Path(__file__).resolve().parents[1]
path=root/'data/_customer_operations_demo_20261027.db'
db=sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)
db.row_factory=sqlite3.Row
def rows(sql,args=()):
    return [dict(r) for r in db.execute(sql,args)]

report={'database':str(path),'scope':'fictional browser acceptance; not a held-out benchmark','cases':{}}
for number in (81,82,83,84,85,86,87,88,89):
    cid=f'CASE-{number:06d}'
    report['cases'][cid]={
        'case':rows('SELECT * FROM cases WHERE case_id=?',(cid,)),
        'service_calls':rows('SELECT * FROM erp_calls WHERE case_id=?',(cid,)),
        'movements':rows('SELECT * FROM rmas WHERE case_id=?',(cid,)),
        'shipments':rows('SELECT * FROM sim_shipments WHERE case_id=?',(cid,)),
        'messages':rows('SELECT * FROM messages WHERE case_id=? ORDER BY created_at',(cid,)),
        'events':rows('SELECT * FROM activity_events WHERE case_id=? ORDER BY event_id',(cid,))}
report['feedback']=rows("SELECT * FROM improvement_proposals WHERE kind='representative_feedback'")
report['trace_exports']=sorted(p.name for p in (root/'docs/evidence/agent_traces').glob('*2026102[78]*'))
report['screenshots']=sorted(p.name for p in (root/'docs/evidence/customer_operations').glob('*.png'))
cases=report['cases']
report['checks']={
    'resolved_without_movement':cases['CASE-000081']['case'][0]['status']=='Resolved' and not cases['CASE-000081']['movements'],
    'component_only':all(r['movement_type']=='part_outbound' for r in cases['CASE-000089']['movements']) and bool(cases['CASE-000089']['shipments']),
    'pickup_recovery_reuses_single_shipment':len(cases['CASE-000084']['shipments'])==1 and cases['CASE-000084']['shipments'][0]['pickup_attempts']==3 and cases['CASE-000084']['shipments'][0]['return_method']=='drop_off_label',
    'shipping_failure_no_shipment':not cases['CASE-000086']['shipments'] and cases['CASE-000086']['movements'][0]['status']=='failed',
    'rejected_movement_no_shipment':not cases['CASE-000087']['shipments'] and cases['CASE-000087']['movements'][0]['status']=='rejected',
    'exchange_receiving_recorded':any(r.get('received_at') and r.get('received_by') for r in cases['CASE-000085']['movements']),
    'email_followup_same_case':any(m['direction']=='inbound' and m.get('parent_message_id') for m in cases['CASE-000083']['messages']),
    'reviewed_feedback_retained':any(r['review_status']=='approved' for r in report['feedback'])}
out=root/'docs/evidence/customer_operations/acceptance-summary.json'
out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'checks':report['checks'],'output':str(out)},indent=2))
for cid in ('CASE-000087','CASE-000089'):
    print(cid)
    for m in cases[cid]['messages']:
        if m['channel']=='review_replay': print(m['message_id'],m['body'])
    for e in cases[cid]['events'][-8:]:
        print(e['summary'],e['status'],e['evidence'][:450] if e['evidence'] else '')
