param([string]$InstallRoot='C:\Program Files\Capstone',[string]$DataRoot='C:\ProgramData\Capstone',[string]$TaskName='Capstone Application')
$ErrorActionPreference='Stop'
$configuration=Join-Path $DataRoot 'config\appsettings.json'
if(!(Test-Path -LiteralPath $configuration)){throw 'Run Configure-Capstone.ps1 before registering startup'}
$appSettings=Get-Content -Raw -LiteralPath $configuration|ConvertFrom-Json
if(!(Test-Path -LiteralPath $appSettings.developer_token_file)){throw 'Protected developer token file is not provisioned'}
if(!(Test-Path -LiteralPath $appSettings.gateway_client_config)){throw 'Gateway bootstrap is not provisioned'}
if(!(Test-Path -LiteralPath $appSettings.research_gateway_client_config)){throw 'Research Gateway bootstrap is not provisioned'}
$gateway=Get-Content -Raw -LiteralPath $appSettings.gateway_client_config|ConvertFrom-Json
$researchGateway=Get-Content -Raw -LiteralPath $appSettings.research_gateway_client_config|ConvertFrom-Json
foreach($item in @($gateway,$researchGateway)){
 if($item.url -ne 'https://192.0.2.254:8443' -or !(Test-Path -LiteralPath $item.ca_file) -or !(Test-Path -LiteralPath $item.token_file)){throw 'Gateway endpoint, token, or trust material is incomplete'}
}
if($gateway.application_id -ne 'capstone' -or $researchGateway.application_id -ne 'capstone-research'){throw 'Operational and research Gateway identities are not independently configured'}
$launcher=Join-Path $InstallRoot 'tools\Start-Capstone.ps1'
$arguments="-NoProfile -ExecutionPolicy RemoteSigned -File `"$launcher`" -InstallRoot `"$InstallRoot`" -DataRoot `"$DataRoot`""
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger=New-ScheduledTaskTrigger -AtStartup
$principal=New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force|Out-Null
Start-ScheduledTask -TaskName $TaskName
