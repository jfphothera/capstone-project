"""Standalone neutral HTTP/TLS service, using the existing capacity authority."""
import argparse,json,socket,ssl,time,threading,urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from .capacity_gateway import Gateway,GatewayOverload,CONTRACT
from .capacity_broker import LedgerBusy,ledger_wait_seconds
from . import __version__ as RELEASE

RETRY_AFTER_SECONDS=1
OVERFLOW_CONNECTIONS=64 # connections over the limit that still get a 503 answer; beyond that they are closed

def server_tls(certificate,key):
    """Server TLS context: TLS 1.2+, certificate chain loaded once at startup."""
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.minimum_version=ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(certificate,key)
    return tls

class Server(ThreadingHTTPServer):
    daemon_threads=True
    # 0.4.1: the accept backlog (socketserver default 5) was smaller than one burst of flows.
    request_queue_size=256
    def __init__(self,address,gateway,service_id='shared-inference',max_connections=64,tls=None):
        self.gateway=gateway;self.service_id=service_id;self.tls=tls;self.max_connections=max_connections
        self.connections=threading.BoundedSemaphore(max_connections)
        self.overflow=threading.BoundedSemaphore(OVERFLOW_CONNECTIONS)
        super().__init__(address,Handler)
    def process_request(self,request,client_address):
        if not self.connections.acquire(blocking=False):
            # 0.4.1: over the limit the client gets an answer (503, Retry-After), not a reset or a read timeout.
            if self.overflow.acquire(blocking=False):
                self.gateway.count('connections_503')
                threading.Thread(target=self._refuse,args=(request,),daemon=True,name='gateway-overflow').start()
            else:
                self.gateway.count('connections_closed_over_limit');self.shutdown_request(request)
            return
        request.settimeout(10)
        try:super().process_request(request,client_address)
        except Exception:self.connections.release();raise
    def _refuse(self,request):
        """Answer one over-limit connection with 503 and Retry-After. No ledger access, bounded reads."""
        try:
            request.settimeout(5)
            if self.tls:request=self.tls.wrap_socket(request,server_side=True)
            head=b''
            while b'\r\n\r\n' not in head and len(head)<65536:
                chunk=request.recv(4096)
                if not chunk:break
                head+=chunk
            # Read the body too: closing with unread data makes Windows send a reset instead of the answer.
            length=0
            for line in head.split(b'\r\n\r\n',1)[0].split(b'\r\n')[1:]:
                name,_,value=line.partition(b':')
                if name.strip().lower()==b'content-length':
                    try:length=min(int(value.strip()),1024*1024)
                    except ValueError:length=0
            left=length-len(head.split(b'\r\n\r\n',1)[1]) if b'\r\n\r\n' in head else 0
            while left>0:
                chunk=request.recv(min(65536,left))
                if not chunk:break
                left-=len(chunk)
            body=json.dumps({'error':'Gateway connection limit reached; retry after %d s with the same request identity'%RETRY_AFTER_SECONDS,
                             'retry_after_seconds':RETRY_AFTER_SECONDS}).encode()
            request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nRetry-After: %d\r\nX-Inference-Service-ID: %s\r\n'
                            b'Content-Length: %d\r\nConnection: close\r\n\r\n'%(RETRY_AFTER_SECONDS,self.service_id.encode(),len(body))+body)
            try:request.shutdown(socket.SHUT_WR)
            except OSError:pass
        except (OSError,ssl.SSLError):pass
        finally:
            try:request.close()
            except OSError:pass
            self.overflow.release()
    _handlers=0;_handlers_lock=threading.Lock()
    def active_handlers(self):
        """0.4.6: connections whose handler is still running (a graceful stop lets their replies finish)."""
        with self._handlers_lock:return self._handlers
    def process_request_thread(self,request,client_address):
        with self._handlers_lock:self._handlers+=1
        try:self._process(request,client_address)
        finally:
            with self._handlers_lock:self._handlers-=1
    def _process(self,request,client_address):
        try:
            if self.tls:
                # Handshake on the connection's own thread under its 10 s timeout, so a
                # silent or plaintext client never blocks the accept loop.
                try:request=self.tls.wrap_socket(request,server_side=True)
                except OSError:
                    self.shutdown_request(request);return
            super().process_request_thread(request,client_address)
        finally:self.connections.release()

KNOWN_ROUTES={'/health','/ready','/v1/models','/v1/usage','/v1/policy','/v1/availability','/v1/fleet','/v1/inference','/v1/submit'}
# 0.4.6: what this release serves beyond 0.4.5 (GET /health, additive), so a client can choose without probing.
FEATURES=['policy_without_availability','fleet_snapshot','upstream_error_capture','structured_chat_qualification']
CLIENT_ATTEMPT_SECONDS=10 # the 0.4.1 client gives up on one submit/status attempt after this long, then retries
SLOW_REQUEST_SECONDS=5
# 0.4.4: the peer closed the connection. SSLEOFError ('EOF occurred in violation of protocol') is an
# OSError, not a ConnectionError, so 0.4.3 logged it as http.unavailable and tried to answer 503 on the
# dead socket. It is now logged as http.client_gone with where it happened and how long the handler took.
PEER_GONE=(ConnectionError,ssl.SSLEOFError,ssl.SSLZeroReturnError)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass # No prompts, responses or credentials in access logs.
    def send(self,status,value,retry_after=None):
        data=json.dumps(value).encode()
        self.phase='responding';self.status_sent=status
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('X-Inference-Service-ID',self.server.service_id)
        if retry_after is not None:self.send_header('Retry-After',str(int(retry_after)))
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);self.wfile.flush()
    def do_GET(self):self.handle_request(False)
    def do_POST(self):self.handle_request(True)
    def _route(self):
        return self.path.split('?',1)[0][:80]
    def _route_key(self):
        """0.4.6: the route without identifiers, for per-route counters (GET /v1/requests/{id}, POST .../cancel)."""
        path=self._route();parts=path.strip('/').split('/')
        if len(parts)>=3 and parts[:2]==['v1','requests']:path='/v1/requests/{id}'+('/cancel' if len(parts)==4 else '')
        elif path not in KNOWN_ROUTES:path='other'
        return ('POST ' if self.command=='POST' else 'GET ')+path
    def _timing(self):
        return {'elapsed_ms':round((time.monotonic()-self.started)*1000),'ledger_wait_ms':round(ledger_wait_seconds()*1000)}
    def handle_request(self,post):
        self.started=time.monotonic();self.phase='reading';self.status_sent=None;ledger_wait_seconds(reset=True)
        gateway=self.server.gateway
        try:
            self._handle(post)
        except PEER_GONE as exc:
            # The client left. Expected when the handler outlasted the client's attempt timeout (it retries
            # with the same request identity); otherwise an independent transport failure. Both are logged.
            timing=self._timing()
            kind='client_attempt_timeout' if timing['elapsed_ms']>=CLIENT_ATTEMPT_SECONDS*1000*.9 else 'transport'
            gateway.count('client_disconnected');gateway.count('client_gone_'+kind)
            gateway.emit('http.client_gone',route=self._route(),phase=self.phase,status=self.status_sent,likely=kind,
                         error=(type(exc).__name__+': '+str(exc))[:120],**timing)
            self.close_connection=True;return
        timing=self._timing()
        try:gateway.note_route(self._route_key(),timing['elapsed_ms']/1000,self.status_sent) # 0.4.6
        except Exception:pass
        if timing['elapsed_ms']>=SLOW_REQUEST_SECONDS*1000:
            gateway.count('slow_requests');gateway.emit('http.slow',route=self._route(),status=self.status_sent,**timing)
    def _handle(self,post):
        try:
            gateway=self.server.gateway
            if self.path=='/health' and not post:return self.send(200,{'ok':True,'contract':CONTRACT,'service_id':self.server.service_id,
                                                                     'release':RELEASE,'features':FEATURES})
            if self.path=='/ready' and not post:
                with gateway.broker.connect() as db:
                    opened=db.execute("SELECT value FROM capacity_service_policy WHERE key='admission_open'").fetchone()
                ready=bool(opened and opened['value']=='1')
                return self.send(200 if ready else 503,{'ready':ready,'contract':CONTRACT,'service_id':self.server.service_id})
            if post and getattr(self.server,'stopping',False):
                # Pre-stop: new admissions are refused; status and cancel keep working.
                if self.path in {'/v1/inference','/v1/submit'}:return self.send(503,{'error':'Shared inference is stopping; retry with the same client_key later'},retry_after=5)
            app=self.headers.get('X-Application-ID','');token=self.headers.get('X-Infrastructure-Token','')
            policy=gateway.broker.authenticate(app,token)
            if self.path=='/v1/models' and not post:
                return self.send(200,gateway.models(app,token))
            if self.path=='/v1/usage' and not post:return self.send(200,gateway.usage(app,token))
            if self.path=='/v1/policy' and not post:return self.send(200,gateway.policy(app,token))
            route=urllib.parse.urlsplit(self.path)
            if route.path=='/v1/policy' and not post:
                # 0.4.6: ?availability=0 is the operator policy alone (no head or qualification read).
                query=urllib.parse.parse_qs(route.query,strict_parsing=True)
                if set(query)-{'availability'} or any(len(v)!=1 for v in query.values()) or query.get('availability',['1'])[0] not in ('0','1'):
                    raise ValueError('Policy takes only availability=0 or 1')
                return self.send(200,gateway.policy(app,token,include_availability=query.get('availability',['1'])[0]=='1'))
            if route.path=='/v1/fleet' and not post:
                # 0.4.6: the application-scoped snapshot; every parameter optional, lists comma-separated.
                query=urllib.parse.parse_qs(route.query,strict_parsing=bool(route.query))
                if set(query)-{'models','operations','num_ctx','capabilities'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Fleet takes optional models, operations, num_ctx and capabilities, once each')
                items=lambda name:[x for x in query.get(name,[''])[0].split(',') if x]
                num_ctx=query.get('num_ctx',[None])[0]
                return self.send(200,gateway.fleet(app,token,models=items('models') or None,operations=items('operations') or None,
                    num_ctx=int(num_ctx) if num_ctx is not None else None,capabilities=items('capabilities')))
            if route.path=='/v1/availability' and not post:
                query=urllib.parse.parse_qs(route.query,strict_parsing=bool(route.query))
                if set(query)-{'model','operation','num_ctx','capabilities'} or any(len(v)!=1 for v in query.values()) or 'model' not in query:
                    raise ValueError('Availability needs one model and optional operation, num_ctx, capabilities')
                num_ctx=query.get('num_ctx',[None])[0]
                return self.send(200,gateway.availability(app,token,query['model'][0],query.get('operation',['/api/generate'])[0],
                    num_ctx=int(num_ctx) if num_ctx is not None else None,
                    capabilities=[c for c in query.get('capabilities',[''])[0].split(',') if c]))
            if self.path in {'/v1/inference','/v1/submit'} and post:
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=1024*1024:raise ValueError('Bounded JSON body required')
                self.phase='reading_body';body=json.loads(self.rfile.read(length));self.phase='handling'
                if self.path=='/v1/submit':
                    return self.send(202,gateway.execute(app,token,body,asynchronous=True))
                result=gateway.execute(app,token,body)
                return self.send(202 if result['status']=='active' else 200,result)
            parts=self.path.strip('/').split('/')
            if len(parts)>=3 and parts[:2]==['v1','requests']:
                rid=parts[2]
                if not post and len(parts)==3:return self.send(200,gateway.result(app,token,rid))
                if post and len(parts)==4 and parts[3]=='cancel':
                    return self.send(200,gateway.cancel(app,token,rid))
            self.send(404,{'error':'Unknown shared-inference route'})
        except PEER_GONE:raise # handle_request logs it with timing; nothing can be answered
        except PermissionError as exc:self.send(403,{'error':str(exc)})
        except GatewayOverload as exc:self.send(503,{'error':str(exc),'retry_after_seconds':exc.retry_after},retry_after=exc.retry_after)
        except LedgerBusy as exc:
            # 0.4.4: the ledger write lock was not free within the request budget (5 s, below the client's
            # 10 s attempt). Nothing was written for this attempt; the client retries with the same identity.
            self.server.gateway.count('ledger_busy_503')
            self.send(503,{'error':'Shared inference ledger busy; retry with the same request identity','retry_after_seconds':exc.retry_after},retry_after=exc.retry_after)
        except (ValueError,KeyError,TypeError) as exc:self.send(400,{'error':str(exc)})
        except TimeoutError as exc:self.send(504,{'error':str(exc)})
        except Exception as exc:
            # Exception type and message only (never a body); the message of a ledger error is the useful part.
            self.server.gateway.count('unavailable_503')
            self.server.gateway.emit('http.unavailable',route=self._route(),error=(type(exc).__name__+': '+str(exc))[:200],phase=self.phase,**self._timing())
            self.send(503,{'error':'Shared inference unavailable; retry status with the same request identity'},retry_after=RETRY_AFTER_SECONDS)

def main():
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,required=True);p.add_argument('--service-id',default='shared-inference')
    p.add_argument('--max-connections',type=int,default=256);p.add_argument('--max-pending',type=int,default=512)
    p.add_argument('--certificate');p.add_argument('--key')
    p.add_argument('--upstream-ca');p.add_argument('--upstream-certificate');p.add_argument('--upstream-key');a=p.parse_args()
    if a.host not in {'127.0.0.1','::1','localhost'} and not(a.certificate and a.key):p.error('Non-loopback service requires operator-provisioned TLS certificate/key')
    if a.max_connections<1 or a.max_pending<1:p.error('Positive connection and pending budgets required')
    from .capacity_lifecycle import service_owner
    with service_owner(a.database):serve(a)

def serve(a):
    from .capacity_lifecycle import claim_authority,resume_after_service_stop
    gateway=Gateway(a.database,max_pending=a.max_pending,upstream_ca=a.upstream_ca,upstream_certificate=a.upstream_certificate,upstream_key=a.upstream_key)
    claim_authority(gateway.broker,a.service_id);resume_after_service_stop(gateway.broker)
    gateway.recover_service_startup()
    server=Server((a.host,a.port),gateway,a.service_id,a.max_connections,tls=server_tls(a.certificate,a.key) if a.certificate else None)
    # Public listener metadata only; also supports an OS-assigned development port.
    print(json.dumps({'event':'service.listening','host':a.host,'port':server.server_port,
        'protocol':'https' if a.certificate else 'http','service_id':a.service_id}),flush=True)
    stopped=threading.Event()
    def monitor():
        while not stopped.is_set():
            try:gateway.refresh_health()
            except Exception:pass # stale freshness independently stops admission
            try:gateway.run_recovery()
            except Exception:pass # suspensions stay in place until a later cycle succeeds
            try:gateway.publish_counters(max_connections=a.max_connections,max_pending=a.max_pending)
            except Exception:pass
            stopped.wait(15)
    thread=threading.Thread(target=monitor,daemon=True,name='enrolled-resource-health');thread.start()
    try:server.serve_forever()
    finally:stopped.set();server.server_close()

if __name__=='__main__':main()
