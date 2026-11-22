"""G4 Gemma HTTP 500 reproduction (Inference Gateway 0.4.6 lab tool). Run on the hub, HUB, with G4-GEMMA500-REPRO.cmd.

Replays the saved c922 customer-communication requests of gemma3:4b against ONE idle head and keeps what the head
answers, including the error body that 0.4.5 discarded. The saved requests are only four distinct bodies
(temperature 0, seed 7): the target (REQ-90426799d3ab4e058c1d9f972525a5da, 3 of 3 failed on heads 10, 3 and 9), one
that failed 5 of 5, one that failed 4 of 5, and one that succeeded 5 of 5. The same body failing on several heads
suggests the request content matters; this test checks that on one idle head. It is a hypothesis until it runs.

Steps (sequential; one call at a time):
  1. direct  each of the four bodies exactly as the Gateway sends it (model, messages, format, options,
             keep_alive 2m, stream false) to http://<head>:11434/api/chat, --repeats times;
             then variants of the target: no format; format "json"; num_predict 1024; seed 8; num_ctx 4096.
  2. gateway the target once and the succeeding body once through the Gateway, pinned to the same head, with your
             approved identity. On Gateway 0.4.6 the reply carries upstream_errors (status, message). An HTTP 500
             there suspends gemma3:4b on that head until its automatic recovery probe passes (about 60 s): this is
             the Gateway's normal protection, not a policy change. Skip with --mode direct.

Direct calls bypass Gateway admission, so before each one the tool checks the observation: nothing may be running on
that head (and no queued work anywhere). It refuses to start while the Gateway has any running or queued work.
It changes no policy, model_capacity, qualification record or installed model; it loads only gemma3:4b on the head
(keep_alive 2m, as the Gateway does). It records the head's runtime version and the model's digest and compares
them with the c922 build (Ollama 0.40.15, gemma3:4b 68ceab7e...). Results go to results\\ beside this file.
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

TOOL='g4-gemma500-repro'
FIXTURE_BLOB='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'
C922_BUILD={'runtime_version':'0.40.15','digest':'68ceab7e468a31c988b44d5a4984a0e21f58c4f7158c6ce29c1804babac38df7'}
MODEL='gemma3:4b'


def head_call(address,path,body=None,timeout=300):
    """One plain HTTP call to the head's Ollama. Returns status, headers, bounded body text and seconds; never raises for HTTP."""
    url='http://%s:11434%s'%(address,path)
    request=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),method='POST' if body is not None else 'GET',
                                   headers={'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    began=time.perf_counter()
    try:
        with opener.open(request,timeout=timeout) as response:
            status,headers,data=response.status,dict(response.headers),response.read(16*1024*1024)
    except urllib.error.HTTPError as exc:
        status,headers=exc.code,dict(exc.headers or {})
        try:data=exc.read(65536)
        except Exception:data=b''
    except (urllib.error.URLError,OSError) as exc:
        return {'status':None,'error':'%s: %s'%(type(exc).__name__,exc),'seconds':round(time.perf_counter()-began,3)}
    text=data.decode('utf-8','replace')
    out={'status':status,'seconds':round(time.perf_counter()-began,3),'content_type':headers.get('Content-Type')}
    try:value=json.loads(text)
    except ValueError:value=None
    if status!=200:out['error_body']=text[:65536];out['error_message']=value.get('error') if isinstance(value,dict) else None
    else:out['value']=value
    return out


def judge(result,schema):
    """For a 200 reply: done reason, token counts, timings and whether the content is JSON with the schema's keys."""
    value=result.get('value') or {}
    message=value.get('message') or {}
    content=message.get('content') if isinstance(message.get('content'),str) else ''
    try:parsed=json.loads(content);json_ok=isinstance(parsed,dict)
    except ValueError:parsed=None;json_ok=False
    required=(schema or {}).get('required') or []
    return {'done':value.get('done'),'done_reason':value.get('done_reason'),'eval_count':value.get('eval_count'),
            'prompt_eval_count':value.get('prompt_eval_count'),'load_ms':(value.get('load_duration') or 0)/1e6,
            'total_ms':(value.get('total_duration') or 0)/1e6,'content_chars':len(content),'json':json_ok,
            'schema_keys_present':json_ok and all(k in parsed for k in required),'content_head':content[:200]}


def head_idle(observation,resource_id,own=()):
    doc=read_observation(observation);a=activity(doc,own)
    busy=[l for l in doc.get('leases') or [] if l.get('resource_id')==resource_id and l.get('request_id') not in set(own)
          and not str(l.get('app_id','')).startswith('maintenance:')]
    return (not busy and a['queued_total']==0 and a['age_s']<=OBSERVATION_MAX_AGE),a,doc


def variants(bodies):
    target=next(b for b in bodies if b['label'].startswith('TARGET'))
    def changed(name,fn):
        payload=json.loads(json.dumps(target['payload']));fn(payload);return {'name':name,'body':target['label'],'payload':payload}
    out=[]
    for b in sorted(bodies,key=lambda b:(not b['label'].startswith('TARGET'),b['label'])):
        out.append({'name':'exact','body':b['label'],'payload':b['payload'],'observed_in_c922':b['observed_summary']})
    out+=[changed('no_format',lambda p:p.pop('format',None)),changed('format_json',lambda p:p.__setitem__('format','json')),
          changed('num_predict_1024',lambda p:p['options'].__setitem__('num_predict',1024)),changed('seed_8',lambda p:p['options'].__setitem__('seed',8)),
          changed('num_ctx_4096',lambda p:p['options'].__setitem__('num_ctx',4096))]
    return out


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--head',default='192.0.2.10',help='head address 192.0.2.N (default 192.0.2.10, where the target failed first)')
    p.add_argument('--mode',choices=('both','direct','gateway'),default='both');p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--client-config',help='needed for the gateway step');p.add_argument('--observation',default=str(OBSERVATION))
    p.add_argument('--yes',action='store_true')
    a=p.parse_args(argv)
    if not a.head.startswith('192.0.2.') or not 1<=a.repeats<=10:print('--head 192.0.2.N and --repeats 1-10');return 2
    resource_id='head-'+a.head.replace('.','-')
    try:
        data=fixture(FIXTURE_BLOB);bodies=data['bodies']
        doc,idle=require_idle(a.observation,what='the Gemma 500 reproduction')
        resource=next((r for r in doc.get('resources') or [] if r.get('resource_id')==resource_id),None)
        if not resource:raise Refused('%s is not a Gateway head'%resource_id)
        inventory=next((i for i in doc.get('inventory') or [] if i.get('resource_id')==resource_id),{})
        installed={m['name']:m.get('digest') for m in inventory.get('models') or []}
        if MODEL not in installed:raise Refused('%s is not installed on %s (Gateway inventory); choose another head with --head'%(MODEL,resource_id))
        gateway=None
        if a.mode in ('both','gateway'):
            if a.client_config:gateway=Gateway(a.client_config)
            elif a.mode=='gateway':raise Refused('The Gateway step needs --client-config of an identity permitted to use %s'%MODEL)
            else:print('No --client-config given: only the direct step runs (the Gateway step needs an identity permitted to use %s).'%MODEL)
        info=gateway.info() if gateway else None
        plan=variants(bodies) if a.mode in ('both','direct') else []
        calls=len(plan[:len(bodies)])*a.repeats+len(plan[len(bodies):])+(2 if gateway else 0)
        print('\nG4 Gemma 500 reproduction on %s (%s): %d calls of %s, one at a time.'%(resource_id,a.head,calls,MODEL))
        if gateway:print('The Gateway step may suspend %s on this head for about a minute if it reproduces the 500.'%MODEL)
        if not a.yes and input('Start now? Type yes: ').strip().lower()!='yes':raise Refused('Not confirmed')
        folder=results_folder(TOOL)
        report={'tool':TOOL,'tools_release':TOOLS_RELEASE,'at':now_iso(),'head':{'address':a.head,'resource_id':resource_id,'health':resource.get('health'),
                'gateway_inventory_digest':installed.get(MODEL),'qualified_in_gateway':MODEL in (resource.get('qualified_models') or [])},
                'c922_build':C922_BUILD,'source':data['source'],'target_request_id':data['target_request_id'],'idle_at_start':idle,
                'gateway':{**gateway.describe(),**info} if gateway else None,'direct':[],'gateway_replays':[],
                'notes':[] if gateway or a.mode=='direct' else ['Gateway step skipped: no --client-config (an identity permitted to use %s).'%MODEL]}
        version=head_call(a.head,'/api/version',timeout=10);tags=head_call(a.head,'/api/tags',timeout=10);ps_before=head_call(a.head,'/api/ps',timeout=10)
        live_digest=next((m.get('digest') for m in ((tags.get('value') or {}).get('models') or []) if m.get('name')==MODEL),None)
        runtime=(version.get('value') or {}).get('version')
        report['head'].update(runtime_version=runtime,live_digest=live_digest,loaded_before=[m.get('name') for m in ((ps_before.get('value') or {}).get('models') or [])],
                              same_build_as_c922=bool(runtime==C922_BUILD['runtime_version'] and live_digest==C922_BUILD['digest']))
        if not report['head']['same_build_as_c922']:
            report['notes'].append('The head does not run the c922 build (runtime %s, digest %s): results qualify the current build only.'%(runtime,(live_digest or '')[:12]))
        for v in plan:
            for i in range(a.repeats if v['name']=='exact' else 1):
                ok,act,_=head_idle(a.observation,resource_id)
                if not ok:
                    report['notes'].append('Stopped the direct step: other Gateway work appeared (%s).'%act);break
                body={'model':MODEL,**v['payload'],'keep_alive':'2m','stream':False}
                print('  direct %-17s %-17s #%d ...'%(v['name'],v['body'],i+1),end='',flush=True)
                at=time.time();result=head_call(a.head,'/api/chat',body)
                row={'variant':v['name'],'body':v['body'],'repeat':i+1,'at':now_iso(at),'status':result.get('status'),'seconds':result.get('seconds'),
                     'observed_in_c922':v.get('observed_in_c922')}
                if result.get('status')==200:row.update(judge(result,v['payload'].get('format') if isinstance(v['payload'].get('format'),dict) else None))
                else:row.update(error_message=result.get('error_message'),error_body=result.get('error_body'),transport_error=result.get('error'))
                report['direct'].append(row);print(' %s %s'%(row['status'],row.get('error_message') or row.get('done_reason') or row.get('transport_error') or ''))
            else:continue
            break
        if gateway:
            for label in [b['label'] for b in bodies if b['label'].startswith('TARGET')]+[b['label'] for b in bodies if b['observed_summary'].startswith('0 of')]:
                ok,act,_=head_idle(a.observation,resource_id)
                if not ok:report['notes'].append('Skipped the Gateway step: other work appeared (%s).'%act);break
                b=next(x for x in bodies if x['label']==label)
                print('  gateway %-17s ...'%label,end='',flush=True)
                envelope,seconds=gateway.run(MODEL,'/api/chat',b['payload'],client_key='labtool-g4-%s-%s'%(uuid.uuid4().hex[:8],label),nodes=[resource_id],
                                             deadline_seconds=180)
                row={'body':label,'status':envelope.get('status'),'error':envelope.get('error'),'upstream_errors':envelope.get('upstream_errors'),
                     'resource_id':(envelope.get('request') or {}).get('resource_id'),'request_id':(envelope.get('request') or {}).get('request_id'),
                     'seconds':round(seconds,3),'http_status':envelope.get('http_status')}
                report['gateway_replays'].append(row);print(' %s %s'%(row['status'],(row['upstream_errors'] or [{}])[0].get('message') or row['error'] or ''))
                if row['status']!='completed':
                    report['notes'].append('Gateway step stopped after %s ended %s: a 5xx suspends %s on this head until its recovery probe passes '
                                           '(lifecycle suspensions shows it).'%(label,row['status'],MODEL));break
            if info and 'upstream_error_capture' not in info['features']:
                report['notes'].append('This Gateway predates 0.4.6: its replies carry no upstream_errors (the direct step has the body).')
        ps_after=head_call(a.head,'/api/ps',timeout=10)
        report['head']['loaded_after']=[m.get('name') for m in ((ps_after.get('value') or {}).get('models') or [])]
        tally=collections.OrderedDict()
        for r in report['direct']:
            key='%s / %s'%(r['body'],r['variant']);t=tally.setdefault(key,collections.Counter());t['HTTP %s'%r['status']]+=1
        report['tally']={k:dict(v) for k,v in tally.items()}
        messages=sorted({r.get('error_message') or (r.get('error_body') or '')[:200] for r in report['direct'] if r['status'] not in (200,None)})
        report['distinct_error_messages']=messages
        lines=['G4 Gemma 500 reproduction, %s (%s), runtime %s, digest %s, same build as c922: %s'%(resource_id,report['at'],runtime,(live_digest or '?')[:12],
               report['head']['same_build_as_c922'])]
        lines+=['  %-40s %s'%(k,', '.join('%s x%d'%kv for kv in v.items())) for k,v in report['tally'].items()]
        lines+=['  error: '+m for m in messages]
        lines+=['  gateway %s: %s %s'%(r['body'],r['status'],(r['upstream_errors'] or [{}])[0].get('message') or '') for r in report['gateway_replays']]
        lines+=['NOTE: '+n for n in report['notes']]
        lines.append('Interpretation is for the engineers: a body that fails on an idle head while its variants succeed points at the request shape; '
                     'no failure on an idle head leaves load or runner state open. Nothing here is a fix.')
        finish(folder,report,lines);return 0
    except Refused as exc:return refused_exit(TOOL,exc)
    except KeyboardInterrupt:print('Stopped.');return 1


if __name__=='__main__':raise SystemExit(main())
