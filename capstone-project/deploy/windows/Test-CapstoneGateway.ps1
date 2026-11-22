param([string]$InstallRoot='C:\Program Files\Capstone',[string]$DataRoot='C:\ProgramData\Capstone')
$ErrorActionPreference='Stop'
$settings=Get-Content -Raw -LiteralPath (Join-Path $DataRoot 'config\appsettings.json')|ConvertFrom-Json
$current=(Get-Content -Raw -LiteralPath (Join-Path $InstallRoot 'current.json')|ConvertFrom-Json).version
$app=Join-Path $InstallRoot "releases\$current\application"
$python=Join-Path $InstallRoot 'runtime\Scripts\python.exe'
& $python (Join-Path $app 'scripts\verify_gateway_clients.py') --operational $settings.gateway_client_config --research $settings.research_gateway_client_config
exit $LASTEXITCODE
