"""Application-owned enrollment, telemetry and qualified physical routing."""
import concurrent.futures
import hashlib
import importlib.util
import ipaddress
import json
import os
import threading
import time
import math
from pathlib import Path

from . import config, telemetry
from .ai_settings import http
from .cluster import ClusterEnrollment
from .coordination import uid
from .scheduler import DeterministicScheduler, NoEligibleWorker


class ClusterService:
    def __init__(self, store):
        self.store = store
        self.enrollment = ClusterEnrollment(store, os.environ.get('CAPSTONE_CLUSTER_NETWORK','192.0.2.0/24'))
        self.stop_event = threading.Event()
        self.thread = None
        self.refresh_lock = threading.Lock()

    def settings(self):
        row=self.store.one("SELECT value FROM cluster_policy WHERE key='settings'")
        return json.loads(row['value']) if row else {'addresses':[], 'model':None,'interval_seconds':60,'enabled':False}

    def configure(self, value):
        addresses=value.get('addresses',[])
        if not isinstance(addresses,list) or len(addresses)!=len(set(addresses)):
            raise ValueError('Unique authorized addresses required')
        for address in addresses:
            ip=ipaddress.ip_address(address)
            if ip not in self.enrollment.network or ip in (self.enrollment.network.network_address,self.enrollment.network.broadcast_address):
                raise ValueError('Address outside authorized host range')
            policy=self.store.one('SELECT * FROM resource_authorizations WHERE address=?',(address,))
            if policy and not self.enrollment.permitted(address):raise PermissionError('Application-exclusive or infrastructure device cannot be enrolled by inference settings')
        interval=int(value.get('interval_seconds',60))
        if not 30<=interval<=3600:raise ValueError('Refresh interval must be 30–3600 seconds')
        routes=value.get('workflow_routes',{})
        if not isinstance(routes,dict):raise ValueError('Workflow routes must be a role mapping')
        for role,route in routes.items():
            if role not in {'erp_research','ticket_interpretation','independent_validation'} or not route.get('model') or not route.get('nodes'):raise ValueError('Explicit role, model and eligible nodes required')
        result={'addresses':addresses,'model':value.get('model'),'interval_seconds':interval,'enabled':bool(value.get('enabled',False)),'workflow_routes':routes}
        for address in addresses:
            if not self.store.one('SELECT 1 FROM resource_authorizations WHERE address=?',(address,)):
                self.enrollment.authorize_resource(address,resource_type='shared_inference',owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
        self.store.upsert('cluster_policy',{'key':'settings','value':json.dumps(result)},'key')
        return result

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        self.thread=threading.Thread(target=self._loop,name='cluster-health',daemon=True);self.thread.start()

    def stop(self):self.stop_event.set()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.enrollment.expire()
                from .gateway_transport import enabled
                if enabled() or self.settings()['enabled']:self.heartbeat()
            except Exception as exc:
                self.store.audit('cluster_refresh_failed',actor='cluster',summary=str(exc))
            self.stop_event.wait(self.settings()['interval_seconds'])

    def refresh(self):
        from .gateway_transport import enabled
        if enabled():raise PermissionError('Gateway-mode model qualification uses operator tooling on the infrastructure host; no raw model probe is permitted')
        if not self.refresh_lock.acquire(blocking=False):raise ValueError('Refresh already running')
        try:
            settings=self.settings()
            path=Path(config.PROJECT_ROOT)/'tools'/'phase4_2_cluster_audit.py'
            spec=importlib.util.spec_from_file_location('cluster_audit',path)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            # Discovery/qualification is explicit; routine heartbeat never generates text.
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(9,len(settings['addresses'])))) as pool:
                def audit(a):
                    location=self.store.one('SELECT endpoint FROM node_locations WHERE address=?',(a,))
                    return module.audit(a,90,settings['model'],endpoint=location['endpoint'] if location else None)
                results=list(pool.map(audit,settings['addresses']))
            workers=[self.enrollment.ingest(r) for r in results]
            return workers
        finally:self.refresh_lock.release()

    def heartbeat(self):
        from . import gateway_transport
        if gateway_transport.enabled():
            # In shared mode this application consumes authorized metadata; it
            # neither probes raw heads nor writes the infrastructure ledger.
            gateway_client=gateway_transport.client()
            advertised={r['resource_id']:r for r in gateway_client.models()['resources']}
            for node_id,resource in advertised.items():
                generation_models=[model for model in resource['models'] if 'completion' in resource.get('model_details',{}).get(model,{}).get('qualified_operations',[])]
                existing=self.enrollment.workers.get('ollama-'+node_id)
                if not existing:
                    from .scheduler import WorkerSpec
                    self.enrollment.workers.register(WorkerSpec('ollama-'+node_id,node_id,node_id,'ollama','unknown',
                        ('model.inference',),('*',),endpoint=gateway_client.url,
                        available_models=tuple(generation_models),measurement_provenance='authenticated gateway qualification subset'))
                else:self.enrollment.workers.heartbeat(existing['worker_id'],health_state='healthy',available_models=generation_models,
                        provenance='authenticated gateway qualification subset')
                self.store.update('worker_registry','worker_id','ollama-'+node_id,{'endpoint':gateway_client.url})
            for worker in self.enrollment.workers.all():
                if worker['worker_type']=='ollama' and worker['node_id'] not in advertised:
                    self.enrollment.workers.heartbeat(worker['worker_id'],health_state='unavailable',available_models=[],provenance='gateway resource unavailable or permission removed')
            return
        for node in self.store.query('SELECT * FROM physical_nodes'):
            worker=self.enrollment.workers.get('ollama-'+node['node_id'])
            if not worker or node['state'] in {'disabled','quarantined','retired'}:continue
            if not self.enrollment.permitted(worker['hostname']):
                self.enrollment.workers.heartbeat(worker['worker_id'],health_state='unavailable',available_models=[],provenance='resource authorization revoked')
                continue
            try:
                inventory=http(node['endpoint'],'/api/tags',timeout=3)['models']
                loaded=http(node['endpoint'],'/api/ps',timeout=3).get('models',[])
                evidence=self.store.one('SELECT evidence FROM node_qualification_evidence WHERE node_id=? ORDER BY observed_at DESC LIMIT 1',(node['node_id'],))
                old=json.loads(evidence['evidence']) if evidence else {}
                digests={m['name']:m.get('digest') for m in inventory}
                version=http(node['endpoint'],'/api/version',timeout=3)
                valid=[r['model'] for r in self.store.query('SELECT * FROM qualified_models WHERE node_id=? AND passed=1',(node['node_id'],))
                       if digests.get(r['model'])==r['digest'] and r['version']==json.dumps(version,sort_keys=True)]
                self.enrollment.workers.heartbeat(worker['worker_id'],health_state='healthy' if valid else 'unavailable',installed_models=list(digests),available_models=valid,loaded_models=[m['name'] for m in loaded],provenance='live Ollama inventory, digest and residency')
                self.store.update('physical_nodes','node_id',node['node_id'],{'last_seen':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'state':'available' if valid else 'degraded'})
            except Exception:
                self.enrollment.workers.heartbeat(worker['worker_id'],health_state='unavailable',available_models=[],provenance='heartbeat transport failure')

    def ingest_metrics(self,node_id,value):
        if not self.store.one('SELECT 1 FROM physical_nodes WHERE node_id=?',(node_id,)):raise ValueError('Unknown enrolled node')
        allowed={'cpu_percent','ram_total_bytes','ram_available_bytes','gpu_percent','vram_total_bytes','vram_available_bytes','sampled_at'}
        if set(value)-allowed:raise ValueError('Unknown metric')
        if abs(time.time()-float(value.get('sampled_at',0)))>120:raise ValueError('Stale telemetry')
        for key,v in value.items():
            if v is not None and (not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<0):raise ValueError('Invalid metric')
            if key.endswith('percent') and v is not None and v>100:raise ValueError('Invalid utilization')
        self.store.insert('node_metrics',{'sample_id':uid('METRIC'),'node_id':node_id,'at':time.time(),'payload':json.dumps(value)})
        if value.get('ram_total_bytes'):
            ram=value['ram_total_bytes']/1024**3
            tier='gpu' if value.get('vram_total_bytes',0) else 'minimum_cpu' if ram<=10 else 'standard_cpu' if ram<=20 else 'expanded_cpu'
            self.store.update('physical_nodes','node_id',node_id,{'tier':tier,'hardware_basis':'authenticated CIM/NVIDIA host measurements'})
            self.store.execute('UPDATE worker_registry SET compute_class=? WHERE node_id=?',(tier,node_id))
        self.store.execute('DELETE FROM node_metrics WHERE at<?',(time.time()-7*86400,))
        return {'accepted':True}

    def route(self,model,nodes,task_id,workflow_id):
        self.enrollment.expire()
        selected=DeterministicScheduler(self.store).select({'task_id':task_id,'workflow_id':workflow_id,
            'capability_requirement':'model.inference','task_type':'inference',
            'required_model':model,'model_eligibility':nodes})
        self.enrollment.workers.reserve(selected['worker_id'])
        return selected
