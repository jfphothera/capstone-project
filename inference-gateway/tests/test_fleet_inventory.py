"""Fleet evolution: observed inventory is authoritative; no rebuild, no assumed profile."""
import json
import time
import pytest
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_observe import snapshot


class Fleet:
    """Simulated heads: installed models per endpoint, runtime version, reachability."""
    def __init__(self):
        self.installed={'http://127.0.0.1:12001':{'small:1':'S1'},'http://127.0.0.1:12002':{'embed:1':'E1'}}
        self.version={'version':'0.12.3'};self.offline=set();self.calls=0
    def __call__(self,endpoint,operation,payload,timeout):
        if endpoint in self.offline:raise ConnectionError('simulated head offline')
        models=self.installed[endpoint]
        if operation=='/api/version':return self.version
        if operation=='/api/tags':return {'models':[{'name':n,'digest':d} for n,d in models.items()]}
        if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
        if operation=='/api/show':return {'capabilities':['embedding'] if payload['model'].startswith('embed') else ['completion']}
        self.calls+=1
        if operation=='/api/embed':return {'model':payload['model'],'embeddings':[[.1,.2]]}
        return {'done':True,'model':payload['model'],'response':'READY'}


@pytest.fixture
def fleet(tmp_path):
    heads=Fleet();gateway=Gateway(tmp_path/'ledger.db',transport=heads)
    gateway.broker.register_client('capstone','t-capstone',models=['small:1','large:1','embed:1'])
    # Installation profiles (CPU8, GPU, embedding) are deliberately not recorded: only observation counts.
    for rid,endpoint in (('head-192-0-2-3','http://127.0.0.1:12001'),('head-192-0-2-6','http://127.0.0.1:12002')):
        gateway.broker.authorize_resource(rid,endpoint=endpoint,resource_type='shared_inference',owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
    assert gateway.qualify('head-192-0-2-3','small:1',operator='ops')['passed']
    assert gateway.qualify('head-192-0-2-6','embed:1',operator='ops',purpose='embedding')['passed']
    return gateway,heads


def events(gateway,after=0):
    with gateway.broker.connect() as db:
        return [(r['event_type'],json.loads(r['payload']).get('model')) for r in db.execute('SELECT * FROM capacity_events WHERE event_id>? ORDER BY event_id',(after,))]


def last_event(gateway):
    with gateway.broker.connect() as db:return db.execute('SELECT coalesce(max(event_id),0) FROM capacity_events').fetchone()[0]


def listed(gateway):
    return {r['resource_id']:r['models'] for r in gateway.models('capstone','t-capstone')['resources']}


def test_added_model_needs_qualification_then_appears_without_rebuild(fleet):
    gateway,heads=fleet;mark=last_event(gateway)
    heads.installed['http://127.0.0.1:12001']['large:1']='L1';gateway.refresh_health()
    assert ('inventory.model_added','large:1') in events(gateway,mark)
    assert gateway.availability('capstone','t-capstone','large:1')['resources']['head-192-0-2-3']=='not_qualified'
    assert listed(gateway)=={'head-192-0-2-3':['small:1'],'head-192-0-2-6':['embed:1']}
    assert gateway.qualify('head-192-0-2-3','large:1',operator='ops')['passed']
    assert listed(gateway)['head-192-0-2-3']==['small:1','large:1']


def test_removed_model_and_digest_and_runtime_change_are_reconciled(fleet):
    gateway,heads=fleet;mark=last_event(gateway)
    heads.installed['http://127.0.0.1:12001']={'small:1':'S2'};gateway.refresh_health()
    assert ('inventory.digest_changed','small:1') in events(gateway,mark) and ('qualification.invalidated','small:1') in events(gateway,mark)
    assert 'head-192-0-2-3' not in listed(gateway)
    assert gateway.availability('capstone','t-capstone','small:1')['resources']['head-192-0-2-3']=='digest_changed'
    mark=last_event(gateway);heads.installed['http://127.0.0.1:12002']={};gateway.refresh_health()
    assert ('inventory.model_removed','embed:1') in events(gateway,mark)
    assert gateway.availability('capstone','t-capstone','embed:1','/api/embed')['resources']['head-192-0-2-6']=='not_installed'
    mark=last_event(gateway);heads.installed['http://127.0.0.1:12002']={'embed:1':'E1'};heads.version={'version':'0.13.0'};gateway.refresh_health()
    assert ('inventory.runtime_changed',None) in events(gateway,mark)
    assert gateway.availability('capstone','t-capstone','embed:1','/api/embed')['resources']['head-192-0-2-6']=='runtime_changed'
    assert listed(gateway)=={}
    # Never silently substitute: a pinned digest refuses the changed build before inference.
    heads.version={'version':'0.12.3'};heads.installed['http://127.0.0.1:12001']={'small:1':'S1'}
    assert gateway.qualify('head-192-0-2-3','small:1',operator='ops')['passed']
    heads.installed['http://127.0.0.1:12001']={'small:1':'S9'}
    result=gateway.execute('capstone','t-capstone',{'contract':'shared.inference/1','client_key':'pinned','model':'small:1','nodes':['head-192-0-2-3'],
        'deadline':time.time()+10,'operation':'/api/generate','payload':{'prompt':'x'},'expected_digest':'S1'})
    assert result['status']=='failed' and 'expected_digest' in result['error'] and heads.calls==3 # three qualifications, no inference


def test_offline_online_and_stale_inventory(fleet):
    gateway,heads=fleet;mark=last_event(gateway)
    heads.offline.add('http://127.0.0.1:12001');gateway.refresh_health()
    assert ('resource.health_changed',None) in events(gateway,mark) and 'head-192-0-2-3' not in listed(gateway)
    assert gateway.availability('capstone','t-capstone','small:1')['resources']['head-192-0-2-3']=='resource_unhealthy'
    heads.offline.clear();gateway.refresh_health()
    assert listed(gateway)['head-192-0-2-3']==['small:1'] # transient outage keeps qualification
    with gateway.broker.connect() as db:db.execute('UPDATE capacity_inventory SET observed_at=?',(time.time()-600,))
    assert gateway.availability('capstone','t-capstone','small:1')['resources']['head-192-0-2-3']=='inventory_stale'


def test_policy_withdrawal_and_unknown_resources_are_not_usable(fleet):
    gateway,heads=fleet
    gateway.broker.register_client('capstone','t-capstone',models=['embed:1'])
    assert gateway.availability('capstone','t-capstone','small:1')['status']=='not_permitted'
    with pytest.raises(PermissionError):
        gateway.execute('capstone','t-capstone',{'contract':'shared.inference/1','client_key':'withdrawn','model':'small:1','nodes':['@authorized'],
            'deadline':time.time()+10,'operation':'/api/generate','payload':{'prompt':'x'}})
    with pytest.raises(PermissionError):gateway.broker.publish('head-192-0-2-99',['small:1'],'healthy')


def test_observation_reports_registered_installed_qualified_and_executing_separately(fleet):
    gateway,heads=fleet;heads.installed['http://127.0.0.1:12001']['large:1']='L1';gateway.refresh_health()
    request=gateway.broker.submit('capstone','t-capstone','lease','small:1',['head-192-0-2-3'],time.time()+60)
    gateway.broker.admit('capstone','t-capstone',request['request_id'])
    document=snapshot(gateway.broker.path,limit=1000)
    inventory={i['resource_id']:i for i in document['inventory']}
    assert [m['name'] for m in inventory['head-192-0-2-3']['models']]==['large:1','small:1'] and inventory['head-192-0-2-3']['runtime_version']=='0.12.3'
    resources={r['resource_id']:r for r in document['resources']}
    assert resources['head-192-0-2-3']['qualified_models']==['small:1'] and [l['resource_id'] for l in document['leases']]==['head-192-0-2-3']
    assert '127.0.0.1' not in json.dumps(document)
    assert {'inventory.observed','inventory.model_added','qualification.completed'}<={e['event_type'] for e in document['events']}
