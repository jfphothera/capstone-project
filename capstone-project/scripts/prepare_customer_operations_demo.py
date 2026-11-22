"""Stage fictional browser acceptance cases in a new isolated SQLite copy."""
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from capstone_rag import caseflow, movement, store

db=ROOT/'data/_customer_operations_demo_20261027.db'
if db.exists():
    raise SystemExit('Demo database already exists; refusing to overwrite evidence')
source=sqlite3.connect(f"file:{(ROOT/'data/operations.db').as_posix()}?mode=ro",uri=True)
target=sqlite3.connect(db); source.backup(target); target.close(); source.close()
st=store.Store(str(db)); agent=caseflow.make_agent(prefer_llm=False,store=st); flow=caseflow.CaseFlow(st,agent)
prefix='My name is Ivan Nowak. Serial 98148469. '
scenarios=[
 ('no_power','CX-01 Power troubleshooting', 'The display is dark and the unit will not power on.'),
 ('component','CX-02 Broken goggles', 'The supplied protective goggles arrived broken. Please help with this damaged component.'),
 ('dropoff','CX-03 Return by drop-off', 'The authorized exterior checks did not resolve the fault. Technical review recommends return of the device.'),
 ('pickup','CX-04 Return by pickup', 'The authorized exterior checks did not resolve the fault. Technical review recommends return of the device.'),
 ('exchange','CX-05 Linked device exchange', 'The device remains faulty after approved checks. We need a replacement device and return of the original.'),
 ('failure','CX-06 Shipping failure simulation', 'The protective goggles were missing from the box.'),
 ('rejection','CX-07 Rejected part movement', 'The protective goggles were missing from the box.'),
 ('email','CX-08 Two-way email follow-up', 'Could you clarify which information you need for my service inquiry?'),
]
manifest={'database':str(db),'cases':{},'seed_mode':'deterministic fixture preparation; browser actions are separately recorded'}
for key,subject,body in scenarios:
    cid=flow.intake(subject,prefix+body)
    flow.pre_investigate(cid)
    if key in ('dropoff','pickup'):
        decision=movement.MovementDecision(movement.DEVICE_RETURN,movement.INBOUND,
            [{'part_number':'UNIT','name':'Original device'}],rule='MR-4-REVIEWED',
            reason='Fictional technical-review disposition: authorized exterior checks exhausted; qualified service return required',
            evidence={'procedure':'PROC-OPS-MOVEMENT','fixture_disposition':'reviewed technical assessment, not automated diagnosis'})
        flow.erp.propose_movement(cid,decision,account_id=st.case(cid)['account_id'],serial_number=st.case(cid)['serial_number'])
    manifest['cases'][key]={'case_id':cid,'subject':subject,'rmas':st.query('SELECT rma_id,movement_type,prerequisite_rma_id FROM rmas WHERE case_id=?',(cid,))}
st.close()
out=ROOT/'docs/evidence/customer_operations/browser-demo-manifest.json'
out.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print(json.dumps(manifest,indent=2))
