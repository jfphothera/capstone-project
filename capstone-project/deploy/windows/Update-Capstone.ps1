param([Parameter(Mandatory=$true)][string]$PackageRoot,[string]$InstallRoot='C:\Program Files\Capstone',[string]$DataRoot='C:\ProgramData\Capstone',[string]$TaskName='Capstone Application')
$ErrorActionPreference='Stop'
$settings=Get-Content -Raw -LiteralPath (Join-Path $DataRoot 'config\appsettings.json')|ConvertFrom-Json
function Assert-VcRuntime {
 $missing=@('msvcp140.dll','msvcp140_atomic_wait.dll','vcruntime140.dll','vcruntime140_1.dll','vcruntime140_threads.dll')|Where-Object {!(Test-Path -LiteralPath (Join-Path $env:WINDIR "System32\$_"))}
 if($missing){throw "Microsoft Visual C++ 2015-2022 Redistributable (x64) is required. Install Microsoft's vc_redist.x64.exe before updating Capstone. Missing: $($missing -join ', ')"}
}
Assert-VcRuntime
$old=Get-Content -Raw -LiteralPath (Join-Path $InstallRoot 'current.json')|ConvertFrom-Json
$manifest=Get-Content -Raw -LiteralPath (Join-Path $PackageRoot 'package-manifest.json')|ConvertFrom-Json
if($null -eq $manifest.compatibility.data_schema_max){throw 'Release manifest has no maximum supported data schema'}
foreach($entry in $manifest.files){$path=Join-Path $PackageRoot $entry.path;if((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.sha256){throw "Package hash mismatch: $($entry.path)"}}
$new=$manifest.version;$release=Join-Path $InstallRoot "releases\$new\application";$releaseRoot=Split-Path -Parent $release
if(!(Test-Path -LiteralPath $release)){New-Item -ItemType Directory -Force -Path $release|Out-Null;Copy-Item -Path (Join-Path $PackageRoot 'payload\application\*') -Destination $release -Recurse -Force;Copy-Item -LiteralPath (Join-Path $PackageRoot 'package-manifest.json') -Destination (Join-Path $releaseRoot 'package-manifest.json')}
$db=Join-Path $DataRoot 'data\operations.db';$python=Join-Path $InstallRoot 'runtime\Scripts\python.exe'
& $python (Join-Path $release 'scripts\check_data_schema.py') $db --minimum ([int]$manifest.compatibility.data_schema_min) --maximum ([int]$manifest.compatibility.data_schema_max)|Out-Null
if($LASTEXITCODE){throw "Current operational data is incompatible with release $new; active release was not changed"}
Copy-Item -Path (Join-Path $PackageRoot 'tools\*') -Destination (Join-Path $InstallRoot 'tools') -Recurse -Force
& (Join-Path $InstallRoot 'tools\Stop-Capstone.ps1') -TaskName $TaskName -InstallRoot $InstallRoot -DataRoot $DataRoot
$oldRelease=Join-Path $InstallRoot "releases\$($old.version)\application"
$oldLock=Join-Path $oldRelease 'requirements.runtime.lock';$newLock=Join-Path $release 'requirements.runtime.lock'
if(!(Test-Path -LiteralPath $oldLock) -or (Get-FileHash $oldLock -Algorithm SHA256).Hash -ne (Get-FileHash $newLock -Algorithm SHA256).Hash){
 & $python -m pip install --no-compile --no-index --find-links (Join-Path $PackageRoot 'payload\wheelhouse') -r $newLock
 if($LASTEXITCODE){throw 'Offline dependency update failed'}
}
$stamp=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ');$backup=Join-Path $DataRoot ("backups\operations-before-$new-$stamp.db")
Copy-Item -LiteralPath $db -Destination $backup
$aiDb=Join-Path $DataRoot 'data\ai_configuration.db';$aiExisted=Test-Path -LiteralPath $aiDb;$aiBackup=$null
if($aiExisted){$aiBackup=Join-Path $DataRoot ("backups\ai-configuration-before-$new-$stamp.db");Copy-Item -LiteralPath $aiDb -Destination $aiBackup}
$pointer=[ordered]@{version=$new;previous=$old.version;activated_at=(Get-Date).ToUniversalTime().ToString('o')}|ConvertTo-Json
Set-Content -LiteralPath (Join-Path $InstallRoot 'current.json.tmp') -Value $pointer -Encoding utf8;Move-Item -LiteralPath (Join-Path $InstallRoot 'current.json.tmp') -Destination (Join-Path $InstallRoot 'current.json') -Force
try{
 Start-ScheduledTask -TaskName $TaskName
 $deadline=(Get-Date).AddSeconds(240);$healthy=$false
 while((Get-Date) -lt $deadline){
  try{$response=Invoke-RestMethod -Uri "http://$($settings.listen_host):$($settings.listen_port)/api/health" -TimeoutSec 3;if($response.ok){$healthy=$true;break}}catch{}
  Start-Sleep -Seconds 2
 }
 if(!$healthy){throw 'Updated application did not answer /api/health within 240 seconds'}
 & (Join-Path $InstallRoot 'tools\Test-CapstoneHealth.ps1') -DataRoot $DataRoot|Out-Null
 if($LASTEXITCODE){throw 'Updated application failed health validation'}
}catch{
 & (Join-Path $InstallRoot 'tools\Stop-Capstone.ps1') -TaskName $TaskName -InstallRoot $InstallRoot -DataRoot $DataRoot
 $rollback=[ordered]@{version=$old.version;previous=$new;rolled_back_at=(Get-Date).ToUniversalTime().ToString('o');reason=$_.Exception.Message}|ConvertTo-Json
 Set-Content -LiteralPath (Join-Path $InstallRoot 'current.json.tmp') -Value $rollback -Encoding utf8;Move-Item -LiteralPath (Join-Path $InstallRoot 'current.json.tmp') -Destination (Join-Path $InstallRoot 'current.json') -Force
 Copy-Item -LiteralPath $backup -Destination $db -Force
 if($aiExisted){Copy-Item -LiteralPath $aiBackup -Destination $aiDb -Force}elseif(Test-Path -LiteralPath $aiDb){Remove-Item -LiteralPath $aiDb -Force}
 Start-ScheduledTask -TaskName $TaskName
 throw "Update rolled back to $($old.version): $($_.Exception.Message)"
}
function New-CapstoneShortcut([string]$Name,[string]$Url){
 $shell=New-Object -ComObject WScript.Shell
 foreach($directory in @([Environment]::GetFolderPath('CommonDesktopDirectory'),(Join-Path ([Environment]::GetFolderPath('CommonStartMenu')) 'Programs'))){
  $shortcut=$shell.CreateShortcut((Join-Path $directory "$Name.lnk"));$shortcut.TargetPath="$env:WINDIR\explorer.exe";$shortcut.Arguments=$Url;$shortcut.WorkingDirectory=$InstallRoot;$shortcut.IconLocation="$env:WINDIR\System32\shell32.dll,14";$shortcut.Description="$Name on this Capstone laptop";$shortcut.Save()
 }
}
New-CapstoneShortcut 'Capstone' 'http://192.0.2.201:8000/'
New-CapstoneShortcut 'Capstone Testing Lab' 'http://192.0.2.201:8000/testing-lab-login?next=/pressure-testing'
[ordered]@{updated=$true;from=$old.version;to=$new;operations_backup=$backup;ai_configuration_backup=$aiBackup;target_manifest=(Join-Path $releaseRoot 'package-manifest.json')}|ConvertTo-Json
