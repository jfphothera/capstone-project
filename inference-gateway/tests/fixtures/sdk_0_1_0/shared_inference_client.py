"""Independent stdlib-only client. No Capstone or Arena imports."""
import ipaddress,json,ssl,time,urllib.request,urllib.parse,uuid

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
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
    def request(self,path,body=None,timeout=120):
        req=urllib.request.Request(self.url+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type':'application/json','X-Application-ID':self.app,'X-Infrastructure-Token':self.token})
        with self.opener.open(req,timeout=timeout) as response:
            if response.headers.get('X-Inference-Service-ID')!=self.service_id:raise PermissionError('Unexpected inference service identity')
            return json.load(response)
    def infer(self,model,nodes,operation,payload,*,client_key=None,timeout=120,progress=None,expected_digest=None):
        end=time.monotonic()+timeout
        current=self.request('/v1/submit',{'contract':'shared.inference/1','client_key':client_key or uuid.uuid4().hex,
            'model':model,'nodes':['@authorized'] if nodes is None else nodes,'operation':operation,'payload':payload,'deadline':time.time()+timeout,
            'expected_digest':expected_digest},min(timeout,10))
        request_id=current['request']['request_id'];previous=None
        while True:
            request=current['request'];key=(current['status'],request['status'],request.get('executing'),request.get('resource_id'),current.get('execution_started_at'))
            if progress and key!=previous:progress(current)
            previous=key
            if current['status'] not in {'active','queued','leased','cancel_requested'}:return current
            if time.monotonic()>=end:
                self.cancel(request_id)
                raise TimeoutError('Gateway inference deadline exceeded; query this request before retrying')
            time.sleep(.1);current=self.status(request_id,timeout=max(.1,min(5,end-time.monotonic())))
    def status(self,request_id,timeout=10):return self.request('/v1/requests/'+urllib.parse.quote(request_id,safe=''),timeout=timeout)
    def cancel(self,request_id):return self.request('/v1/requests/'+urllib.parse.quote(request_id,safe='')+'/cancel',{},timeout=5)
    def models(self):return self.request('/v1/models')
