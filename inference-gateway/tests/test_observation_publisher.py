"""Observation publisher for Arena: atomic file, overlapping cursor, reset safety."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import pytest
from inference_gateway.capacity_gateway import Gateway
from inference_gateway import observation_publisher as publisher
from inference_gateway.observation_publisher import PublicationBlocked,accept_ledger,publish_once

ROOT=Path(__file__).resolve().parents[1]
# Arena's published reader patterns (docs/handoff/GATEWAY-OBSERVATION-INTEGRATION.md), copied, not imported.
ARENA_ID=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$')
ARENA_EVENT_TYPE=re.compile(r'^[a-z][a-z0-9_.]{0,63}$')


@pytest.fixture
def layout(tmp_path):
    for name in ('state','publish','publisher'):(tmp_path/name).mkdir()
    database=tmp_path/'state'/'capacity-ledger.db'
    gateway=Gateway(database);broker=gateway.broker
    for app in ('capstone','coder'):broker.register_client(app,'PRIVATE-TOKEN-'+app,models=['fixture:1'])
    broker.authorize_resource('head-192-0-2-3',endpoint='http://192.0.2.3:11434',resource_type='shared_inference',
        owner='private-owner',applications=['capstone','coder'],permitted_uses=['inference'])
    request=broker.submit('capstone','PRIVATE-TOKEN-capstone','PRIVATE-CLIENT-KEY','fixture:1',['head-192-0-2-3'],time.time()+60)
    with broker.connect() as db:
        db.execute('INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,result,error,updated) VALUES(?,?,?,?,?,?,?,?)',
            (request['request_id'],'capstone','PRIVATE-HASH','PRIVATE-OWNER','failed','PRIVATE-RESPONSE','PRIVATE-ERROR',time.time()))
        broker._event(db,'capstone',request['request_id'],'queued',{'prompt':'PRIVATE-PROMPT'})
    return {'gateway':gateway,'database':database,'output':tmp_path/'publish'/'shared-inference-observation.json',
            'state':tmp_path/'publisher'/'publisher-state.json'}


def add_events(gateway,count):
    with gateway.broker.connect() as db:
        for _ in range(count):gateway.broker._event(db,'infrastructure',None,'resource.authorized',{})


def publish(layout,**kwargs):
    return publish_once(layout['database'],layout['output'],layout['state'],service_id='shared-inference',**kwargs)


def accept(layout):
    return accept_ledger(layout['database'],layout['state'],operator='test-operator')


def test_first_publication_requires_operator_acceptance(layout):
    with pytest.raises(PublicationBlocked,match='accept'):publish(layout)
    assert not layout['output'].exists()
    accepted=accept(layout)
    assert accepted['accepted_ledger_id'].startswith('LEDGER-') and accepted['previous_ledger_id'] is None
    publish(layout)
    document=json.loads(layout['output'].read_text())
    assert document['contract']=='shared.inference.observation/1'
    assert document['service_id']=='shared-inference' and document['ledger_id']==accepted['accepted_ledger_id']


def test_published_document_has_no_private_fields_and_is_arena_readable(layout):
    accept(layout);publish(layout)
    encoded=layout['output'].read_text();document=json.loads(encoded)
    assert 'PRIVATE-' not in encoded and 'private-owner' not in encoded and '192.0.2.3' not in encoded
    assert not {'token_hash','result','error','payload_hash','client_key','endpoint','payload','owner','execution_owner'} & set(encoded.split('"'))
    assert isinstance(document['observed_at'],float) and isinstance(document['admission_open'],bool)
    assert isinstance(document['next_event_id'],int)
    for key in ('applications','resources','leases','qualification_observations','events'):assert isinstance(document[key],list)
    assert {a['app_id'] for a in document['applications']}=={'capstone','coder'}
    assert [r['resource_id'] for r in document['resources']]==['head-192-0-2-3']
    assert all(ARENA_ID.match(e['app_id']) and ARENA_EVENT_TYPE.match(e['event_type']) for e in document['events'])
    assert document['last_hour']['window_seconds']==3600 and document['last_hour']['requests_created']=={'queued':1}


def test_publication_never_modifies_the_ledger(layout):
    accept(layout)
    before=hashlib.sha256(layout['database'].read_bytes()).hexdigest()
    publish(layout);publish(layout)
    assert hashlib.sha256(layout['database'].read_bytes()).hexdigest()==before


def test_cursor_overlaps_and_persists_per_ledger(layout):
    accept(layout);add_events(layout['gateway'],150)
    first=publish(layout)
    document=json.loads(layout['output'].read_text())
    assert document['events'][0]['event_id']==1 and first['next_event_id']==document['next_event_id']==153
    add_events(layout['gateway'],5)
    second=publish(layout)
    document=json.loads(layout['output'].read_text())
    # max(0, last_next_event_id - 100): events 54..158, so Arena recovers anything it missed.
    assert [e['event_id'] for e in document['events']]==list(range(54,159)) and second['next_event_id']==158
    state=json.loads(layout['state'].read_text())
    assert state['cursors']=={first['ledger_id']:158}
    # A restarted publisher continues from the persisted cursor.
    third=publish(layout)
    assert third['next_event_id']==158 and json.loads(layout['output'].read_text())['events'][0]['event_id']==59
    with pytest.raises(ValueError,match='advance'):publish(layout,overlap=100,limit=100)


def test_replaced_or_restored_ledger_is_blocked_until_accepted(layout,tmp_path):
    first=accept(layout);publish(layout)
    backup=tmp_path/'backup.db';shutil.copyfile(layout['database'],backup)
    add_events(layout['gateway'],20);publish(layout)
    published=layout['output'].read_bytes()
    # Restored from an older backup: same ledger_id, event history behind the cursor.
    shutil.copyfile(backup,layout['database'])
    with pytest.raises(PublicationBlocked,match='behind'):publish(layout)
    assert publisher.status(layout['database'],layout['state'])['blocked']=='event history behind cursor'
    assert layout['output'].read_bytes()==published
    restored=accept(layout)
    assert restored['accepted_ledger_id']==first['accepted_ledger_id'] and restored['cursor']==3
    publish(layout)
    # Replaced by a fresh ledger: new ledger_id, never published without acceptance.
    layout['database'].unlink();Gateway(layout['database'])
    with pytest.raises(PublicationBlocked,match='not the accepted'):publish(layout)
    replaced=accept(layout)
    assert replaced['previous_ledger_id']==first['accepted_ledger_id']!=replaced['accepted_ledger_id']
    publish(layout)
    state=json.loads(layout['state'].read_text())
    assert set(state['cursors'])=={first['accepted_ledger_id'],replaced['accepted_ledger_id']}
    assert [a['operator'] for a in state['acceptances']]==['test-operator']*3


def test_atomic_replace_never_exposes_a_partial_document(layout):
    accept(layout);publish(layout);add_events(layout['gateway'],400)
    stop=threading.Event();seen=[]
    def read():
        while not stop.is_set():
            try:seen.append(json.loads(layout['output'].read_bytes())['contract'])
            except (PermissionError,FileNotFoundError):pass # Arena keeps the last good document
    reader=threading.Thread(target=read);reader.start()
    try:
        for _ in range(30):publish(layout)
    finally:stop.set();reader.join(5)
    assert seen and set(seen)=={'shared.inference.observation/1'}
    assert sorted(p.name for p in layout['output'].parent.iterdir())==[layout['output'].name]


def test_replace_waits_for_a_reader_holding_the_file_open(layout):
    accept(layout);publish(layout)
    held=layout['output'].open('rb');released=threading.Timer(.3,held.close);released.start()
    try:publisher.replace_atomically(layout['output'],b'{"contract":"replacement"}')
    finally:released.join();held.close()
    assert json.loads(layout['output'].read_text())=={'contract':'replacement'}


def test_failed_write_keeps_previous_publication_and_no_temporary(layout,monkeypatch):
    accept(layout);publish(layout);previous=layout['output'].read_bytes()
    def failing(fd):raise OSError('simulated disk failure')
    monkeypatch.setattr(publisher.os,'fsync',failing)
    with pytest.raises(OSError):publish(layout)
    assert layout['output'].read_bytes()==previous
    assert sorted(p.name for p in layout['output'].parent.iterdir())==[layout['output'].name]


def test_publication_folder_must_be_dedicated(layout,tmp_path):
    with pytest.raises(ValueError,match='dedicated'):
        publisher.check_layout(layout['database'],layout['database'].parent/'observation.json',layout['state'])
    with pytest.raises(ValueError,match='dedicated'):
        publisher.check_layout(layout['database'],layout['state'].parent/'observation.json',layout['state'])
    with pytest.raises(ValueError,match='exist'):
        publisher.check_layout(layout['database'],tmp_path/'missing'/'observation.json',layout['state'])
    publisher.check_layout(layout['database'],layout['output'],layout['state'])


def test_run_loop_publishes_each_interval_and_logs_transitions_only(layout,caplog):
    stale=layout['output'].parent/('.'+layout['output'].name+'.orphan.tmp');stale.write_text('{}')
    os.utime(stale,(time.time()-120,time.time()-120))
    stop=threading.Event();log=logging.getLogger('test-publisher')
    thread=threading.Thread(target=publisher.run,args=(layout['database'],layout['output'],layout['state']),
        kwargs={'service_id':'shared-inference','interval':1,'stop':stop,'log':log})
    with caplog.at_level(logging.WARNING,logger='test-publisher'):
        thread.start()
        try:
            time.sleep(1.5)
            assert not layout['output'].exists()
            accept(layout)
            deadline=time.time()+5
            while time.time()<deadline and not layout['output'].exists():time.sleep(.05)
            first=json.loads(layout['output'].read_text())['observed_at']
            time.sleep(1.5)
            assert json.loads(layout['output'].read_text())['observed_at']>first
        finally:stop.set();thread.join(5)
    events=[json.loads(r.getMessage())['event'] for r in caplog.records]
    assert events==['observation.blocked','observation.published']
    assert not stale.exists()


def test_cli_once_status_and_accept(layout):
    environment={k:v for k,v in os.environ.items() if k!='PYTHONPATH'};environment['PYTHONPATH']=str(ROOT/'src')
    def cli(*args):
        return subprocess.run([sys.executable,'-m','inference_gateway.observation_publisher',*args,'--database',str(layout['database']),
            '--state',str(layout['state'])],env=environment,capture_output=True,text=True,timeout=30)
    once=('run','--once','--output',str(layout['output']))
    blocked=cli(*once);assert blocked.returncode==3 and 'observation.blocked' in blocked.stdout
    assert json.loads(cli('status').stdout)['blocked']=='ledger not accepted'
    accepted=cli('accept-ledger','--operator','test-operator');assert accepted.returncode==0
    published=cli(*once);assert published.returncode==0,published.stderr
    assert json.loads(published.stdout)['ledger_id']==json.loads(accepted.stdout)['accepted_ledger_id']
    assert json.loads(cli('status').stdout)['blocked'] is None
    assert 'PRIVATE-' not in layout['output'].read_text()
