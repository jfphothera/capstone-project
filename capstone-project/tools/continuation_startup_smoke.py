"""Normal run_app startup on copied state, no historical case replay."""
import http.cookiejar,json,os,secrets,socket,sqlite3,subprocess,sys,tempfile,time,urllib.error,urllib.request
from contextlib import closing
from pathlib import Path

def main():
    root=Path(__file__).resolve().parents[1];out=root/'docs/phase4_2_continuation';out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='capstone-startup-') as temp:
        folder=Path(temp);database=folder/'operations.db'
        with closing(sqlite3.connect('file:'+(root/'data/operations.db').as_posix()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(database)) as target:
            source.backup(target);target.execute("UPDATE cases SET run_id='ISOLATED-STARTUP-SMOKE'")
            target.commit()
        settings=folder/'configuration.db'
        with closing(sqlite3.connect('file:'+(root/'data/ai_configuration.db').as_posix()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(settings)) as target:source.backup(target)
        with socket.socket() as reserve:reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
        token=secrets.token_urlsafe(32);env=os.environ.copy();env.update(CAPSTONE_AI_SETTINGS=str(settings),CAPSTONE_DEVELOPER_TOKEN=token,
            HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        log=open(out/'startup.log','w',encoding='utf-8')
        child=subprocess.Popen([sys.executable,str(root/'run_app.py'),'--no-llm','--host','127.0.0.1','--port',str(port),'--db',str(database)],
            cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        origin='http://127.0.0.1:'+str(port)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path,body=None):
            req=urllib.request.Request(origin+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            try:
                with opener.open(req,timeout=5) as response:return response.status,response.read()
            except urllib.error.HTTPError as exc:return exc.code,exc.read()
        try:
            deadline=time.time()+90
            while time.time()<deadline:
                if child.poll() is not None:raise RuntimeError('Normal application startup exited; see isolated startup.log')
                try:
                    if request('/api/health')[0]==200:break
                except OSError:pass
                time.sleep(.2)
            else:raise TimeoutError('Application startup smoke deadline exceeded')
            statuses={}
            for route,expected in [('/',200),('/chat',200),('/trace',403),('/pressure-testing',403),('/agent-arena',404),('/agent-arena-login',404)]:
                status,_=request(route);statuses[route]=status;assert status==expected,(route,status)
            public=json.loads(request('/api/health')[1]);assert public['ok'] and 'store' not in public
            assert request('/api/developer/session',{'token':token})[0]==200
            for route in ('/trace','/pressure-testing'):
                status,_=request(route);statuses[route+' authenticated']=status;assert status==200
            result={'normal_run_app_startup':True,'copied_state':True,'historical_cases_not_replayed':True,'statuses':statuses,
                'public_health_ok':public['ok'],'arena_service_required':False,'temporary_port':port}
            (out/'startup_smoke.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
        finally:
            # Windows venv's launcher can own a second interpreter. Terminate
            # only this smoke process tree so the actual app releases its DBs.
            if os.name=='nt' and child.poll() is None:
                subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,check=True,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            elif child.poll() is None:child.terminate()
            child.wait(timeout=15);log.close()

if __name__=='__main__':main()
