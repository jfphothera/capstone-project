"""Exercise real Uvicorn/FastAPI startup and graceful shutdown twice."""
import json,sys,threading,time,sqlite3,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag import startup
startup.require()
from capstone_rag import store
from capstone_rag.webapp import create_app
from capstone_rag.webapp.runtime import get_runtime
import uvicorn
root=Path('docs/evidence/local_integration');baseline=Path((root/'baseline-location.txt').read_text().strip())/'operations.db'
directory=Path('data/startup_probes')/uuid.uuid4().hex;directory.mkdir(parents=True);db=directory/'operations.db'
with sqlite3.connect(baseline) as source,sqlite3.connect(db) as target:source.backup(target)
results=[]
for cycle in range(2):
    st=store.Store(str(db));server=uvicorn.Server(uvicorn.Config(create_app(prefer_llm=True,store=st),host='127.0.0.1',port=8001,log_level='info'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+60
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.1)
    if not server.started:raise RuntimeError('Real app startup failed')
    rt=get_runtime();running=rt.ops.queue.running
    server.should_exit=True;thread.join(timeout=15)
    result={'cycle':cycle+1,'started':server.started,'worker_started':running,'server_stopped':not thread.is_alive(),'worker_stopped':not rt.ops.queue.running,'arrivals_stopped':not rt.ops.simulator.running,'shutdown_events':len(st.query("SELECT * FROM audit_events WHERE event_type='webapp_stopped'"))}
    results.append(result);st.close();print(json.dumps(result),flush=True)
    if not all(result[k] for k in ['started','worker_started','server_stopped','worker_stopped','arrivals_stopped']):raise RuntimeError('Lifecycle did not cleanly complete')
(root/'startup-shutdown-proof.json').write_text(json.dumps({'isolated_database':str(db),'cycles':results},indent=2)+'\n',encoding='utf-8')
