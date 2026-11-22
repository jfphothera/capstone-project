"""Opt-in: real Ollama replies through the Gateway's real urllib transport (no fakes).

Skipped unless INFERENCE_GATEWAY_LIVE_OLLAMA names an Ollama origin, e.g.
  INFERENCE_GATEWAY_LIVE_OLLAMA=http://127.0.0.1:11434 INFERENCE_GATEWAY_LIVE_MODEL=qwen3:4b
Loads the model and runs a few short generations. This project ran it against a local
Ollama only; running it against a lab head (acceptance row 20) is Agent Arena's
step, from 192.0.2.254, under a separately authorized change, on an idle head.
"""
import os
import threading
import time
import pytest
from inference_gateway import recovery
from inference_gateway.capacity_gateway import Gateway

ORIGIN=os.environ.get('INFERENCE_GATEWAY_LIVE_OLLAMA')
MODEL=os.environ.get('INFERENCE_GATEWAY_LIVE_MODEL','qwen3:4b')
pytestmark=pytest.mark.skipif(not ORIGIN,reason='opt-in: set INFERENCE_GATEWAY_LIVE_OLLAMA to an Ollama origin to run')


def classify(gateway,operation,payload,timeout=60,origin=None):
    try:gateway.upstream(origin or ORIGIN,operation,payload,timeout)
    except Exception as exc:return recovery.classify(exc,'dispatch'),recovery.http_detail(exc)
    return None,'answered'


def test_real_ollama_error_replies_map_to_the_documented_classes(tmp_path):
    gateway=Gateway(tmp_path/'ledger.db')
    version=gateway.upstream(ORIGIN,'/api/version',None,5)['version'];print('ollama',version)
    assert classify(gateway,'/api/generate',{'model':'no-such-model:1','prompt':'x','stream':False})==(recovery.REQUEST_FAULT,'HTTP 404')
    assert classify(gateway,'/api/chat',{'model':MODEL,'messages':'not-a-list','stream':False})==(recovery.REQUEST_FAULT,'HTTP 400')
    # A wrong option type is answered 500 by Ollama, not 400: the Gateway refuses it before dispatch.
    assert classify(gateway,'/api/generate',{'model':MODEL,'prompt':'x','stream':False,'options':{'num_ctx':'abc'}})==(recovery.MODEL_ON_HEAD,'HTTP 500')
    with pytest.raises(ValueError,match='Context allocation'):
        Gateway.validate({'contract':'shared.inference/1','client_key':'k','model':MODEL,'nodes':['@authorized'],'deadline':time.time()+60,
                          'operation':'/api/generate','payload':{'prompt':'x','options':{'num_ctx':'abc'}}})
    assert classify(gateway,'/api/generate',{'model':MODEL,'prompt':'x','stream':False},origin='http://127.0.0.1:9')[0]==recovery.HEAD_UNREACHABLE
    # Sent, then no answer within the timeout: uncertain (raw TimeoutError from the reply read, not URLError).
    assert classify(gateway,'/api/generate',{'model':MODEL,'prompt':'Count to one hundred in words.','stream':False,'think':False,
                                             'keep_alive':'20s','options':{'num_predict':300,'num_ctx':1024}},timeout=1.5)[0]==recovery.UNCERTAIN


def test_real_ollama_busy_runner_stays_listed_and_only_an_empty_head_is_safe_to_probe(tmp_path):
    gateway=Gateway(tmp_path/'ledger.db');resource={'endpoint':ORIGIN};policy={'ps_settle_seconds':2}
    ps=lambda:gateway.upstream(ORIGIN,'/api/ps',None,5)['models']
    gateway.upstream(ORIGIN,'/api/generate',{'model':MODEL,'prompt':'x','stream':False,'think':False,'keep_alive':'2s','options':{'num_predict':1,'num_ctx':1024}},120)
    done=threading.Event()
    def long_call():
        try:gateway.upstream(ORIGIN,'/api/generate',{'model':MODEL,'prompt':'Count slowly from one to two hundred in words.',
                             'stream':False,'think':False,'keep_alive':'2s','options':{'num_predict':200,'num_ctx':1024}},180)
        finally:done.set()
    worker=threading.Thread(target=long_call,daemon=True);worker.start()
    time.sleep(6) # well past the previous expires_at (keep_alive 2 s)
    assert not done.is_set() and [m['name'] for m in ps()]==[MODEL] # a busy runner is never unloaded
    assert gateway._safe_to_probe(resource,policy)=='runner_loaded'
    worker.join(180);time.sleep(5) # idle: unloaded when its keep_alive ends
    assert ps()==[] and gateway._safe_to_probe(resource,policy)=='safe'
