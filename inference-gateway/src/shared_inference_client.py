"""Independent stdlib-only client. No Capstone or Arena imports.

0.4.1: a busy Gateway no longer fails a flow. Status reads and submissions that time out,
are reset, or are answered 503 are retried with the same request_id / client_key until the
request's own deadline; status polling backs off from 0.25 s to 2 s; Retry-After is honoured.
Works with Gateway 0.2.0 and later (shared.inference/1).

0.4.2 (Gateway 0.4.6), additive; every 0.4.1 method and its behaviour is unchanged:
  gateway_info()                 GET /health: release and features (older Gateways: release None, features [])
  policy(availability=False)     the operator policy alone, no availability work; older Gateways: the full policy
  fleet(models=..., ...)         GET /v1/fleet, one application-scoped snapshot with installed, qualified and
                                 available as separate states; raises GatewayFeatureUnavailable on older Gateways
  fleet_cached(...)              the same, reused in this process until the snapshot's valid_until
"""
import http.client,ipaddress,json,socket,ssl,threading,time,urllib.error,urllib.request,urllib.parse,uuid

__version__='0.4.2'


class GatewayFeatureUnavailable(LookupError):
    """0.4.2: the Gateway predates this feature (it answered 404); use the 0.4.1 calls instead."""
POLL_FIRST,POLL_MAX,POLL_FACTOR=.25,2.0,1.5
SUBMIT_ATTEMPT_SECONDS,STATUS_ATTEMPT_SECONDS=10,10
RETRY_STATUS={429,502,503}


def retry_delay(exc):
    """Seconds to wait before retrying the same request after this error, or None when it is final.

    Retried: no answer (timeout, reset, refused, truncated reply) and 429/502/503 answers.
    Final: every other HTTP answer (400 invalid or over the application's queue, 403, 404, 504).
    """
    if isinstance(exc,urllib.error.HTTPError):
        if exc.code not in RETRY_STATUS:return None
        try:return min(10.0,max(0.0,float(exc.headers.get('Retry-After'))))
        except (TypeError,ValueError,AttributeError):return 1.0
    if isinstance(exc,urllib.error.URLError):exc=exc.reason
    if isinstance(exc,ssl.SSLCertVerificationError):return None # the wrong service: never retried
    if isinstance(exc,(TimeoutError,socket.timeout,ConnectionError,http.client.HTTPException)):return 0.0
    if isinstance(exc,OSError) and not isinstance(exc,PermissionError):return 0.0
    return None


class Client:
    @classmethod
    def from_config(cls,path):
        from pathlib import Path
        import os
        path=Path(path);config=json.loads(path.read_text(encoding='utf-8'))
        if config.get('contract')!='shared.inference/1' or config.get('configuration_version')!=1:raise ValueError('Unsupported approved bootstrap configuration')
        token=os.environ.get(config.get('token_environment','CAPSTONE_INFRASTRUCTURE_TOKEN'))
        if config.get('token_file'):
            token_path=Path(config['token_file']);token_path=token_path if token_path.is_absolute() else path.parent/token_path
            token=token_path.read_text().strip()
        if not token:raise PermissionError('Approved application credential unavailable')
        ca=config.get('ca_file')
        if ca and not Path(ca).is_absolute():ca=str(path.parent/ca)
        return cls(config['url'],config['application_id'],token,service_id=config['service_id'],ca_file=ca)
    def __init__(self,url,application_id,token,*,service_id='shared-inference',ca_file=None):
        origin=urllib.parse.urlsplit(url)
        if origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('','/'):raise ValueError('Use a service origin without credentials/query/path')
        try:loopback=ipaddress.ip_address(origin.hostname).is_loopback
        except ValueError:loopback=origin.hostname=='localhost'
        if origin.scheme!='https' and not (origin.scheme=='http' and loopback):raise ValueError('Remote shared inference requires verified HTTPS')
        self.url=url.rstrip('/');self.app=application_id;self.token=token;self.service_id=service_id
        self.retries={'submit':0,'status':0,'cancel':0} # diagnostics: retried attempts since this client was made
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
    def request(self,path,body=None,timeout=120):
        """One HTTP exchange, no retry (unchanged since 0.2.0)."""
        req=urllib.request.Request(self.url+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type':'application/json','X-Application-ID':self.app,'X-Infrastructure-Token':self.token})
        with self.opener.open(req,timeout=timeout) as response:
            if response.headers.get('X-Inference-Service-ID')!=self.service_id:raise PermissionError('Unexpected inference service identity')
            return json.load(response)
    def _until(self,kind,path,body,*,end,attempt_seconds):
        """The same request (same path and body, so the same request_id or client_key) until `end` (monotonic)."""
        backoff=POLL_FIRST
        while True:
            remaining=end-time.monotonic()
            try:return self.request(path,body,timeout=max(.1,min(attempt_seconds,remaining)))
            except Exception as exc:
                delay=retry_delay(exc)
                if isinstance(exc,urllib.error.HTTPError):exc.close()
                if delay is None:raise
                wait=max(delay,backoff);backoff=min(POLL_MAX,backoff*2)
                if time.monotonic()+wait>=end:raise
                self.retries[kind]+=1;time.sleep(wait)
    def infer(self,model,nodes,operation,payload,*,client_key=None,timeout=120,progress=None,expected_digest=None,required_capabilities=None):
        end=time.monotonic()+timeout
        body={'contract':'shared.inference/1','client_key':client_key or uuid.uuid4().hex,
            'model':model,'nodes':['@authorized'] if nodes is None else nodes,'operation':operation,'payload':payload,'deadline':time.time()+timeout,
            'expected_digest':expected_digest}
        # Gateway 0.3.0+: refuse heads without measured qualification of these capabilities.
        if required_capabilities is not None:body['required_capabilities']=list(required_capabilities)
        # A retried submission is the same body with the same client_key: the Gateway returns the same request.
        current=self._until('submit','/v1/submit',body,end=end,attempt_seconds=min(timeout,SUBMIT_ATTEMPT_SECONDS))
        request_id=current['request']['request_id'];previous=None;interval=POLL_FIRST
        path='/v1/requests/'+urllib.parse.quote(request_id,safe='')
        while True:
            request=current['request'];key=(current['status'],request['status'],request.get('executing'),request.get('resource_id'),current.get('execution_started_at'))
            if progress and key!=previous:progress(current)
            if key!=previous:interval=POLL_FIRST # something moved: look again soon
            previous=key
            if current['status'] not in {'active','queued','leased','cancel_requested'}:return current
            if time.monotonic()>=end:
                try:self.cancel(request_id)
                except Exception:pass # best effort: the Gateway's own deadline ends the request anyway
                raise TimeoutError('Gateway inference deadline exceeded; query this request before retrying')
            time.sleep(max(0,min(interval,end-time.monotonic())));interval=min(POLL_MAX,interval*POLL_FACTOR)
            try:current=self._until('status',path,None,end=end,attempt_seconds=STATUS_ATTEMPT_SECONDS)
            except Exception as exc:
                if retry_delay(exc) is None:raise
                continue # the deadline was reached while retrying: the check above cancels and raises
    def status(self,request_id,timeout=10):return self.request('/v1/requests/'+urllib.parse.quote(request_id,safe=''),timeout=timeout)
    def cancel(self,request_id):
        return self._until('cancel','/v1/requests/'+urllib.parse.quote(request_id,safe='')+'/cancel',{},end=time.monotonic()+10,attempt_seconds=5)
    def models(self):return self.request('/v1/models')
    # Gateway 0.3.0+ application-scoped policy and pre-flight availability.
    def policy(self,availability=True):
        """0.4.2: availability=False asks Gateway 0.4.6+ for the operator policy alone (no per-model availability work).

        An older Gateway answers 404 to the query; the full 0.4.1 policy is then returned (its `models[].operations`
        are present, and `availability_included` is absent).
        """
        if availability:return self.request('/v1/policy')
        try:return self.request('/v1/policy?availability=0')
        except urllib.error.HTTPError as exc:
            if exc.code!=404:raise
            exc.close();return self.request('/v1/policy')
    def gateway_info(self,timeout=10):
        """0.4.2: GET /health. {'release': '0.4.6', 'features': [...]} on Gateway 0.4.6+, release None before."""
        value=self.request('/health',timeout=timeout)
        return {'ok':value.get('ok'),'contract':value.get('contract'),'service_id':value.get('service_id'),
                'release':value.get('release'),'features':list(value.get('features') or [])}
    def fleet(self,models=None,*,operations=None,num_ctx=None,capabilities=(),timeout=30):
        """0.4.2: GET /v1/fleet (Gateway 0.4.6+): per model, installed / qualified / available as separate states.

        models: names to report (default: every permitted model). operations: subset of /api/generate, /api/chat,
        /api/embed (default generate and embed). Raises GatewayFeatureUnavailable on an older Gateway (404).
        The snapshot is advisory; the Gateway admits and checks every request again.
        """
        query={}
        if models:query['models']=','.join(models)
        if operations:query['operations']=','.join(operations)
        if num_ctx is not None:query['num_ctx']=str(num_ctx)
        if capabilities:query['capabilities']=','.join(capabilities)
        try:return self.request('/v1/fleet'+('?'+urllib.parse.urlencode(query) if query else ''),timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code!=404:raise
            exc.close();raise GatewayFeatureUnavailable('This Gateway has no /v1/fleet (before 0.4.6)') from None
    def fleet_cached(self,models=None,*,operations=None,num_ctx=None,capabilities=(),timeout=30):
        """0.4.2: fleet(), reused in this process until the snapshot's own valid_until (Gateway clock, at most 5 s)."""
        key=(tuple(models or ()),tuple(operations or ()),num_ctx,tuple(sorted(capabilities or ())))
        lock=self.__dict__.setdefault('_fleet_lock',threading.Lock())
        with lock:
            cache=self.__dict__.setdefault('_fleet_cache',{})
            hit=cache.get(key)
            if hit and time.monotonic()<hit[0]:return hit[1]
        value=self.fleet(models,operations=operations,num_ctx=num_ctx,capabilities=capabilities,timeout=timeout)
        snapshot=value.get('snapshot') or {}
        # What is left of the snapshot's validity when the Gateway served it (both times on the Gateway's clock): a
        # snapshot served from the Gateway's own cache near its end is reused here only for the rest of its time.
        served=snapshot.get('served_at',snapshot.get('generated_at',0))
        try:ttl=max(0.0,min(float(snapshot.get('max_age_seconds') or 0),float(snapshot.get('valid_until',0))-float(served)))
        except (TypeError,ValueError):ttl=0.0
        with lock:self._fleet_cache[key]=(time.monotonic()+ttl,value)
        return value
    def availability(self,model,operation='/api/generate',*,num_ctx=None,capabilities=()):
        query={'model':model,'operation':operation}
        if num_ctx is not None:query['num_ctx']=str(num_ctx)
        if capabilities:query['capabilities']=','.join(capabilities)
        return self.request('/v1/availability?'+urllib.parse.urlencode(query))
