import io,json,time,urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from capstone_rag import gateway_transport,experiments
from capstone_rag.webapp.routers import lab


def _queue_full():
    return urllib.error.HTTPError('https://gateway/v1/submit',400,'Bad Request',
        {'Retry-After':'0.01'},io.BytesIO(b'{"error":"Application queue capacity exhausted"}'))


def test_gateway_queue_capacity_is_retried_with_same_client_key(monkeypatch):
    class Gateway:
        def __init__(self):self.keys=[]
        def availability(self,*args,**kwargs):return {'status':'available'}
        def infer(self,*args,**kwargs):
            self.keys.append(kwargs['client_key'])
            if len(self.keys)<3:raise _queue_full()
            return {'status':'completed','result':{'resource_id':'head-3','response':{}}}
    gateway=Gateway();monkeypatch.setattr(gateway_transport,'client',lambda workload:gateway)
    value=gateway_transport.invoke(None,{'id':'gateway','url':'https://gateway','resource_id':'@authorized'},
        'qwen3.5:2b','/api/generate',{'prompt':'fixed'},2,request_id='CALL-fixed',workload='research')
    assert gateway.keys==['CALL-fixed']*3
    assert value['client_diagnostics']['backpressure_retries']==2


def test_gateway_queue_capacity_has_distinct_admission_deadline(monkeypatch):
    class Gateway:
        def availability(self,*args,**kwargs):return {'status':'available'}
        def infer(self,*args,**kwargs):raise _queue_full()
    monkeypatch.setattr(gateway_transport,'client',lambda workload:Gateway())
    with pytest.raises(gateway_transport.GatewayAdmissionDeadline,match='waited too long.*admission deadline'):
        gateway_transport.invoke(None,{'id':'gateway','url':'https://gateway','resource_id':'@authorized'},
            'qwen3.5:2b','/api/generate',{'prompt':'fixed'},.05,request_id='CALL-fixed',workload='research')


def test_stair_step_plan_reaches_1024_and_has_stop_rules():
    steps=lab._capacity_steps({'stair_step':True,'stair_maximum':1024,'step_calls':2,
        'step_basis':'calls','stair_stop_rule':'diminishing_returns'})
    assert len(steps)==1024 and steps[0]['parallel']==1 and steps[-1]['parallel']==1024
    assert steps[-1]['calls']>=1024
    with pytest.raises(ValueError,match='stop rule'):
        lab._capacity_steps({'stair_step':True,'stair_stop_rule':'guess'})


def test_diminishing_returns_stops_after_two_sub_ten_percent_steps():
    manager=object.__new__(experiments.Manager);saved=[]
    manager.save=lambda value:saved.append(dict(value)) or value
    throughput=iter([100,105,109])
    manager._capacity_step_summary=lambda exp,step:{**step,'calls_failed':0,
        'total_tokens_per_second':next(throughput),'calls_per_second':1}
    exp={'id':'EXP-test','capacity_steps':[]}
    for number in (1,2):
        exp,stop=manager._finish_capacity_step(exp,{'step':number,'parallel':number,'stop_rule':'diminishing_returns'})
        assert not stop
    exp,stop=manager._finish_capacity_step(exp,{'step':3,'parallel':3,'stop_rule':'diminishing_returns'})
    assert stop and exp['diminishing_returns_at']==3
    manager._capacity_step_summary=lambda exp,step:{**step,'calls_failed':1,
        'total_tokens_per_second':100,'calls_per_second':1}
    failed,stop=manager._finish_capacity_step({'id':'EXP-failure','capacity_steps':[]},
        {'step':1,'parallel':1,'stop_rule':'first_failure'})
    assert stop and failed['termination_reason']=='stair_step_first_failure'


def test_lab_state_uses_one_inventory_call_for_ten_rapid_loads(monkeypatch,tmp_path):
    calls={'models':0,'availability':0}
    resources=[{'resource_id':f'head-{number}','status':'healthy','models':[],'model_details':{}} for number in range(2,11)]
    for index in range(207):
        name=f'model-{index}:2b';resource=resources[index%9];resource['models'].append(name)
        resource['model_details'][name]={'qualified_operations':['completion']}
    class Gateway:
        def models(self):calls['models']+=1;time.sleep(.05);return {'resources':resources}
        def policy(self):return {'application':'capstone-research'}
        def availability(self,*args,**kwargs):calls['availability']+=1;raise AssertionError('state must not fan out availability calls')
    monkeypatch.setattr(gateway_transport,'enabled',lambda:True)
    monkeypatch.setattr(gateway_transport,'client',lambda workload:Gateway())
    monkeypatch.setattr(lab,'_inventory_path',lambda:tmp_path/'missing.json')
    lab._cache.update(at=0,value=None,building=False,inflight=False,error=None,generation=0)
    started=time.monotonic()
    with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(lambda _:lab.state(True),range(10)))
    assert time.monotonic()-started<1
    for _ in range(50):
        value=lab.state(False)
        if value['inventory_status']=='ready':break
        time.sleep(.02)
    assert value['inventory_status']=='ready' and len(value['models'])==207
    assert calls=={'models':1,'availability':0}


def test_ui_exposes_1024_deadline_and_retry():
    from lab_js import lab_js_text,compact
    text=lab_js_text();flat=compact(text)
    assert "numberInput(selection.concurrency,1,1024," in flat and "max:'3600'" in flat
    assert 'Retry' in text and 'AbortController' in text
