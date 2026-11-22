param(
 [string]$InstallRoot='C:\Program Files\Capstone',
 [string]$DataRoot='C:\ProgramData\Capstone'
)
$ErrorActionPreference='Stop'
function Assert-VcRuntime {
 $missing=@('msvcp140.dll','msvcp140_atomic_wait.dll','vcruntime140.dll','vcruntime140_1.dll','vcruntime140_threads.dll')|Where-Object {!(Test-Path -LiteralPath (Join-Path $env:WINDIR "System32\$_"))}
 if($missing){throw "Microsoft Visual C++ 2015-2022 Redistributable (x64) is required. Install Microsoft's vc_redist.x64.exe, then run Install-Capstone.ps1 again. Missing: $($missing -join ', ')"}
}
Assert-VcRuntime
$packageRoot=Split-Path -Parent $PSScriptRoot
$manifest=Get-Content -Raw -LiteralPath (Join-Path $packageRoot 'package-manifest.json')|ConvertFrom-Json
foreach($entry in $manifest.files){
 $path=Join-Path $packageRoot $entry.path
 if(!(Test-Path -LiteralPath $path)){throw "Package file missing: $($entry.path)"}
 if((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.sha256){throw "Package hash mismatch: $($entry.path)"}
}
$version=$manifest.version
$release=Join-Path $InstallRoot "releases\$version\application"
$releaseRoot=Split-Path -Parent $release
if(Test-Path -LiteralPath $release){throw "Release $version is already installed"}
New-Item -ItemType Directory -Force -Path $release,(Join-Path $InstallRoot 'tools'),(Join-Path $DataRoot 'data'),(Join-Path $DataRoot 'config'),(Join-Path $DataRoot 'secrets'),(Join-Path $DataRoot 'logs'),(Join-Path $DataRoot 'backups'),(Join-Path $DataRoot 'models'),(Join-Path $DataRoot 'temp')|Out-Null
Copy-Item -Path (Join-Path $packageRoot 'payload\application\*') -Destination $release -Recurse -Force
Copy-Item -LiteralPath (Join-Path $packageRoot 'package-manifest.json') -Destination (Join-Path $releaseRoot 'package-manifest.json')
Copy-Item -Path (Join-Path $packageRoot 'tools\*') -Destination (Join-Path $InstallRoot 'tools') -Recurse -Force
if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'data\operations.db'))){Copy-Item -LiteralPath (Join-Path $packageRoot 'payload\seed-data\operations.db') -Destination (Join-Path $DataRoot 'data\operations.db')}
foreach($name in @('erp','qms')){if(!(Test-Path -LiteralPath (Join-Path $DataRoot "data\$name"))){Copy-Item -LiteralPath (Join-Path $packageRoot "payload\seed-data\$name") -Destination (Join-Path $DataRoot "data\$name") -Recurse}}
if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'data\qms_compatibility.json'))){Copy-Item -LiteralPath (Join-Path $packageRoot 'payload\seed-data\qms_compatibility.json') -Destination (Join-Path $DataRoot 'data\qms_compatibility.json')}
if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'models\huggingface'))){Copy-Item -LiteralPath (Join-Path $packageRoot 'payload\models\huggingface') -Destination (Join-Path $DataRoot 'models\huggingface') -Recurse}
if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'config\appsettings.json'))){Copy-Item -LiteralPath (Join-Path $packageRoot 'tools\appsettings.template.json') -Destination (Join-Path $DataRoot 'config\appsettings.json')}
if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'config\inference-gateway.client.json'))){Copy-Item -LiteralPath (Join-Path $release 'config\inference-gateway.client.template.json') -Destination (Join-Path $DataRoot 'config\inference-gateway.client.json')}
if(!(Test-Path -LiteralPath (Join-Path $DataRoot 'config\inference-gateway.research-client.json'))){Copy-Item -LiteralPath (Join-Path $release 'config\inference-gateway.research-client.template.json') -Destination (Join-Path $DataRoot 'config\inference-gateway.research-client.json')}
if(!(Test-Path -LiteralPath (Join-Path $InstallRoot 'runtime\Scripts\python.exe'))){
 $packagedBase=Join-Path $packageRoot 'payload\python-runtime'
 if(!(Test-Path -LiteralPath (Join-Path $packagedBase 'python.exe'))){throw 'Package has no Python 3.12 runtime'}
 $installedBase=Join-Path $InstallRoot 'python-base'
 New-Item -ItemType Directory -Force -Path $installedBase|Out-Null
 Copy-Item -Path (Join-Path $packagedBase '*') -Destination $installedBase -Recurse -Force
 $base=Join-Path $installedBase 'python.exe'
 $env:TEMP=Join-Path $DataRoot 'temp';$env:TMP=$env:TEMP
 $env:SSL_CERT_FILE=Join-Path $packageRoot 'payload\python-runtime\Lib\public-ca-bundle.pem'
 $env:REQUESTS_CA_BUNDLE=$env:SSL_CERT_FILE;$env:PIP_CERT=$env:SSL_CERT_FILE
 & $base -m venv (Join-Path $InstallRoot 'runtime');if($LASTEXITCODE){throw 'Managed runtime creation failed'}
 & (Join-Path $InstallRoot 'runtime\Scripts\python.exe') -m pip install --no-compile --no-index --find-links (Join-Path $packageRoot 'payload\wheelhouse') -r (Join-Path $release 'requirements.runtime.lock')
 if($LASTEXITCODE){throw 'Offline dependency installation failed'}
}
$database=Join-Path $DataRoot 'data\operations.db'
$python=Join-Path $InstallRoot 'runtime\Scripts\python.exe'
& $python (Join-Path $release 'scripts\check_data_schema.py') $database --minimum ([int]$manifest.compatibility.data_schema_min) --maximum ([int]$manifest.compatibility.data_schema_max)|Out-Null
if($LASTEXITCODE){throw "Operational database schema is not compatible with release $version"}
$pointer=[ordered]@{version=$version;activated_at=(Get-Date).ToUniversalTime().ToString('o');previous=$null}|ConvertTo-Json
$temporary=Join-Path $InstallRoot 'current.json.tmp';Set-Content -LiteralPath $temporary -Value $pointer -Encoding utf8;Move-Item -LiteralPath $temporary -Destination (Join-Path $InstallRoot 'current.json') -Force
function New-CapstoneShortcut([string]$Name,[string]$Url){
 $shell=New-Object -ComObject WScript.Shell
 foreach($directory in @([Environment]::GetFolderPath('CommonDesktopDirectory'),(Join-Path ([Environment]::GetFolderPath('CommonStartMenu')) 'Programs'))){
  $shortcut=$shell.CreateShortcut((Join-Path $directory "$Name.lnk"));$shortcut.TargetPath="$env:WINDIR\explorer.exe";$shortcut.Arguments=$Url;$shortcut.WorkingDirectory=$InstallRoot;$shortcut.IconLocation="$env:WINDIR\System32\shell32.dll,14";$shortcut.Description="$Name on this Capstone laptop";$shortcut.Save()
 }
}
New-CapstoneShortcut 'Capstone' 'http://192.0.2.201:8000/'
New-CapstoneShortcut 'Capstone Testing Lab' 'http://192.0.2.201:8000/testing-lab-login?next=/pressure-testing'
& icacls.exe $DataRoot /grant:r '*S-1-5-19:(RX)' /Q|Out-Null
if($LASTEXITCODE){throw 'Unable to grant LOCAL SERVICE access to the Capstone data root'}
foreach($directory in @('data','logs','backups','models','temp')){
 & icacls.exe (Join-Path $DataRoot $directory) /grant:r '*S-1-5-19:(OI)(CI)(M)' /T /Q|Out-Null
 if($LASTEXITCODE){throw "Unable to grant LOCAL SERVICE application access: $directory"}
}
[ordered]@{installed=$true;version=$version;install_root=$InstallRoot;data_root=$DataRoot;external_registration_required=@('Gateway credential and CA trust','Agent Arena Ticketing adapter and machine credential','Developer token file')}|ConvertTo-Json -Depth 4
