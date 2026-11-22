# Rehearses a Capstone Windows release with the lab provisioner's own calls, in Windows PowerShell 5.1,
# without touching this laptop's Program Files, ProgramData or network settings:
#   1. Install-Capstone.ps1 into scratch roots (its own installer: runtime venv, release, current.json);
#   2. Configure-Capstone.ps1 with Capstone's own two bootstrap templates, pointed at scratch copies, with
#      DUMMY token files (never real tokens) and the Gateway's public CA; listening on 192.0.2.1:<port>;
#   3. Start-Capstone.ps1 exactly as the startup task runs it, then: still running after 60 s, /api/health
#      answers, uvicorn's stderr lines in the error log (rc.2 died in 0.6 s on the first one);
#   4. the Gateway verifier from the installed release under -I: it must import its client (rc.2 could not).
# The dummy tokens cannot authenticate, so a Gateway authentication failure is expected and reported as such.
param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$Work, [int]$Port = 18000, [string]$Ca = 'F:\gateway-ca\inference-gateway-ca.pem')
$ErrorActionPreference = 'Stop'
$results = New-Object Collections.ArrayList
function Note([string]$Check, [bool]$Ok, [string]$Detail) { [void]$results.Add([pscustomobject]@{ Result = $(if ($Ok) { 'PASS' } else { 'FAIL' }); Check = $Check; Detail = $Detail }); Write-Host ('{0}  {1}: {2}' -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Check, $Detail) }

if (Test-Path $Work) { throw "$Work exists: use a new folder" }
$pkg = Join-Path $Work 'package'; $inst = Join-Path $Work 'install'; $data = Join-Path $Work 'data'; $gw = Join-Path $Work 'gateway'
New-Item -ItemType Directory $gw -Force | Out-Null
# The provisioner's own unpack, with its path check against the folder a real laptop uses
# (C:\ProgramData\AgentArenaLab\cs). The 1.4.4 rehearsal unpacked into a short scratch folder, so it never saw
# that the run folder was too deep (DESKTOP-REDACT11, 2026-11-07).
Import-Module (Join-Path $PSScriptRoot '..\..\engine\modules\Lab.Core.psm1') -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zr = [IO.Compression.ZipFile]::OpenRead($Zip); try { $names = @($zr.Entries | ForEach-Object { $_.FullName }) } finally { $zr.Dispose() }
$fit = Test-LabZipFits -Destination 'C:\ProgramData\AgentArenaLab\cs' -Entries $names
Note 'fits Windows'' path limit where the laptop unpacks it' $fit.Ok ('longest {0} of 259 characters at C:\ProgramData\AgentArenaLab\cs' -f $fit.Longest)
if ($pkg.Length -gt 'C:\ProgramData\AgentArenaLab\cs'.Length) { Note 'scratch folder no shorter than the real one' $true "$pkg is longer: a stricter test" }
$null = Expand-LabZipChecked -Zip $Zip -Destination $pkg
$manifest = Get-Content (Join-Path $pkg 'package-manifest.json') -Raw | ConvertFrom-Json
Note 'package' ($manifest.format -eq 'capstone-windows-release-v1') ("version $($manifest.version), $(@($manifest.files).Count) files, sha256 " + (Get-FileHash $Zip -Algorithm SHA256).Hash.Substring(0, 12).ToLower())

# 1. Install
$t0 = Get-Date
$out = & (Join-Path $pkg 'tools\Install-Capstone.ps1') -InstallRoot $inst -DataRoot $data 2>&1 | Out-String
Note 'Install-Capstone.ps1' ($out -match '"installed"\s*:\s*true') ('{0:N0} s; {1}' -f ((Get-Date) - $t0).TotalSeconds, (($out -split "`r?`n" | Where-Object { $_ -match 'installed|version' } | Select-Object -Last 2) -join ' '))

# 2. Configure with Capstone's own templates, re-pointed at scratch files
$cfgDir = Join-Path $inst ('releases\{0}\application\config' -f $manifest.version)
Copy-Item $Ca (Join-Path $gw 'inference-gateway-ca.pem')
$boots = @{}
foreach ($pair in @(@('capstone', 'inference-gateway.client.template.json'), @('capstone-research', 'inference-gateway.research-client.template.json'))) {
    $b = Get-Content (Join-Path $cfgDir $pair[1]) -Raw | ConvertFrom-Json
    $tok = Join-Path $gw ($pair[0] + '.token'); Set-Content -LiteralPath $tok -Value ('dummy-not-a-real-token-' + [guid]::NewGuid().ToString('N')) -Encoding ASCII
    $b.token_file = $tok; $b.ca_file = (Join-Path $gw 'inference-gateway-ca.pem')
    # Without a byte-order mark, like Capstone's own templates: Configure copies the file as it is, and Python's
    # JSON reader refuses a BOM (the first run wrote one and the verifier stopped on JSONDecodeError).
    $bf = Join-Path $gw ($pair[0] + '.bootstrap.json'); [IO.File]::WriteAllText($bf, ($b | ConvertTo-Json), (New-Object Text.UTF8Encoding $false)); $boots[$pair[0]] = $bf
}
$dev = Join-Path $gw 'developer.token'; Set-Content -LiteralPath $dev -Value ('dummy-developer-' + [guid]::NewGuid().ToString('N')) -Encoding ASCII
$out = & (Join-Path $inst 'tools\Configure-Capstone.ps1') -OperationalGatewayBootstrap $boots['capstone'] -ResearchGatewayBootstrap $boots['capstone-research'] -DeveloperTokenFile $dev -DataRoot $data -ListenHost '192.0.2.1' -ListenPort $Port 2>&1 | Out-String
Note 'Configure-Capstone.ps1' ($out -match '"configured"\s*:\s*true') ((($out -split "`r?`n") | Where-Object { $_ -match 'configured|mode' } | Select-Object -Last 2) -join ' ')
# Configure restricts the token files to SYSTEM, Administrators and LOCAL SERVICE (as it must). This rehearsal
# runs Capstone as the signed-in, unelevated user, so it gives that user read access back on the scratch copies.
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($f in @(Get-ChildItem $gw -File) + @(Get-ChildItem (Join-Path $data 'gateway') -File -ErrorAction SilentlyContinue)) { & icacls.exe $f.FullName /grant "*${me}:(R)" /Q | Out-Null }

# 3. Start exactly as the startup task does, in Windows PowerShell 5.1
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$p = Start-Process $ps -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $inst 'tools\Start-Capstone.ps1') + '"'), '-InstallRoot', ('"' + $inst + '"'), '-DataRoot', ('"' + $data + '"') -PassThru -WindowStyle Hidden
$health = $null
for ($i = 0; $i -lt 60; $i++) { Start-Sleep 1; if ($p.HasExited) { break }; try { $health = Invoke-RestMethod "http://192.0.2.1:$Port/api/health" -TimeoutSec 3 } catch { } }
$alive = -not $p.HasExited
try { $health = Invoke-RestMethod "http://192.0.2.1:$Port/api/health" -TimeoutSec 5 } catch { }
$errLog = Join-Path $data 'logs\capstone.error.log'
$errText = $(if (Test-Path $errLog) { Get-Content $errLog -Raw } else { '' })
Note 'Start-Capstone.ps1 still running after 60 s' $alive $(if ($alive) { "process $($p.Id) running" } else { "exited with $($p.ExitCode)" })
Note '/api/health answers' ([bool]$health) $(if ($health) { ($health | ConvertTo-Json -Compress -Depth 3).Substring(0, [Math]::Min(300, ($health | ConvertTo-Json -Compress -Depth 3).Length)) } else { 'no answer' })
Note 'uvicorn stderr went to the error log (and did not stop the script)' ($errText -match 'Uvicorn running|Started server process|Application startup') ((($errText -split "`r?`n") | Where-Object { $_ } | Select-Object -First 2) -join ' | ')

# 4. The Gateway verifier from the installed release, isolated
$py = Join-Path $inst 'runtime\Scripts\python.exe'
$verifier = Join-Path $inst ('releases\{0}\application\scripts\verify_gateway_clients.py' -f $manifest.version)
$env:CAPSTONE_GATEWAY_CLIENT_CONFIG = Join-Path $data 'config\inference-gateway.client.json'
$env:CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG = Join-Path $data 'config\inference-gateway.research-client.json'
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$vout = & $py -I $verifier --operational $env:CAPSTONE_GATEWAY_CLIENT_CONFIG --research $env:CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG 2>&1 | Out-String
$ErrorActionPreference = $prev
Note 'Gateway verifier imports its client (no ModuleNotFoundError)' ($vout -notmatch 'ModuleNotFoundError') ((($vout -split "`r?`n") | Where-Object { $_ } | Select-Object -Last 3) -join ' | ')
# The dummy tokens reach the real Gateway over TLS with its CA and are refused there: an HTTP error, not a
# connection, certificate or file-format error.
Note 'the verifier reaches the Gateway and is refused (dummy tokens)' ($vout -match '"error_class":\s*"HTTPError"') ((($vout -split "`r?`n") | Where-Object { $_ -match 'status|error_class' }) -join ' ')

if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*$inst*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$results | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Write-Host
$results | ConvertTo-Json | Set-Content (Join-Path $Work 'rehearsal-results.json') -Encoding UTF8
