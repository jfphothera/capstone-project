"""Release package and installer verbs, end to end against temporary roots.

Real: wheel build, package hashes, pip --target with the base interpreter, release
launcher validation, ledger creation/backup, publication. Simulated: the SCM, local
accounts, elevation and icacls with synthetic SIDs (they need an elevated session on
a real hub; see docs/ACCEPTANCE_PLAN.md).
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from inference_gateway import deployment
from inference_gateway.deployment import Installer,Refused

V=deployment.release_version() # the release under test; update/rollback below relabel it to exercise the mechanics

ROOT=Path(__file__).resolve().parents[1]
BASE_PYTHON=str(Path(sys.base_prefix)/('python.exe' if os.name=='nt' else 'bin/python3'))
SERVICE_SID,ARENA_SID,ADMIN_SID='S-1-5-21-10-20-30-1001','S-1-5-21-10-20-30-1002','S-1-5-21-10-20-30-500'


def load_builder():
    spec=importlib.util.spec_from_file_location('build_release',ROOT/'scripts'/'build_release.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@pytest.fixture(scope='module')
def package(tmp_path_factory):
    result=load_builder().build(tmp_path_factory.mktemp('release'),require_all=False,write_back=False)
    return Path(result['package']),result


class FakeSystem:
    def __init__(self):
        self.accounts={'.\\svc-inference-gw':SERVICE_SID,'.\\arena':ARENA_SID,'.\\admin':ADMIN_SID}
        self.privileged={ADMIN_SID};self.services={};self.acls={};self.secrets_seen=[]
    def run(self,command,**kwargs):return subprocess.run(command,capture_output=True,text=True,**kwargs)
    def is_elevated(self):return True
    def is_machine_wide(self,path):return True # test interpreter stands in for C:\Program Files\Python312
    def resolve_sid(self,account):return self.accounts.get(account)
    def privileged_member(self,sid):return sid in self.privileged
    def icacls(self,path,grants):Path(path).mkdir(parents=True,exist_ok=True);self.acls[str(path)]=list(grants)
    def service_state(self,name):return self.services[name]['state'] if name in self.services else None
    def create_service(self,name,command,account,password):
        self.secrets_seen.append(password);self.services[name]={'command':command,'account':account,'state':'stopped'}
    def set_command(self,name,command,account=None,password=None):self.services[name]['command']=command
    def start_service(self,name):self.services[name]['state']='running'
    def stop_service(self,name):self.services[name]['state']='stopped'
    def delete_service(self,name):del self.services[name]


@pytest.fixture
def site(tmp_path,package,certificates):
    settings={'schema':deployment.SETTINGS_SCHEMA,'python':BASE_PYTHON,'install_root':str(tmp_path/'Program Files'/'InferenceGateway'),
              'data_root':str(tmp_path/'ProgramData'/'InferenceGateway'),'service_name':'InferenceGateway','service_account':'.\\svc-inference-gw',
              'arena_accounts':['.\\arena'],'listen':{'host':'192.0.2.254','port':8443},'drain_seconds':5}
    path=tmp_path/'deployment-settings.json';path.write_text(json.dumps(settings))
    system=FakeSystem();installer=Installer(settings,system,package=package[0])
    tls=installer.paths['tls'];tls.mkdir(parents=True)
    good=certificates['good'] # stands in for the certificate verb (tested in test_security)
    for name,source in (('server.pem','server_certificate'),('server.key','server_key'),('inference-gateway-ca.pem','ca_file_for_clients')):shutil.copyfile(good[source],tls/name)
    return {'settings':settings,'settings_file':path,'system':system,'installer':installer,'tmp':tmp_path}


def healthy(url,ca):return {'ok':True,'/health':200,'/ready':200}
def unhealthy(url,ca):return {'ok':False,'/health':'ConnectionRefusedError','/ready':'ConnectionRefusedError'}


def test_package_is_complete_hashed_and_secret_free(package):
    root,result=package
    assert deployment.verify_package(root)==len([p for p in root.rglob('*') if p.is_file()])-1 # every file except SHA256SUMS itself
    manifest=json.loads((root/'deployment-manifest.json').read_text())
    assert manifest['schema']=='inference-gateway.windows-deployment/1' and manifest['release']==result['release']
    assert manifest['files']['wheel/'+deployment.wheel_in(root).name]==deployment.hashlib.sha256(deployment.wheel_in(root).read_bytes()).hexdigest()
    assert manifest['listener']=={**manifest['listener'],'bind':'192.0.2.254','port':8443,'allowed_clients':['192.0.2.201','192.0.2.202']}
    assert not [p for p in root.rglob('*') if p.suffix in {'.db','.key','.token','.pem','.pfx'}]
    assert Path(result['zip_sha256'] and result['zip']).is_file() and Path(result['zip']+'.sha256').read_text().split()[0]==result['zip_sha256']
    copy=root.parent/'tampered';shutil.copytree(root,copy)
    (copy/'bin'/'gateway-deploy.py').write_text('print("changed")')
    with pytest.raises(Refused,match='gateway-deploy.py'):deployment.verify_package(copy)


def test_package_entry_plan_changes_nothing_and_reports_json(package,tmp_path):
    root,_=package
    settings={'schema':deployment.SETTINGS_SCHEMA,'python':sys.executable,'install_root':str(tmp_path/'i'),'data_root':str(tmp_path/'d'),
              'service_account':'.\\no-such-account-gw','arena_accounts':['.\\no-such-arena'],'listen':{'host':'192.0.2.254','port':8443}}
    path=tmp_path/'settings.json';path.write_text(json.dumps(settings))
    run=lambda *a:subprocess.run([BASE_PYTHON,'-I',str(root/'bin'/'gateway-deploy.py'),*a],capture_output=True,text=True,timeout=300)
    planned=run('plan','--settings',str(path))
    report=json.loads(planned.stdout)
    assert planned.returncode==1 and report['changes_made'] is False and report['checks']['package']['ok']
    assert {'python','service_account','arena_accounts'}<=set(report['blocking']) # a venv interpreter and unknown accounts are refused
    assert not (tmp_path/'i').exists() and not (tmp_path/'d').exists()
    path.write_text('{"schema":"wrong"}')
    bad=run('plan','--settings',str(path));assert bad.returncode==2 and json.loads(bad.stdout)['ok'] is False


def test_install_verify_update_rollback_repair_uninstall(site,monkeypatch):
    installer,system=site['installer'],site['system'];paths=installer.paths
    plan=installer.plan()
    assert plan['ok'] and plan['checks']['python']['base_interpreter'] and not plan['checks']['ledger']['ok']
    with pytest.raises(Refused,match='ledger'):installer.install('pw-1')
    result=installer.install('operator-typed-secret',new_ledger=True)
    assert result['ok'] and result['started'] and 'operator-typed-secret' not in json.dumps(result)
    service=system.services['InferenceGateway']
    assert service['account']=='.\\svc-inference-gw' and '"%s" -I '%BASE_PYTHON in service['command'] and ('releases\\'+V) in service['command'].replace('/','\\')
    assert '*%s:(OI)(CI)M'%SERVICE_SID in system.acls[str(paths['state'])] and system.acls[str(paths['ca'])]==['*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F']
    assert ARENA_SID not in json.dumps(system.acls) # Arena's read ACE comes only from the publication ACL script
    config=json.loads(paths['config_file'].read_text())
    assert config['listen']=={'host':'192.0.2.254','port':8443} and config['observation']['output'].endswith('shared-inference-observation.json')
    with pytest.raises(Refused,match='service'):installer.install('again',new_ledger=True) # one registration only
    # Verify reports exactly what is missing: the publisher has not published (no accepted ledger).
    report=installer.verify(probe=healthy)
    assert report['failed']==['publisher'] and report['checks']['endpoints']['/ready']==200
    from inference_gateway.observation_publisher import accept_ledger,publish_once
    accept_ledger(paths['ledger'],paths['publisher_state'],operator='ops')
    publish_once(paths['ledger'],paths['published'],paths['publisher_state'],service_id='shared-inference')
    assert installer.verify(probe=healthy)['ok']
    # Update to a newer release (same wheel, labelled 0.4.8 to exercise the mechanics).
    monkeypatch.setattr(installer,'version','0.4.8')
    updated=installer.update(probe=healthy)
    assert updated['from']==V and updated['to']=='0.4.8' and updated['ledger']['kept'] and updated['held_at_upgrade']==[] and Path(updated['backup']['source']).name=='capacity-ledger.db'
    assert 'releases\\0.4.8' in service['command'].replace('/','\\') and (paths['releases']/V).is_dir()
    rolled=installer.rollback()
    assert rolled['to']==V and ('releases\\'+V) in service['command'].replace('/','\\')
    # A release that does not become healthy is undone automatically.
    monkeypatch.setattr(installer,'version','0.4.7') # a label no real release uses (0.4.3 is now the package itself)
    with pytest.raises(Refused,match='returned to '+V):installer.update(probe=unhealthy)
    assert ('releases\\'+V) in service['command'].replace('/','\\') and json.loads(paths['state_file'].read_text())['current']==V
    # Repair restores damaged release files and keeps every piece of data.
    monkeypatch.setattr(installer,'version',V)
    damaged=paths['releases']/V/'inference_gateway'/'capacity_broker.py';damaged.write_text('broken')
    before={p:p.read_bytes() for p in (paths['config_file'],paths['ledger'])}
    assert installer.repair()['ok'] and 'CapacityBroker' in damaged.read_text()
    assert all(p.read_bytes()==data for p,data in before.items())
    # Uninstall removes the registration only.
    gone=installer.uninstall()
    assert gone['service_removed'] and 'InferenceGateway' not in system.services
    assert paths['ledger'].is_file() and paths['config_file'].is_file() and (paths['releases']/V).is_dir()
    # One verified backup before each switch: update, rollback and the failed update.
    assert len(list(paths['backups'].glob('*.db')))==3 and all(Path(str(b)+'.json').is_file() for b in paths['backups'].glob('*.db'))
    assert system.secrets_seen==['operator-typed-secret']


def test_install_refuses_unsafe_identities_and_missing_tls(site):
    installer,system=site['installer'],site['system']
    for account,arena,expected in (('.\\admin',['.\\arena'],'service_account'),('.\\svc-inference-gw',['.\\svc-inference-gw'],'arena_accounts'),
                                   ('.\\svc-inference-gw',['.\\admin'],'arena_accounts')):
        installer.settings.update(service_account=account,arena_accounts=arena)
        assert expected in installer.plan()['blocking']
        with pytest.raises(Refused,match=expected):installer.install('pw',new_ledger=True)
    installer.settings.update(service_account='.\\svc-inference-gw',arena_accounts=['.\\arena'])
    installer.paths['key'].unlink()
    with pytest.raises(Refused,match='TLS'):installer.install('pw',new_ledger=True)
    assert not system.services and not installer.paths['ledger'].exists()


def test_cli_verbs_print_one_json_object_with_contract_exit_codes(site,monkeypatch,capsys):
    monkeypatch.setattr(deployment,'package_root',lambda:None)
    assert deployment.main(['plan','--settings',str(site['settings_file'])],system=site['system'])==0
    assert json.loads(capsys.readouterr().out)['verb']=='plan'
    monkeypatch.setattr(sys,'stdin',open(os.devnull))
    assert deployment.main(['install','--settings',str(site['settings_file']),'--new-ledger'],system=site['system'])==2 # no password
    assert 'password' in json.loads(capsys.readouterr().out)['error']
    assert deployment.main(['rollback','--settings',str(site['settings_file'])],system=site['system'])==1
    assert json.loads(capsys.readouterr().out)['ok'] is False
    assert deployment.main(['nonsense'],system=site['system'])==2


def test_rollback_to_0_3_waits_until_no_head_holds_two_reservations(tmp_path):
    """0.3.x recreates its one-lease-per-head index at start; a 2-slot head must be down to one lease first."""
    import time
    from inference_gateway.capacity_broker import CapacityBroker
    settings={'install_root':str(tmp_path/'i'),'data_root':str(tmp_path/'d'),'python':sys.executable,'service_account':'.\\svc','arena_accounts':['.\\a'],
              'listen':{'host':'192.0.2.254','port':8443}}
    installer=Installer(settings,FakeSystem());installer.paths['state'].mkdir(parents=True)
    broker=CapacityBroker(installer.paths['ledger']);broker.register_client('capstone','t',models=['m:1'],max_concurrency=2)
    broker.authorize_resource('head-192-0-2-7',endpoint='http://192.0.2.7:11434',resource_type='shared_inference',owner='infrastructure',
                              applications=['capstone'],permitted_uses=['inference'],slots=2,memory_gb=31.4)
    broker.publish('head-192-0-2-7',['m:1'],'healthy')
    requests=[broker.submit('capstone','t','k%d'%i,'m:1',['head-192-0-2-7'],time.time()+600) for i in range(2)]
    for r in requests:assert broker.admit('capstone','t',r['request_id'])['status']=='leased'
    with pytest.raises(Refused,match='head-192-0-2-7'):installer._prepare_downgrade(wait_seconds=0)
    with broker.connect() as db:db.execute("UPDATE capacity_requests SET expires=?",(time.time()-1,)) # stopped service: no renewals
    installer._prepare_downgrade(wait_seconds=0)
    import sqlite3
    db=sqlite3.connect(installer.paths['ledger'])
    try:db.execute("CREATE UNIQUE INDEX one_active_resource ON capacity_requests(resource_id) WHERE status IN ('leased','cancel_requested')") # what 0.3.0 does at start
    finally:db.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows SCM')
def test_real_scm_binding_reads_service_state_without_changes():
    system=deployment.WindowsSystem()
    assert not system.is_machine_wide(os.path.join(os.environ.get('SystemDrive','C:')+'\\','Users','someone','AppData','Local','Programs','Python','Python312','python.exe'))
    assert system.is_machine_wide(r'C:\Program Files\Python312\python.exe')
    assert system.service_state('EventLog')=='running' and system.service_state('InferenceGateway-NoSuchService')is None


# ---------------------------------------------------------------- 0.4.1 item C

def _installed(site):
    installer=site['installer'];installer.install('pw',new_ledger=True)
    from inference_gateway.observation_publisher import accept_ledger,publish_once
    p=installer.paths;accept_ledger(p['ledger'],p['publisher_state'],operator='ops')
    publish_once(p['ledger'],p['published'],p['publisher_state'],service_id='shared-inference')
    return installer,site['system'],site['system'].services['InferenceGateway']


def test_update_rolls_back_when_starting_the_new_release_raises(site,monkeypatch):
    """Hub, 2026-11-12: StartServiceW error 1053 skipped the switch-back and left the service stopped on the new release."""
    installer,system,service=_installed(site)
    monkeypatch.setattr(installer,'version','0.4.9')
    real=system.start_service
    def start(name):
        if 'releases\\0.4.9' in service['command'].replace('/','\\'):
            raise OSError(1053,'StartServiceW failed') # the service did not respond to the start request in time
        real(name)
    monkeypatch.setattr(system,'start_service',start)
    with pytest.raises(Refused,match=r'did not become healthy \(OSError: .*1053.*returned to '+V):installer.update(probe=healthy)
    assert ('releases\\'+V) in service['command'].replace('/','\\') and service['state']=='running'
    state=json.loads(installer.paths['state_file'].read_text())
    assert state['current']==V and state['history'][-1]['action']=='update_failed_rolled_back' and '1053' in state['history'][-1]['error']


def test_update_reports_when_the_switch_back_fails_too(site,monkeypatch):
    installer,system,service=_installed(site)
    monkeypatch.setattr(installer,'version','0.4.9')
    def start(name):raise OSError(1053,'StartServiceW failed')
    monkeypatch.setattr(system,'start_service',start)
    with pytest.raises(Refused,match='returning to %s failed.*run repair or rollback'%V):installer.update(probe=healthy)
    state=json.loads(installer.paths['state_file'].read_text())
    assert state['current']==V and state['history'][-1]['action']=='update_failed_rollback_failed'


@pytest.mark.skipif(os.name!='nt',reason='Windows ACLs')
def test_release_folder_acl_is_reset_to_inherit(tmp_path):
    """pip --target (3.12.10) left 43 of 43 hub release files owner-only; the service account could not read them."""
    parent=tmp_path/'releases';release=parent/'0.4.1'/'inference_gateway';release.mkdir(parents=True)
    launcher=release/'service_launcher.py';launcher.write_text('# launcher')
    owner_only=subprocess.run(['icacls',str(launcher),'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F','*S-1-3-4:F'],capture_output=True,text=True)
    assert owner_only.returncode==0
    before=subprocess.run(['icacls',str(launcher)],capture_output=True,text=True).stdout
    assert '(I)' not in before
    system=FakeSystem();Installer({'install_root':str(tmp_path),'data_root':str(tmp_path/'d')},system)._inherit_acl(parent/'0.4.1')
    after=subprocess.run(['icacls',str(launcher)],capture_output=True,text=True).stdout
    aces=[line.strip() for line in after.splitlines() if ':(' in line]
    assert aces and all('(I)' in ace for ace in aces) # every entry now comes from the parent (Users RX on the hub)
