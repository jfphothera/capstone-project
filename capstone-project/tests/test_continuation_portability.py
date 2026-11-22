from datetime import datetime,timezone
from capstone_rag.cluster import ClusterEnrollment
from capstone_rag.store import Store


def evidence(address,endpoint):
    return {'address':address,'endpoint':endpoint,'actual_execution':True,'observed_at':datetime.now(timezone.utc).isoformat(),
        'inventory':[{'name':'test:1','digest':'one'}],'model':'test:1','digest':'one','metadata':{'test:1':{'capabilities':['completion']}},
        'generation_completed':True,'correct':True,'version':{'version':'test'},'response':{'model':'test:1','done':True,'response':'READY'}}


def test_authorized_node_move_preserves_identity_but_requires_new_qualification(tmp_path):
    st=Store(str(tmp_path/'nodes.db'));old=ClusterEnrollment(st,'192.168.20.0/24')
    policy={'resource_type':'shared_inference','owner':'infrastructure','applications':['capstone'],'permitted_uses':['inference'],'node_id':'stable-node'}
    old.authorize_resource('192.168.20.7',endpoint='http://192.168.20.7:12000',**policy)
    first=old.ingest(evidence('192.168.20.7','http://192.168.20.7:12000'))
    new=ClusterEnrollment(st,'192.168.40.0/24')
    new.authorize_resource('192.168.40.8',endpoint='http://192.168.40.8:13000',**policy)
    worker=new.workers.get(first['worker_id'])
    assert worker['node_id']=='stable-node' and worker['available_models']==[]
    assert not new.permitted('192.168.20.7')
    qualified=new.ingest(evidence('192.168.40.8','http://192.168.40.8:13000'))
    assert qualified['worker_id']==first['worker_id'] and qualified['available_models']==['test:1']
    assert len(st.query('SELECT * FROM node_qualification_evidence'))==2
    assert st.one('SELECT location_version FROM node_locations')['location_version']==2
