# Set-HubLabNetworkPrivate.ps1 - run by SET-HUB-LAB-NETWORK-PRIVATE.cmd on the admin hub (192.0.2.254), as administrator.
# Why (2026-11-11): the Inference Gateway's firewall rules for the Capstone laptop (InferenceGateway-HTTPS-8443, from its
# own deployment manifest) apply only when the lab cable's network is Private. The lab network has no router, so Windows
# calls it "Unidentified network" and set it back to Public when the hub restarted (06:43): the Capstone laptop's
# connections to 8443 then timed out. Arena's rule applies on any network type, so Arena kept working.
#   1. sets the lab cable's network to Private now;
#   2. keeps it Private: a scheduled task (SYSTEM, at start-up and every 5 minutes) sets it back if Windows turns it
#      Public again. The task's script lives in C:\ProgramData\AgentArenaLab\network (administrators only).
# It deletes nothing. Everything it did is written next to this file (network-fix-<date>.txt), with how to undo it.
param([string]$LabAddress = '', [switch]$DryRun)
# The hub's address comes from the release's own config\lab-network.json (engine\hub-tools\lab-network -> config).
if (-not $LabAddress) { try { $LabAddress = [string](Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\..\..\config\lab-network.json') | ConvertFrom-Json).hub } catch { } }
if (-not $LabAddress) { Write-Host '  The hub address is not known (config\lab-network.json not found): nothing was changed.' -ForegroundColor Red; exit 1 }
$ErrorActionPreference = 'Continue'
$log = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }
# The record goes to the stick's reports\<computer> (the release folder is checksummed); next to this file otherwise.
$root = $PSScriptRoot; while ($root -and -not (Test-Path -LiteralPath (Join-Path $root 'reports'))) { $up = Split-Path -Parent $root; if (-not $up -or $up -eq $root) { $root = $null } else { $root = $up } }
$recordDir = if ($root) { Join-Path $root "reports\$env:COMPUTERNAME" } else { $PSScriptRoot }; New-Item -ItemType Directory -Path $recordDir -Force -ErrorAction SilentlyContinue | Out-Null
$record = Join-Path $recordDir ('network-fix-{0}-{1:yyyyMMdd-HHmmss}.txt' -f $env:COMPUTERNAME, (Get-Date))
function Finish([int]$code) { [IO.File]::WriteAllText($record, ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false)); exit $code }
$taskName = 'Agent Arena Lab - keep the lab network Private'
$keepDir = Join-Path $env:ProgramData 'AgentArenaLab\network'
$keepFile = Join-Path $keepDir 'Keep-LabNetworkPrivate.ps1'

Write-Host ''
Write-Host "  Agent Arena lab hub: make the lab cable's network Private, so the Inference Gateway's firewall rule applies" -ForegroundColor Green
Say ("computer {0}, user {1}{2}" -f $env:COMPUTERNAME, $env:USERNAME, $(if ($DryRun) { ' - DRY RUN: nothing is changed' } else { '' }))

$ip = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $LabAddress }) | Select-Object -First 1
if (-not $ip) { Say "This computer does not have the address $LabAddress (it is not the hub, or its lab cable is out). Nothing was changed." 'Red'; Finish 1 }
$p = Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue
Say ("lab cable: adapter '{0}' {1}, network '{2}', type now: {3}" -f $ip.InterfaceAlias, $LabAddress, $p.Name, $p.NetworkCategory) 'Cyan'
$rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'InferenceGateway-HTTPS-8443*' })
foreach ($r in $rules) { Say ("Gateway firewall rule '{0}': enabled {1}, applies on: {2}" -f $r.DisplayName, $r.Enabled, $r.Profile) }
if (-not $rules.Count) { Say 'no Inference Gateway 8443 firewall rule on this computer' 'Yellow' }

# 1. Private now.
if ([string]$p.NetworkCategory -eq 'Private') { Say 'the lab network is already Private' 'Green' }
elseif ($DryRun) { Say 'would set the lab network to Private now' 'Yellow' }
else {
    try { Set-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -NetworkCategory Private -ErrorAction Stop; Say ("lab network set to Private (undo: Set-NetConnectionProfile -InterfaceAlias '{0}' -NetworkCategory Public)" -f $ip.InterfaceAlias) 'Green' }
    catch { Say ('could not set the lab network to Private: ' + $_.Exception.Message) 'Red' }
}

# 2. Keep it Private after a restart or a cable re-plug.
$keeper = @"
# Keep-LabNetworkPrivate.ps1 (Agent Arena Lab, $(Get-Date -Format 'yyyy-MM-dd')): run by the task '$taskName' as SYSTEM.
# Sets the network of the adapter holding $LabAddress back to Private when Windows has made it Public.
`$ip = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { `$_.IPAddress -eq '$LabAddress' }) | Select-Object -First 1
if (-not `$ip) { return }
`$p = Get-NetConnectionProfile -InterfaceIndex `$ip.InterfaceIndex -ErrorAction SilentlyContinue
if (`$p -and [string]`$p.NetworkCategory -eq 'Public') {
    Set-NetConnectionProfile -InterfaceIndex `$ip.InterfaceIndex -NetworkCategory Private -ErrorAction SilentlyContinue
    Add-Content -LiteralPath (Join-Path `$PSScriptRoot 'keep-private.log') -Value ('{0:yyyy-MM-dd HH:mm:ss} {1} was Public: set to Private' -f (Get-Date), `$ip.InterfaceAlias)
}
"@
if ($DryRun) { Say "would write $keepFile and register the task '$taskName' (SYSTEM, at start-up and every 5 minutes)" 'Yellow' }
else {
    try {
        New-Item -ItemType Directory -Path $keepDir -Force -ErrorAction Stop | Out-Null
        # Administrators and SYSTEM only may change what SYSTEM runs; everyone else may read it.
        & icacls.exe $keepDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
        [IO.File]::WriteAllText($keepFile, $keeper, (New-Object Text.UTF8Encoding $true))
        $act = New-ScheduledTaskAction -Execute "$env:windir\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $keepFile)
        $trg = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)))
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
        Register-ScheduledTask -TaskName $taskName -Action $act -Trigger $trg -Settings $set -User 'SYSTEM' -RunLevel Highest -Force -ErrorAction Stop | Out-Null
        Say ("kept Private from now on: task '{0}' (SYSTEM, at start-up and every 5 minutes) runs {1} (undo: Unregister-ScheduledTask -TaskName '{0}' -Confirm:`$false)" -f $taskName, $keepFile) 'Green'
    } catch { Say ('could not set up the task that keeps it Private: ' + $_.Exception.Message) 'Red' }
}

# Check.
$now = [string](Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue).NetworkCategory
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$svc = Get-Service -Name InferenceGateway -ErrorAction SilentlyContinue
$listen = @(Get-NetTCPConnection -State Listen -LocalPort 8443 -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq $LabAddress })
Say ("now: lab network {0}; keep-Private task {1}; Inference Gateway service {2}; listening on {3}:8443: {4}" -f $now, $(if ($task) { 'present' } else { 'MISSING' }), $(if ($svc) { $svc.Status } else { 'not found' }), $LabAddress, $(if ($listen.Count) { 'yes' } else { 'NO' })) 'Cyan'
$ok = ($now -eq 'Private') -and $task -and $svc -and ([string]$svc.Status -eq 'Running') -and $listen.Count
Write-Host ''
if ($DryRun) { Say 'RESULT dry run (nothing changed)' 'Yellow'; Finish 0 }
if ($ok) { Say 'RESULT success: the Capstone laptop can reach the Gateway again. Next: the Capstone laptop, START-LAB-PROVISIONER > 4.' 'Green'; Finish 0 }
Say 'RESULT failed: see the lines above (in red). Bring the stick back.' 'Red'; Finish 1
