import json
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


def path_for(name):
    candidates=(ROOT/'deploy'/'windows'/name,ROOT.parents[1]/'tools'/name,ROOT.parents[2]/'tools'/name)
    return next((path for path in candidates if path.is_file()),None)


def text(name):
    return path_for(name).read_text(encoding='utf-8')


def test_windows_release_exposes_dual_identity_configuration_and_verification():
    configure=text('Configure-Capstone.ps1')
    start=text('Start-Capstone.ps1')
    assert 'OperationalGatewayBootstrap' in configure and 'ResearchGatewayBootstrap' in configure
    assert "'capstone'" in configure and "'capstone-research'" in configure
    assert 'https://192.0.2.254:8443' in configure
    assert 'token_environment' in configure and 'must not embed or name an environment credential' in configure
    assert 'CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG' in start
    build=path_for('Build-CapstoneRelease.ps1')
    assert ('Test-CapstoneGateway.ps1' in build.read_text(encoding='utf-8')) if build else path_for('Test-CapstoneGateway.ps1').is_file()


def test_local_service_gets_narrow_file_acl_and_secrets_are_not_bundled():
    configure=text('Configure-Capstone.ps1')
    install=text('Install-Capstone.ps1')
    assert "'*S-1-5-19:(R)'" in configure
    assert 'inheritance:r' in configure
    assert 'DeveloperTokenFile)){Protect-GatewayFile $path $true}' in configure
    assert "icacls.exe $DataRoot /grant '*S-1-5-19:(OI)(CI)M'" not in install
    assert "@('data','logs','backups','models','temp')" in install


def test_updates_preserve_testing_lab_state_and_candidate_history():
    install=text('Install-Capstone.ps1')
    update=text('Update-Capstone.ps1')
    assert "if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'data\\operations.db')))" in install
    assert 'ai-configuration-before' in update
    assert 'operations-before' in update
    assert "'experiments'" not in update.lower() or 'Remove-Item' not in update


def test_samples_are_non_secret_gateway_03_client_configs():
    operational=json.loads((ROOT/'config'/'inference-gateway.client.template.json').read_text(encoding='utf-8'))
    research=json.loads((ROOT/'config'/'inference-gateway.research-client.template.json').read_text(encoding='utf-8'))
    for value,identity in ((operational,'capstone'),(research,'capstone-research')):
        assert value['url']=='https://192.0.2.254:8443'
        assert value['application_id']==identity
        assert value['service_id']=='shared-inference'
        assert 'token_file' in value and 'ca_file' in value
        assert 'token' not in value and 'token_environment' not in value
    assert operational['token_file']!=research['token_file']
