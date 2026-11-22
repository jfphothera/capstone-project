param([string]$InstallRoot='C:\Program Files\Capstone',[string]$DataRoot='C:\ProgramData\Capstone')
$ErrorActionPreference='Stop'
$settings=Get-Content -Raw -LiteralPath (Join-Path $DataRoot 'config\appsettings.json') | ConvertFrom-Json
if(!(Test-Path -LiteralPath $settings.developer_token_file)){throw "Capstone cannot start: developer token file is missing: $($settings.developer_token_file)"}
foreach($configPath in @($settings.gateway_client_config,$settings.research_gateway_client_config)){
  if(!(Test-Path -LiteralPath $configPath)){throw "Capstone cannot start: Gateway client configuration is missing: $configPath"}
  $gateway=Get-Content -Raw -LiteralPath $configPath|ConvertFrom-Json
  if(!(Test-Path -LiteralPath $gateway.token_file)){throw "Capstone cannot start: Gateway token file is missing: $($gateway.token_file)"}
  if(!(Test-Path -LiteralPath $gateway.ca_file)){throw "Capstone cannot start: approved Gateway CA file is missing: $($gateway.ca_file)"}
  if($gateway.service_id -ne 'shared-inference'){throw "Capstone cannot start: Gateway service identity is not shared-inference"}
}
if(Get-NetTCPConnection -LocalAddress $settings.listen_host -LocalPort ([int]$settings.listen_port) -State Listen -ErrorAction SilentlyContinue){throw "Capstone cannot start: port $($settings.listen_host):$($settings.listen_port) is already in use"}
$current=(Get-Content -Raw -LiteralPath (Join-Path $InstallRoot 'current.json')|ConvertFrom-Json).version
$app=Join-Path $InstallRoot "releases\$current\application"
$python=Join-Path $InstallRoot 'runtime\Scripts\python.exe'
if(!(Test-Path -LiteralPath $python)){throw 'Managed Capstone Python runtime is missing'}
$env:CAPSTONE_DATA_ROOT=Join-Path $DataRoot 'data'
$env:CAPSTONE_DB=Join-Path $DataRoot 'data\operations.db'
$env:CAPSTONE_AI_SETTINGS=Join-Path $DataRoot 'data\ai_configuration.db'
$env:CAPSTONE_APPLICATION_ID=$settings.application_id
$env:CAPSTONE_ORCHESTRATOR_ID=$settings.orchestrator_id
$env:CAPSTONE_INFERENCE_MODE='gateway'
$env:CAPSTONE_GATEWAY_CLIENT_CONFIG=$settings.gateway_client_config
$env:CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG=$settings.research_gateway_client_config
if($settings.qms_service_config){$env:CAPSTONE_QMS_SERVICE_CONFIG=$settings.qms_service_config}
if($settings.erp_service_config){$env:CAPSTONE_ERP_SERVICE_CONFIG=$settings.erp_service_config}
# QMS/ERP source switch: 'local' (default) or 'remote' (head services above).
$env:CAPSTONE_BUSINESS_SERVICES=if($settings.business_services -eq 'remote'){'remote'}else{'local'}
$env:CAPSTONE_DEVELOPER_TOKEN_FILE=$settings.developer_token_file
$env:HF_HOME=Join-Path $DataRoot 'models\huggingface'
$env:TEMP=Join-Path $DataRoot 'temp';$env:TMP=$env:TEMP
$env:SSL_CERT_FILE=Join-Path $InstallRoot 'runtime\Lib\site-packages\certifi\cacert.pem'
$env:REQUESTS_CA_BUNDLE=$env:SSL_CERT_FILE
$env:HF_HUB_OFFLINE='1';$env:TRANSFORMERS_OFFLINE='1';$env:LANGSMITH_TRACING='false';$env:LANGCHAIN_TRACING_V2='false'
$log=Join-Path $DataRoot 'logs\capstone.log'
$errors=Join-Path $DataRoot 'logs\capstone.error.log'
Set-Location -LiteralPath $app
$ErrorActionPreference='Continue'
& $python (Join-Path $app 'run_app.py') --host $settings.listen_host --port ([int]$settings.listen_port) 1>>$log 2>>$errors
exit $LASTEXITCODE
