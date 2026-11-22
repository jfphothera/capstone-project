# Repair-HubGatewayUpdate.ps1 - run by FIX-GATEWAY-UPDATE.cmd on the admin hub, as administrator (2026-11-12).
# Why: on 2026-11-12 14:00 the hub's Gateway update 0.3.0 -> 0.4.0 stopped at "[Errno 1053] StartServiceW failed".
# Gateway 0.4.0's update switches back to 0.3.0 only when the new release starts and then fails its health check; an
# error from StartServiceW itself is not caught, so the service was left pointing at 0.4.0, stopped. The likely cause is
# the one of 2026-11-05: pip --target leaves the new release's files owner-only, so the service account cannot read them.
#   1. records the service, its Windows events, the deployment state, the Gateway logs and the release files' access;
#   2. resets the new release's files to inherit Users read (only where they do not), as the provisioner does;
#   3. runs the Gateway's own update once more, from the 0.4.0 package on this stick;
#   4. if 0.4.0 still does not run: switches back to 0.3.0 with the Gateway's own switch-back code, so Capstone works.
# It deletes nothing; the ledger is never touched except by the Gateway's own update. Report: reports\<computer>.
param([switch]$DryRun)
$ErrorActionPreference = 'Continue'
$stick = $PSScriptRoot; while ($stick -and -not @(Get-ChildItem -LiteralPath $stick -Directory -Filter 'AGENT-ARENA-LAB-*' -ErrorAction SilentlyContinue).Count) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
if (-not $stick) { Write-Host '  The provisioner release folder was not found on this stick: nothing was changed.' -ForegroundColor Red; exit 2 }
$rel = (Get-ChildItem -LiteralPath $stick -Directory -Filter 'AGENT-ARENA-LAB-*' | Sort-Object Name | Select-Object -Last 1).FullName
foreach ($m in 'Lab.Core', 'Lab.Gateway') { Import-Module (Join-Path $rel "engine\modules\$m.psm1") -Force -DisableNameChecking 3>$null }
$out = Join-Path $stick ('reports\{0}\gateway-fix-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date)); New-Item -ItemType Directory -Path $out -Force | Out-Null
$log = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }
function Save([string]$name, $v) { [IO.File]::WriteAllText((Join-Path $out $name), [string]($v | Out-String -Width 300), (New-Object Text.UTF8Encoding $false)) }
function Finish([int]$code) { [IO.File]::WriteAllText((Join-Path $out 'FIX-LOG.txt'), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false)); exit $code }
$py = 'C:\Program Files\Python312\python.exe'; $svcName = 'InferenceGateway'; $install = 'C:\Program Files\InferenceGateway'
$settings = 'C:\ProgramData\AgentArenaLab\gateway\deployment-settings.json'

Write-Host ''; Write-Host '  Agent Arena lab hub: finish the Inference Gateway update (or return to 0.3.0)' -ForegroundColor Green
Say ("computer {0}, user {1}{2}" -f $env:COMPUTERNAME, $env:USERNAME, $(if ($DryRun) { ' - DRY RUN: nothing is changed' } else { '' }))
foreach ($f in $py, $settings) { if (-not (Test-Path -LiteralPath $f)) { Say "missing: $f (this is not the hub, or the Gateway was never installed). Nothing was changed." 'Red'; Finish 2 } }

# 1. What Windows and the Gateway say now, before anything changes.
$svc = Get-LabGatewayService -Name $svcName
Say ("service {0}: {1}, release in its command line: {2}" -f $svcName, $(if ($svc.Present) { $svc.State } else { 'not installed' }), $(if ($svc.Release) { $svc.Release } else { '?' })) 'Cyan'
Save 'service-config.txt' @((& sc.exe qc $svcName), '', (& sc.exe queryex $svcName), '', (Get-CimInstance Win32_Service -Filter "Name='$svcName'" | Format-List Name, State, StartMode, StartName, PathName, ExitCode, ServiceSpecificExitCode))
try { $ev = Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Service Control Manager'; StartTime = (Get-Date).AddHours(-8) } -ErrorAction Stop | Where-Object { $_.Message -match 'Inference' } | Select-Object -First 40
      Save 'service-events.txt' ($ev | Format-List TimeCreated, Id, LevelDisplayName, Message); Say ("Windows service events for the Gateway (8 h): {0}; newest: {1}" -f @($ev).Count, $(if ($ev) { '{0:HH:mm} id {1}: {2}' -f $ev[0].TimeCreated, $ev[0].Id, (($ev[0].Message -split "`r?`n")[0]) } else { 'none' })) }
catch { Say ('service events not read: ' + $_.Exception.Message) 'Yellow' }
$state = 'C:\ProgramData\InferenceGateway\deployment\deployment-state.json'; if (Test-Path -LiteralPath $state) { Copy-Item -LiteralPath $state -Destination (Join-Path $out 'deployment-state.json'); try { $st = Get-Content -Raw -LiteralPath $state | ConvertFrom-Json; Say ("Gateway deployment record: current {0}, previous {1}" -f $st.current, $(if ($st.previous) { $st.previous } else { '-' })) } catch { } }
foreach ($lf in @(Get-ChildItem -LiteralPath 'C:\ProgramData\InferenceGateway\logs' -File -ErrorAction SilentlyContinue)) { Save ('gateway-log-' + $lf.Name + '.txt') (Get-Content -LiteralPath $lf.FullName -Tail 80 -ErrorAction SilentlyContinue) }
$new = Join-Path $install 'releases\0.4.0'
if (Test-Path -LiteralPath $new) {
    $files = @(Get-ChildItem -LiteralPath $new -Recurse -File -Force -ErrorAction SilentlyContinue); $blocked = @($files | Where-Object { -not (Test-LabUsersCanRead $_.FullName) })
    Save 'release-0.4.0-access.txt' @(("{0} files, {1} not readable by Users" -f $files.Count, $blocked.Count), '', ($blocked | Select-Object -First 20 | ForEach-Object FullName), '', (& icacls.exe (Join-Path $new 'inference_gateway\service_launcher.py')))
    Say ("release 0.4.0: {0} files, {1} not readable by Users (the service account reads them as a member of Users)" -f $files.Count, $blocked.Count) $(if ($blocked.Count) { 'Yellow' } else { 'Gray' })
} else { Say 'release 0.4.0 is not installed yet (the update installs it)' }
if ($DryRun) { Say 'RESULT dry run (nothing changed)' 'Yellow'; Finish 0 }

# 2. The release files readable by the service account.
try { $acl = Repair-LabGatewayReleaseAcl -InstallRoot $install; Say ('release files: ' + $acl.Detail) 'Green' } catch { Say ('release files could not be repaired: ' + $_.Exception.Message) 'Red' }

# 3. The Gateway's own update, once more, from this stick's 0.4.0 package (checked against its .sha256 first).
$zip = @(Get-ChildItem -LiteralPath (Join-Path $rel 'packages\gateway') -Filter 'inference-gateway-*.zip' -ErrorAction SilentlyContinue) | Select-Object -First 1
$pkg = Expand-LabGatewayPackage -Zip $zip.FullName -Staging (Join-Path $env:TEMP ('gw-fix-{0:yyyyMMddHHmmss}' -f (Get-Date)))
Say ("package {0}: archive matches its .sha256" -f $zip.Name)
$up = & $py -I (Join-Path $pkg.Root 'bin\gateway-deploy.py') update --settings $settings 2>&1; $upText = ($up | Out-String).Trim(); Save 'gateway-update.json' $upText
$uj = $null; try { $uj = ($upText -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1) | ConvertFrom-Json } catch { }
$v = Get-LabGatewayUpdateVerdict -Json $uj
if ($v.Ok) {
    Say ('UPDATED: ' + $v.Detail) 'Green'
    if ($v.Held.Count) { Say ('Gateway 0.4.0 tests these heads itself from 1 minute after the update (they return within about 25 minutes): ' + ($v.Held -join ', ')) }
} else {
    Say ('the update did not finish: ' + $(if ($uj -and $uj.error) { $uj.error } else { $upText.Substring(0, [Math]::Min(300, $upText.Length)) })) 'Red'
    # 4. Back to 0.3.0 with the Gateway's own switch-back (Installer._switch: drains, expires leftover leases, points the
    #    service at the old release, starts it and waits until it runs), then the same record the update writes.
    $back = @'
import json, os, sys
root = sys.argv[1]; sys.path.insert(0, [os.path.join(root, 'wheel', n) for n in os.listdir(os.path.join(root, 'wheel')) if n.endswith('.whl')][0])
os.environ['INFERENCE_GATEWAY_PACKAGE'] = root
from inference_gateway import deployment as d
inst = d.Installer(d.load_settings(sys.argv[2]), d.WindowsSystem(), package=d.package_root())
ok = inst._switch('0.3.0')
if ok: inst._record(action='update_failed_rolled_back', current='0.3.0', previous='0.4.0')
print(json.dumps({'ok': bool(ok), 'running': '0.3.0' if ok else None}))
'@
    $bf = Join-Path $out 'switch-back.py'; [IO.File]::WriteAllText($bf, $back, (New-Object Text.UTF8Encoding $false))
    $b = (& $py -I $bf $pkg.Root $settings 2>&1 | Out-String).Trim(); Save 'switch-back.txt' $b
    Say ('switched back to 0.3.0: ' + $b) $(if ($b -match '"ok": true') { 'Yellow' } else { 'Red' })
}

# 5. What runs now.
Start-Sleep 5
$svc = Get-LabGatewayService -Name $svcName
$listen = @(Get-NetTCPConnection -State Listen -LocalPort 8443 -ErrorAction SilentlyContinue)
Say ("now: service {0}, release {1}, listening on 8443: {2}" -f $svc.State, $svc.Release, $(if ($listen.Count) { 'yes' } else { 'NO' })) 'Cyan'
Write-Host ''
if ($svc.State -eq 'Running' -and $listen.Count -and $svc.Release -eq '0.4.0') { Say 'RESULT success: Gateway 0.4.0 runs. Next: START-LAB-PROVISIONER > 3 > 2 again (APPLY, QUALIFY).' 'Green'; Finish 0 }
if ($svc.State -eq 'Running' -and $listen.Count) { Say ('RESULT back on {0}: the Gateway runs and Capstone works; 0.4.0 waits for the cause in this report. Bring the stick back.' -f $svc.Release) 'Yellow'; Finish 1 }
Say 'RESULT failed: the Gateway is not running. Bring the stick back now.' 'Red'; Finish 1
