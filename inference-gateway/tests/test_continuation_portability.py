import json,time
from datetime import datetime,timezone
import pytest
from inference_gateway.capacity_broker import CapacityBroker






def test_shared_resource_move_cannot_interrupt_lease(tmp_path):
    broker=CapacityBroker(tmp_path/'capacity.db')
    policy={'resource_type':'shared_inference','owner':'infrastructure','applications':['test-app'],'permitted_uses':['inference']}
    broker.register_client('test-app','test-token',models=['test:1'])
    broker.authorize_resource('stable-node',endpoint='http://192.168.20.7:12000',**policy);broker.publish('stable-node',['test:1'],'healthy')
    request=broker.submit('test-app','test-token','one','test:1',['stable-node'],time.time()+60)
    broker.admit('test-app','test-token',request['request_id'])
    with pytest.raises(ValueError,match='allocated'):broker.authorize_resource('stable-node',endpoint='http://192.168.40.8:13000',**policy)
    broker.finish('test-app','test-token',request['request_id'])
    broker.authorize_resource('stable-node',endpoint='http://192.168.40.8:13000',**policy)
    with broker.connect() as db:row=db.execute('SELECT * FROM capacity_resources').fetchone()
    assert row['resource_id']=='stable-node' and row['health']=='unqualified' and json.loads(row['qualified_models'])==[]
