"""G1/G3 launch-storm measurement (Inference Gateway 0.4.6 lab tool). Run on the hub, HUB, with LAUNCH-STORM.cmd.

N simulated worker launches start at the same moment (default 16, 18 and 24, three rounds each) and do only
discovery, no inference:
  rc24      what Capstone 6.0.0-rc.24 does before a trial: GET /v1/models, GET /v1/policy, then GET /v1/availability
            for every advertised model, and one more availability check for the main model's request shape;
  scoped    (Gateway 0.4.6 and later) the new contract: GET /v1/policy?availability=0 and one GET /v1/fleet scoped to
            the trial's models (default qwen3.5:4b and qwen3:4b), operation /api/chat, num_ctx 8192.
Reported per sequence and level: API calls per launch, client-side p50/p95/max per route, the round's wall time,
and the Gateway's own counters over each round from the observation file (per-route counts and times on 0.4.6,
write-lock waits and holds per transaction label, slow requests, 503 answers).

Run it before and after the 0.4.6 update for a before/after on the real hub (the scoped sequence exists only after).
It refuses to start while the Gateway has running or queued work. It only reads: no request is submitted, nothing
is loaded, and no policy, capacity or qualification changes. Results go to results\\ beside this file.
"""
# ---- common part of every Gateway 0.4.6 lab tool (assembled by scripts/build_lab_tools.py; do not edit the copies) ----
import argparse,base64,collections,json,os,socket,ssl,statistics,sys,threading,time,urllib.error,urllib.parse,urllib.request,uuid,zlib
from pathlib import Path

HERE=Path(__file__).resolve().parent
OBSERVATION=Path(r'C:\ProgramData\InferenceGateway\observation\shared-inference-observation.json')
TOOLS_RELEASE='0.4.6'
OBSERVATION_MAX_AGE=60


class Refused(Exception):
    """The tool will not run (or stops): the reason is printed and written to the results folder."""


def now_iso(t=None):
    from datetime import datetime
    return datetime.fromtimestamp(t or time.time()).astimezone().isoformat(timespec='seconds')


def pct(values,f):
    values=sorted(v for v in values if v is not None)
    return round(values[min(len(values)-1,int(len(values)*f))],3) if values else None


def summary(values):
    values=[v for v in values if v is not None]
    return {'n':len(values),'p50':pct(values,.5),'p95':pct(values,.95),'max':round(max(values),3) if values else None,
            'mean':round(statistics.fmean(values),3) if values else None}


def results_folder(tool):
    stamp=time.strftime('%Y%m%dT%H%M%S')
    folder=HERE/'results'/('%s-%s-%s'%(os.environ.get('COMPUTERNAME','host'),tool,stamp))
    folder.mkdir(parents=True,exist_ok=False)
    return folder


def write_json(folder,name,value):
    (folder/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')


class Gateway:
    """A minimal shared.inference/1 client (standard library only) built from an approved client config file.

    The config is the one Capstone uses (url, application_id, token_file, ca_file, service_id). The token is read
    from its file, sent only to the Gateway, and never printed or written to the results.
    """
    def __init__(self,config_path):
        path=Path(config_path)
        try:config=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:raise Refused('Cannot read the client config %s (%s)'%(path,type(exc).__name__)) from None
        if config.get('contract')!='shared.inference/1' or config.get('configuration_version')!=1:raise Refused('Not an approved shared.inference/1 client config: %s'%path)
        token_path=Path(config.get('token_file') or '')
        if not token_path.is_absolute():token_path=path.parent/token_path
        try:self._token=token_path.read_text(encoding='utf-8').strip()
        except OSError as exc:raise Refused('Cannot read the token file named by the client config (%s)'%type(exc).__name__) from None
        if not self._token:raise Refused('The token file is empty')
        ca=config.get('ca_file')
        if ca and not Path(ca).is_absolute():ca=str(path.parent/ca)
        self.url=config['url'].rstrip('/');self.app=config['application_id'];self.service_id=config.get('service_id','shared-inference')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
        self.calls=collections.Counter()

    def describe(self):
        return {'url':self.url,'application_id':self.app,'service_id':self.service_id}

    def call(self,method,path,body=None,timeout=30):
        """(status, value, seconds). HTTP errors are answers (status 4xx/5xx), not exceptions; no answer raises OSError."""
        request=urllib.request.Request(self.url+path,data=None if body is None else json.dumps(body).encode(),method=method,
                                       headers={'Content-Type':'application/json','X-Application-ID':self.app,'X-Infrastructure-Token':self._token})
        self.calls[method+' '+path.split('?',1)[0]]+=1
        began=time.perf_counter()
        try:
            with self.opener.open(request,timeout=timeout) as response:
                if response.headers.get('X-Inference-Service-ID')!=self.service_id:raise Refused('Unexpected inference service identity at %s'%self.url)
                status,data=response.status,response.read()
        except urllib.error.HTTPError as exc:
            status=exc.code
            try:data=exc.read(65536)
            except Exception:data=b''
        seconds=time.perf_counter()-began
        try:value=json.loads(data) if data else {}
        except ValueError:value={'raw':data[:2000].decode('utf-8','replace')}
        return status,value,seconds

    def info(self):
        status,value,_=self.call('GET','/health',timeout=10)
        if status!=200:raise Refused('The Gateway health check answered %s'%status)
        return {'release':value.get('release'),'features':value.get('features') or [],'contract':value.get('contract')}

    def run(self,model,operation,payload,*,client_key,nodes=None,deadline_seconds=300,required_capabilities=None,poll_max=2.0,on_submit=None,stop=None):
        """Submit one request (/v1/submit) and poll its status until it ends. Returns (envelope, client-side seconds).

        Ends at once, without waiting for the request, when `stop` (a threading.Event) is set or the identity is
        refused (403: revoked or expired); the envelope's status is then 'stopped' or 'revoked'.
        """
        body={'contract':'shared.inference/1','client_key':client_key,'model':model,'nodes':nodes or ['@authorized'],
              'operation':operation,'payload':payload,'deadline':time.time()+deadline_seconds}
        if required_capabilities:body['required_capabilities']=list(required_capabilities)
        began=time.perf_counter();end=time.monotonic()+deadline_seconds+30;delay=.25
        stopped=lambda:stop is not None and stop.is_set()
        while True:
            if stopped():return {'status':'stopped'},time.perf_counter()-began
            try:status,value,_=self.call('POST','/v1/submit',body,timeout=15)
            except OSError:status,value=None,{}
            if status in (200,202):break
            if status not in (None,429,502,503) or time.monotonic()>end:
                return {'status':'revoked' if status==403 else 'refused','http_status':status,'error':value.get('error') if isinstance(value,dict) else None},time.perf_counter()-began
            time.sleep(delay);delay=min(poll_max,delay*1.5)
        rid=value['request']['request_id']
        if on_submit:on_submit(rid)
        delay=.25
        while value.get('status') in ('active','queued','leased','cancel_requested') and time.monotonic()<end:
            if stop is not None:
                if stop.wait(delay):return {**value,'status':'stopped'},time.perf_counter()-began
            else:time.sleep(delay)
            delay=min(poll_max,delay*1.5)
            try:
                status,fresh,_=self.call('GET','/v1/requests/'+urllib.parse.quote(rid,safe=''),timeout=15)
                if status==200:value=fresh
                elif status==403:return {**value,'status':'revoked'},time.perf_counter()-began
            except OSError:pass
        return value,time.perf_counter()-began


def read_observation(path=OBSERVATION,attempts=10):
    """The publisher replaces the file every 5 s; a read that meets the replace (sharing violation or a partial file
    on Windows) is retried briefly before it counts as unreadable."""
    for attempt in range(attempts):
        try:return json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            if attempt==attempts-1 or not Path(path).parent.is_dir():
                raise Refused('The Gateway observation file %s does not exist: run this on the hub (HUB), where the Gateway runs'%path) from None
        except (OSError,ValueError) as exc:
            if attempt==attempts-1:raise Refused('Cannot read the Gateway observation file %s (%s)'%(path,type(exc).__name__)) from None
        time.sleep(.2)


def activity(doc,own=()):
    """Work in the Gateway that is not this tool's: running leases (maintenance probes excluded) and queued requests."""
    own=set(own)
    leases=[l for l in doc.get('leases') or [] if not str(l.get('app_id','')).startswith('maintenance:') and l.get('request_id') not in own]
    return {'observed_at':doc.get('observed_at'),'age_s':round(time.time()-float(doc.get('observed_at') or 0),1),
            'admission_open':doc.get('admission_open'),'foreign_leases':len(leases),'foreign_apps':sorted({str(l.get('app_id')) for l in leases}),
            'foreign_heads':sorted({str(l.get('resource_id')) for l in leases}),'queued_total':int((doc.get('requests') or {}).get('queued',0) or 0)}


def require_idle(path=OBSERVATION,*,own=(),own_queued=0,what='this test'):
    """Refuse unless the Gateway is idle: a fresh observation, admission open, nothing running or queued but our own."""
    doc=read_observation(path);a=activity(doc,own)
    if a['age_s']>OBSERVATION_MAX_AGE:
        raise Refused('The Gateway observation is %.0f s old (limit %d s): cannot confirm that the Gateway is idle, so %s does not run.'%(a['age_s'],OBSERVATION_MAX_AGE,what))
    if not a['admission_open']:raise Refused('Gateway admission is closed (drained or stopping); %s does not run.'%what)
    queued=a['queued_total']-own_queued
    if a['foreign_leases'] or queued>0:
        raise Refused('The Gateway has active work: %d running and %d queued requests (applications: %s; heads: %s). '
                      'An experiment may be running, so %s does not run. Wait until it has finished, then start this again.'
                      %(a['foreign_leases'],max(queued,0),', '.join(a['foreign_apps']) or '-',', '.join(a['foreign_heads']) or '-',what))
    return doc,a


def ledger_activity(ledger,*,own_app=None,own_ids=()):
    """Outside work, read from the Gateway ledger itself (read-only, current, not the 5 s observation): every queued
    or running request (maintenance probes excluded) that is not this tool's. A request of `own_app` that is not in
    `own_ids` counts as outside work too (with --client-config the identity may be shared). None if unreadable."""
    import sqlite3
    try:
        db=sqlite3.connect(Path(ledger).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        try:rows=db.execute("SELECT request_id,app_id,status,resource_id FROM capacity_requests WHERE status IN ('queued','leased','cancel_requested') "
                            "AND app_id NOT LIKE 'maintenance:%'").fetchall()
        finally:db.close()
    except Exception:return None
    own=set(own_ids)
    foreign=[r for r in rows if not (r[1]==own_app and (own_app==IDENTITY_APP or r[0] in own))]
    return {'foreign_running':sum(r[2]!='queued' for r in foreign),'foreign_queued':sum(r[2]=='queued' for r in foreign),
            'foreign_apps':sorted({r[1] for r in foreign}),'foreign_heads':sorted({r[3] for r in foreign if r[3]})}


GATEWAY_DATA=Path(r'C:\ProgramData\InferenceGateway')
GATEWAY_INSTALL=Path(r'C:\Program Files\InferenceGateway')
IDENTITY_FOLDER=Path(r'C:\ProgramData\AgentArenaLab\lab-capacity-check')
IDENTITY_APP='lab-capacity-check'
IDENTITY_MODELS=['qwen3.5:4b','qwen3:4b']


class TemporaryIdentity:
    """A Gateway identity of its own for one run, issued and revoked with the Gateway's own credential tool.

    Issue: `credentials reissue` (issue, or revoke and re-key an existing row) from a clients-only policy file:
    the two models, max_concurrency 24, priority 0 (the lowest), /api/chat and /api/generate, context 8192. Then
    `credentials grant-heads --like <template>`: the identity may use exactly the template application's heads.
    Close (always: a finally, Ctrl+C, refusal): `credentials revoke`, `credentials withdraw-heads`, then the token,
    client config and policy file are deleted. The token lives only in an administrators-only folder under
    C:\\ProgramData\\AgentArenaLab, never on the stick. A run that crashed leaves state.json there: the next start
    finishes that cleanup first. Nothing else is touched: no other client, capacity, model_capacity or policy block.
    """
    def __init__(self,*,folder=IDENTITY_FOLDER,launcher=None,ledger=None,url=None,ca=None,service_id=None,like='capstone-research',
                 python=None,log=print):
        self.folder=Path(folder);self.like=like;self.log=log;self.python=python or sys.executable
        config={}
        try:config=json.loads((GATEWAY_DATA/'config'/'gateway-service.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        if launcher is None:
            try:current=json.loads((GATEWAY_DATA/'deployment'/'deployment-state.json').read_text(encoding='utf-8'))['current']
            except (OSError,ValueError,KeyError):raise Refused('Cannot read the Gateway deployment state: run this on the hub, as an administrator') from None
            launcher=GATEWAY_INSTALL/'releases'/current/'inference_gateway'/'service_launcher.py'
        self.launcher=Path(launcher);self.ledger=Path(ledger or config.get('ledger') or GATEWAY_DATA/'state'/'capacity-ledger.db')
        self.url=url or config.get('advertised_url') or 'https://192.0.2.254:8443';self.service_id=service_id or config.get('service_id') or 'shared-inference'
        self.ca=ca if ca is not None else str(GATEWAY_DATA/'tls'/'inference-gateway-ca.pem')
        self.steps=[];self.issued=False;self.closed=False

    def tool(self,*args):
        """One Gateway credentials call; its JSON answer (never contains the token)."""
        import subprocess
        result=subprocess.run([self.python,'-I',str(self.launcher),'credentials',*args,'--database',str(self.ledger)],capture_output=True,text=True,timeout=120)
        try:value=json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError,IndexError):value={'ok':False,'error':(result.stderr or result.stdout)[-300:]}
        self.steps.append({'at':now_iso(),'verb':args[0],'ok':bool(value.get('ok')),**{k:v for k,v in value.items() if k not in ('token_file','ok')}})
        return value

    def state(self,step):
        if self.folder.is_dir():write_json(self.folder,'state.json',{'app_id':IDENTITY_APP,'step':step,'at':now_iso(),'ledger':str(self.ledger)})

    TRUSTED={'S-1-5-18':'SYSTEM','S-1-5-32-544':'Administrators'}
    DANGEROUS_ON_PARENT=('FullControl','Modify','ChangePermissions','TakeOwnership','DeleteSubdirectoriesAndFiles','Delete')

    @staticmethod
    def _acl(paths):
        """Owner and access rules (as SIDs) of each path, read with Windows PowerShell's Get-Acl."""
        import subprocess
        script=("$me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$out=@();"
                "$paths=($env:LABTOOL_ACL_PATHS|ConvertFrom-Json);foreach($p in $paths){$a=Get-Acl -LiteralPath $p;"
                "$r=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])|%{@{sid=$_.IdentityReference.Value;type=\"$($_.AccessControlType)\";"
                "rights=\"$($_.FileSystemRights)\";inherited=$_.IsInherited}});"
                "$out+=@{path=$p;owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value;rules=$r}};"
                "@{me=$me;items=$out}|ConvertTo-Json -Compress -Depth 5")
        shell=os.path.join(os.environ.get('SystemRoot',r'C:\Windows'),'System32','WindowsPowerShell','v1.0','powershell.exe')
        # Windows PowerShell computes its own module path when PSModulePath is unset (an inherited PowerShell 7 value
        # would hide Get-Acl's module from it).
        env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'};env['LABTOOL_ACL_PATHS']=json.dumps([str(p) for p in paths])
        done=subprocess.run([shell,'-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=60,env=env)
        try:value=json.loads(done.stdout)
        except ValueError:raise Refused('Could not read the permissions of %s'%', '.join(map(str,paths))) from None
        items=value.get('items');items=items if isinstance(items,list) else [items]
        if len(items)!=len(paths) or any(not isinstance(i,dict) for i in items):raise Refused('Could not read the permissions of %s'%', '.join(map(str,paths)))
        for i in items:
            if not isinstance(i.get('rules'),list):i['rules']=[i['rules']] if isinstance(i.get('rules'),dict) else []
        return value['me'],items

    def _foreign(self,me,item,*,parent=False):
        """Why this path is not safe for the token (owner or permissions of anyone but SYSTEM, Administrators, the operator)."""
        trusted=set(self.TRUSTED)|{me};problems=[]
        if item.get('owner') not in trusted:problems.append('owner %s'%item.get('owner'))
        for rule in item.get('rules') or []:
            if rule.get('type')!='Allow' or rule.get('sid') in trusted:continue
            if rule.get('sid')=='S-1-3-4' and item.get('owner') in trusted:continue # OWNER RIGHTS: the (trusted) owner's own rights
            if parent:
                # On the parent only rights that could remove or replace our folder matter. CREATOR OWNER (S-1-3-0) is
                # the inherit-only template C:\ProgramData passes down: it applies to a child's creator, not here.
                if rule.get('sid')=='S-1-3-0':continue
                rights=str(rule.get('rights'))
                if rights.isdigit():
                    if not int(rights)&(0x10000000|0x40000000|0x40000|0x80000|0x10000|0x40):continue # GENERIC_ALL/WRITE, WRITE_DAC/OWNER, DELETE, delete child
                elif not any(r in rights for r in self.DANGEROUS_ON_PARENT):continue
            problems.append('%s may %s'%(rule.get('sid'),rule.get('rights')))
        return problems

    def _folder(self):
        """The token folder: refuse one that anyone but SYSTEM, Administrators and the operator owns or may use; create a
        fresh one with only those three; verify its permissions before anything is written into it."""
        import subprocess
        if os.name!='nt':
            self.folder.mkdir(parents=True,exist_ok=True,mode=0o700);return
        self.folder.parent.mkdir(parents=True,exist_ok=True)
        me,[parent]=self._acl([self.folder.parent])
        if self._foreign(me,parent,parent=True):
            raise Refused('%s is not safe for the token: %s. Only SYSTEM, Administrators and you may own it or delete in it. '
                          'Ask the operator to fix or remove that folder, then start this again.'%(self.folder.parent,'; '.join(self._foreign(me,parent,parent=True))))
        if self.folder.exists():
            paths=[self.folder]+sorted(self.folder.rglob('*'))
            me,items=self._acl(paths)
            bad={i['path']:self._foreign(me,i) for i in items if self._foreign(me,i)}
            if bad:
                raise Refused('%s is not safe for the token: %s. Someone other than SYSTEM, Administrators and you can own or change it, so '
                              'a token there could be read or redirected. Remove that folder as an administrator, then start this again.'
                              %(self.folder,'; '.join('%s: %s'%(Path(k).name or k,', '.join(v)) for k,v in bad.items())))
        else:
            self.folder.mkdir()
            done=subprocess.run(['icacls',str(self.folder),'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',
                                 '*%s:(OI)(CI)F'%self._acl([self.folder])[0],'/Q'],capture_output=True,text=True)
            if done.returncode:raise Refused('Could not restrict %s to administrators'%self.folder)
            subprocess.run(['icacls',str(self.folder),'/setowner','*S-1-5-32-544','/Q'],capture_output=True,text=True) # best effort
        me,[item]=self._acl([self.folder])
        if self._foreign(me,item) or any(r.get('inherited') for r in item.get('rules') or []):
            raise Refused('Could not make %s private to SYSTEM, Administrators and you (%s)'%(self.folder,'; '.join(self._foreign(me,item)) or 'inherited permissions'))

    def leftover(self):
        """True when an earlier run did not finish its cleanup (state.json, or the identity still enabled)."""
        listed=self.tool('list')
        if not listed.get('ok'):raise Refused('The Gateway credential tool did not answer: %s'%listed.get('error'))
        enabled=any(a['app_id']==IDENTITY_APP and a['enabled'] for a in listed.get('applications',[]))
        return enabled or (self.folder/'state.json').exists()

    def open(self,expires_in=4*3600):
        """Issue the identity for at most `expires_in` seconds (the Gateway refuses it, then revokes it, after that)."""
        if not self.launcher.is_file():raise Refused('The Gateway release tool %s is missing'%self.launcher)
        if not self.ledger.is_file():raise Refused('The Gateway ledger %s is missing'%self.ledger)
        self._folder()
        if self.leftover():
            self.log('An earlier run left %s behind: revoking it first.'%IDENTITY_APP);self.close(final=False)
        for name in ('identity.token','gateway-client.json'):(self.folder/name).unlink(missing_ok=True)
        # queue_limit 24 = max_concurrency: closed-loop callers never have more than one request each.
        write_json(self.folder,'policy.json',{'configuration_version':1,'contract':'shared.inference/1','clients':[
            {'app_id':IDENTITY_APP,'models':IDENTITY_MODELS,'max_concurrency':24,'queue_limit':24,'priority':0,
             'operations':['/api/chat','/api/generate'],'max_context':8192}]})
        self.state('issuing');self.issued=True
        self.expires_at=time.time()+expires_in
        out=self.tool('reissue','--app-id',IDENTITY_APP,'--policy',str(self.folder/'policy.json'),'--output',str(self.folder/'identity.token'),
                      '--expires-in-seconds',str(int(expires_in)))
        if not out.get('ok') and 'invalid choice' in str(out.get('error')):
            raise Refused('The installed Gateway release has no temporary-identity commands (they come with 0.4.6). '
                          'Update the Gateway first, or run this with --client-config <an approved identity\'s gateway-client.json>.')
        if not out.get('ok'):raise Refused('Issuing %s failed: %s'%(IDENTITY_APP,out.get('error')))
        granted=self.tool('grant-heads','--app-id',IDENTITY_APP,'--like',self.like)
        if not granted.get('ok'):raise Refused('Granting %s the heads of %s failed: %s'%(IDENTITY_APP,self.like,granted.get('error')))
        if not granted.get('granted') and not granted.get('already'):raise Refused('%s is authorized on no head'%self.like)
        write_json(self.folder,'gateway-client.json',{'configuration_version':1,'contract':'shared.inference/1','service_id':self.service_id,'url':self.url,
                   'application_id':IDENTITY_APP,'token_file':str(self.folder/'identity.token'),**({'ca_file':self.ca} if self.ca else {})})
        self.state('issued')
        self.log('Temporary identity %s issued (heads of %s: %s).'%(IDENTITY_APP,self.like,', '.join(granted.get('granted',[])+granted.get('already',[]))))
        return str(self.folder/'gateway-client.json')

    def close(self,final=True):
        """Revoke, withdraw the heads, delete the secrets. Safe to repeat (a second final close does nothing); never raises."""
        if final and self.closed:return
        if final and not self.issued:self.closed=True;return # nothing was issued by this run (leftovers are handled at its start)
        try:
            self.state('revoking')
            revoked=self.tool('revoke','--app-id',IDENTITY_APP)
            withdrawn=self.tool('withdraw-heads','--app-id',IDENTITY_APP)
            for name in ('identity.token','gateway-client.json','policy.json'):(self.folder/name).unlink(missing_ok=True)
            unknown=not revoked.get('ok') and 'Unknown application' in str(revoked.get('error'))
            if (revoked.get('ok') or unknown) and (withdrawn.get('ok') or unknown):
                (self.folder/'state.json').unlink(missing_ok=True)
                if final:self.closed=True
                if final and self.issued:self.log('Temporary identity %s revoked; its token was deleted.'%IDENTITY_APP)
            else:
                self.log('WARNING: %s could not be fully revoked (%s; %s). Start this tool again to finish the cleanup.'
                         %(IDENTITY_APP,revoked.get('error'),withdrawn.get('error')))
        except Exception as exc:
            self.log('WARNING: cleanup of %s failed (%s). Start this tool again to finish it.'%(IDENTITY_APP,type(exc).__name__))

    def report(self):
        return {'app_id':IDENTITY_APP,'models':IDENTITY_MODELS,'max_concurrency':24,'queue_limit':24,'priority':0,'heads_like':self.like,'steps':self.steps,
                'token_folder':str(self.folder),'expires_at':getattr(self,'expires_at',None),
                'note':'issued and revoked with the Gateway credentials tool; the disabled client row stays as history'}

    def cleanup_hint(self,command):
        until=getattr(self,'expires_at',None)
        return ('If this window is closed, or the PC stops, before the end: the temporary Gateway identity %s stays usable until %s, '
                'when the Gateway revokes it by itself. To remove it at once, run:  %s --cleanup'
                %(IDENTITY_APP,now_iso(until) if until else 'its expiry',command))


def identity_args(p):
    p.add_argument('--client-config',help='use an existing approved identity instead of the temporary lab-capacity-check one')
    p.add_argument('--heads-like',default='capstone-research',help='the temporary identity may use exactly this application\'s heads')
    p.add_argument('--cleanup',action='store_true',help='only revoke and remove a temporary identity an interrupted run left behind, then exit')
    for name in ('--gateway-launcher','--gateway-ledger','--gateway-url','--gateway-ca','--identity-folder'):p.add_argument(name,help=argparse.SUPPRESS)


def run_cleanup(a):
    """--cleanup: revoke and withdraw the temporary identity and delete its files; safe while other work runs."""
    try:
        identity=temporary_identity(a)
        if not identity.leftover():print('Nothing to clean up: %s is not enabled and no interrupted run was found.'%IDENTITY_APP);return 0
        identity.issued=True;identity.close()
        print('Cleanup done.' if identity.closed else 'Cleanup did not finish; see the warning above.')
        return 0 if identity.closed else 1
    except Refused as exc:print('\nREFUSED: %s\n'%exc);return 1


def outside_work(observation,ledger,*,own_app,own_ids):
    """Outside work now: from the ledger when it can be read (current), else from the observation (up to 5 s old)."""
    found=ledger_activity(ledger,own_app=own_app,own_ids=own_ids) if ledger else None
    if found is None:
        a=activity(read_observation(observation),own_ids)
        found={'foreign_running':a['foreign_leases'],'foreign_queued':0,'foreign_apps':a['foreign_apps'],'foreign_heads':a['foreign_heads'],'source':'observation'}
    else:found['source']='ledger'
    found['any']=bool(found['foreign_running'] or found['foreign_queued'])
    return found


def temporary_identity(a):
    return TemporaryIdentity(folder=a.identity_folder or IDENTITY_FOLDER,launcher=a.gateway_launcher,ledger=a.gateway_ledger,url=a.gateway_url,
                             ca=a.gateway_ca,like=a.heads_like)


def ask_client_config(value):
    if value:return value
    print('This test sends requests through the Gateway with an approved application identity.')
    print('Give the path of that identity\'s client config (gateway-client.json with token_file and ca_file).')
    answer=input('Client config path: ').strip().strip('"')
    if not answer:raise Refused('No client config given')
    return answer


def fixture(blob):
    return json.loads(zlib.decompress(base64.b64decode(blob)).decode('utf-8'))


def finish(folder,report,lines):
    write_json(folder,'report.json',report)
    text='\n'.join(lines)+'\n';(folder/'summary.txt').write_text(text,encoding='utf-8')
    print(text);print('Results: %s'%folder)


def refused_exit(tool,exc,extra=None):
    print('\nREFUSED: %s\n'%exc)
    try:
        folder=results_folder(tool+'-refused');write_json(folder,'refused.json',{'tool':tool,'at':now_iso(),'reason':str(exc),**(extra or {})})
        print('A note was written to %s'%folder)
    except Exception:pass
    return 1
# ---- end of common part ----

TOOL='launch-storm'


def rc24(gateway,timed,main):
    status,inventory=timed('GET /v1/models',lambda:gateway.call('GET','/v1/models'))
    timed('GET /v1/policy',lambda:gateway.call('GET','/v1/policy'))
    advertised={}
    for r in (inventory or {}).get('resources') or []:
        for m in r.get('models') or []:advertised.setdefault(m,(r.get('model_details') or {}).get(m) or {})
    for m,d in sorted(advertised.items()):
        ops=d.get('qualified_operations') or []
        op='/api/embed' if 'embedding' in ops and 'completion' not in ops else '/api/generate'
        caps=sorted(set(d.get('qualified_capabilities') or [])&{'chat','tools','structured_output','vision','context'})
        q={'model':m,'operation':op};q.update({'capabilities':','.join(caps)} if caps else {})
        timed('GET /v1/availability',lambda q=q:gateway.call('GET','/v1/availability?'+urllib.parse.urlencode(q)))
    timed('GET /v1/availability',lambda:gateway.call('GET','/v1/availability?'+urllib.parse.urlencode({'model':main,'operation':'/api/chat','num_ctx':'8192'})))


def scoped(gateway,timed,models):
    timed('GET /v1/policy?availability=0',lambda:gateway.call('GET','/v1/policy?availability=0'))
    timed('GET /v1/fleet',lambda:gateway.call('GET','/v1/fleet?'+urllib.parse.urlencode({'models':','.join(models),'operations':'/api/chat','num_ctx':'8192'})))


def gateway_load(observation):
    return read_observation(observation).get('gateway_load') or {}


def delta(before,after):
    out={'routes':{},'ledger':{},'counters':{}}
    for route,v in (after.get('routes') or {}).items():
        p=(before.get('routes') or {}).get(route,{})
        n=v.get('count',0)-p.get('count',0)
        if n:out['routes'][route]={'count':n,'ms_total':v.get('ms_total',0)-p.get('ms_total',0),'ms_mean':round((v.get('ms_total',0)-p.get('ms_total',0))/n,1)}
    b=((before.get('ledger') or {}).get('labels') or {})
    for label,v in ((after.get('ledger') or {}).get('labels') or {}).items():
        p=b.get(label,{});n=v.get('count',0)-p.get('count',0)
        if n or v.get('timeouts',0)-p.get('timeouts',0):
            out['ledger'][label]={'count':n,'timeouts':v.get('timeouts',0)-p.get('timeouts',0),'wait_ms_total':v.get('wait_ms_total',0)-p.get('wait_ms_total',0),
                                  'hold_ms_total':v.get('hold_ms_total',0)-p.get('hold_ms_total',0)}
    for k,v in (after.get('counters') or {}).items():
        d=v-(before.get('counters') or {}).get(k,0)
        if d:out['counters'][k]=d
    out['published']={'before':before.get('updated_at'),'after':after.get('updated_at')}
    return out


def storm(gateway,launches,sequence,args):
    timings=collections.defaultdict(list);errors=collections.Counter();lock=threading.Lock();barrier=threading.Barrier(launches);per=[0]*launches
    def launch(i):
        def timed(route,fn):
            began=time.perf_counter()
            try:status,value,_=fn()
            except Exception as exc:status,value=None,None;errors['%s: %s'%(route,type(exc).__name__)]+=1
            with lock:
                timings[route].append((time.perf_counter()-began)*1000);per[i]+=1
                if status not in (200,None):errors['%s: HTTP %s'%(route,status)]+=1
            return status,value
        barrier.wait()
        if sequence=='rc24':rc24(gateway,timed,args.main)
        else:scoped(gateway,timed,args.models.split(','))
    began=time.perf_counter()
    pool=[threading.Thread(target=launch,args=(i,),daemon=True) for i in range(launches)]
    for t in pool:t.start()
    for t in pool:t.join(600)
    wall=(time.perf_counter()-began)*1000
    return {'sequence':sequence,'launches':launches,'wall_ms':round(wall),'api_calls':sum(per),'api_calls_per_launch':round(sum(per)/launches,1),
            'errors':dict(errors),'per_route_ms':{k:summary(v) for k,v in sorted(timings.items())},
            'all_calls_ms':summary([x for v in timings.values() for x in v])}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    identity_args(p);p.add_argument('--levels',default='16,18,24');p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--main',default='qwen3.5:4b');p.add_argument('--models',default='qwen3.5:4b,qwen3:4b')
    p.add_argument('--observation',default=str(OBSERVATION));p.add_argument('--yes',action='store_true')
    a=p.parse_args(argv)
    try:levels=[int(x) for x in a.levels.split(',') if x.strip()]
    except ValueError:print('--levels must be numbers, for example 16,18,24');return 2
    if not levels or any(not 1<=x<=64 for x in levels) or not 1<=a.rounds<=10:print('Levels 1-64 and rounds 1-10');return 2
    if a.cleanup:return run_cleanup(a)
    identity=None
    try:
        if not a.client_config:
            identity=temporary_identity(a)
            if identity.leftover():print('An earlier run left %s behind; it is revoked now.'%IDENTITY_APP);identity.close(final=False)
        doc,idle=require_idle(a.observation,what='the launch-storm measurement')
        ledger=identity.ledger if identity else Path(a.gateway_ledger or GATEWAY_DATA/'state'/'capacity-ledger.db')
        own_app=IDENTITY_APP if identity else None
        found=outside_work(a.observation,ledger,own_app=own_app,own_ids=())
        if found['any']:
            raise Refused('The Gateway has active work: %d running and %d queued requests (applications: %s). An experiment may be running, '
                          'so the launch-storm measurement does not run. Wait until it has finished, then start this again.'
                          %(found['foreign_running'],found['foreign_queued'],', '.join(found['foreign_apps']) or '-'))
        print('\nLaunch storm: levels %s simultaneous launches, %d rounds each. Discovery calls only: nothing is run on a head.'%(','.join(map(str,levels)),a.rounds))
        if identity:print('It issues a temporary Gateway identity, %s, and revokes it at the end.'%IDENTITY_APP)
        if not a.yes and input('Start now? Type yes: ').strip().lower()!='yes':raise Refused('Not confirmed')
        # The identity expires by itself 40 minutes after issue (the storm takes a few), even if this window is killed.
        gateway=Gateway(identity.open(expires_in=2400) if identity else a.client_config);info=gateway.info()
        hint=identity.cleanup_hint('LAUNCH-STORM.cmd') if identity else None
        if hint:print(hint)
        sequences=['rc24']+(['scoped'] if 'fleet_snapshot' in info['features'] else [])
        print('Gateway %s at %s as %s; sequences %s.'%(info.get('release') or 'before 0.4.6',gateway.url,gateway.app,sequences))
        folder=results_folder(TOOL);rounds=[];notes=[]
        if hint:(folder/'IF-INTERRUPTED.txt').write_text(hint+'\n',encoding='utf-8')
        stopped=None
        for level in levels:
            for sequence in sequences:
                for r in range(a.rounds):
                    found=outside_work(a.observation,ledger,own_app=own_app,own_ids=())
                    if found['any']:stopped=found;break
                    # Watch for outside work during the round too (every second): the round's numbers would be mixed.
                    seen=[];done=threading.Event()
                    def watch():
                        while not done.wait(1):
                            f=outside_work(a.observation,ledger,own_app=own_app,own_ids=())
                            if f['any']:seen.append(f);return
                    watcher=threading.Thread(target=watch,daemon=True);watcher.start()
                    before=gateway_load(a.observation)
                    result=storm(gateway,level,sequence,a)
                    done.set();watcher.join(5)
                    if seen:stopped=seen[0];break
                    time.sleep(6) # the in-service publisher writes the counters every 5 s
                    result['gateway']=delta(before,gateway_load(a.observation));result['round']=r+1
                    rounds.append(result)
                    print('  %-6s %2d launches round %d: %5.1f calls/launch, wall %6d ms, p50 %s ms, p95 %s ms%s'%(sequence,level,r+1,result['api_calls_per_launch'],
                          result['wall_ms'],result['all_calls_ms']['p50'],result['all_calls_ms']['p95'],', errors %s'%result['errors'] if result['errors'] else ''),flush=True)
                    time.sleep(2)
                if stopped:break
            if stopped:break
        if stopped:
            print('  OUTSIDE WORK: %d running and %d queued requests of %s: the measurement stops now.'
                  %(stopped['foreign_running'],stopped['foreign_queued'],', '.join(stopped['foreign_apps']) or '-'),flush=True)
            notes.append('Stopped: outside work appeared (%d running, %d queued, %s); the round in progress was discarded.'
                         %(stopped['foreign_running'],stopped['foreign_queued'],', '.join(stopped['foreign_apps']) or '-'))
        if identity:identity.close();notes.append('Temporary identity %s revoked.'%IDENTITY_APP)
        report={'tool':TOOL,'tools_release':TOOLS_RELEASE,'at':now_iso(),'gateway':{**gateway.describe(),**info},'idle_at_start':idle,'rounds':rounds,'notes':notes,
                'identity':identity.report() if identity else {'client_config':'given by --client-config'},'stopped_by_outside_work':stopped,
                'scope':'Client-side times on the hub include TLS and the Gateway handler; the Gateway counters cover all callers in each round.'}
        lines=['Launch storm %s, Gateway %s, identity %s'%(report['at'],info.get('release') or 'before 0.4.6',gateway.app),
               '%-7s %7s %6s %12s %10s %10s %10s'%('seq','launch','round','calls/launch','wall ms','p50 ms','p95 ms')]
        for r in rounds:lines.append('%-7s %7d %6d %12.1f %10d %10s %10s'%(r['sequence'],r['launches'],r['round'],r['api_calls_per_launch'],r['wall_ms'],r['all_calls_ms']['p50'],r['all_calls_ms']['p95']))
        lines+=['NOTE: '+n for n in notes]
        finish(folder,report,lines);return 1 if stopped else 0
    except Refused as exc:return refused_exit(TOOL,exc)
    except KeyboardInterrupt:print('Stopped.');return 1
    finally:
        if identity:identity.close() # repeating it is harmless


if __name__=='__main__':raise SystemExit(main())
