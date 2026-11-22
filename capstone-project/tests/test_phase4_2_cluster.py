from datetime import datetime, timezone

import pytest

from capstone_rag.cluster import ClusterEnrollment
from capstone_rag.store import Store


def fixture():
    return {'address': '192.0.2.11', 'endpoint': 'http://192.0.2.11:11434',
            'actual_execution': True, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'inventory': [{'name': 'tiny:1', 'digest': 'abc'}], 'model': 'tiny:1',
            'digest': 'abc', 'metadata': {'tiny:1': {'capabilities': ['completion']}},
            'generation_completed': True, 'correct': True,
            'response': {'done': True, 'model': 'tiny:1', 'response': 'READY'}}


def test_inventory_alone_does_not_qualify(tmp_path):
    st = Store(str(tmp_path/'cluster.db'))
    cluster = ClusterEnrollment(st, '192.0.2.0/24')
    cluster.authorize_resource('192.0.2.11',resource_type='shared_inference',owner='test infrastructure',applications=['capstone'],permitted_uses=['inference'])
    value = fixture(); value['generation_completed'] = False
    assert cluster.ingest(value)['available_models'] == []
    value = fixture(); value['correct'] = False
    assert cluster.ingest(value)['available_models'] == []
    assert cluster.ingest(fixture())['available_models'] == ['tiny:1']
    assert len(st.query('SELECT * FROM node_qualification_evidence')) == 3


def test_digest_and_metadata_and_provenance_required(tmp_path):
    st = Store(str(tmp_path/'cluster.db')); cluster = ClusterEnrollment(st, '192.0.2.0/24')
    cluster.authorize_resource('192.0.2.11',resource_type='shared_inference',owner='test infrastructure',applications=['capstone'],permitted_uses=['inference'])
    value = fixture(); value['digest'] = 'changed'
    assert cluster.ingest(value)['health_state'] != 'healthy'
    value = fixture(); value['metadata']['tiny:1']['capabilities'] = ['embedding']
    assert cluster.ingest(value)['available_models'] == []
    value = fixture(); value['actual_execution'] = False
    with pytest.raises(ValueError): cluster.ingest(value)
    value = fixture(); value['address'] = '192.168.1.1'
    with pytest.raises(ValueError): cluster.ingest(value)


def test_disabled_node_cannot_be_revived_by_refresh(tmp_path):
    st = Store(str(tmp_path/'cluster.db')); cluster = ClusterEnrollment(st, '192.0.2.0/24')
    cluster.authorize_resource('192.0.2.11',resource_type='shared_inference',owner='test infrastructure',applications=['capstone'],permitted_uses=['inference'])
    node = cluster.ingest(fixture())
    cluster.set_state(node['node_id'], 'disabled')
    assert cluster.ingest(fixture())['operational_state'] == 'disabled'
    assert cluster.workers.get(node['worker_id'])['available_models'] == []


def test_stale_capacity_expires(tmp_path):
    st = Store(str(tmp_path/'cluster.db')); cluster = ClusterEnrollment(st, '192.0.2.0/24')
    cluster.authorize_resource('192.0.2.11',resource_type='shared_inference',owner='test infrastructure',applications=['capstone'],permitted_uses=['inference'])
    node = cluster.ingest(fixture())
    cluster.freshness_seconds = -1
    cluster.expire()
    assert cluster.workers.get(node['worker_id'])['health_state'] == 'unavailable'


def test_multiple_qualifications_survive_inventory_refresh(tmp_path):
    st=Store(str(tmp_path/'cluster.db'));cluster=ClusterEnrollment(st,'192.0.2.0/24')
    cluster.authorize_resource('192.0.2.11',resource_type='shared_inference',owner='test infrastructure',applications=['capstone'],permitted_uses=['inference'])
    first=fixture();first['inventory'].append({'name':'other:1','digest':'def'})
    cluster.ingest(first)
    second=fixture();second.update(model='other:1',digest='def',inventory=first['inventory'],
        metadata={'other:1':{'capabilities':['completion']}},response={'done':True,'model':'other:1','response':'READY'})
    node=cluster.ingest(second)
    assert set(node['available_models'])=={'tiny:1','other:1'}
    second['inventory'][0]['digest']='changed'
    node=cluster.ingest(second)
    assert node['available_models']==['other:1']
