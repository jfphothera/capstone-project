"""Managed service lifecycle: SCM state machine, real ctypes binding, config, drain, crash recovery, logs.

Console-mode processes run the same runtime the SCM host runs. No Windows service is
registered; SCM registration itself is verified physically (docs/ACCEPTANCE_PLAN.md).
"""
import http.server
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import pytest
from shared_inference_client import Client
from inference_gateway import service_config,windows_service
from inference_gateway.windows_service import ServiceHost,START_PENDING,RUNNING,STOP_PENDING,STOPPED
from inference_gateway.capacity_gateway import Gateway
from inference_gateway import capacity_lifecycle as lifecycle

ROOT=Path(__file__).resolve().parents[1]
LAUNCHER=ROOT/'src'/'inference_gateway'/'service_launcher.py'


class FakeScm:
    def __init__(self):self.statuses=[];self.handler=None
    def register(self,handler):self.handler=handler
    def set_status(self,**fields):self.statuses.append(fields)


class FakeRuntime:
    def __init__(self,fail_start=False):self.fail_start=fail_start;self.alive=True;self.stopped=False
    def start(self):
        if self.fail_start:raise RuntimeError('ledger owned elsewhere')
        return self
    def running(self):return self.alive
    def stop(self,progress=None):
        for _ in range(3):progress({'active_allocations':1})
        self.stopped=True;return {}


def run_host(runtime,action):
    scm=FakeScm();host=ServiceHost(scm,lambda:runtime,poll_seconds=.01);thread=threading.Thread(target=lambda:setattr(host,'code',host.main()));thread.start()
    deadline=time.time()+5
    while time.time()<deadline and not any(s['state']==RUNNING for s in scm.statuses) and thread.is_alive():time.sleep(.01)
    action(scm,runtime);thread.join(5)
    return scm.statuses,host.code


def test_scm_state_machine_stop_drain_and_failures():
    statuses,code=run_host(FakeRuntime(),lambda scm,runtime:scm.handler(windows_service.CONTROL_STOP))
    states=[s['state'] for s in statuses]
    assert states[0]==START_PENDING and states[1]==RUNNING and states[-1]==STOPPED and code==0
    assert statuses[1]['accepted']==windows_service.ACCEPT_STOP|windows_service.ACCEPT_PRESHUTDOWN
    drain=[s['checkpoint'] for s in statuses if s['state']==STOP_PENDING]
    assert len(drain)>=4 and drain==sorted(drain) and len(set(drain))==len(drain) # checkpoints advance while draining
    assert statuses[-1]['exit_code']==0
    statuses,code=run_host(FakeRuntime(),lambda scm,runtime:scm.handler(windows_service.CONTROL_PRESHUTDOWN))
    assert statuses[-1]['state']==STOPPED and code==0
    statuses,code=run_host(FakeRuntime(),lambda scm,runtime:setattr(runtime,'alive',False))
    assert statuses[-1]=={**statuses[-1],'state':STOPPED,'exit_code':1066,'specific':windows_service.EXIT_RUNTIME_FAILED} and code==2
    scm=FakeScm();host=ServiceHost(scm,lambda:FakeRuntime(fail_start=True))
    assert host.main()==1 and scm.statuses[-1]['exit_code']==1066 and scm.statuses[-1]['specific']==windows_service.EXIT_START_FAILED
    assert host.control(windows_service.CONTROL_INTERROGATE)==0 and host.control(200)==windows_service.ERROR_CALL_NOT_IMPLEMENTED


@pytest.mark.skipif(os.name!='nt',reason='advapi32 service APIs exist only on Windows')
def test_win32_binding_reaches_the_real_dispatcher():
    import ctypes
    scm=windows_service.Win32Scm('InferenceGatewayTest')
    assert ctypes.sizeof(scm.SERVICE_STATUS)==28
    with pytest.raises(OSError) as refused:scm.dispatch(lambda:None)
    assert refused.value.args[0]==windows_service.ERROR_FAILED_SERVICE_CONTROLLER_CONNECT # not started by the SCM


class Ollama(http.server.ThreadingHTTPServer):
    """Loopback stand-in for one head, reached through the gateway's real HTTP upstream."""
    daemon_threads=True
    def __init__(self):
        self.calls=0;self.release=threading.Event();self.entered=threading.Event()
        super().__init__(('127.0.0.1',0),OllamaHandler)


class OllamaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def reply(self,value):
        data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/api/version':return self.reply({'version':'0.12.3'})
        return self.reply({'models':[{'name':'m:1','digest':'D1'}]})
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path=='/api/show':return self.reply({'capabilities':['completion']})
        self.server.calls+=1
        if body.get('prompt')=='slow':self.server.entered.set();self.server.release.wait(15)
        self.reply({'done':True,'model':'m:1','response':'READY','eval_count':1})


@pytest.fixture
def deployment(tmp_path,certificates):
    ollama=Ollama();threading.Thread(target=ollama.serve_forever,daemon=True).start()
    for name in ('state','logs'):(tmp_path/name).mkdir()
    ledger=tmp_path/'state'/'capacity-ledger.db';gateway=Gateway(ledger)
    gateway.broker.register_client('capstone','t-capstone-'+'x'*32,models=['m:1'])
    gateway.broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:%d'%ollama.server_port,resource_type='shared_inference',
        owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
    assert gateway.qualify('head-192-0-2-3','m:1',operator='ops')['passed'] # real HTTP to the loopback stand-in
    import socket
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    good=certificates['good']
    config={'schema':service_config.SCHEMA,'service_id':'shared-inference','ledger':str(ledger),'listen':{'host':'127.0.0.1','port':port},
            'advertised_url':'https://127.0.0.1:%d'%port,'tls':{'certificate':good['server_certificate'],'key':good['server_key']},
            'shutdown':{'drain_seconds':20},'logging':{'directory':str(tmp_path/'logs'),'max_bytes':1024*1024,'backup_count':3},'health_interval_seconds':5}
    path=tmp_path/'gateway-service.json';path.write_text(json.dumps(config))
    yield {'config':path,'settings':config,'ledger':ledger,'ollama':ollama,'ca':good['ca_file_for_clients'],'url':config['advertised_url'],'tmp':tmp_path}
    ollama.release.set();ollama.shutdown();ollama.server_close()


def start(deployment):
    environment={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH',)}
    process=subprocess.Popen([sys.executable,'-I',str(LAUNCHER),'console','--config',str(deployment['config'])],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,env=environment,creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
    line=process.stdout.readline()
    assert line,process.stderr.read()
    assert json.loads(line)['event']=='service.listening'
    return process


def stop(process):
    process.send_signal(signal.CTRL_BREAK_EVENT);out,err=process.communicate(timeout=40)
    return process.returncode,[json.loads(l) for l in out.splitlines() if l.strip()]


def ready(deployment):
    import ssl
    context=ssl.create_default_context(cafile=deployment['ca'])
    try:
        with urllib.request.urlopen(deployment['url']+'/ready',timeout=5,context=context) as response:return response.status
    except urllib.error.HTTPError as error:return error.code


def client(deployment):return Client(deployment['url'],'capstone','t-capstone-'+'x'*32,ca_file=deployment['ca'])


@pytest.mark.skipif(os.name!='nt',reason='Console stop uses CTRL_BREAK_EVENT')
def test_graceful_stop_drains_in_flight_work_then_resumes_on_start(deployment):
    process=start(deployment);ollama=deployment['ollama']
    try:
        assert ready(deployment)==200
        holder={};worker=threading.Thread(target=lambda:holder.setdefault('r',client(deployment).infer('m:1',None,'/api/generate',{'prompt':'slow'},client_key='inflight',timeout=30)))
        worker.start();assert ollama.entered.wait(10)
        process.send_signal(signal.CTRL_BREAK_EVENT)
        time.sleep(1.5)
        with pytest.raises(urllib.error.HTTPError) as refused:client(deployment).infer('m:1',None,'/api/generate',{'prompt':'x'},client_key='late',timeout=5)
        assert refused.value.code==503 and ready(deployment)==503 # draining: no new admission
        ollama.release.set();worker.join(20)
        out,err=process.communicate(timeout=40)
    finally:
        if process.poll() is None:process.kill()
    assert process.returncode==0 and holder['r']['status']=='completed' # the drain let in-flight work finish
    stopped=[json.loads(l) for l in out.splitlines() if l.strip()][-1]
    assert stopped['event']=='service.stopped' and stopped['drained_by_service'] and stopped['active_allocations_remaining']==0
    assert lifecycle.status(lifecycle.CapacityBroker(deployment['ledger']))['resume_on_service_start']
    process=start(deployment)
    try:assert ready(deployment)==200 and client(deployment).status(holder['r']['request']['request_id'])['status']=='completed'
    finally:stop(process)
    log=(deployment['tmp']/'logs'/'gateway-service.log').read_text()
    events=[json.loads(l)['event'] for l in log.splitlines()]
    for expected in ('service.starting','service.recovered','service.listening','service.stop_requested','service.drain_progress','service.stopped','service.admission_resumed_after_stop'):
        assert expected in events,expected
    assert 'slow' not in log and 'READY' not in log and 'x'*32 not in log # no prompts, outputs or tokens


@pytest.mark.skipif(os.name!='nt',reason='Console stop uses CTRL_BREAK_EVENT')
def test_crash_recovery_never_replays_and_restarts_cleanly(deployment):
    process=start(deployment);ollama=deployment['ollama']
    worker=threading.Thread(target=lambda:client(deployment).request('/v1/submit',{'contract':'shared.inference/1','client_key':'crash','model':'m:1',
        'nodes':['@authorized'],'deadline':time.time()+60,'operation':'/api/generate','payload':{'prompt':'slow'}}))
    worker.start();assert ollama.entered.wait(10);worker.join(10)
    process.kill();process.wait(10) # hard crash with inference in flight
    calls=ollama.calls;ollama.release.set()
    process=start(deployment) # kernel released the ownership lock
    try:
        with lifecycle.CapacityBroker(deployment['ledger']).connect() as db:
            request_id=db.execute("SELECT request_id FROM capacity_requests WHERE client_key='crash'").fetchone()[0]
        result=client(deployment).status(request_id)
        assert result['status']=='unknown' and 'result' not in result and ollama.calls==calls # uncertain, never replayed
        assert ready(deployment)==200
    finally:code,lines=stop(process)
    assert code==0
    log=(deployment['tmp']/'logs'/'gateway-service.log').read_text()
    assert any(json.loads(l).get('interrupted_requests')==1 for l in log.splitlines() if 'service.recovered' in l)


@pytest.mark.skipif(os.name!='nt',reason='Console stop uses CTRL_BREAK_EVENT')
def test_operator_drain_survives_restart_and_second_owner_is_refused(deployment):
    lifecycle.set_admission(lifecycle.CapacityBroker(deployment['ledger']),False,'ops')
    process=start(deployment)
    try:
        assert ready(deployment)==503
        second=subprocess.run([sys.executable,'-I',str(LAUNCHER),'console','--config',str(deployment['config'])],capture_output=True,text=True,timeout=30)
        assert second.returncode==1 and 'owns this ledger' in second.stdout
        status=subprocess.run([sys.executable,'-I',str(LAUNCHER),'status','--config',str(deployment['config'])],capture_output=True,text=True,timeout=30)
        report=json.loads(status.stdout)
        assert report['service_running'] and not report['admission_open'] and status.returncode==1 # drained: a failed readiness check
        assert report['heads']['head-192-0-2-3']['qualified_models']==['m:1'] and report['applications']['enabled']==['capstone']
    finally:code,lines=stop(process)
    assert code==0
    lifecycle.set_admission(lifecycle.CapacityBroker(deployment['ledger']),True,'ops')


def test_configuration_refusals(deployment,certificates):
    valid=deployment['settings']
    assert service_config.load(deployment['config'])['listen']['port']==valid['listen']['port']
    cases={'relative ledger':{'ledger':'state\\ledger.db'},'missing ledger':{'ledger':str(deployment['tmp']/'none.db')},
           'plaintext LAN':{'listen':{'host':'192.0.2.254','port':8443},'tls':{},'advertised_url':'http://192.0.2.254:8443'},
           'unknown key':{'tokens':{'capstone':'secret'}},'port mismatch':{'advertised_url':'https://127.0.0.1:1'},
           'SAN mismatch':{'tls':{'certificate':certificates['wrong-san']['server_certificate'],'key':certificates['wrong-san']['server_key']}},
           'bad drain':{'shutdown':{'drain_seconds':5000}}}
    for name,change in cases.items():
        path=deployment['tmp']/'bad.json';path.write_text(json.dumps({**valid,**change}))
        with pytest.raises(service_config.ConfigError):service_config.load(path)
        assert windows_service.main(['validate','--config',str(path)])==2,name


def test_service_log_is_bounded_and_rotates(tmp_path):
    from inference_gateway.service_runtime import service_logger
    settings={'logging':{'directory':str(tmp_path),'max_bytes':64*1024,'backup_count':2}}
    log=service_logger(settings,name='test-rotation')
    for index in range(4000):log.info(json.dumps({'event':'capacity.queued','event_id':index}))
    for handler in log.handlers:handler.close()
    files=sorted(p.name for p in tmp_path.iterdir())
    assert files==['gateway-service.log','gateway-service.log.1','gateway-service.log.2']
    assert all(p.stat().st_size<=64*1024+200 for p in tmp_path.iterdir())
