"""Shared fixtures. Certificates are issued by the project's own script into temporary folders only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
SHELL=shutil.which('pwsh')
ME=os.environ.get('USERDOMAIN','.')+'\\'+os.environ.get('USERNAME','')


@pytest.fixture(scope='session')
def certificates(tmp_path_factory):
    """One lab CA; 'good' covers 127.0.0.1 and 192.0.2.254, 'wrong-san' only 192.0.2.254."""
    if os.name!='nt' or not SHELL:pytest.skip('Certificate issuance uses PowerShell 7 and .NET on Windows')
    root=tmp_path_factory.mktemp('pki');issued={}
    for name,ips in (('good','127.0.0.1,192.0.2.254'),('wrong-san','192.0.2.254')):
        result=subprocess.run([SHELL,'-NoProfile','-NonInteractive','-File',str(ROOT/'scripts'/'New-GatewayCertificate.ps1'),'-CaDirectory',str(root/'ca'),
            '-OutputDirectory',str(root/name),'-IpAddress',ips,'-NewCa','-KeyReaderAccount',ME,'-AdministratorAccount',ME],capture_output=True,text=True,timeout=120)
        assert result.returncode==0,result.stdout+result.stderr
        issued[name]=json.loads(result.stdout)
    assert issued['good']['ca_created'] and not issued['wrong-san']['ca_created'] # one CA, two server certificates
    return issued
