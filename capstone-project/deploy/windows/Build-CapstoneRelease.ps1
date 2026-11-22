param(
 [string]$Version=(Get-Content -Raw (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'VERSION')).Trim(),
 [Parameter(Mandatory=$true)][string]$PythonRuntimeRoot,
 [string]$WheelhouseSource,
 [string]$OutputDirectory=(Join-Path $PSScriptRoot 'dist')
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$work=Join-Path $PSScriptRoot "work\Capstone-$Version";if(Test-Path $work){Remove-Item -LiteralPath $work -Recurse -Force}
$app=Join-Path $work 'payload\application';$seed=Join-Path $work 'payload\seed-data';$wheels=Join-Path $work 'payload\wheelhouse';$runtime=Join-Path $work 'payload\python-runtime';$models=Join-Path $work 'payload\models';$tools=Join-Path $work 'tools'
New-Item -ItemType Directory -Force -Path $app,$seed,$wheels,$runtime,$models,$tools,$OutputDirectory|Out-Null
foreach($file in @('VERSION','requirements.txt','requirements.runtime.lock','run_app.py')){Copy-Item -LiteralPath (Join-Path $root $file) -Destination $app}
foreach($dir in @('src','scripts','tests','docs')){
 & robocopy.exe (Join-Path $root $dir) (Join-Path $app $dir) /E /R:1 /W:1 /XD '__pycache__' 'test-temp' /XF '*.pyc' /NFL /NDL /NJH /NJS /NP | Out-Null
 if($LASTEXITCODE -ge 8){throw "Application source copy failed with robocopy code $LASTEXITCODE"}
}
Copy-Item -LiteralPath (Join-Path $root 'config') -Destination (Join-Path $app 'config') -Recurse
foreach($dir in @('erp','qms')){Copy-Item -LiteralPath (Join-Path $root "data\$dir") -Destination (Join-Path $seed $dir) -Recurse}
Copy-Item -LiteralPath (Join-Path $root 'data\qms_compatibility.json') -Destination $seed
& (Join-Path $PythonRuntimeRoot 'python.exe') (Join-Path $root 'scripts\create_release_seed.py') (Join-Path $root 'data\operations.db') (Join-Path $seed 'operations.db');if($LASTEXITCODE){throw 'Release seed creation failed'}
& robocopy.exe $PythonRuntimeRoot $runtime /E /R:1 /W:1 /XD (Join-Path $PythonRuntimeRoot 'Lib\site-packages') '__pycache__' /XF '*.pem' '*.key' '*.pfx' '*.p12' '*.token' '*.secret' /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -ge 8){throw "Python runtime copy failed with robocopy code $LASTEXITCODE"}
$publicCa=Join-Path $PythonRuntimeRoot 'Lib\site-packages\certifi\cacert.pem'
if(!(Test-Path -LiteralPath $publicCa)){$publicCa=Join-Path $PythonRuntimeRoot 'Lib\public-ca-bundle.pem'}
if(!(Test-Path -LiteralPath $publicCa)){throw 'Public Certifi CA bundle is unavailable in the build runtime'}
Copy-Item -LiteralPath $publicCa -Destination (Join-Path $runtime 'Lib\public-ca-bundle.pem')
$rejectedCa=Join-Path $PythonRuntimeRoot 'Lib\codex-rejected-cas.pem'
if(!(Test-Path -LiteralPath $rejectedCa)){throw 'Build runtime trust-policy certificate is unavailable'}
Copy-Item -LiteralPath $rejectedCa -Destination (Join-Path $runtime 'Lib\codex-rejected-cas.pem')
if($WheelhouseSource){
 if(!(Test-Path -LiteralPath $WheelhouseSource)){throw 'Specified offline wheelhouse source is unavailable'}
 Copy-Item -Path (Join-Path $WheelhouseSource '*') -Destination $wheels -Force
}else{
 & (Join-Path $PythonRuntimeRoot 'python.exe') -m pip download --quiet --only-binary=:all: --dest $wheels -r (Join-Path $root 'requirements.runtime.lock')
 if($LASTEXITCODE){throw 'Wheelhouse creation failed'}
}
if(!(Get-ChildItem -LiteralPath $wheels -Filter '*.whl' -File)){throw 'Wheelhouse creation failed'}
foreach($file in @('README.md','appsettings.template.json','Start-Capstone.ps1','Stop-Capstone.ps1','Test-CapstoneHealth.ps1','Test-CapstoneGateway.ps1','Test-CapstoneEndToEnd.ps1','Test-CapstoneLabHeads.ps1','Register-CapstoneTask.ps1','Configure-Capstone.ps1','Install-Capstone.ps1','Update-Capstone.ps1','Rollback-Capstone.ps1','Export-CapstoneBusinessData.ps1','Qualify-CapstoneLab.cmd')){Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $tools}
$files=Get-ChildItem -LiteralPath $work -File -Recurse|Sort-Object FullName
$manifest=[ordered]@{format='capstone-windows-release-v1';version=$Version;created_utc='reproducible-build';target='Windows 11 x64 / 192.0.2.201';infrastructure_host='192.0.2.254';compatibility=@{python='3.12';data_schema_min=11;data_schema_max=11;gateway_contract='shared.inference/1';arena_contract='existing Ticketing adapter'};secrets_included=$false;files=@()}
foreach($file in $files){$relative=$file.FullName.Substring($work.Length+1).Replace('\','/');$manifest.files+=[ordered]@{path=$relative;sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant();size=$file.Length}}
$manifest|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $work 'package-manifest.json') -Encoding utf8
$zip=Join-Path $OutputDirectory "Capstone-$Version-windows-x64.zip";if(Test-Path $zip){Remove-Item -LiteralPath $zip -Force}
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive=[IO.Compression.ZipFile]::Open($zip,[IO.Compression.ZipArchiveMode]::Create)
try{foreach($file in (Get-ChildItem -LiteralPath $work -File -Recurse|Sort-Object FullName)){$relative=$file.FullName.Substring($work.Length+1).Replace('\','/');$entry=$archive.CreateEntry($relative,[IO.Compression.CompressionLevel]::Optimal);$entry.LastWriteTime=[datetimeoffset]'1980-01-01T00:00:00Z';$input=$file.OpenRead();$output=$entry.Open();try{$input.CopyTo($output)}finally{$output.Dispose();$input.Dispose()}}}finally{$archive.Dispose()}
[ordered]@{package=$zip;sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant();version=$Version}|ConvertTo-Json
