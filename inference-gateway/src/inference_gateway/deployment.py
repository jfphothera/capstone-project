"""Gateway Windows installer: plan, certificate, install, verify, update, rollback, repair, uninstall.

Invoked by the operator or by Agent Arena's F: provisioner from the release
package (bin/gateway-deploy.py). Inputs are a non-secret settings file and, for
install/repair, the service account password on stdin (--password-stdin) or an
interactive prompt; it is passed to the SCM in memory and never stored, logged
or placed on a command line. Every verb prints exactly one JSON object.
Exit 0 success, 1 a failed check or refused step, 2 bad input.

Preserved by every verb: ledger, configuration, certificates, publisher state,
logs and backups. Releases are side by side; update and rollback change only
the registered command line.
"""
import argparse,getpass,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path

SETTINGS_SCHEMA='inference-gateway.deployment-settings/1'
STATE_SCHEMA='inference-gateway.deployment-state/1'
SERVICE_DESCRIPTION='Inference Gateway: authenticated shared inference admission and head scheduling (shared.inference/1)'
PYTHON_SERIES=(3,12)
SYSTEM_SID,ADMINS_SID,USERS_SID,BACKUP_OPERATORS_SID='S-1-5-18','S-1-5-32-544','S-1-5-32-545','S-1-5-32-551'


class Refused(Exception):
    """A check failed or a step was refused (exit 1)."""


def release_version():
    from . import __version__
    return __version__


def _version(text):
    parts=[]
    for piece in str(text).split('.')[:3]:
        digits=''.join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts+[0]*(3-len(parts)))


def _legacy_ledger(path):
    """True when a ledger was last written by 0.3.x (it still has the one-lease-per-head index)."""
    import sqlite3
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='one_active_resource'").fetchone())
    finally:db.close()


# ---------------------------------------------------------------- layout

def layout(settings):
    install=Path(settings['install_root']);data=Path(settings['data_root'])
    return {'install_root':install,'releases':install/'releases','data_root':data,'config':data/'config',
            'state':data/'state','tls':data/'tls','ca':data/'ca','observation':data/'observation',
            'observation_state':data/'observation-state','logs':data/'logs','backups':data/'backups','deployment':data/'deployment',
            'config_file':data/'config'/'gateway-service.json','ledger':data/'state'/'capacity-ledger.db',
            'certificate':data/'tls'/'server.pem','key':data/'tls'/'server.key','client_ca':data/'tls'/'inference-gateway-ca.pem',
            'published':data/'observation'/'shared-inference-observation.json','publisher_state':data/'observation-state'/'publisher-state.json',
            'state_file':data/'deployment'/'deployment-state.json'}


def folder_acls(paths,service_sid):
    """Each folder's complete ACL (inheritance removed). Arena's read ACE is added by the publication ACL script."""
    private=['*%s:(OI)(CI)F'%SYSTEM_SID,'*%s:(OI)(CI)F'%ADMINS_SID]
    reader,writer='*%s:(OI)(CI)RX'%service_sid,'*%s:(OI)(CI)M'%service_sid
    return [(paths['install_root'],private+['*%s:(OI)(CI)RX'%USERS_SID]),(paths['data_root'],private),
            (paths['config'],private+[reader]),(paths['state'],private+[writer]),(paths['tls'],private+[reader]),
            (paths['ca'],private),(paths['observation'],private+[writer]),(paths['observation_state'],private+[writer]),
            (paths['logs'],private+[writer]),(paths['backups'],private),(paths['deployment'],private)]


def service_config(settings,paths):
    from .service_config import SCHEMA
    host,port=settings['listen']['host'],settings['listen']['port']
    return {'schema':SCHEMA,'service_id':settings.get('service_id','shared-inference'),'ledger':str(paths['ledger']),
            'listen':{'host':host,'port':port},'advertised_url':'https://%s:%d'%(host,port),
            'tls':{'certificate':str(paths['certificate']),'key':str(paths['key'])},
            'shutdown':{'drain_seconds':settings.get('drain_seconds',90)},'logging':{'directory':str(paths['logs'])},
            'observation':{'output':str(paths['published']),'state':str(paths['publisher_state'])}}


def command_line(python,release_dir,config_file,service_name):
    launcher=Path(release_dir)/'inference_gateway'/'service_launcher.py'
    return '"%s" -I "%s" service --config "%s" --name %s'%(python,launcher,config_file,service_name)


# ---------------------------------------------------------------- system

class WindowsSystem:
    """Real Windows operations. SCM calls use advapi32 through ctypes."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes,self.w=ctypes,wintypes
        self.advapi=ctypes.WinDLL('advapi32',use_last_error=True)
        class SERVICE_STATUS_PROCESS(ctypes.Structure):
            _fields_=[(n,wintypes.DWORD) for n in ('dwServiceType','dwCurrentState','dwControlsAccepted','dwWin32ExitCode',
                      'dwServiceSpecificExitCode','dwCheckPoint','dwWaitHint','dwProcessId','dwServiceFlags')]
        class SC_ACTION(ctypes.Structure):_fields_=[('Type',ctypes.c_int),('Delay',wintypes.DWORD)]
        class FAILURE_ACTIONS(ctypes.Structure):
            _fields_=[('dwResetPeriod',wintypes.DWORD),('lpRebootMsg',wintypes.LPWSTR),('lpCommand',wintypes.LPWSTR),('cActions',wintypes.DWORD),('lpsaActions',ctypes.POINTER(SC_ACTION))]
        self.STATUS,self.SC_ACTION,self.FAILURE_ACTIONS=SERVICE_STATUS_PROCESS,SC_ACTION,FAILURE_ACTIONS
        for name,res in (('OpenSCManagerW',wintypes.HANDLE),('OpenServiceW',wintypes.HANDLE),('CreateServiceW',wintypes.HANDLE)):getattr(self.advapi,name).restype=res
        self.advapi.CloseServiceHandle.argtypes=[wintypes.HANDLE]

    def _check(self,ok,what):
        if not ok:raise OSError(self.ctypes.get_last_error(),what+' failed')
        return ok

    def _manager(self,access):return self._check(self.advapi.OpenSCManagerW(None,None,self.w.DWORD(access)),'OpenSCManagerW')

    def _service(self,name,access,manager_access=0x1):
        manager=self._manager(manager_access)
        try:
            handle=self.advapi.OpenServiceW(self.w.HANDLE(manager),self.w.LPCWSTR(name),self.w.DWORD(access))
            if not handle:
                error=self.ctypes.get_last_error()
                if error==1060:return None # ERROR_SERVICE_DOES_NOT_EXIST
                raise OSError(error,'OpenServiceW failed')
            return handle
        finally:self.advapi.CloseServiceHandle(manager)

    def service_state(self,name):
        handle=self._service(name,0x4) # SERVICE_QUERY_STATUS
        if handle is None:return None
        try:
            status=self.STATUS();needed=self.w.DWORD()
            self._check(self.advapi.QueryServiceStatusEx(self.w.HANDLE(handle),0,self.ctypes.byref(status),self.ctypes.sizeof(status),self.ctypes.byref(needed)),'QueryServiceStatusEx')
            return {1:'stopped',2:'start_pending',3:'stop_pending',4:'running'}.get(status.dwCurrentState,'other')
        finally:self.advapi.CloseServiceHandle(handle)

    def _configure(self,handle):
        w,c=self.w,self.ctypes
        delayed=w.BOOL(True);self._check(self.advapi.ChangeServiceConfig2W(w.HANDLE(handle),3,c.byref(delayed)),'delayed auto-start')
        description=w.LPWSTR(SERVICE_DESCRIPTION);self._check(self.advapi.ChangeServiceConfig2W(w.HANDLE(handle),1,c.byref(description)),'description')
        actions=(self.SC_ACTION*3)(self.SC_ACTION(1,30000),self.SC_ACTION(1,60000),self.SC_ACTION(1,300000)) # restart: 30 s, 1 min, then every 5 min
        failure=self.FAILURE_ACTIONS(86400,None,None,3,actions);self._check(self.advapi.ChangeServiceConfig2W(w.HANDLE(handle),2,c.byref(failure)),'failure actions')
        flag=w.BOOL(True);self._check(self.advapi.ChangeServiceConfig2W(w.HANDLE(handle),4,c.byref(flag)),'failure actions on error exit')
        preshutdown=w.DWORD(180000);self._check(self.advapi.ChangeServiceConfig2W(w.HANDLE(handle),7,c.byref(preshutdown)),'preshutdown timeout')

    def create_service(self,name,command,account,password):
        manager=self._manager(0x2) # SC_MANAGER_CREATE_SERVICE
        try:
            depends=self.ctypes.create_unicode_buffer('Tcpip\0') # double-NUL-terminated list
            handle=self.advapi.CreateServiceW(self.w.HANDLE(manager),name,'Inference Gateway',self.w.DWORD(0xF01FF),self.w.DWORD(0x10),self.w.DWORD(2),self.w.DWORD(1),
                                              command,None,None,depends,account,password)
            self._check(handle,'CreateServiceW')
        finally:self.advapi.CloseServiceHandle(manager)
        try:self._configure(handle)
        finally:self.advapi.CloseServiceHandle(handle)

    def set_command(self,name,command,account=None,password=None):
        handle=self._check(self._service(name,0xF01FF),'OpenServiceW')
        try:
            none=self.w.DWORD(0xFFFFFFFF) # SERVICE_NO_CHANGE
            depends=self.ctypes.create_unicode_buffer('Tcpip\0')
            self._check(self.advapi.ChangeServiceConfigW(self.w.HANDLE(handle),none,none,none,command,None,None,depends,account,password,None),'ChangeServiceConfigW')
            self._configure(handle)
        finally:self.advapi.CloseServiceHandle(handle)

    def start_service(self,name):
        handle=self._check(self._service(name,0x10),'OpenServiceW') # SERVICE_START
        try:self._check(self.advapi.StartServiceW(self.w.HANDLE(handle),0,None),'StartServiceW')
        finally:self.advapi.CloseServiceHandle(handle)

    def stop_service(self,name):
        handle=self._check(self._service(name,0x20),'OpenServiceW') # SERVICE_STOP
        try:
            status=self.STATUS()
            if not self.advapi.ControlService(self.w.HANDLE(handle),1,self.ctypes.byref(status)) and self.ctypes.get_last_error()!=1062:raise OSError(self.ctypes.get_last_error(),'ControlService failed')
        finally:self.advapi.CloseServiceHandle(handle)

    def delete_service(self,name):
        handle=self._check(self._service(name,0x10000),'OpenServiceW') # DELETE
        try:self._check(self.advapi.DeleteService(self.w.HANDLE(handle)),'DeleteService')
        finally:self.advapi.CloseServiceHandle(handle)

    def is_machine_wide(self,path):
        """Outside every user profile, so the service account can read it."""
        profiles=Path(os.environ.get('SystemDrive','C:')+'\\')/'Users'
        try:Path(path).resolve().relative_to(profiles.resolve());return False
        except ValueError:return True

    def is_elevated(self):
        try:return bool(self.ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:return False

    def resolve_sid(self,account):
        script="(New-Object Security.Principal.NTAccount('%s')).Translate([Security.Principal.SecurityIdentifier]).Value"%account.replace("'","''")
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=60)
        return result.stdout.strip() if result.returncode==0 and result.stdout.strip().startswith('S-1-') else None

    def privileged_member(self,sid):
        """True if sid is (transitively, locally) in Administrators or Backup Operators; None if unreadable."""
        script=(r"$seen=@{};function M($g){if($seen[$g]){return};$seen[$g]=1;$n=((New-Object Security.Principal.SecurityIdentifier($g)).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1];"
                r"$x=[ADSI]('WinNT://'+$env:COMPUTERNAME+'/'+$n+',group');foreach($m in @($x.psbase.Invoke('Members'))){$s=(New-Object Security.Principal.SecurityIdentifier([byte[]]$m.GetType().InvokeMember('objectSid','GetProperty',$null,$m,$null),0)).Value;$s;"
                r"if($m.GetType().InvokeMember('Class','GetProperty',$null,$m,$null) -eq 'Group'){M $s}}};M '%s';M '%s'")%(ADMINS_SID,BACKUP_OPERATORS_SID)
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=60)
        if result.returncode:return None
        return sid in result.stdout.split()

    def icacls(self,path,grants):
        Path(path).mkdir(parents=True,exist_ok=True)
        result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',*grants,'/Q'],capture_output=True,text=True)
        if result.returncode:raise Refused('icacls failed for %s'%path)

    def run(self,command,**kwargs):return subprocess.run(command,capture_output=True,text=True,**kwargs)


# ---------------------------------------------------------------- package

def package_root():
    """The release package folder (…/inference-gateway-<version>/), when running from it."""
    root=os.environ.get('INFERENCE_GATEWAY_PACKAGE')
    return Path(root) if root else None


def verify_package(root):
    sums=root/'SHA256SUMS';bad=[];count=0
    for line in sums.read_text(encoding='utf-8').splitlines():
        if not line.strip():continue
        digest,name=line.split('  ',1);path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('Package path escapes the package')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:bad.append(name)
        count+=1
    if bad:raise Refused('Package files fail their SHA-256: '+', '.join(bad))
    return count


def wheel_in(root):
    wheels=sorted((root/'wheel').glob('inference_gateway-*-py3-none-any.whl'))
    if len(wheels)!=1:raise Refused('The package must contain exactly one Gateway wheel')
    return wheels[0]


# ---------------------------------------------------------------- verbs

class Installer:
    def __init__(self,settings,system,*,package=None):
        self.settings=settings;self.system=system;self.package=package;self.paths=layout(settings)
        self.name=settings.get('service_name','InferenceGateway');self.version=release_version()

    # -- checks shared by plan/install/verify
    def checks(self,*,for_install=True):
        s,p,results=self.settings,self.paths,{}
        python=Path(s['python'])
        probe=self.system.run([str(python),'-I','-c','import sys,json;print(json.dumps([sys.version_info[:3],sys.prefix==sys.base_prefix]))']) if python.is_file() else None
        try:version,base=json.loads(probe.stdout) if probe and probe.returncode==0 else (None,False)
        except ValueError:version,base=None,False
        machine=self.system.is_machine_wide(python)
        results['python']={'ok':bool(version) and tuple(version[:2])==PYTHON_SERIES and base and machine,'found':version,'base_interpreter':base,
                           'machine_wide':machine,'path':str(python),'note':'machine-wide CPython 3.12, not a venv and not under a user profile'}
        results['elevated']={'ok':self.system.is_elevated()}
        sid=self.system.resolve_sid(s['service_account'])
        privileged=self.system.privileged_member(sid) if sid else None
        results['service_account']={'ok':bool(sid) and privileged is False,'sid':sid,'privileged_member':privileged,
            'required_rights':['SeServiceLogonRight'],'note':'standard local account, not in Administrators or Backup Operators'}
        arena=[self.system.resolve_sid(a) for a in s.get('arena_accounts',[])]
        results['arena_accounts']={'ok':bool(arena) and all(arena) and sid not in arena and all(self.system.privileged_member(a) is False for a in arena if a),'sids':arena}
        state=self.system.service_state(self.name)
        results['service']={'ok':state is None if for_install else state=='running','state':state}
        if self.package:
            try:results['package']={'ok':True,'files':verify_package(self.package)}
            except (Refused,OSError,ValueError) as exc:results['package']={'ok':False,'error':str(exc)}
        results['tls']={'ok':p['certificate'].is_file() and p['key'].is_file(),'certificate':str(p['certificate'])}
        results['ledger']={'ok':p['ledger'].is_file(),'path':str(p['ledger'])}
        return results

    def plan(self):
        checks=self.checks();release=self.paths['releases']/self.version
        steps=[{'step':'folders','acl':[(str(path),grants) for path,grants in folder_acls(self.paths,checks['service_account']['sid'] or '<service-sid>')]},
               {'step':'release','target':str(release),'exists':release.exists()},
               {'step':'config','target':str(self.paths['config_file']),'write':not self.paths['config_file'].exists()},
               {'step':'ledger','target':str(self.paths['ledger']),'action':'keep' if checks['ledger']['ok'] else 'requires --new-ledger or --restore-backup'},
               {'step':'tls','action':'keep' if checks['tls']['ok'] else 'run the certificate verb first'},
               {'step':'service','name':self.name,'command':command_line(self.settings['python'],release,self.paths['config_file'],self.name),
                'start':'delayed-auto','depends_on':['Tcpip'],'recovery':'restart 30 s, 60 s, then every 300 s; reset after 1 day','preshutdown_ms':180000},
               {'step':'publication_acl','script':'scripts/Set-ObservationPublicationAcl.ps1'}]
        blocking=[k for k,v in checks.items() if not v['ok'] and k not in ('tls','ledger')]
        return {'ok':not blocking,'verb':'plan','release':self.version,'checks':checks,'blocking':blocking,'steps':steps,'changes_made':False}

    def certificate(self,*,renew=False):
        sid=self.system.resolve_sid(self.settings['service_account'])
        if not sid:raise Refused('The service account does not exist yet')
        script=(self.package or Path(__file__).resolve().parents[2])/'scripts'/'New-GatewayCertificate.ps1'
        pwsh=shutil.which('pwsh')
        if not pwsh:raise Refused('PowerShell 7 (pwsh) is required for certificate issuance')
        args=[pwsh,'-NoProfile','-NonInteractive','-File',str(script),'-CaDirectory',str(self.paths['ca']),'-OutputDirectory',str(self.paths['tls']),
              '-IpAddress',self.settings['listen']['host'],'-KeyReaderAccount','*'+sid]
        if not (self.paths['ca']/'ca.pem').exists():args.append('-NewCa')
        if renew:args.append('-Renew')
        result=self.system.run(args,timeout=120)
        try:issued=json.loads(result.stdout)
        except ValueError:raise Refused('Certificate script failed: '+result.stderr[-300:])
        if result.returncode:raise Refused(issued.get('error','certificate issuance failed'))
        return {'ok':True,'verb':'certificate',**issued,'restart_required':renew}

    def _record(self,**change):
        path=self.paths['state_file'];state=json.loads(path.read_text()) if path.exists() else {'schema':STATE_SCHEMA,'history':[]}
        state.update({k:v for k,v in change.items() if k in ('current','previous')})
        state['history'].append({'at':time.time(),**change});path.write_text(json.dumps(state,indent=2))
        return state

    def _state(self):
        path=self.paths['state_file']
        return json.loads(path.read_text()) if path.exists() else {'schema':STATE_SCHEMA,'history':[]}

    def _release(self,*,replace=False):
        release=self.paths['releases']/self.version
        if release.exists() and not replace:return release
        wheel=wheel_in(self.package) if self.package else None
        if not wheel:raise Refused('Run from the release package to install its wheel')
        staging=release.with_name(release.name+'.staging')
        if staging.exists():shutil.rmtree(staging)
        result=self.system.run([self.settings['python'],'-I','-m','pip','install','--no-index','--no-deps','--no-compile','--target',str(staging),str(wheel)])
        if result.returncode:raise Refused('pip could not install the wheel: '+result.stderr[-300:])
        if release.exists():shutil.rmtree(release)
        os.replace(staging,release)
        self._inherit_acl(release)
        return release

    def _inherit_acl(self,release):
        """0.4.1: pip --target (Python 3.12.10) leaves every release file owner-only (SYSTEM, Administrators,
        OWNER RIGHTS), so the service account cannot read service_launcher.py. Reset the release folder
        to inherit the install root's ACL (Users: read and execute)."""
        if os.name!='nt':return
        result=self.system.run(['icacls',str(release),'/reset','/T','/C','/Q'])
        if result.returncode:raise Refused('icacls /reset failed for %s: %s'%(release,(result.stdout+result.stderr)[-300:]))

    def _validate(self,release):
        launcher=release/'inference_gateway'/'service_launcher.py'
        result=self.system.run([self.settings['python'],'-I',str(launcher),'validate','--config',str(self.paths['config_file'])])
        try:report=json.loads(result.stdout)
        except ValueError:report={'ok':False,'problems':[result.stderr[-300:]]}
        if result.returncode:raise Refused('Configuration does not validate: '+'; '.join(report.get('problems',[])))
        return report

    def _wait(self,want,timeout):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.system.service_state(self.name)==want:return True
            time.sleep(1)
        return False

    def install(self,password,*,new_ledger=False,restore_backup=None,start=True,operator=None):
        checks=self.checks()
        for key in ('python','elevated','service_account','arena_accounts','service','package'):
            if key in checks and not checks[key]['ok']:raise Refused('Prerequisite failed: '+key)
        if not checks['tls']['ok']:raise Refused('TLS certificate and key missing: run the certificate verb first')
        sid=checks['service_account']['sid']
        for path,grants in folder_acls(self.paths,sid):self.system.icacls(path,grants)
        release=self._release()
        if not self.paths['config_file'].exists():self.paths['config_file'].write_text(json.dumps(service_config(self.settings,self.paths),indent=2))
        if not self.paths['ledger'].exists():
            if restore_backup:
                from .capacity_lifecycle import restore
                restore(restore_backup,self.paths['ledger'],operator or 'installer')
            elif new_ledger:
                # Full schema now, so verify and the publisher work before the first service start.
                from .capacity_gateway import Gateway
                Gateway(self.paths['ledger'])
            else:raise Refused('No ledger: pass --new-ledger for a first installation or --restore-backup for a migration')
        self._validate(release)
        self.system.create_service(self.name,command_line(self.settings['python'],release,self.paths['config_file'],self.name),self.settings['service_account'],password)
        self._record(action='install',current=self.version,previous=None)
        started=False
        if start:self.system.start_service(self.name);started=self._wait('running',90)
        return {'ok':not start or started,'verb':'install','release':self.version,'service':self.name,'started':started,'release_dir':str(release),
                'next':['bootstrap policy and resources','issue application credentials','qualify heads','tell the Arena operator, then publisher accept-ledger',
                        'Set-ObservationPublicationAcl.ps1','verify']}

    def verify(self,*,probe=None):
        checks=self.checks(for_install=False);p=self.paths
        from .service_config import load,ConfigError
        try:settings=load(p['config_file']);checks['config']={'ok':True}
        except (ConfigError,OSError,ValueError) as exc:settings=None;checks['config']={'ok':False,'problems':getattr(exc,'problems',[str(exc)])}
        if settings:
            from .windows_service import operator_status
            status=operator_status(settings,observation_file=p['published'],publisher_state=p['publisher_state'])
            checks['ledger_ready']={'ok':status['admission_open'],'ledger_id':status['ledger_id']}
            checks['publisher']={'ok':status.get('publisher',{}).get('fresh',False),**status.get('publisher',{})}
            checks['heads']={'ok':True,'heads':status['heads'],'available_models':status['available_models']}
            checks['endpoints']=(probe or self._probe)(settings['advertised_url'],str(p['client_ca']))
        checks.pop('elevated',None);checks.pop('package',None)
        failed=[k for k,v in checks.items() if not v['ok']]
        return {'ok':not failed,'verb':'verify','release':self._state().get('current'),'failed':failed,'checks':checks}

    @staticmethod
    def _probe(url,ca):
        import ssl,urllib.request,urllib.error
        context=ssl.create_default_context(cafile=ca);out={}
        for path in ('/health','/ready'):
            try:
                with urllib.request.urlopen(url+path,timeout=5,context=context) as response:out[path]=response.status
            except urllib.error.HTTPError as exc:out[path]=exc.code
            except (OSError,ValueError) as exc:out[path]=type(exc).__name__
        return {'ok':out=={'/health':200,'/ready':200},**out}

    def _switch(self,version,*,password=None):
        release=self.paths['releases']/version
        if not release.is_dir():raise Refused('Release %s is not installed'%version)
        self._validate(release)
        if self.system.service_state(self.name)=='running':
            self.system.stop_service(self.name)
            if not self._wait('stopped',self.settings.get('drain_seconds',90)+60):raise Refused('Service did not stop within the drain window')
        if _version(version)<(0,4,0):self._prepare_downgrade()
        self.system.set_command(self.name,command_line(self.settings['python'],release,self.paths['config_file'],self.name),
                                self.settings['service_account'] if password else None,password)
        self.system.start_service(self.name)
        return self._wait('running',90)

    def _prepare_downgrade(self,*,wait_seconds=75):
        """Before starting a 0.3.x release on a ledger 0.4.0 used.

        0.3.x recreates its one-lease-per-head unique index at start and cannot start while
        any head still has two reservations (possible with slots > 1). With the service
        stopped nothing renews leases, so every lease ends within 60 s: expire them, then
        check. Suspensions stay in the ledger (0.3.x ignores them); held heads stay
        quarantined, which 0.3.x already treats as operator-owned.
        """
        from .capacity_broker import CapacityBroker
        broker=CapacityBroker(self.paths['ledger']);deadline=time.monotonic()+wait_seconds
        while True:
            broker.reconcile_expired()
            with broker.connect() as db:
                shared=[r[0] for r in db.execute("SELECT resource_id FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND resource_id IS NOT NULL GROUP BY resource_id HAVING count(*)>1")]
            if not shared:return
            if time.monotonic()>=deadline:raise Refused('Heads still hold more than one reservation (%s); 0.3.x cannot start until they expire'%', '.join(shared))
            time.sleep(2)

    def _upgrade_report(self,*,expected,timeout=60):
        """What 0.4.0 did to the ledger on its first start: read-only, waits for the service to open it."""
        import sqlite3
        from .capacity_broker import UPGRADE_REPORT_KEY
        deadline=time.monotonic()+(timeout if expected else 0)
        while True:
            db=sqlite3.connect(Path(self.paths['ledger']).resolve().as_uri()+'?mode=ro',uri=True)
            try:row=db.execute('SELECT value FROM capacity_service_policy WHERE key=?',(UPGRADE_REPORT_KEY,)).fetchone()
            finally:db.close()
            if row or time.monotonic()>=deadline:return json.loads(row[0]) if row else None
            time.sleep(1)

    def _backup(self):
        from .capacity_lifecycle import backup
        target=self.paths['backups']/('capacity-ledger-%s-%s.db'%(time.strftime('%Y%m%dT%H%M%S'),self.version))
        return backup(self.paths['ledger'],target)

    def update(self,*,probe=None):
        state=self._state();previous=state.get('current')
        if not previous:raise Refused('Nothing installed to update; use install')
        if previous==self.version:raise Refused('Release %s is already current; use repair'%self.version)
        record=self._backup();self._release()
        legacy=_legacy_ledger(self.paths['backups']/record['backup'])
        # 0.4.1: an exception from the switch (e.g. StartServiceW error 1053) is a failed start too;
        # 0.4.0 let it skip the switch-back and left the service on the new release, stopped.
        try:started=self._switch(self.version);error=None
        except Exception as exc:started=False;error='%s: %s'%(type(exc).__name__,exc)
        if started and self.verify(probe=probe)['checks'].get('endpoints',{}).get('/health')==200:
            self._record(action='update',current=self.version,previous=previous,backup=record['backup'])
            from .capacity_observe import ledger_identity
            report=self._upgrade_report(expected=legacy and _version(self.version)>=(0,4,0))
            after=ledger_identity(self.paths['ledger'])['ledger_id']
            return {'ok':True,'verb':'update','from':previous,'to':self.version,'backup':record,
                    'ledger':{'before':record['ledger_id'],'after':after,'kept':after==record['ledger_id']},
                    'schema_upgrade':report and {'release':report['release'],'at':report['at']},
                    'held_at_upgrade':report['held'] if report else [],
                    'note':('each held head is probed after its wait ends: a small qualified model, then the model that failed; '
                            'see lifecycle suspensions') if report and report['held'] else None,
                    'rollback':'supported: rollback expires leftover leases, then 0.3.x starts on this ledger and ignores 0.4.0 tables'}
        # Automatic return to the previous release on a failed start; the ledger is untouched.
        try:back=self._switch(previous);back_error=None
        except Exception as exc:back=False;back_error='%s: %s'%(type(exc).__name__,exc)
        if not back:
            self._record(action='update_failed_rollback_failed',attempted=self.version,returning_to=previous,error=error,rollback_error=back_error)
            raise Refused('Release %s did not start (%s) and returning to %s failed (%s): service state %s; run repair or rollback'%(
                self.version,error or 'not healthy',previous,back_error or 'did not start',self.system.service_state(self.name)))
        self._record(action='update_failed_rolled_back',current=previous,previous=self.version,**({'error':error} if error else {}))
        raise Refused('Release %s did not become healthy%s; returned to %s'%(self.version,' (%s)'%error if error else '',previous))

    def rollback(self):
        state=self._state();target=state.get('previous')
        if not target:raise Refused('No previous release recorded')
        record=self._backup()
        if not self._switch(target):raise Refused('Previous release %s did not start'%target)
        self._record(action='rollback',current=target,previous=state.get('current'),backup=record['backup'])
        return {'ok':True,'verb':'rollback','to':target,'from':state.get('current'),'backup':record,
                'note':'schema changes are additive; the ledger is kept as is'+
                        ('. 0.3.x ignores 0.4.0 suspensions and slots: held heads stay quarantined (operator reconcile), '
                         'model suspensions stop applying, every head takes one lease' if _version(target)<(0,4,0)<=_version(state.get('current') or '0') else '')}

    def repair(self,password=None):
        sid=self.system.resolve_sid(self.settings['service_account'])
        if not sid:raise Refused('The service account does not exist')
        for path,grants in folder_acls(self.paths,sid):self.system.icacls(path,grants)
        release=self._release(replace=True)
        if self.system.service_state(self.name) is None:
            if not password:raise Refused('The service is not registered: repair needs the service account password')
            self.system.create_service(self.name,command_line(self.settings['python'],release,self.paths['config_file'],self.name),self.settings['service_account'],password)
        else:self._switch(self.version,password=password)
        self._record(action='repair',current=self.version)
        return {'ok':True,'verb':'repair','release':self.version,'preserved':['ledger','config','tls','observation','logs','backups']}

    def uninstall(self,*,remove_releases=False):
        state=self.system.service_state(self.name)
        if state is not None:
            if state!='stopped':
                self.system.stop_service(self.name)
                if not self._wait('stopped',self.settings.get('drain_seconds',90)+60):raise Refused('Service did not stop within the drain window')
            self.system.delete_service(self.name)
        if remove_releases and self.paths['releases'].exists():shutil.rmtree(self.paths['releases'])
        self._record(action='uninstall',current=None)
        return {'ok':True,'verb':'uninstall','service_removed':state is not None,'releases_removed':remove_releases,
                'preserved':[str(self.paths['data_root'])],'note':'data is never deleted by the installer'}


def load_settings(path):
    settings=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    problems=[]
    if settings.get('schema')!=SETTINGS_SCHEMA:problems.append('schema must be '+SETTINGS_SCHEMA)
    for key in ('python','install_root','data_root'):
        if not isinstance(settings.get(key),str) or not Path(settings[key]).is_absolute():problems.append(key+' must be an absolute path')
    if not isinstance(settings.get('service_account'),str) or not settings['service_account']:problems.append('service_account required')
    if not isinstance(settings.get('arena_accounts'),list) or not settings['arena_accounts']:problems.append('arena_accounts must list the Arena accounts')
    listen=settings.get('listen') or {}
    if not isinstance(listen.get('port'),int) or not 1<=listen['port']<=65535 or not isinstance(listen.get('host'),str):problems.append('listen host and port required')
    if problems:raise ValueError('; '.join(problems))
    return settings


def main(argv=None,*,system=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='verb',required=True)
    for verb in ('plan','certificate','install','verify','update','rollback','repair','uninstall'):
        command=sub.add_parser(verb);command.add_argument('--settings',required=True)
        if verb in ('install','repair'):command.add_argument('--password-stdin',action='store_true')
        if verb=='install':
            command.add_argument('--new-ledger',action='store_true');command.add_argument('--restore-backup')
            command.add_argument('--no-start',action='store_true');command.add_argument('--operator')
        if verb=='certificate':command.add_argument('--renew',action='store_true')
        if verb=='uninstall':command.add_argument('--remove-releases',action='store_true')
    try:a=p.parse_args(argv);settings=load_settings(a.settings)
    except SystemExit as exc:return 2 if exc.code else 0
    except (ValueError,OSError) as exc:print(json.dumps({'ok':False,'error':str(exc)}));return 2
    installer=Installer(settings,system or WindowsSystem(),package=package_root())
    def password():
        if not a.password_stdin and not sys.stdin.isatty():return None
        try:return sys.stdin.readline().rstrip('\r\n') if a.password_stdin else getpass.getpass('Service account password: ')
        except EOFError:return None # NUL reports isatty() on Windows
    try:
        if a.verb=='plan':result=installer.plan()
        elif a.verb=='certificate':result=installer.certificate(renew=a.renew)
        elif a.verb=='install':
            secret=password()
            if not secret:raise ValueError('The service account password is required (typed, or --password-stdin)')
            result=installer.install(secret,new_ledger=a.new_ledger,restore_backup=a.restore_backup,start=not a.no_start,operator=a.operator)
        elif a.verb=='verify':result=installer.verify()
        elif a.verb=='update':result=installer.update()
        elif a.verb=='rollback':result=installer.rollback()
        elif a.verb=='repair':result=installer.repair(password() if a.password_stdin else None)
        else:result=installer.uninstall(remove_releases=a.remove_releases)
    except ValueError as exc:print(json.dumps({'ok':False,'verb':a.verb,'error':str(exc)}));return 2
    except (Refused,OSError) as exc:print(json.dumps({'ok':False,'verb':a.verb,'error':str(exc)}));return 1
    print(json.dumps(result,default=str))
    return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
