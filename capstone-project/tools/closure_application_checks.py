"""Read-only integrity checks plus optional copied-state no-LLM startup.

Writes only closure evidence and temporary fixture state; no original database,
model cache, network configuration, service account or certificate is changed.
"""
import argparse,http.cookiejar,json,os,secrets,socket,sqlite3,subprocess,sys,tempfile,time,urllib.request,urllib.error
from contextlib import closing
from pathlib import Path
from capstone_rag import index,qms,telemetry
from capstone_rag.store import Store

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'docs/phase4_2_closure'

def backup(source,target):
    with closing(sqlite3.connect('file:'+source.as_posix()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(target)) as b:a.backup(b)

def startup():
    with tempfile.TemporaryDirectory(prefix='archive-startup-check-') as temp:
        folder=Path(temp);database=folder/'operations.db';settings=folder/'configuration.db'
        backup(ROOT/'data/operations.db',database);backup(ROOT/'data/ai_configuration.db',settings)
        with closing(sqlite3.connect(database)) as db:db.execute("UPDATE cases SET run_id='TEST-ARCHIVE-STARTUP'");db.commit()
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        secret=secrets.token_urlsafe(32);env=os.environ.copy();env.update(CAPSTONE_AI_SETTINGS=str(settings),CAPSTONE_DEVELOPER_TOKEN=secret,
            CAPSTONE_INFERENCE_MODE='development',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        log=open(OUT/'startup.log','w',encoding='utf-8')
        child=subprocess.Popen([sys.executable,str(ROOT/'run_app.py'),'--no-llm','--host','127.0.0.1','--port',str(port),'--db',str(database)],
            cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path,body=None):
            req=urllib.request.Request('http://127.0.0.1:'+str(port)+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            try:
                with opener.open(req,timeout=5) as response:return response.status,response.read()
            except urllib.error.HTTPError as exc:return exc.code,exc.read()
        try:
            end=time.monotonic()+90
            while time.monotonic()<end:
                if child.poll() is not None:raise RuntimeError('Startup exited; see closure startup.log')
                try:
                    if request('/api/health')[0]==200:break
                except OSError:pass
                time.sleep(.2)
            else:raise TimeoutError('Copied-state startup exceeded 90 seconds')
            statuses={}
            for path,expected in [('/',200),('/chat',200),('/trace',403),('/pressure-testing',403),('/agent-arena',404),('/agent-arena-login',404)]:
                code,_=request(path);statuses[path]=code;assert code==expected,(path,code)
            public=json.loads(request('/api/health')[1]);assert public['ok'] and 'store' not in public
            assert request('/api/developer/session',{'token':secret})[0]==200
            for path in ('/trace','/pressure-testing'):
                code,_=request(path);statuses[path+' authenticated']=code;assert code==200
        finally:
            if child.poll() is None:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                else:child.terminate()
            child.wait(timeout=15);log.close()
        # Explicitly synthetic record, in the temporary copied database only.
        store=Store(str(database))
        try:
            event=telemetry.emit(store,'closure.verification',agent_id='coordinator',task_status='completed',summary='TEST: archival telemetry self-check',detail={'synthetic_verification':True})
            telemetry_ok=bool(store.one('SELECT event_id FROM arena_events WHERE event_id=?',(event['event_id'],)))
        finally:store.close()
        result={'startup_ok':True,'statuses':statuses,'telemetry_ok':telemetry_ok,'synthetic_telemetry_self_test':True,'copied_state_only':True,
            'arena_required':False,'physical_inference_run':False,'clean_child_process_shutdown':True}
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--startup',action='store_true');args=p.parse_args();OUT.mkdir(exist_ok=True)
    result={'databases':{},'qms_fingerprint':index.corpus_fingerprint(qms.load_chunks()),'startup_ok':False,'telemetry_ok':False,'archive_performed':False}
    for name in ('operations.db','ai_configuration.db','shared_capacity.db'):
        path=ROOT/'data'/name
        if not path.exists():continue
        with closing(sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True)) as db:
            entry={'integrity':db.execute('PRAGMA integrity_check').fetchone()[0]}
            if name=='operations.db':entry['physical_nodes']=db.execute('SELECT count(*) FROM physical_nodes').fetchone()[0]
            if name=='ai_configuration.db':entry['active_profile']=db.execute("SELECT value FROM preferences WHERE key='active_profile'").fetchone()[0]
            result['databases'][name]=entry
    assert result['qms_fingerprint']=='a3b68d86451db05e' and all(v['integrity']=='ok' for v in result['databases'].values())
    if args.startup:result.update(startup())
    (OUT/'application_checks.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':main()
