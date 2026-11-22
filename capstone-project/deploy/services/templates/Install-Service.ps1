param([string]$InstallRoot='C:\Program Files\{{SERVICE}}',[string]$DataRoot='C:\ProgramData\{{SERVICE}}')
$ErrorActionPreference='Stop'
function Assert-VcRuntime {
 $missing=@('msvcp140.dll','msvcp140_atomic_wait.dll','vcruntime140.dll','vcruntime140_1.dll','vcruntime140_threads.dll')|Where-Object {!(Test-Path -LiteralPath (Join-Path $env:WINDIR "System32\$_"))}
 if($missing){throw "Microsoft Visual C++ 2015-2022 Redistributable (x64) is required. Install Microsoft's vc_redist.x64.exe, then run Install-{{SERVICE}}.ps1 again. Missing: $($missing -join ', ')"}
}
Assert-VcRuntime
$packageRoot=Split-Path -Parent $PSScriptRoot;$manifest=Get-Content -Raw (Join-Path $packageRoot 'package-manifest.json')|ConvertFrom-Json
foreach($entry in $manifest.files){$path=Join-Path $packageRoot $entry.path;if(!(Test-Path $path) -or (Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256){throw "Package verification failed: $($entry.path)"}}
$release=Join-Path $InstallRoot "releases\$($manifest.version)";if(Test-Path $release){throw "Release $($manifest.version) is already installed"}
New-Item -ItemType Directory -Force -Path "$release\application","$InstallRoot\tools","$DataRoot\data","$DataRoot\config","$DataRoot\secrets","$DataRoot\logs","$DataRoot\backups"|Out-Null
Copy-Item "$packageRoot\payload\application\*" "$release\application" -Recurse -Force;Copy-Item "$packageRoot\payload\python-runtime" "$release\python-runtime" -Recurse;Copy-Item "$packageRoot\tools\*" "$InstallRoot\tools" -Force
if(!(Get-ChildItem "$DataRoot\data" -Force)){Copy-Item "$packageRoot\payload\data\{{KIND}}\*" "$DataRoot\data" -Recurse -Force}
[ordered]@{version=$manifest.version;previous=$null}|ConvertTo-Json|Set-Content "$InstallRoot\current.json" -Encoding utf8
& icacls.exe $DataRoot /grant:r '*S-1-5-19:(RX)' /Q|Out-Null;foreach($directory in @("$DataRoot\data","$DataRoot\logs")){& icacls.exe $directory /grant:r '*S-1-5-19:(OI)(CI)(M)' /T /Q|Out-Null;if($LASTEXITCODE){throw "Unable to grant LOCAL SERVICE access to $directory"}}
[ordered]@{installed=$true;service='{{KIND}}';version=$manifest.version;install_root=$InstallRoot;data_root=$DataRoot;credentials_issued=$false}|ConvertTo-Json
