# Rehearses Capstone's QMS or ERP service (Capstone 6.0.0-rc.4) with the lab provisioner's own functions, in Windows
# PowerShell 5.1, without touching this laptop's Program Files, ProgramData, firewall or scheduled tasks:
#   1. the approved archive (SHA-256) unpacked by Expand-LabServicePackage, and its path checked against the folder a
#      head really uses (C:\ProgramData\AgentArenaLab\sv-<service>);
#   2. its own Install script into scratch roots; LOCAL SERVICE's access to its data checked;
#   3. Set-LabServiceToken: a new token, its own Configure script, the token and client file on a scratch "stick";
#   4. its own Start script, as its startup task runs it, but on 127.0.0.1 and as this user; its health answer;
#   5. Receive-LabServiceHandoff on a scratch "laptop": the stick copies are deleted; the service accepts the token,
#      refuses another one, and Capstone's own remote_services client reads through it.
# The firewall rule and the LOCAL SERVICE task need an administrator: they are covered by the unit tests only.
param([ValidateSet('qms', 'erp')][string]$Service = 'qms', [Parameter(Mandatory)][string]$Work, [int]$Port = 0,
      [string]$Packages = (Join-Path $env:USERPROFILE 'PycharmProjects\Capstone\deploy\services\dist'),
      [string]$CapstoneSrc = (Join-Path $env:USERPROFILE 'PycharmProjects\Capstone\src'))
$ErrorActionPreference = 'Stop'
$results = New-Object Collections.ArrayList
function Note([string]$Check, [bool]$Ok, [string]$Detail) { [void]$results.Add([pscustomobject]@{ Result = $(if ($Ok) { 'PASS' } else { 'FAIL' }); Check = $Check; Detail = $Detail }); Write-Host ('{0}  {1}: {2}' -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Check, $Detail) }
if (Test-Path $Work) { throw "$Work exists: use a new folder" }
$lab = Resolve-Path (Join-Path $PSScriptRoot '..\..')
foreach ($m in 'Lab.Core', 'Lab.Gateway', 'Lab.Apps', 'Lab.Services') { Import-Module (Join-Path $lab "engine\modules\$m.psm1") -Force -Global -DisableNameChecking }
$cfg = Get-LabConfig -Path (Join-Path $lab 'config\lab-network.json')
# The provisioner runs elevated on a head, so its SYSTEM-and-Administrators folders are writable to it. This rehearsal
# is not elevated: the same grants plus this user (only in this rehearsal; the provisioner's function is unchanged).
$meSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
function global:Set-LabFolderAcl { param([string]$Path, [string[]]$Grants) Lab.Gateway\Set-LabFolderAcl -Path $Path -Grants (@($Grants) + "*${meSid}:(OI)(CI)F") }
$real = Get-LabServiceSpecByName -Config $cfg -Name $Service
if (-not $Port) { $Port = $real.Port + 10000 }
# The real spec, moved to scratch roots and 127.0.0.1 (its node, product, probe and approved archive unchanged).
$spec = $real.PSObject.Copy()
$spec.Head = '127.0.0.1'; $spec.Port = $Port
$spec.InstallRoot = Join-Path $Work ('pf\' + $real.Product); $spec.DataRoot = Join-Path $Work ('pd\' + $real.Product); $spec.TaskName = 'none-' + [guid]::NewGuid().ToString('N')
$spec.LaptopToken = Join-Path $Work ('laptop\services\{0}.token' -f $Service); $spec.LaptopClient = Join-Path $Work ('laptop\services\{0}-capstone-client.json' -f $Service)
$stick = Join-Path $Work 'stick'; New-Item -ItemType Directory -Path $stick -Force | Out-Null
$run = New-LabRun -Role 'rehearsal' -Root (Join-Path $Work 'root')

# 1. Package
$zip = Find-LabServicePackage -Folder $Packages -Spec $real
Note 'approved package present' ([bool]$zip) $(if ($zip) { $zip.FullName } else { "none in $Packages" })
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zr = [IO.Compression.ZipFile]::OpenRead($zip.FullName); try { $names = @($zr.Entries | ForEach-Object { $_.FullName }) } finally { $zr.Dispose() }
$fit = Test-LabZipFits -Destination ('C:\ProgramData\AgentArenaLab\sv-' + $Service) -Entries $names
Note 'fits Windows'' path limit where a head unpacks it' $fit.Ok ('longest {0} of 259 characters' -f $fit.Longest)
$bs = @($names | Where-Object { $_ -match '\\' }).Count
$pkg = Expand-LabServicePackage -Run $run -Zip $zip.FullName -Spec $real
Note 'unpacked by Windows PowerShell 5.1 and every file matches its manifest' ($pkg.Files -gt 0 -and (Test-Path (Join-Path $pkg.Root 'tools'))) ('{0} {1}: {2} files ({3} archive entries use \ as the folder separator)' -f $real.Product, $pkg.Version, $pkg.Files, $bs)

# 2. Install
$log = Join-Path $run.Folder 'service.log'
$r = Invoke-LabExternalScript -Script (Join-Path $pkg.Root ('tools\Install-{0}.ps1' -f $real.Product)) -Arguments @('-InstallRoot', $spec.InstallRoot, '-DataRoot', $spec.DataRoot) -LogFile $log
Note ("Install-{0}.ps1" -f $real.Product) ($r.ExitCode -eq 0 -and $r.Output -match '"installed"\s*:\s*true') ('exit {0}; {1}' -f $r.ExitCode, (($r.Output -split "`r?`n" | Where-Object { $_ -match 'version|installed|error|Exception' } | Select-Object -First 2) -join ' '))
$st = Get-LabServiceState -Spec $spec
Note 'installed release recorded' ($st.Installed -and $st.Version -eq $real.Approved.version) ("current.json: $($st.Version)")
$sample = Get-ChildItem (Join-Path $spec.DataRoot 'data') -File -Recurse | Select-Object -First 1
$ls = @((Get-Acl -LiteralPath $sample.FullName).Access | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-19' -and $_.AccessControlType -eq 'Allow' -and ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadData) })
Note 'LOCAL SERVICE can read its data files' ($ls.Count -gt 0) ("{0}: {1}" -f $sample.Name, (($ls | ForEach-Object { $_.FileSystemRights }) -join ', '))

# 3. Token (the provisioner's own function: Configure, then the stick)
$t = Set-LabServiceToken -Run $run -Spec $spec -UsbRoot $stick -LogFile $log
$ho = Get-LabServiceHandoff -UsbRoot $stick -Name $Service
Note 'token and client file on the stick, nothing left in the run folder' ((Test-Path $ho.Token) -and (Test-Path $ho.Client) -and -not (Get-ChildItem (Join-Path $run.Root 'services') -File -ErrorAction SilentlyContinue)) ("fingerprint $($t.Fingerprint); " + (((Get-Content $ho.Client -Raw) -replace '\s+', ' ')))
$svcCfg = Get-Content (Join-Path $spec.DataRoot 'config\service.json') -Raw | ConvertFrom-Json
$sids = @((Get-Acl -LiteralPath $svcCfg.token_file).Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
Note "the service's token file: SYSTEM, Administrators and LOCAL SERVICE only" ((@($sids | Sort-Object -Unique) -join ',') -eq 'S-1-5-18,S-1-5-19,S-1-5-32-544') ($sids -join ', ')
# This rehearsal runs the service as the signed-in, unelevated user, so that user gets read access back on the scratch copy.
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $svcCfg.token_file /grant "*${me}:(R)" /Q | Out-Null

# 4. Start as the task does (Windows PowerShell 5.1), on 127.0.0.1
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$p = Start-Process $ps -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $spec.InstallRoot ('tools\Start-{0}.ps1' -f $real.Product)) + '"'), '-InstallRoot', ('"' + $spec.InstallRoot + '"'), '-DataRoot', ('"' + $spec.DataRoot + '"') -PassThru -WindowStyle Hidden
try {
    $ok = Wait-LabServiceHealth -Spec $spec -Seconds 30
    Note ("{0} answers its health check" -f $real.Product) $ok $(if ($ok) { "http://127.0.0.1:$Port/api/health" } else { Get-LabServiceLogTail -Spec $spec })
    $h = Invoke-RestMethod ("http://127.0.0.1:$Port/api/health") -TimeoutSec 5
    Note 'health names the service and its format' ($h.service -eq $Service -and $h.format -eq 'capstone-business-service/1') ($h | ConvertTo-Json -Compress)
    $code = 0; try { Invoke-WebRequest ("http://127.0.0.1:$Port" + $real.Probe) -UseBasicParsing -TimeoutSec 5 | Out-Null } catch { $code = [int]$_.Exception.Response.StatusCode }
    Note 'a data route without the token is refused' ($code -eq 401) "HTTP $code"

    # 5. The laptop's side
    New-Item -ItemType Directory -Path (Split-Path -Parent $spec.LaptopToken) -Force | Out-Null   # (created by the laptop step on a real laptop)
    Remove-Item (Split-Path -Parent $spec.LaptopToken) -Recurse -Force
    $fp = Receive-LabServiceHandoff -Run $run -Spec $spec -UsbRoot $stick
    Note 'the laptop took the token; the stick holds none any more' ($fp -eq $t.Fingerprint -and -not (Test-Path $ho.Token) -and -not (Test-Path $ho.Client) -and -not (Test-Path $ho.Folder)) ("fingerprint $fp; handoff folder removed: $(-not (Test-Path $ho.Folder))")
    $c = Get-LabServiceClientState -Spec $spec -UsbRoot $stick
    Note 'the service accepts the laptop''s token' ($c.Authorized -eq $true) $c.Detail
    $good = [IO.File]::ReadAllText($spec.LaptopToken)
    [IO.File]::WriteAllText($spec.LaptopToken, (New-LabServiceToken))
    $c2 = Get-LabServiceClientState -Spec $spec
    Note 'and refuses any other token' ($c2.Authorized -eq $false) $c2.Detail
    [IO.File]::WriteAllText($spec.LaptopToken, $good); $good = $null
    # Capstone's own client (remote_services.get), with the client file exactly as the laptop holds it.
    $env:CAPSTONE_QMS_SERVICE_CONFIG = $spec.LaptopClient; $env:CAPSTONE_ERP_SERVICE_CONFIG = $spec.LaptopClient
    $path = $(if ($Service -eq 'qms') { '/api/qms/files' } else { '/api/erp/orders' })
    $b = [IO.File]::ReadAllBytes($spec.LaptopClient)
    Note 'the laptop''s client file has no byte-order mark (Capstone''s Python refuses one)' (-not ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)) ('first byte 0x{0:X2}' -f $b[0])
    $py = "import sys,json;sys.path.insert(0,r'$CapstoneSrc');from capstone_rag import remote_services as m;r=m.get('$Service','$path');print(json.dumps({'keys':sorted(r),'rows':len(next(iter(r.values())))}))"
    $ErrorActionPreference = 'Continue'
    $out = & python -I -c $py 2>&1 | ForEach-Object { [string]$_ } | Out-String
    $ErrorActionPreference = 'Stop'
    Note "Capstone's remote_services client reads $path through the token" ($out -match '"rows":\s*[1-9]') ($out.Trim())
} finally {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($spec.InstallRoot, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
}
$fail = @($results | Where-Object { $_.Result -eq 'FAIL' }).Count
$results | ConvertTo-Json | Set-Content (Join-Path $Work 'rehearsal-results.json') -Encoding UTF8
Write-Host ''
Write-Host ('REHEARSAL {0}: {1} checks, {2} failed' -f $(if ($fail) { 'FAIL' } else { 'PASS' }), $results.Count, $fail)
if ($fail) { exit 1 }
