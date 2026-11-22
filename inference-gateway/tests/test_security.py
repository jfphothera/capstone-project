"""Security negatives: real TLS with freshly issued lab certificates, credentials, authorization, bypass."""
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
import pytest
from shared_inference_client import Client
from inference_gateway import credentials
from inference_gateway.capacity_bootstrap import apply
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_gateway_server import Server,server_tls

ROOT=Path(__file__).resolve().parents[1]
SHELL=shutil.which('pwsh')
ME=os.environ.get('USERDOMAIN','.')+'\\'+os.environ.get('USERNAME','')


def transport(endpoint,operation,payload,timeout):
    if operation=='/api/version':return {'version':'0.12.3'}
    if operation=='/api/tags':return {'models':[{'name':'m:1','digest':'D1'}]}
    if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
    if operation=='/api/show':return {'capabilities':['completion']}
    return {'done':True,'model':'m:1','response':'READY'}


def ledger(tmp_path):
    gateway=Gateway(tmp_path/'ledger.db',transport=transport)
    gateway.broker.register_client('capstone','t-capstone-'+'x'*32,models=['m:1'],queue_limit=1)
    gateway.broker.register_client('coder','t-coder-'+'x'*32,models=['m:1'])
    gateway.broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',owner='infrastructure',
        applications=['capstone','coder'],permitted_uses=['inference'])
    assert gateway.qualify('head-192-0-2-3','m:1',operator='ops')['passed']
    return gateway


def serve(gateway,tls=None):
    server=Server(('127.0.0.1',0),gateway,tls=tls);threading.Thread(target=server.serve_forever,daemon=True).start()
    return server


@pytest.fixture
def https(tmp_path,certificates):
    gateway=ledger(tmp_path);good=certificates['good']
    server=serve(gateway,server_tls(good['server_certificate'],good['server_key']))
    yield gateway,'https://127.0.0.1:%d'%server.server_port,good['ca_file_for_clients']
    server.shutdown();server.server_close()


def test_sdk_verifies_the_gateway_certificate(https):
    gateway,url,ca=https
    assert Client(url,'capstone','t-capstone-'+'x'*32,ca_file=ca).models()['resources'][0]['resource_id']=='head-192-0-2-3'
    with pytest.raises(urllib.error.URLError) as untrusted:Client(url,'capstone','t-capstone-'+'x'*32).models()
    assert isinstance(untrusted.value.reason,ssl.SSLCertVerificationError)


def test_certificate_without_the_origin_san_is_refused(tmp_path,certificates):
    wrong=certificates['wrong-san'];server=serve(ledger(tmp_path),server_tls(wrong['server_certificate'],wrong['server_key']))
    try:
        with pytest.raises(urllib.error.URLError) as mismatch:
            Client('https://127.0.0.1:%d'%server.server_port,'capstone','t-capstone-'+'x'*32,ca_file=wrong['ca_file_for_clients']).models()
        assert 'IP address mismatch' in str(mismatch.value.reason) or 'match' in str(mismatch.value.reason)
    finally:server.shutdown();server.server_close()


def test_silent_and_plaintext_clients_cannot_block_or_downgrade(https):
    gateway,url,ca=https;port=int(url.rsplit(':',1)[1])
    silent=[socket.create_connection(('127.0.0.1',port)) for _ in range(3)] # never send a ClientHello
    try:
        began=time.monotonic()
        assert Client(url,'capstone','t-capstone-'+'x'*32,ca_file=ca).models()['contract']=='shared.inference/1'
        assert time.monotonic()-began<3
        with pytest.raises((urllib.error.URLError,ConnectionError,http.client.HTTPException)):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:%d/health'%port,timeout=5).read()
        legacy=ssl.create_default_context(cafile=ca);legacy.minimum_version=ssl.TLSVersion.MINIMUM_SUPPORTED
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',DeprecationWarning) # deliberately requesting an obsolete protocol
            legacy.maximum_version=ssl.TLSVersion.TLSv1_1
        with pytest.raises((ssl.SSLError,OSError)):
            with socket.create_connection(('127.0.0.1',port),timeout=5) as raw:legacy.wrap_socket(raw,server_hostname='127.0.0.1')
    finally:
        for connection in silent:connection.close()


def test_credentials_authorization_and_budget_negatives(https):
    gateway,url,ca=https;token='t-capstone-'+'x'*32
    good=Client(url,'capstone',token,ca_file=ca)
    def status(client,path,body=None):
        with pytest.raises(urllib.error.HTTPError) as error:client.request(path,body)
        return error.value.code
    assert status(Client(url,'capstone','',ca_file=ca),'/v1/models')==403 # missing credential
    assert status(Client(url,'capstone','wrong',ca_file=ca),'/v1/models')==403
    assert status(Client(url,'coder',token,ca_file=ca),'/v1/models')==403 # another application's token
    assert status(Client(url,'unknown-app',token,ca_file=ca),'/v1/models')==403
    with pytest.raises(PermissionError):Client(url,'capstone',token,service_id='other-service',ca_file=ca).models()
    body=lambda **kw:{'contract':'shared.inference/1','client_key':kw.pop('key','k'),'model':'m:1','nodes':['head-192-0-2-3'],'deadline':time.time()+30,
                      'operation':'/api/generate','payload':{'prompt':'x'},**kw}
    assert status(good,'/v1/submit',body(model='unlisted:1'))==403
    assert status(good,'/v1/submit',body(operation='/api/pull'))==403
    assert status(good,'/v1/submit',body(operation='/api/delete'))==403
    assert status(good,'/v1/submit',body(payload={'prompt':'x','options':{'num_predict':9000}}))==400
    assert status(good,'/v1/submit',body(payload={'prompt':'x','keep_alive':-1}))==400
    assert status(good,'/v1/submit',body(deadline=time.time()+7200))==403
    assert status(good,'/v1/submit',body(priority=10))==400
    assert status(good,'/v1/submit',body(required_capabilities=['root']))==400
    gateway.broker.revoke('capstone')
    assert status(good,'/v1/models')==403 and status(good,'/v1/submit',body(key='after'))==403


def test_client_caused_refusals_never_take_a_head_out_of_service(https):
    gateway,url,ca=https;client=Client(url,'coder','t-coder-'+'x'*32,ca_file=ca)
    for key,payload,extra in (('ctx',{'prompt':'x','options':{'num_ctx':4096}},{}),('pin',{'prompt':'x'},{'expected_digest':'OTHER'}),
                              ('cap',{'prompt':'x'},{'required_capabilities':['tools']})):
        result=client.infer('m:1',['head-192-0-2-3'],'/api/generate',payload,client_key=key,timeout=10,**extra)
        assert result['status']=='failed' and 'execution_started_at' not in result
        with gateway.broker.connect() as db:assert db.execute("SELECT health FROM capacity_resources").fetchone()[0]=='healthy',key


def test_direct_transport_fallback_is_impossible(tmp_path):
    gateway=Gateway(tmp_path/'ledger.db')
    for endpoint in ('http://8.8.8.8:11434','http://ollama.lab:11434','http://169.254.1.1:11434','http://user:pw@192.0.2.3:11434','http://192.0.2.3:11434/api'):
        with pytest.raises(ValueError):gateway.upstream(endpoint,'/api/version',None,1)
    with pytest.raises(ValueError):Client('http://192.0.2.3:11434','capstone','t') # raw head over plaintext
    class Redirect(http.server.BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):self.send_response(302);self.send_header('Location','http://192.0.2.3:11434/api/tags');self.end_headers()
    redirector=http.server.HTTPServer(('127.0.0.1',0),Redirect);threading.Thread(target=redirector.serve_forever,daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:Client('http://127.0.0.1:%d'%redirector.server_port,'capstone','t').models()
        assert refused.value.code==302 # never followed to the raw head
    finally:redirector.shutdown();redirector.server_close()


def test_credential_issue_rotate_revoke_and_list(tmp_path,monkeypatch,capsys):
    database=tmp_path/'ledger.db';Gateway(database)
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1',
        'clients':[{'app_id':'capstone','credential':'provisioned','models':['m:1'],'priority':6,'max_context':4096},
                   {'app_id':'coder','credential':'provisioned','models':['m:1'],'operations':['/api/chat']}],'resources':[]}))
    out=tmp_path/'tokens';out.mkdir()
    assert credentials.main(['list','--database',str(tmp_path/'missing.db')])==2 and not (tmp_path/'missing.db').exists()
    assert credentials.main(['issue','--database',str(database),'--policy',str(policy),'--app-id','capstone','--output',str(out/'capstone.token')])==0
    printed=capsys.readouterr().out;token=(out/'capstone.token').read_text().strip()
    assert len(token)>=43 and token not in printed
    broker=Gateway(database).broker;assert broker.authenticate('capstone',token)['priority']==6
    with broker.connect() as db:assert token not in json.dumps([dict(r) for r in db.execute('SELECT * FROM capacity_clients')])
    if os.name=='nt':
        acl=subprocess.run(['icacls',str(out/'capstone.token')],capture_output=True,text=True).stdout
        assert 'BUILTIN\\Users' not in acl and 'Everyone' not in acl and 'Authenticated Users' not in acl
    assert credentials.main(['issue','--database',str(database),'--policy',str(policy),'--app-id','capstone','--output',str(out/'again.token')])==1
    assert not (out/'again.token').exists()
    assert credentials.main(['issue','--database',str(database),'--policy',str(policy),'--app-id','capstone','--output',str(out/'capstone.token')])==1
    # Bootstrap updates policy for provisioned clients without touching the token.
    assert credentials.main(['issue','--database',str(database),'--policy',str(policy),'--app-id','coder','--output',str(out/'coder.token')])==0
    apply(policy,database);assert broker.authenticate('capstone',token)
    assert broker.client_limits('coder')=={'operations':['/api/chat'],'max_context':None}
    assert credentials.main(['rotate','--database',str(database),'--app-id','capstone','--output',str(out/'capstone-2.token')])==0
    with pytest.raises(PermissionError):broker.authenticate('capstone',token)
    rotated=(out/'capstone-2.token').read_text().strip();assert broker.authenticate('capstone',rotated)
    assert credentials.main(['revoke','--database',str(database),'--app-id','capstone'])==0
    with pytest.raises(PermissionError):broker.authenticate('capstone',rotated)
    apply(policy,database) # applying policy never re-enables a revoked provisioned client
    with pytest.raises(PermissionError):broker.authenticate('capstone',rotated)
    capsys.readouterr()
    assert credentials.main(['list','--database',str(database)])==0
    listing=json.loads(capsys.readouterr().out)
    assert [(a['app_id'],a['enabled']) for a in listing['applications']]==[('capstone',False),('coder',True)] and 'token_hash' not in json.dumps(listing)
    assert credentials.main(['rotate','--database',str(database),'--app-id','capstone','--output',str(out/'capstone-3.token'),'--enable'])==0
    assert broker.authenticate('capstone',(out/'capstone-3.token').read_text().strip())
