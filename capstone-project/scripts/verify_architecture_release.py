"""Preservation, persisted browser scenarios and isolated API export smoke check."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
out=root/'docs/evidence/multi_agent'
report={'original_tables':[],'protected_files':[]}
old=sqlite3.connect(f'file:{(out/"baseline/operations.db").as_posix()}?mode=ro',uri=True)
live=sqlite3.connect(f'file:{(root/"data/operations.db").as_posix()}?mode=ro',uri=True)
quote=lambda x:'"'+x.replace('"','""')+'"'
for (table,) in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
    cols=[r[1] for r in old.execute('PRAGMA table_info('+quote(table)+')')];columns=','.join(map(quote,cols))
    before=old.execute(f'SELECT {columns} FROM {quote(table)}').fetchall();after=live.execute(f'SELECT {columns} FROM {quote(table)}').fetchall()
    report['original_tables'].append({'table':table,'rows_before':len(before),'rows_now':len(after),'original_values_retained':all(r in after for r in before)})
old.close();live.close()
bundle=Path(r'REDACTED/capstone_review_bundle.zip')
with zipfile.ZipFile(bundle) as archive:
    for name in archive.namelist():
        rel=name.removeprefix('snapshot/')
        if not name.startswith('snapshot/data/'):continue
        if (rel.startswith(('data/qms/','data/erp/','data/labels/','data/evaluation/')) or rel in ('data/ticket_log.jsonl','data/escalations.jsonl','data/memory/resolutions.jsonl','data/revision_register.json','data/qms_compatibility.json')) and not name.endswith('/'):
            p=root/rel
            report['protected_files'].append({'path':rel,'matches_review_snapshot':p.exists() and p.read_bytes()==archive.read(name)})
report['passed']=all(r['original_values_retained'] for r in report['original_tables']) and all(r['matches_review_snapshot'] for r in report['protected_files'])
(out/'preservation.json').write_text(json.dumps(report,indent=2)+'\n')

db=sqlite3.connect(f'file:{(root/"data/_multi_agent_demo_20261028.db").as_posix()}?mode=ro',uri=True);db.row_factory=sqlite3.Row
def rows(sql,args=()):return [dict(r) for r in db.execute(sql,args)]
acceptance={'database':'data/_multi_agent_demo_20261028.db','case_notes':rows('SELECT * FROM case_notes'),
    'tags':rows('SELECT * FROM case_tag_events'),'contacts':rows('SELECT * FROM contact_observations'),
    'contact_reviews':rows('SELECT * FROM contact_decisions'),'workflow_runs':rows('SELECT workflow_id,case_id,status FROM workflow_runs'),
    'checks':{'callback_no_email':not rows("SELECT 1 FROM messages WHERE case_id='CASE-000082' AND direction='outbound'"),
        'callback_no_movement':not rows("SELECT 1 FROM rmas WHERE case_id='CASE-000082'"),
        'component_shipment':bool(rows("SELECT 1 FROM sim_shipments WHERE case_id='CASE-000083'")),
        'shipping_email_separately_sent':bool(rows("SELECT 1 FROM messages WHERE case_id='CASE-000083' AND status='sent'")),
        'workflow_resumed_after_approvals':bool(rows("SELECT 1 FROM workflow_runs WHERE case_id='CASE-000083' AND status='completed'")),
        'chat_graph_findings':len(rows('SELECT * FROM conversation_findings'))>=3}}
(out/'browser-acceptance.json').write_text(json.dumps(acceptance,indent=2)+'\n');db.close()

# Copy the browser database for route/export checks: never write the benchmark
# databases, original operation records or the browser scenario while scoring.
scratch=out/'api-export-smoke.db'
source=sqlite3.connect(f'file:{(root/"data/_multi_agent_demo_20261028.db").as_posix()}?mode=ro',uri=True);dest=sqlite3.connect(scratch)
source.backup(dest);dest.close();source.close()
from capstone_rag import config,store,agent
config.DB_PATH=str(scratch)
config.MEMORY_PATH=str(out/'api-memory.jsonl');config.ESCALATION_PATH=str(out/'api-escalations.jsonl');config.RMA_PATH=str(out/'api-approvals.jsonl');config.TICKET_LOG_PATH=str(out/'api-tickets.jsonl')
from capstone_rag.webapp import create_app
from fastapi.testclient import TestClient
st=store.Store(str(scratch));app=create_app(prefer_llm=False,store=st,agent=agent.Agent(prefer_llm=False),start_workers=False)
with TestClient(app) as client:
    response=client.get('/api/trace/case/CASE-000083');assert response.status_code==200,response.text
    result=response.json();assert len(result['contact_enrichment']['contact_decisions'])==2
    assert result['workflow_runs'][0]['status']=='completed'
    export=client.post('/api/trace/case/CASE-000083/export');assert export.status_code==200,export.text
    (out/'api-export-smoke.json').write_text(json.dumps(export.json(),indent=2)+'\n')
st.close()
print(json.dumps({'preservation_passed':report['passed'],'tables':len(report['original_tables']),'protected_files':len(report['protected_files']),
    'browser_checks':acceptance['checks'],'api_export':'passed'},indent=2))
assert report['passed'] and all(acceptance['checks'].values())
