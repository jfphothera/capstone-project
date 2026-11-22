"""Persistent private-LAN inference settings; profiles are data, never permissions."""
import copy,hashlib,ipaddress,json,os,re,socket,sqlite3,time,urllib.request,uuid
from pathlib import Path
from contextlib import contextmanager
from . import config
BASE_ROLES=('ticket_interpretation','erp_research','qms_research','investigation','resolution_planning','customer_communication','communication_review','independent_validation')
# Collaboration stages with their own route (model + context), rc.20. Profiles
# saved before rc.20 inherit these from resolution_planning, recorded explicitly
# as inherited_from in the snapshot (never a silent default).
STAGE_ROLES=('supervisor','peer_reviewer','reflection')
# Tool-selection delegate: a separately configured tool-capable model that
# chooses tools for a role whose model has no native tool calling (declared
# strategy 'delegated'). Part of the system under test.
TOOL_ROLES=('tool_delegate',)
ROLES=BASE_ROLES+STAGE_ROLES+TOOL_ROLES
# Agentic Testing participants OUTSIDE the system under test. Their routes are
# added to a run snapshot by the Testing Lab only; they never appear in a
# production profile and their calls are attributed separately.
LAB_ROLES=('lab_reviewer',)
# Declared tool-selection strategies (no silent switching between them).
TOOL_STRATEGIES=('native','mediated','delegated')
GATEWAY_CAPABILITIES=frozenset(('chat','tools','structured_output','vision','context'))
LEGACY_BUILT_INS=('baseline-local','baseline-gpu','balanced','reasoning-heavy','collaborative-local')
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'))
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def validate_options(options):
    if not set(options)<= {'temperature','num_ctx','num_predict','seed','top_p','top_k','repeat_penalty'}:raise ValueError('Unsupported inference option')
    ranges={'temperature':(0,2),'num_ctx':(512,65536),'num_predict':(1,8192),'seed':(0,2147483647),'top_p':(0,1),'top_k':(1,1000),'repeat_penalty':(0,3)}
    for key,value in options.items():
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not ranges[key][0]<=value<=ranges[key][1]:raise ValueError('Invalid inference option: '+key)
        if key in ('num_ctx','num_predict','seed','top_k') and int(value)!=value:raise ValueError('Integer inference option required: '+key)
def private_url(url):
    from urllib.parse import urlsplit
    parts=urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.path not in ('','/') or parts.query or parts.fragment:raise ValueError('Use a private Ollama origin, without credentials, paths or query strings')
    addresses={x[4][0] for x in socket.getaddrinfo(parts.hostname,parts.port or 80,type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(x).is_loopback for x in addresses):raise ValueError('Direct inference is allowed only on this laptop (127.0.0.1)')
    return url.rstrip('/')
def http(url,path,payload=None,timeout=5):
    if os.environ.get('CAPSTONE_INFERENCE_MODE','gateway')=='gateway' and path in {'/api/generate','/api/chat','/api/embed'}:
        raise PermissionError('Direct inference/model loading is disabled in gateway mode')
    private_url(url)
    req=urllib.request.Request(url.rstrip('/')+path,data=None if payload is None else canonical(payload).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=timeout) as response:return json.load(response)
class Registry:
    def __init__(self,path=None):
        self.path=str(path or os.environ.get('CAPSTONE_AI_SETTINGS',Path(config.DATA_DIR)/'ai_configuration.db'))
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            # WAL lets the web server read while campaign workers write.
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""CREATE TABLE IF NOT EXISTS endpoints(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inference_slots(endpoint TEXT PRIMARY KEY,owner TEXT,expires REAL,last_model TEXT);
            CREATE TABLE IF NOT EXISTS campaign_budgets(experiment_id TEXT PRIMARY KEY,remaining_tokens INTEGER NOT NULL,remaining_calls INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS experiment_runs(id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS experiment_run_summaries(id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL,status TEXT NOT NULL,updated_at REAL NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS experiment_runs_by_experiment ON experiment_runs(experiment_id);
            CREATE INDEX IF NOT EXISTS experiment_run_summaries_by_experiment ON experiment_run_summaries(experiment_id,status);
            CREATE TABLE IF NOT EXISTS retrieval_profiles(id TEXT PRIMARY KEY,payload TEXT NOT NULL);""")
            self._migrate(db)

    def _migrate(self,db):
        """rc.7 migration: one Gateway endpoint, migrated custom profiles.

        Idempotent and write-free once applied. Every Testing Lab worker opens
        this registry, so unconditional rewrites here used to take a write
        lock on the shared settings database once per worker start.
        """
        def upsert(table,key,value):
            text=canonical(value);row=db.execute(f'SELECT payload FROM {table} WHERE id=?',(key,)).fetchone()
            if not row or row[0]!=text:db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)',(key,text))
        gateway={'id':'gateway','name':'Inference Gateway','url':'https://192.0.2.254:8443','timeout':120,
            'max_concurrency':1,'transport':'gateway','resource_id':'@authorized'}
        if [r[0] for r in db.execute('SELECT payload FROM endpoints')]!=[canonical(gateway)]:
            db.execute('DELETE FROM endpoints')
            db.execute('INSERT INTO endpoints VALUES (?,?)',('gateway',canonical(gateway)))
        for row in db.execute('SELECT id,payload FROM profiles').fetchall():
            if row[0] in LEGACY_BUILT_INS:continue
            profile=json.loads(row[1]);profile['default_endpoint']='gateway'
            for route in profile.get('routes',{}).values():
                route['endpoint']='gateway'
                if route.get('model')=='qwen3:4b':route['model']='qwen3.5:4b'
            if canonical(profile)!=row[1]:db.execute('UPDATE profiles SET payload=? WHERE id=?',(canonical(profile),row[0]))
        legacy=[key for key in LEGACY_BUILT_INS if db.execute('SELECT 1 FROM profiles WHERE id=?',(key,)).fetchone()]
        db.executemany('DELETE FROM profiles WHERE id=?',((key,) for key in legacy))
        routes={role:{'endpoint':'gateway','model':'qwen3.5:4b','enabled':True,
            'options':{'temperature':0.1,'num_ctx':8192}} for role in ROLES}
        upsert('profiles','gateway-default',{'id':'gateway-default','name':'Gateway · Qwen3.5 4B','default_endpoint':'gateway','routes':routes,
            'retrieval_profile':'minilm','strict':True,'case_budget_seconds':300})
        active=db.execute("SELECT value FROM preferences WHERE key='active_profile'").fetchone()
        if not active or active[0]!='gateway-default':
            db.execute("INSERT INTO preferences VALUES ('active_profile','gateway-default') ON CONFLICT(key) DO UPDATE SET value='gateway-default'")
        for key,value in (('minilm',{'id':'minilm','name':'Existing MiniLM index','enabled':True,'validated':True,'model':config.EMBEDDING_MODEL,'path':config.INDEX_PATH}),
                          ('qwen3-experimental',{'id':'qwen3-experimental','name':'Qwen3 embedding experiment','enabled':False,'validated':False,'model':'qwen3-embedding:0.6b','endpoint':'gpu'})):
            if not db.execute('SELECT 1 FROM retrieval_profiles WHERE id=?',(key,)).fetchone():
                db.execute('INSERT INTO retrieval_profiles VALUES (?,?)',(key,canonical(value)))
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20)
        try:
            db.execute('PRAGMA busy_timeout=20000')
            with db:yield db
        finally:db.close()
    def list(self,table):
        if table not in ('endpoints','profiles','experiments','experiment_runs','retrieval_profiles'):raise ValueError('Unknown registry collection')
        with self.connect() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM '+table)]
    def get(self,table,key):
        # Primary-key lookup. The previous implementation parsed every row of
        # the table (every run of every campaign) to find one id.
        if table not in ('endpoints','profiles','experiments','experiment_runs','retrieval_profiles'):raise ValueError('Unknown registry collection')
        with self.connect() as db:row=db.execute('SELECT payload FROM '+table+' WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self,table,value):
        if table not in ('endpoints','profiles','retrieval_profiles'):raise ValueError('Invalid collection')
        value=copy.deepcopy(value);key=value.get('id') or uuid.uuid4().hex
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',key):raise ValueError('Invalid identifier')
        value['id']=key
        if table=='endpoints':
            value['url']=private_url(value['url']);value['timeout']=min(600,max(5,float(value.get('timeout',120))));value['max_concurrency']=1
        if table=='profiles':
            if key=='gateway-default':raise ValueError('The built-in Gateway profile is preserved; duplicate or save a custom profile')
            if value.get('default_endpoint') not in {e['id'] for e in self.list('endpoints')}:raise ValueError('Default endpoint is not registered')
            for role,route in value.get('routes',{}).items():
                if role not in ROLES or (route.get('endpoint')!='default' and not self.get('endpoints',route.get('endpoint'))) or not route.get('model'):raise ValueError('Invalid specialist route')
                options=route.get('options',{})
                validate_options(options)
                if route.get('expected_digest') is not None and (not isinstance(route['expected_digest'],str) or not route['expected_digest'].strip()):raise ValueError('Invalid expected model digest')
                required=route.get('required_capabilities',[])
                if not isinstance(required,list) or any(not isinstance(item,str) or not item for item in required):raise ValueError('Invalid required capabilities')
            if not set(BASE_ROLES)<=set(value.get('routes',{}))<=set(ROLES):raise ValueError('Every configurable role needs an explicit route')
            profile=self.get('retrieval_profiles',value.get('retrieval_profile','minilm'))
            if not profile or not profile['enabled'] or not profile['validated']:raise ValueError('Retrieval profile is not built and validated')
            value['strict']=True
            value['case_budget_seconds']=min(900,max(30,int(value.get('case_budget_seconds',300))))
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO '+table+' VALUES (?,?)',(key,canonical(value)))
        return value
    def active(self):
        with self.connect() as db:key=db.execute("SELECT value FROM preferences WHERE key='active_profile'").fetchone()[0]
        return self.get('profiles',key)
    def activate(self,key):
        if not self.get('profiles',key):raise ValueError('Unknown profile')
        with self.connect() as db:db.execute("UPDATE preferences SET value=? WHERE key='active_profile'",(key,))
        return self.active()
    def snapshot(self,profile=None):
        value=copy.deepcopy(profile or self.active())
        for stage in STAGE_ROLES:
            if stage not in value['routes'] and 'resolution_planning' in value['routes']:
                value['routes'][stage]={**copy.deepcopy(value['routes']['resolution_planning']),'inherited_from':'resolution_planning'}
        if 'tool_delegate' not in value['routes'] and 'investigation' in value['routes']:
            value['routes']['tool_delegate']={**copy.deepcopy(value['routes']['investigation']),'inherited_from':'investigation'}
        for route in value['routes'].values():
            if route.get('endpoint')=='default':route['endpoint']=value['default_endpoint'];route['inherited_default']=True
        value['execution_mode']='local' if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1' and not os.environ.get('CAPSTONE_GATEWAY_CLIENT_CONFIG') else 'distributed'
        value['endpoints']={e['id']:e for e in self.list('endpoints')};value['retrieval']=self.get('retrieval_profiles',value.get('retrieval_profile','minilm'));value['configuration_hash']=fingerprint(value)
        return value
    def health(self,key,workload='operational',models=None):
        """Gateway inventory for one endpoint. rc.25 (G1 client side): with
        ``models`` the availability checks are scoped to those models only
        (launch preflight), and the policy read uses the cheap adapter path."""
        endpoint=self.get('endpoints',key)
        if not endpoint:raise ValueError('Unknown endpoint')
        result={**endpoint,'reachable':False,'installed_models':[],'loaded_models':[],'loaded_check_succeeded':False,'last_error':None,'checked_at':time.time()}
        try:
            from .gateway_transport import client
            from . import gateway_capabilities
            gateway=client(workload);resource=endpoint.get('resource_id')
            if models and resource in (None,'@authorized'):
                # Gateway 0.4.6: one application-scoped fleet snapshot for the
                # selected models replaces inventory + per-model availability.
                rows=gateway_capabilities.installed_models_from_fleet(gateway,models)
                if rows is not None:
                    result.update(reachable=True,installed_models=rows,gateway_policy=gateway_capabilities.policy(gateway)['policy'],
                        last_error=None,inventory_scope='fleet_snapshot_selected_models',
                        note='Gateway /v1/fleet snapshot (advisory); admission and dispatch re-check every real request')
                    return result
            inventory=gateway.models();policy=gateway_capabilities.policy(gateway)['policy'];resources=inventory['resources']
            selected=resources if resource in (None,'@authorized') else [r for r in resources if r['resource_id']==resource]
            models_filter=models;models={}
            for advertised in selected:
                for name in advertised['models']:
                    detail=advertised.get('model_details',{}).get(name,{})
                    rank=lambda d:('completion' in d.get('qualified_operations',[]),d.get('execution_policy',{}).get('max_context',0))
                    if name not in models or rank(detail)>rank(models[name]):models[name]=detail
            installed=[];model_errors=[]
            wanted=set(models_filter or ())
            for name,detail in models.items():
                if wanted and name not in wanted:continue
                operations=set(detail.get('qualified_operations',[]))
                operation='/api/embed' if 'embedding' in operations and 'completion' not in operations else '/api/generate'
                capabilities=sorted(set(detail.get('qualified_capabilities',[]))&GATEWAY_CAPABILITIES)
                try:
                    checked=gateway.availability(name,operation,capabilities=capabilities)
                    installed.append({'name':name,**detail,'availability':checked,'currently_available':checked.get('status')=='available'})
                except Exception as exc:
                    error=str(exc);model_errors.append(f'{name}: {error}')
                    installed.append({'name':name,**detail,'availability':{'status':'error','error':error},
                        'currently_available':False,'last_error':error})
            result.update(reachable=True,installed_models=installed,gateway_policy=policy,
                last_error='; '.join(model_errors) or None,inventory_scope='authorized_qualified_subset',
                note='Authenticated Gateway qualification subset; availability is a point-in-time observation and loaded state is unavailable to this client')
            return result
        except Exception as exc:result['last_error']=str(exc)
        result['note']='Inference Gateway inventory could not be verified; no direct transport or fallback was attempted.'
        return result
    def validate_available(self,profile):
        errors=[];health={}
        for role,route in profile['routes'].items():
            if not route.get('enabled',True):continue
            key=profile['default_endpoint'] if route['endpoint']=='default' else route['endpoint']
            if key not in health:
                selected=sorted({r['model'] for r in profile['routes'].values() if r.get('enabled',True) and
                                 (profile['default_endpoint'] if r['endpoint']=='default' else r['endpoint'])==key})
                workload=profile.get('gateway_workload','operational')
                if os.environ.get('CAPSTONE_LAB_RUN_DIR') and os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway':
                    # One bounded snapshot per campaign selection, shared by all
                    # worker processes; real requests stay Gateway-authorized.
                    from . import gateway_capabilities
                    health[key]=gateway_capabilities.campaign_snapshot(lambda:self.health(key,workload,models=selected),
                        workload=workload,endpoint=key,models=selected)
                else:health[key]=self.health(key,workload,models=selected)
            state=health[key]
            if not state['reachable'] or route['model'] not in {m['name'] for m in state['installed_models']}:errors.append({'role':role,'endpoint':key,'model':route['model'],'error':state['last_error'] or 'Selected model is not installed'})
            installed=next((m for m in state['installed_models'] if m['name']==route['model']),{})
            if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' and 'completion' not in installed.get('qualified_operations',[]):errors.append({'role':role,'endpoint':key,'model':route['model'],'error':'Selected model lacks generation qualification'})
            if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' and route.get('options',{}).get('num_ctx',0)>installed.get('execution_policy',{}).get('max_context',1024):errors.append({'role':role,'endpoint':key,'model':route['model'],'error':'Selected context exceeds qualified capacity'})
            if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' and route.get('expected_digest') and route['expected_digest']!=installed.get('digest'):errors.append({'role':role,'endpoint':key,'model':route['model'],'error':'Selected model digest differs from the pinned digest'})
            if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' and not set(route.get('required_capabilities',[]))<=set(installed.get('qualified_capabilities',[])):errors.append({'role':role,'endpoint':key,'model':route['model'],'error':'Selected model lacks a required qualified capability'})
            if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' and installed.get('availability',{}).get('status') not in ('available','busy'):
                availability=installed.get('availability',{})
                errors.append({'role':role,'endpoint':key,'model':route['model'],
                    'error':installed.get('last_error') or availability.get('error') or availability.get('reason') or availability.get('status') or 'Gateway did not verify this request shape as eligible'})
            capabilities=installed.get('qualified_capabilities',installed.get('capabilities',[]))
            if capabilities and 'embedding' in capabilities and 'completion' not in capabilities:errors.append({'role':role,'endpoint':key,'model':route['model'],'error':'Embedding-only model cannot serve a conversational role'})
        return {'passed':not errors,'errors':errors,'endpoints':health}
