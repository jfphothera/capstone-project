# 2-INSTALL-AND-VERIFY: integrity check, discovery, guarded GUI install, then proof.
# Works for a fresh install, a reinstall after uninstall, and an upgrade in place, on any
# registered head or orchestrator display: this computer's identity comes from the registry.
. (Join-Path $PSScriptRoot 'HeadCommon.ps1')
$usb = $UsbRoot
$pkg = Join-Path $usb ("package\" + $UsbProfile.package)
$ev = Join-Path $usb ("evidence\" + $env:COMPUTERNAME + "\" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-install")
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript (Join-Path $ev 'transcript.txt') | Out-Null
$results = New-Object System.Collections.ArrayList
function Note([string]$Check, [bool]$Ok, [string]$Detail) {
    $word = $(if ($Ok) { 'PASS' } else { 'FAIL' })
    [void]$results.Add([pscustomobject]@{ Result = $word; Check = $Check; Detail = $Detail })
    Write-Host ("  {0}  {1}: {2}" -f $word, $Check, $Detail) -ForegroundColor $(if ($Ok) { 'Green' } else { 'Red' })
}
function Info([string]$Text) { Write-Host "  INFO  $Text"; [void]$results.Add([pscustomobject]@{ Result = 'INFO'; Check = ''; Detail = $Text }) }
function Finish([string]$Headline) {
    $results | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Set-Content (Join-Path $ev 'SUMMARY.txt') -Encoding UTF8
    Add-Content (Join-Path $ev 'SUMMARY.txt') "`r`n$Headline`r`nToolset $($UsbProfile.toolset_version) ran from $usb (package $($UsbProfile.package)) on $env:COMPUTERNAME as $env:USERNAME"
    $log = Join-Path $env:LOCALAPPDATA 'AgentArena\logs\agent-arena.log'
    if (Test-Path $log) { Copy-Item $log (Join-Path $ev 'agent-arena.log') }
    Write-Host "`n  $Headline`n  Evidence: $ev`n" -ForegroundColor Cyan
    Stop-Transcript | Out-Null
}

Write-Banner 'INSTALL AND VERIFY'

# 0. One toolset, one package, one version (field defect D1: a 2.0.0 copy ran from a 2.0.1 stick).
$pi = Get-PackageInfo
$folderVersion = $(if ((Split-Path $usb -Leaf) -match '^AGENT-ARENA-(\d+\.\d+\.\d+)$') { $Matches[1] } else { $null })
$exeVersion = $null
if ($pi.exists) { $exeVersion = ((& (Join-Path $pi.folder 'app\AgentArena.exe') --self-test --profile-root (Join-Path $env:TEMP 'agent-arena-version-probe') | Out-String) -split "`r?`n" | Where-Object { $_ -match '^version ' }) -replace '^version ', '' }
$versions = "folder $folderVersion, toolset $($UsbProfile.toolset_version), package $($UsbProfile.version), manifest $($pi.manifest_version), program $exeVersion"
Note 'One version on this USB' ($folderVersion -and (@($folderVersion, $UsbProfile.version, $pi.manifest_version, $exeVersion) | Where-Object { $_ -ne $UsbProfile.toolset_version }).Count -eq 0) $versions
if (@($results | Where-Object Result -eq 'FAIL').Count) { Finish 'STOPPED: this USB mixes versions or its package is missing. Re-run PREPARE-USB on the coordinator. Nothing was installed.'; return }

# 1. The package on this USB is exactly what was built.
$bad = @()
foreach ($line in Get-Content (Join-Path $pkg 'SHA256SUMS.txt')) {
    if ($line -match '^([0-9A-F]{64})\s+(.+)$') {
        $f = Join-Path $pkg $Matches[2]
        if (-not (Test-Path -LiteralPath $f) -or (Get-FileHash -LiteralPath $f).Hash -ne $Matches[1]) { $bad += $Matches[2] }
    }
}
Note 'Package integrity' ($bad.Count -eq 0) $(if ($bad.Count) { 'mismatch: ' + ($bad -join ', ') } else { 'all files match SHA256SUMS.txt' })
if ($bad.Count) { Finish 'STOPPED: the USB copy is damaged. Nothing was installed.'; return }

# 2. Read-only discovery and gates. Nothing changes until all gates pass.
$before = Get-Discovery 'before'
Save-Discovery $before $ev | Out-Null
if ($before.uninstall_verdict) { Note 'Previous uninstall left nothing behind' ([bool]$before.uninstall_verdict.clean) $before.uninstall_verdict.detail }
$installedBefore = $before.agent_arena.current_version
if ($installedBefore -and (Compare-Version $installedBefore $UsbProfile.version) -gt 0) {
    Write-Host "  WARNING  Agent Arena $installedBefore is installed, which is NEWER than this USB's $($UsbProfile.version)." -ForegroundColor Yellow
    if ((Read-Host '  Type OLDER to install the older version anyway, or press Enter to stop') -ne 'OLDER') { Note 'Installed version' $false "a newer $installedBefore is installed; not downgraded"; Finish 'STOPPED: a newer version is already installed. Nothing was changed.'; return }
    Info "Operator chose to replace $installedBefore with the older $($UsbProfile.version)"
}
elseif ($installedBefore) { Info "Existing install $installedBefore will be upgraded or repaired in place to $($UsbProfile.version)" }
$me = Select-Identity $before.identity -Ask
$displayId = $(if ($me) { $me.display_id } else { $null })
Note 'Display identity' ([bool]$me) $(if ($me) { "$displayId ($($me.role), $($me.address)) from the $($me.source)" } else { "none: $(@($before.identity.notes) -join ' ')" })
Note 'Event service reachable' ($before.coordinator.verdict -eq 'ok') ("$($before.coordinator.event_service) verdict=$($before.coordinator.verdict) service_id=$($before.coordinator.health.service_id) attempts=$($before.coordinator.attempts)")
$codeFile = $(if ($displayId) { Join-Path $usb ("enroll\" + $displayId + ".code.txt") } else { Join-Path $usb 'enroll\(no identity).code.txt' })
Note 'WebView2 Runtime' ([bool]$before.webview2) ([string]$before.webview2)
Note '.NET Framework 4.8' ([int]$before.dotnet_release -ge 528040) ("release $($before.dotnet_release)")
$haveCredential = [bool]$before.agent_arena.credential_exists
Note 'Enrollment' ($haveCredential -or (Test-Path $codeFile)) $(if ($haveCredential) { 'existing credential will be reused (no code needed)' } elseif (Test-Path $codeFile) { 'no credential; the one-time code on this USB will be pre-filled' } else { 'no credential and no code on this USB' })
if (@($results | Where-Object Result -eq 'FAIL').Count) { Finish 'STOPPED before installing: fix the FAIL lines above. Nothing on this computer was changed.'; return }

# 3. The visible installer does the work. This script continues as soon as setup reports
#    its result, so the installer window may stay open (its Launch button is handy).
$resultFile = Join-Path $env:LOCALAPPDATA 'AgentArena\last-setup-result.json'
$started = Get-Date
$setupArgs = @('--install')
if (-not $haveCredential) { $setupArgs += @('--code-file', ('"' + $codeFile + '"')) }
Write-Host "`n  The Agent Arena installer window is opening." -ForegroundColor Yellow
Write-Host "  Check 'Detected identity' says $displayId, click Install, wait for SUCCESS, then click Launch Arena." -ForegroundColor Yellow
Write-Host "  You can leave the installer window open; this window carries on by itself.`n" -ForegroundColor Yellow
$installer = Start-Process (Join-Path $pkg 'app\AgentArena.exe') -ArgumentList $setupArgs -PassThru
$outcome = $null
while (-not $outcome) {
    Start-Sleep -Milliseconds 500
    if ((Test-Path $resultFile) -and (Get-Item $resultFile).LastWriteTime -gt $started) {
        $r = Get-Content $resultFile -Raw | ConvertFrom-Json
        if ($r.action -eq 'install') { $outcome = $r }
    }
    elseif ($installer.HasExited) { break }
}
if (-not $outcome) { Note 'Setup finished' $false 'the installer window was closed before Install was clicked'; Finish 'NOT INSTALLED: run 2-INSTALL-AND-VERIFY again and click Install.'; return }
Note 'Setup finished' ([bool]$outcome.ok) ("result ok=$($outcome.ok), version $($outcome.version), enrolled=$($outcome.enrolled)")
Write-Host "  Waiting 15 seconds so you can click Launch Arena..." -ForegroundColor Yellow
Start-Sleep 15

# 4. Proof.
$after = Get-Discovery 'after'
Save-Discovery $after $ev | Out-Null
$ver = $after.agent_arena.current_version
$exe = $(if ($ver) { Join-Path $env:LOCALAPPDATA "Programs\AgentArena\$ver\AgentArena.exe" } else { $null })
Note 'Installed' ([bool]$ver -and $ver -eq $UsbProfile.version -and (Test-Path $exe)) ("version $ver at $exe")
if (-not $ver) { Finish 'NOT INSTALLED: see the installer messages (agent-arena.log is in this evidence folder).'; return }
$self = Start-Process $exe -ArgumentList '--self-test' -PassThru -WindowStyle Hidden; $self.WaitForExit()
Note 'Program and Arena files verified' ($self.ExitCode -eq 0) ("self-test exit $($self.ExitCode)")
$diag = Join-Path $ev 'diagnostics-after.txt'
$dp = Start-Process $exe -ArgumentList '--diagnostics-report', ('"' + $diag + '"') -PassThru -WindowStyle Hidden; $dp.WaitForExit()
$report = $(if (Test-Path $diag) { Get-Content $diag -Raw } else { '' })
Note 'Authenticated with the event service' ($report -match '\[PASS\]\s+Authentication') (($report -split "`r?`n" | Where-Object { $_ -match 'Authentication' }) -join ' ')
Note 'Live subscription for this head' ($report -match '\[PASS\]\s+Viewer subscription') (($report -split "`r?`n" | Where-Object { $_ -match 'Viewer subscription' }) -join ' ')
Info (($report -split "`r?`n" | Where-Object { $_ -match '\] Display ' }) -join ' ')
Note 'Enrollment' ([bool]$outcome.enrolled -and $after.agent_arena.credential_exists) $(if ($haveCredential) { 'existing credential reused; no new code was needed' } else { 'enrolled with the one-time code' })
Note 'Sign-in startup (one entry)' ([string]$after.agent_arena.run_value -like "*\$ver\AgentArena.exe*") ([string]$after.agent_arena.run_value)
Note 'Desktop and Start menu shortcuts' ($after.agent_arena.desktop_shortcut -and $after.agent_arena.start_menu) ("desktop=$($after.agent_arena.desktop_shortcut) start_menu=$($after.agent_arena.start_menu)")
$lv = $after.legacy_viewer
$ownedLeft = @($lv.files | Where-Object { $_.name -in 'Install.ps1','Viewer.ps1','Diagnostics.ps1','Metrics.ps1','Uninstall.ps1','README.md','VERIFY-ON-EACH-HEAD.md','config.json','prior-power-scheme.txt','diagnostics.json','maintenance.stop' })
Note 'No original-viewer leftovers' ($ownedLeft.Count -eq 0 -and -not $lv.startup_shortcut -and -not $lv.scheduled_task -and @($lv.processes).Count -eq 0) ("owned files $($ownedLeft.Count), shortcut $([bool]$lv.startup_shortcut), task $([bool]$lv.scheduled_task), processes $(@($lv.processes).Count)")
$viewers = { @(Get-Process AgentArena -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "$ViewerTitle*" }) }
if ((& $viewers).Count -eq 0) { Info 'Arena was not launched from the installer; starting it now'; Start-Process $exe | Out-Null }
$title = $null
for ($i = 0; $i -lt 30 -and -not $title; $i++) { Start-Sleep 1; $title = Save-WindowImage $ViewerTitle (Join-Path $ev 'arena-window-1.png') }
Start-Sleep 10
$title = Save-WindowImage $ViewerTitle (Join-Path $ev 'arena-window-2.png')
Note 'Arena window open for this display' ($title -like "*$displayId*") ([string]$title)
Note 'Exactly one Arena window' ((& $viewers).Count -eq 1) ("$((& $viewers).Count) viewer process(es); the installer window may also be open")
$beforePids = ($before.ollama.processes | ForEach-Object { $_.pid } | Sort-Object) -join ','
$afterPids = ($after.ollama.processes | ForEach-Object { $_.pid } | Sort-Object) -join ','
$nowOllama = Get-Json 'http://127.0.0.1:11434/api/version' 3
Note 'Ollama untouched' (($beforePids -eq $afterPids) -and ($nowOllama.version -eq $before.ollama.version)) ("pids $beforePids -> $afterPids, version $($before.ollama.version) -> $($nowOllama.version)")
Note 'Power plan unchanged' ($before.power_scheme -eq $after.power_scheme) ([string]$after.power_scheme)
# Field defect D4: delete the code only after this run's enrollment is proven (setup says enrolled,
# a credential exists, and the service accepted it for this display).
$enrolledNow = (-not $haveCredential) -and [bool]$outcome.enrolled -and $after.agent_arena.credential_exists -and ($report -match '\[PASS\]\s+Authentication') -and ($report -match [regex]::Escape("display $displayId "))
if ($enrolledNow -and (Test-Path $codeFile)) { Remove-Item -LiteralPath $codeFile -Force; Info 'The one-time enrollment code was used for this enrollment and has been deleted from the USB.' }
elseif (Test-Path $codeFile) { Info 'The one-time enrollment code on this USB was NOT used and is kept.' }
$failed = @($results | Where-Object Result -eq 'FAIL').Count
Finish $(if ($failed) { "FINISHED WITH $failed FAIL(S): bring this folder back to the coordinator." } else { 'ALL CHECKS PASSED: Agent Arena is installed and live on this head. Bring this folder back to the coordinator.' })
