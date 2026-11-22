"""Operator scripts on temporary folders only: no real task, service, firewall or ledger is touched.

The current user stands in for the gateway account and LOCAL SERVICE for the
Arena console account. Effective access from the real Arena account still needs
physical verification on the admin hub.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import pytest
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.observation_publisher import accept_ledger

ROOT=Path(__file__).resolve().parents[1]
SHELL=shutil.which('pwsh') or shutil.which('powershell')
pytestmark=pytest.mark.skipif(os.name!='nt' or not SHELL,reason='Windows ACL and Task Scheduler scripts need Windows PowerShell')
GATEWAY=os.environ.get('USERDOMAIN','.')+'\\'+os.environ.get('USERNAME','')
ARENA='NT AUTHORITY\\LOCAL SERVICE'
LOCAL_SERVICE='S-1-5-19'
TASK='{http://schemas.microsoft.com/windows/2004/02/mit/task}'


def script(name,*args):
    return subprocess.run([SHELL,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts'/name),*args],
        capture_output=True,text=True,timeout=120)


def acl(path):
    command=("(Get-Acl -LiteralPath '%s').Access | ForEach-Object { [ordered]@{ sid=$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;"
             " rights=\"$($_.FileSystemRights)\"; inheritance=\"$($_.InheritanceFlags)\"; propagation=\"$($_.PropagationFlags)\"; inherited=$_.IsInherited } } | ConvertTo-Json -AsArray")%path
    result=subprocess.run([SHELL,'-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def folders(tmp_path):
    paths={name:tmp_path/name for name in ('state','publish','publisher','logs')}
    paths['state'].mkdir()
    paths['database']=paths['state']/'capacity-ledger.db'
    Gateway(paths['database']).broker.register_client('capstone','PRIVATE-TOKEN',models=['fixture:1'])
    return paths


def apply(folders,*extra):
    return script('Set-ObservationPublicationAcl.ps1','-PublishDirectory',str(folders['publish']),'-StateDirectory',str(folders['publisher']),
        '-LogDirectory',str(folders['logs']),'-LedgerDirectory',str(folders['state']),'-GatewayAccount',GATEWAY,'-ArenaAccount',ARENA,*extra)


def test_acl_plan_changes_nothing(folders):
    result=apply(folders,'-PlanOnly')
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads(result.stdout)
    assert report['applied'] is False and report['ok'] and not folders['publish'].exists()
    grants=[item['grants'] for item in report['plan']]
    assert '*%s:(OI)(IO)R'%LOCAL_SERVICE in grants[0] and not any(LOCAL_SERVICE in g for g in grants[1]+grants[2])


def test_acl_gives_arena_read_on_published_files_only(folders):
    result=apply(folders)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads(result.stdout);gateway_sid=report['gateway_sid']
    entries=acl(folders['publish'])
    assert not any(e['inherited'] for e in entries)
    assert {e['sid'] for e in entries}=={'S-1-5-18','S-1-5-32-544',gateway_sid,LOCAL_SERVICE}
    arena=[e for e in entries if e['sid']==LOCAL_SERVICE]
    # Inherit-only, files only: no folder listing, no write, no subfolders.
    assert len(arena)==1 and arena[0]['inheritance']=='ObjectInherit' and arena[0]['propagation']=='InheritOnly'
    assert 'Write' not in arena[0]['rights'] and 'Modify' not in arena[0]['rights'] and 'Read' in arena[0]['rights']
    gateway=[e for e in entries if e['sid']==gateway_sid];assert 'Modify' in gateway[0]['rights']
    for name in ('publisher','logs'):assert LOCAL_SERVICE not in {e['sid'] for e in acl(folders[name])}
    # A publication written afterwards inherits Arena's read ACE; the ledger does not.
    accept_ledger(folders['database'],folders['publisher']/'publisher-state.json',operator='test-operator')
    from inference_gateway.observation_publisher import publish_once
    output=folders['publish']/'shared-inference-observation.json'
    publish_once(folders['database'],output,folders['publisher']/'publisher-state.json',service_id='shared-inference')
    published=[e for e in acl(output) if e['sid']==LOCAL_SERVICE]
    assert len(published)==1 and published[0]['inherited'] and 'Read' in published[0]['rights']
    assert LOCAL_SERVICE not in {e['sid'] for e in acl(folders['database'])}
    assert LOCAL_SERVICE not in {e['sid'] for e in acl(folders['publisher']/'publisher-state.json')}


def test_acl_reports_a_ledger_readable_by_arena(folders):
    granted=subprocess.run(['icacls',str(folders['state']),'/grant','*%s:(OI)(CI)R'%LOCAL_SERVICE],capture_output=True,text=True)
    assert granted.returncode==0
    result=apply(folders)
    assert result.returncode==1
    findings=json.loads(result.stdout)['findings']
    assert findings and all(f.startswith('ledger:') for f in findings)


def test_acl_refuses_an_arena_account_that_is_an_administrator(folders):
    # Any local user that is a member of Administrators (often the built-in Administrator) stands in; read-only, -PlanOnly.
    # WinNT provider, as in the script: Get-LocalGroupMember fails (1789) when a member cannot be resolved.
    member=subprocess.run([SHELL,'-NoProfile','-Command',"$g=[ADSI]('WinNT://'+$env:COMPUTERNAME+'/'+((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')).Translate([Security.Principal.NTAccount]).Value -split '\\\\')[-1]+',group');"
        "foreach($m in @($g.psbase.Invoke('Members'))){ if($m.GetType().InvokeMember('Class','GetProperty',$null,$m,$null) -eq 'User'){ $s=New-Object Security.Principal.SecurityIdentifier([byte[]]$m.GetType().InvokeMember('objectSid','GetProperty',$null,$m,$null),0); try{ $s.Translate([Security.Principal.NTAccount]).Value; break }catch{} } }"],
        capture_output=True,text=True,timeout=60).stdout.strip().splitlines()
    member=member[0] if member else ''
    if not member:pytest.skip('No local user account is a member of Administrators on this machine')
    # The effective rule: an account that can elevate is refused even when its process runs filtered.
    result=script('Set-ObservationPublicationAcl.ps1','-PublishDirectory',str(folders['publish']),'-StateDirectory',str(folders['publisher']),
        '-LogDirectory',str(folders['logs']),'-LedgerDirectory',str(folders['state']),'-GatewayAccount','NT AUTHORITY\\NETWORK SERVICE','-ArenaAccount',member,'-PlanOnly')
    report=json.loads(result.stdout)
    assert result.returncode==1 and any('member of Administrators' in f for f in report['findings'])
    assert report['privileged_membership']=={'Administrators':'checked (transitive local membership)','Backup Operators':'checked (transitive local membership)'}


def test_acl_refuses_a_shared_publication_folder(folders):
    result=script('Set-ObservationPublicationAcl.ps1','-PublishDirectory',str(folders['state']),'-StateDirectory',str(folders['publisher']),
        '-LogDirectory',str(folders['logs']),'-LedgerDirectory',str(folders['state']),'-GatewayAccount',GATEWAY,'-ArenaAccount',ARENA,'-PlanOnly')
    assert result.returncode!=0 and 'dedicated' in result.stdout+result.stderr


def plan_task(folders,**overrides):
    values={'Python':sys.executable,'Database':str(folders['database']),'PublishFile':str(folders['publish']/'shared-inference-observation.json'),
        'StateFile':str(folders['publisher']/'publisher-state.json'),'LogFile':str(folders['logs']/'observation-publisher.log'),
        'GatewayAccount':GATEWAY,**overrides}
    return script('Register-ObservationPublisherTask.ps1',*[part for key,value in values.items() for part in ('-'+key,value)],'-PlanOnly')


def test_task_plan_defines_a_supervised_five_second_publisher(folders):
    result=plan_task(folders)
    assert result.returncode==0,result.stderr
    task=ET.fromstring(result.stdout.strip().split('\n',1)[1])
    find=lambda path:task.find('/'.join(TASK+p for p in path.split('/')))
    assert find('Triggers/BootTrigger/Repetition/Interval').text=='PT1M'
    assert find('Settings/MultipleInstancesPolicy').text=='IgnoreNew'
    assert find('Settings/ExecutionTimeLimit').text=='PT0S'
    assert find('Principals/Principal/UserId').text==GATEWAY and find('Principals/Principal/RunLevel').text=='LeastPrivilege'
    assert find('Actions/Exec/Command').text==sys.executable
    arguments=find('Actions/Exec/Arguments').text
    assert arguments.startswith('-I -m inference_gateway.observation_publisher run ') and '--interval 5' in arguments
    query=subprocess.run([SHELL,'-NoProfile','-Command',"if (Get-ScheduledTask -TaskName 'InferenceGateway-ObservationPublisher' -ErrorAction SilentlyContinue) { 'registered' }"],
        capture_output=True,text=True,timeout=60)
    assert 'registered' not in query.stdout
    # The supervisor's exact command line publishes once the ledger is accepted.
    for name in ('publish','publisher','logs'):folders[name].mkdir()
    accept_ledger(folders['database'],folders['publisher']/'publisher-state.json',operator='test-operator')
    process=subprocess.Popen('"%s" %s'%(sys.executable,arguments),cwd=folders['publisher'])
    try:
        output=folders['publish']/'shared-inference-observation.json';deadline=time.time()+20
        while time.time()<deadline and not output.exists():time.sleep(.1)
        assert json.loads(output.read_text())['service_id']=='shared-inference'
    finally:process.terminate();process.wait(10)
    assert 'observation.published' in (folders['logs']/'observation-publisher.log').read_text()


def test_task_plan_refuses_publication_beside_the_ledger(folders):
    result=plan_task(folders,PublishFile=str(folders['state']/'observation.json'))
    assert result.returncode!=0 and 'dedicated' in result.stdout+result.stderr
