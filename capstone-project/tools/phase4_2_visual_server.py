"""Loopback-only QA server: isolated cases and persisted actual distributed events."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from capstone_rag import config,store,telemetry
from capstone_rag.webapp.app import create_app

os.environ['CAPSTONE_AI_SETTINGS']=str(Path(config.DATA_DIR)/'phase4_2_acceptance/settings.db')
directory=Path(tempfile.mkdtemp(prefix='phase42-visual-'))
shutil.copy2(config.DB_PATH,directory/'operations.db')
st=store.Store(str(directory/'operations.db'))
trace=json.loads(Path('docs/phase4_2/distributed_case_trace.json').read_text())
for task in trace['remote_tasks']:
    telemetry.register(st,task['logical_agent_id'],task['logical_agent_id'].replace('_',' ').title(),'distributed advisory')
for event in trace['arena_events']:
    if st.one('SELECT 1 FROM arena_events WHERE event_id=?',(event['event_id'],)):continue
    row={k:v for k,v in event.items() if k!='sequence'}
    for key in ('evidence_refs','detail'):
        if not isinstance(row.get(key),str):row[key]=json.dumps(row.get(key) or {})
    st.insert('arena_events',row)
app=create_app(prefer_llm=False,store=st,start_workers=False)
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8767,log_level='warning')
