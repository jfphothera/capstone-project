param(
 [Alias('GatewayBootstrap')][Parameter(Mandatory=$true)][string]$OperationalGatewayBootstrap,
 [Parameter(Mandatory=$true)][string]$ResearchGatewayBootstrap,
 [Parameter(Mandatory=$true)][string]$DeveloperTokenFile,
 [string]$DataRoot='C:\ProgramData\Capstone',
 [string]$ListenHost='192.0.2.201',
 [int]$ListenPort=8000,
 [string]$QmsServiceConfig,
 [string]$ErpServiceConfig,
 [switch]$ArenaAdapterRegistered,
 [switch]$VerifyLocalAddress
)
$ErrorActionPreference='Stop'

function Read-GatewayBootstrap([string]$Path,[string]$ExpectedApplicationId){
 $value=Get-Content -Raw -LiteralPath $Path|ConvertFrom-Json
 if($value.configuration_version -ne 1 -or $value.contract -ne 'shared.inference/1' -or $value.service_id -ne 'shared-inference'){throw 'Gateway bootstrap contract/service identity is unsupported'}
 if($value.application_id -ne $ExpectedApplicationId){throw "Gateway bootstrap is not scoped to $ExpectedApplicationId"}
 if($value.url -ne 'https://192.0.2.254:8443'){throw 'Dedicated deployment requires https://192.0.2.254:8443'}
 if($value.PSObject.Properties.Name -contains 'token' -or $value.PSObject.Properties.Name -contains 'token_environment' -or $value.url -match '@'){throw 'Gateway 0.3 deployment bootstrap must use token_file and must not embed or name an environment credential'}
 if(!$value.token_file -or !(Test-Path -LiteralPath $value.token_file)){throw "Provisioned $ExpectedApplicationId token_file is unavailable"}
 if(!$value.ca_file -or !(Test-Path -LiteralPath $value.ca_file)){throw "Provisioned $ExpectedApplicationId Gateway CA file is unavailable"}
 return $value
}

function Protect-GatewayFile([string]$Path,[bool]$Secret){
 if($Secret){
  & icacls.exe $Path /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' '*S-1-5-19:(R)' /Q|Out-Null
 }else{
  & icacls.exe $Path /grant:r '*S-1-5-19:(R)' /Q|Out-Null
 }
 if($LASTEXITCODE){throw "Unable to apply the LOCAL SERVICE file ACL: $Path"}
}

$operational=Read-GatewayBootstrap $OperationalGatewayBootstrap 'capstone'
$research=Read-GatewayBootstrap $ResearchGatewayBootstrap 'capstone-research'
if((Resolve-Path $operational.token_file).Path -eq (Resolve-Path $research.token_file).Path){throw 'Operational and research Gateway identities require different token files'}
if(!(Test-Path -LiteralPath $DeveloperTokenFile)){throw 'Provision the protected developer token file before configuration'}
if($VerifyLocalAddress -and !(Get-NetIPAddress -IPAddress $ListenHost -ErrorAction SilentlyContinue)){throw "The target address $ListenHost is not assigned. This script does not modify network settings."}

$configDirectory=Join-Path $DataRoot 'config';New-Item -ItemType Directory -Force -Path $configDirectory|Out-Null
$operationalTarget=Join-Path $configDirectory 'inference-gateway.client.json'
$researchTarget=Join-Path $configDirectory 'inference-gateway.research-client.json'
Copy-Item -LiteralPath $OperationalGatewayBootstrap -Destination $operationalTarget -Force
Copy-Item -LiteralPath $ResearchGatewayBootstrap -Destination $researchTarget -Force
foreach($path in @($operationalTarget,$researchTarget,$operational.ca_file,$research.ca_file)){Protect-GatewayFile $path $false}
foreach($path in @($operational.token_file,$research.token_file,$DeveloperTokenFile)){Protect-GatewayFile $path $true}

$serviceConfigs=[ordered]@{}
foreach($entry in @(@('qms',$QmsServiceConfig,'head-192-0-2-3'),@('erp',$ErpServiceConfig,'head-192-0-2-4'))){if($entry[1]){$service=Get-Content -Raw -LiteralPath $entry[1]|ConvertFrom-Json;if(!$service.url -or !$service.token_file -or $service.execution_node -ne $entry[2] -or !(Test-Path -LiteralPath $service.token_file)){throw "$($entry[0]) service client configuration is invalid"};$target=Join-Path $configDirectory "$($entry[0])-service.client.json";Copy-Item -LiteralPath $entry[1] -Destination $target -Force;Protect-GatewayFile $target $false;Protect-GatewayFile $service.token_file $true;$serviceConfigs[$entry[0]]=$target}}
$settings=[ordered]@{schema_version=1;application_id='capstone';research_application_id='capstone-research';orchestrator_id='main';listen_host=$ListenHost;listen_port=$ListenPort;inference_mode='gateway';gateway_client_config=$operationalTarget;research_gateway_client_config=$researchTarget;qms_service_config=$serviceConfigs.qms;erp_service_config=$serviceConfigs.erp;developer_token_file=(Resolve-Path $DeveloperTokenFile).Path;arena_adapter=[ordered]@{owner='Agent Arena';contract='existing Ticketing adapter (read-only operations database)';operations_database=(Join-Path $DataRoot 'data\operations.db');destination_host='192.0.2.254';registration_status=if($ArenaAdapterRegistered){'complete'}else{'external_registration_required'}}}
$settings|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $configDirectory 'appsettings.json') -Encoding utf8
[ordered]@{configured=$true;application_identity='capstone';research_identity='capstone-research';listen_address="$ListenHost`:$ListenPort";gateway_origin=$operational.url;arena_registration=$settings.arena_adapter.registration_status;network_modified=$false;credentials_issued=$false;secrets_copied=$false;local_service_acl_applied=$true}|ConvertTo-Json
