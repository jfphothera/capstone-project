"""Windows Service Control Manager host for the Gateway (standard library ctypes, no pywin32).

The process registered with the SCM is the interpreter that runs this module: it
calls StartServiceCtrlDispatcherW, registers a control handler, reports
START_PENDING/RUNNING/STOP_PENDING/STOPPED with checkpoints, and runs the
pre-stop drain on STOP and PRESHUTDOWN. A failure exits with a service-specific
code so SCM recovery actions apply. Register the *base* interpreter (not a venv
redirector, whose child process cannot connect to the SCM).

Commands: service | console | validate | status. Each non-service command prints
one JSON object; exit 0 success, 1 failed check, 2 bad input.
"""
import argparse
import faulthandler
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from . import service_config

SERVICE_NAME='InferenceGateway'
SERVICE_WIN32_OWN_PROCESS=0x10
STOPPED,START_PENDING,STOP_PENDING,RUNNING=1,2,3,4
CONTROL_STOP,CONTROL_INTERROGATE,CONTROL_SHUTDOWN,CONTROL_PRESHUTDOWN=1,4,5,0xF
ACCEPT_STOP,ACCEPT_PRESHUTDOWN=0x1,0x100
NO_ERROR,ERROR_CALL_NOT_IMPLEMENTED,ERROR_SERVICE_SPECIFIC_ERROR,ERROR_FAILED_SERVICE_CONTROLLER_CONNECT=0,120,1066,1063
EXIT_START_FAILED,EXIT_RUNTIME_FAILED=1,2


class ServiceHost:
    """SCM state machine. `scm` provides register(handler) and set_status(**fields)."""
    def __init__(self,scm,runtime_factory,*,poll_seconds=1):
        self.scm=scm;self.factory=runtime_factory;self.poll=poll_seconds
        self.stop_requested=threading.Event();self.checkpoint=0;self.state=None

    def report(self,state,*,accepted=0,exit_code=NO_ERROR,specific=0,wait_hint=0):
        self.checkpoint=0 if state in (RUNNING,STOPPED) else self.checkpoint+1;self.state=state
        self.scm.set_status(state=state,accepted=accepted,exit_code=exit_code,specific=specific,checkpoint=self.checkpoint,wait_hint=wait_hint)

    def control(self,code):
        if code in (CONTROL_STOP,CONTROL_SHUTDOWN,CONTROL_PRESHUTDOWN):
            self.stop_requested.set();self.report(STOP_PENDING,wait_hint=30000);return NO_ERROR
        if code==CONTROL_INTERROGATE:return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    def main(self):
        self.scm.register(self.control)
        self.report(START_PENDING,wait_hint=60000)
        try:runtime=self.factory().start()
        except BaseException:
            self.report(STOPPED,exit_code=ERROR_SERVICE_SPECIFIC_ERROR,specific=EXIT_START_FAILED);return EXIT_START_FAILED
        self.report(RUNNING,accepted=ACCEPT_STOP|ACCEPT_PRESHUTDOWN)
        failed=False
        while not self.stop_requested.wait(self.poll):
            if not runtime.running():failed=True;break
        # Drain with checkpoints so the SCM keeps waiting while allocations finish.
        try:runtime.stop(progress=lambda state:self.report(STOP_PENDING,wait_hint=10000))
        except BaseException:failed=True
        if failed:self.report(STOPPED,exit_code=ERROR_SERVICE_SPECIFIC_ERROR,specific=EXIT_RUNTIME_FAILED);return EXIT_RUNTIME_FAILED
        self.report(STOPPED);return 0


class Win32Scm:
    """ctypes binding to advapi32 service APIs."""
    def __init__(self,name=SERVICE_NAME):
        import ctypes
        from ctypes import wintypes
        self.ctypes=ctypes;self.name=name;self.handle=None
        advapi=ctypes.WinDLL('advapi32',use_last_error=True)
        class SERVICE_STATUS(ctypes.Structure):
            _fields_=[(n,wintypes.DWORD) for n in ('dwServiceType','dwCurrentState','dwControlsAccepted','dwWin32ExitCode',
                      'dwServiceSpecificExitCode','dwCheckPoint','dwWaitHint')]
        self.MAIN=ctypes.WINFUNCTYPE(None,wintypes.DWORD,ctypes.POINTER(wintypes.LPWSTR))
        self.HANDLER=ctypes.WINFUNCTYPE(wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.LPVOID)
        class ENTRY(ctypes.Structure):_fields_=[('lpServiceName',wintypes.LPWSTR),('lpServiceProc',self.MAIN)]
        self.SERVICE_STATUS,self.ENTRY=SERVICE_STATUS,ENTRY
        self.dispatcher=advapi.StartServiceCtrlDispatcherW;self.dispatcher.argtypes=[ctypes.POINTER(ENTRY)];self.dispatcher.restype=wintypes.BOOL
        self.registrar=advapi.RegisterServiceCtrlHandlerExW;self.registrar.argtypes=[wintypes.LPCWSTR,self.HANDLER,wintypes.LPVOID];self.registrar.restype=wintypes.HANDLE
        self.setter=advapi.SetServiceStatus;self.setter.argtypes=[wintypes.HANDLE,ctypes.POINTER(SERVICE_STATUS)];self.setter.restype=wintypes.BOOL

    def dispatch(self,service_main):
        """Blocks until the service stops. Raises OSError(1063) when not started by the SCM."""
        self._main=self.MAIN(lambda argc,argv:service_main()) # references kept alive for the callback
        table=(self.ENTRY*2)(self.ENTRY(self.name,self._main),self.ENTRY(None,self.MAIN()))
        if not self.dispatcher(table):raise OSError(self.ctypes.get_last_error(),'StartServiceCtrlDispatcherW failed')

    def register(self,handler):
        self._handler=self.HANDLER(lambda code,event,data,context:handler(code))
        self.handle=self.registrar(self.name,self._handler,None)
        if not self.handle:raise OSError(self.ctypes.get_last_error(),'RegisterServiceCtrlHandlerExW failed')

    def set_status(self,*,state,accepted,exit_code,specific,checkpoint,wait_hint):
        status=self.SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS,state,accepted,exit_code,specific,checkpoint,wait_hint)
        if not self.setter(self.handle,self.ctypes.byref(status)):raise OSError(self.ctypes.get_last_error(),'SetServiceStatus failed')


def _print(value):
    if sys.stdout:print(json.dumps(value,default=str),flush=True)


def _runtime(settings,**kwargs):
    from .service_runtime import GatewayRuntime
    return GatewayRuntime(settings,**kwargs)


def _fault_log(settings):
    handle=open(Path(settings['logging']['directory'])/'gateway-fault.log','a',encoding='utf-8')
    faulthandler.enable(handle);return handle


def run_service(settings,name=SERVICE_NAME):
    if sys.stdout is None or sys.stderr is None:sys.stdout=sys.stderr=open(os.devnull,'w') # SCM gives no console
    _fault_log(settings)
    scm=Win32Scm(name);host=ServiceHost(scm,lambda:_runtime(settings));result={}
    scm.dispatch(lambda:result.setdefault('code',host.main()))
    return result.get('code',0)


def run_console(settings,*,stop=None,ready=None):
    """Foreground mode with the same runtime and drain; Ctrl+C or Ctrl+Break stops gracefully."""
    stop=stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGINT','SIGBREAK','SIGTERM'):
            if hasattr(signal,name):signal.signal(getattr(signal,name),lambda *a:stop.set())
    runtime=_runtime(settings).start()
    _print({'event':'service.listening','host':settings['listen']['host'],'port':runtime.server.server_port,
            'protocol':'https' if settings.get('tls',{}).get('certificate') else 'http','service_id':settings['service_id'],'mode':'console'})
    if ready:ready(runtime)
    while not stop.wait(.5):
        if not runtime.running():
            runtime.stop();_print({'event':'service.failed'});return 1
    _print({'event':'service.stopped',**runtime.stop()});return 0


def operator_status(settings,*,observation_file=None,publisher_state=None):
    """Read-only operator diagnostics; never reconciles, admits or writes the ledger."""
    from .capacity_observe import snapshot,ledger_identity
    from .capacity_lifecycle import exclusive_lock
    checks={}
    try:
        with exclusive_lock(str(Path(settings['ledger']).resolve())+'.service.lock','held'):running=False
    except RuntimeError:running=True
    identity=ledger_identity(settings['ledger'])
    doc=snapshot(settings['ledger'],after_event_id=max(0,identity['max_event_id']-200),limit=200,service_id=settings['service_id'])
    heads=[r for r in doc['resources'] if r['resource_type']=='shared_inference']
    usable=[r for r in heads if r['health']=='healthy' and r['fresh']]
    checks['service_running']=running;checks['ready']=doc['admission_open']
    out={'service_running':running,'admission_open':doc['admission_open'],'ledger_id':doc['ledger_id'],
         'applications':{'enabled':sorted(a['app_id'] for a in doc['applications'] if a['enabled']),
                         'disabled':sorted(a['app_id'] for a in doc['applications'] if not a['enabled'])},
         'heads':{r['resource_id']:{'health':r['health'],'fresh':r['fresh'],'qualified_models':r['qualified_models']} for r in heads},
         'available_models':sorted({m for r in usable for m in r['qualified_models']}),
         'queue':doc['requests'].get('queued',0),'active_leases':len(doc['leases']),
         'last_hour':doc['last_hour'],'recent_events':[e['event_type'] for e in doc['events'][-20:]],
         'suspensions':doc['suspensions'],'alerts':doc['alerts']}
    if observation_file:
        try:age=time.time()-json.loads(Path(observation_file).read_text(encoding='utf-8'))['observed_at']
        except (OSError,ValueError,KeyError):age=None
        out['publisher']={'observation_age_seconds':age,'fresh':age is not None and age<=30}
        checks['publisher_fresh']=out['publisher']['fresh']
        if publisher_state:
            from .observation_publisher import status as publisher_status
            out['publisher']['state']=publisher_status(settings['ledger'],publisher_state)
            checks['publisher_unblocked']=out['publisher']['state']['blocked'] is None
    out['checks']=checks;out['ok']=all(checks.values())
    return out


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    for name in ('service','console','validate','status'):
        command=sub.add_parser(name);command.add_argument('--config',required=True)
        if name=='service':command.add_argument('--name',default=SERVICE_NAME)
        if name=='status':command.add_argument('--observation-file');command.add_argument('--publisher-state')
    try:a=p.parse_args(argv)
    except SystemExit as exc:return 2 if exc.code else 0
    try:settings=service_config.load(a.config)
    except (service_config.ConfigError,OSError,ValueError) as exc:
        _print({'ok':False,'problems':getattr(exc,'problems',[str(exc)])});return 2
    if a.command=='validate':_print({'ok':True,'settings':service_config.summary(settings)});return 0
    if a.command=='status':
        result=operator_status(settings,observation_file=a.observation_file,publisher_state=a.publisher_state)
        _print(result);return 0 if result['ok'] else 1
    if a.command=='console':
        _fault_log(settings)
        try:return run_console(settings)
        except Exception as exc:_print({'ok':False,'error':type(exc).__name__+': '+str(exc)[:300]});return 1
    try:return run_service(settings,a.name)
    except OSError as exc:
        if exc.errno==ERROR_FAILED_SERVICE_CONTROLLER_CONNECT or getattr(exc,'winerror',None)==ERROR_FAILED_SERVICE_CONTROLLER_CONNECT or exc.args[:1]==(ERROR_FAILED_SERVICE_CONTROLLER_CONNECT,):
            _print({'ok':False,'error':'Not started by the Service Control Manager. Use the console command, or register the base interpreter (not a venv).'});return 2
        raise


if __name__=='__main__':raise SystemExit(main())
