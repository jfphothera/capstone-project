param(
 [Parameter(Mandatory=$true)][string]$Version,
 [string]$InstallRoot='C:\Program Files\Capstone',
 [string]$DataRoot='C:\ProgramData\Capstone',
 [string]$TaskName='Capstone Application',
 [switch]$AllowIncompatibleSchema
)
$ErrorActionPreference='Stop'
$pointerPath=Join-Path $InstallRoot 'current.json'
$current=Get-Content -Raw -LiteralPath $pointerPath|ConvertFrom-Json
$release=Join-Path $InstallRoot "releases\$Version\application"
if(!(Test-Path -LiteralPath (Join-Path $release 'VERSION'))){throw "Installed release not found: $Version"}
if($Version -eq $current.version){throw "Release $Version is already active"}
$manifestPath=Join-Path (Split-Path -Parent $release) 'package-manifest.json'
if(!(Test-Path -LiteralPath $manifestPath) -and !$AllowIncompatibleSchema){throw "Target release has no retained compatibility manifest; rollback refused"}
$db=Join-Path $DataRoot 'data\operations.db'
if(Test-Path -LiteralPath $manifestPath){
 $manifest=Get-Content -Raw -LiteralPath $manifestPath|ConvertFrom-Json
 if($null -eq $manifest.compatibility.data_schema_max -and !$AllowIncompatibleSchema){throw 'Target release has no maximum supported data schema; rollback refused'}
 if(!$AllowIncompatibleSchema){
  $python=Join-Path $InstallRoot 'runtime\Scripts\python.exe'
  & $python (Join-Path $release 'scripts\check_data_schema.py') $db --minimum ([int]$manifest.compatibility.data_schema_min) --maximum ([int]$manifest.compatibility.data_schema_max)|Out-Null
  if($LASTEXITCODE){throw "Current operational data is incompatible with release $Version; rollback refused"}
 }
}
& (Join-Path $InstallRoot 'tools\Stop-Capstone.ps1') -TaskName $TaskName -InstallRoot $InstallRoot -DataRoot $DataRoot
$stamp=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ');$backup=Join-Path $DataRoot ("backups\operations-before-rollback-$Version-$stamp.db");Copy-Item -LiteralPath $db -Destination $backup
$aiDb=Join-Path $DataRoot 'data\ai_configuration.db';$aiExisted=Test-Path -LiteralPath $aiDb;$aiBackup=$null
if($aiExisted){$aiBackup=Join-Path $DataRoot ("backups\ai-configuration-before-rollback-$Version-$stamp.db");Copy-Item -LiteralPath $aiDb -Destination $aiBackup}
$rollback=[ordered]@{version=$Version;previous=$current.version;rolled_back_at=(Get-Date).ToUniversalTime().ToString('o');data_preserved=$true}|ConvertTo-Json
$temporary=$pointerPath+'.tmp';Set-Content -LiteralPath $temporary -Value $rollback -Encoding utf8;Move-Item -LiteralPath $temporary -Destination $pointerPath -Force
try{
 Start-ScheduledTask -TaskName $TaskName;Start-Sleep -Seconds 5
 & (Join-Path $InstallRoot 'tools\Test-CapstoneHealth.ps1') -DataRoot $DataRoot|Out-Null
 if($LASTEXITCODE){throw 'Rolled-back application failed health validation'}
}catch{
 $restore=[ordered]@{version=$current.version;previous=$Version;rollback_failed_at=(Get-Date).ToUniversalTime().ToString('o');reason=$_.Exception.Message}|ConvertTo-Json
 Set-Content -LiteralPath $temporary -Value $restore -Encoding utf8;Move-Item -LiteralPath $temporary -Destination $pointerPath -Force
 Copy-Item -LiteralPath $backup -Destination $db -Force
 if($aiExisted){Copy-Item -LiteralPath $aiBackup -Destination $aiDb -Force}elseif(Test-Path -LiteralPath $aiDb){Remove-Item -LiteralPath $aiDb -Force}
 Start-ScheduledTask -TaskName $TaskName
 throw
}
[ordered]@{rolled_back=$true;from=$current.version;to=$Version;data_root=$DataRoot;operations_backup=$backup;ai_configuration_backup=$aiBackup;schema_override=[bool]$AllowIncompatibleSchema;experiment_records_preserved=$true}|ConvertTo-Json
