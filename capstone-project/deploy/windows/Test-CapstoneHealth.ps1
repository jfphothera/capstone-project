param([string]$InstallRoot='C:\Program Files\Capstone',[string]$DataRoot='C:\ProgramData\Capstone',[int]$TimeoutSeconds=10)
$ErrorActionPreference='Stop'
$settings=Get-Content -Raw -LiteralPath (Join-Path $DataRoot 'config\appsettings.json')|ConvertFrom-Json
$result=[ordered]@{application=$false;gateway_authenticated=$false;gateway_tcp=$false;arena_tcp=$null;external_steps=@()}
try{$health=Invoke-RestMethod -TimeoutSec $TimeoutSeconds -Uri "http://$($settings.listen_host):$($settings.listen_port)/api/health";$result.application=[bool]$health.ok;$result.version=$health.version}catch{$result.application_error=$_.Exception.Message}
$gateway=Get-Content -Raw -LiteralPath $settings.gateway_client_config|ConvertFrom-Json
$gatewayUri=[uri]$gateway.url
$gatewayPort=if($gatewayUri.Port -gt 0){$gatewayUri.Port}elseif($gatewayUri.Scheme -eq 'https'){443}else{80}
$result.gateway_tcp=[bool](Test-NetConnection -ComputerName $gatewayUri.Host -Port $gatewayPort -InformationLevel Quiet)
try{
 $gatewayOutput=& (Join-Path $InstallRoot 'tools\Test-CapstoneGateway.ps1') -InstallRoot $InstallRoot -DataRoot $DataRoot 2>&1
 if($LASTEXITCODE -eq 0){$result.gateway_authenticated=$true;$result.gateway_verification=($gatewayOutput -join "`n"|ConvertFrom-Json)}
 else{$result.gateway_verification=[ordered]@{verified=$false;status='offline_or_unverified'}}
}catch{$result.gateway_verification=[ordered]@{verified=$false;status='offline_or_unverified';error_class=$_.Exception.GetType().Name}}
if($settings.arena_adapter.registration_status -ne 'complete'){$result.external_steps+= 'Agent Arena owner must supply its verified adapter endpoint/port, install/register the existing Ticketing adapter, and provision its machine credential; no Arena API or port is assumed locally.'}
if(!(Test-Path -LiteralPath $settings.developer_token_file)){$result.external_steps+='Authorized administrator must provision the protected developer token file.'}
if(!(Test-Path -LiteralPath $gateway.ca_file)){$result.external_steps+='Gateway trust material is not provisioned at the configured path.'}
$researchGateway=Get-Content -Raw -LiteralPath $settings.research_gateway_client_config|ConvertFrom-Json
foreach($required in @($gateway.token_file,$researchGateway.token_file,$gateway.ca_file,$researchGateway.ca_file)){if(!(Test-Path -LiteralPath $required)){$result.external_steps+="Required Gateway client file is not provisioned: $required"}}
$result|ConvertTo-Json -Depth 5
if(!$result.application -or !$result.gateway_authenticated){exit 1}
