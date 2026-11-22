# Rehearses a Capstone UPDATE (old release -> new release) with Capstone's own Update-Capstone.ps1, as the provisioner
# runs it (the lab's patched copy when the updater needs one), in Windows PowerShell 5.1, without touching this laptop's
# Program Files, ProgramData or network settings:
#   1. the old release installed into scratch roots by its own installer, configured with DUMMY tokens (never real
#      ones) and the Gateway's public CA, listening on 192.0.2.1:<port> (not 127.0.0.1, like DESKTOP-REDACT11's 192.0.2.201);
#   2. a scheduled task for the signed-in user (DESKTOP-REDACT11's runs as LOCAL SERVICE; the updater only needs a task it can
#      stop and start) running a stand-in for Capstone's web process (capstone_standin.py: /api/health only on
#      listen_host:listen_port, 20 s after start), because Capstone's startup asks the Gateway, which refuses dummy tokens;
#   3. the updater, run as the provisioner runs it. The dummy tokens cannot pass Capstone's authenticated health gate,
#      so the expected end is a rollback for "failed health validation" AFTER the new release answered /api/health in
#      time, then the old release running again;
#   4. optionally (-Control) the updater as shipped, to show its own wait does not see Capstone.
param([Parameter(Mandatory)][string]$OldZip, [Parameter(Mandatory)][string]$NewZip, [Parameter(Mandatory)][string]$Work,
      [int]$Port = 8000, [string]$Ca = 'F:\gateway-ca\inference-gateway-ca.pem', [switch]$Control)
$ErrorActionPreference = 'Stop'
trap { Write-Host ('ERROR {0}: {1}' -f $_.InvocationInfo.PositionMessage, $_.Exception.Message); break }
$results = New-Object Collections.ArrayList
function Note([string]$Check, [bool]$Ok, [string]$Detail) { [void]$results.Add([pscustomobject]@{ Result = $(if ($Ok) { 'PASS' } else { 'FAIL' }); Check = $Check; Detail = $Detail }); Write-Host ('{0}  {1}: {2}' -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Check, $Detail) }
function Health { try { Invoke-RestMethod "http://192.0.2.1:$Port/api/health" -TimeoutSec 3 } catch { $null } }
function WaitHealth([int]$Seconds) { $t0 = Get-Date; while (((Get-Date) - $t0).TotalSeconds -lt $Seconds) { $h = Health; if ($h -and $h.ok) { return $h }; Start-Sleep 2 }; return $null }

if (Test-Path $Work) { throw "$Work exists: use a new folder" }
Import-Module (Join-Path $PSScriptRoot '..\..\engine\modules\Lab.Core.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '..\..\engine\modules\Lab.Apps.psm1') -Force
$inst = Join-Path $Work 'install'; $data = Join-Path $Work 'data'; $gw = Join-Path $Work 'gateway'
New-Item -ItemType Directory $gw -Force | Out-Null
$pkgOld = Join-Path $Work 'pkg-old'; $pkgNew = Join-Path $Work 'pkg-new'
$null = Expand-LabZipChecked -Zip $OldZip -Destination $pkgOld; $null = Expand-LabZipChecked -Zip $NewZip -Destination $pkgNew
$mOld = Get-Content (Join-Path $pkgOld 'package-manifest.json') -Raw | ConvertFrom-Json; $mNew = Get-Content (Join-Path $pkgNew 'package-manifest.json') -Raw | ConvertFrom-Json
Note 'packages' $true ("{0} -> {1}" -f $mOld.version, $mNew.version)
$task = 'Capstone Rehearsal ' + [guid]::NewGuid().ToString('N').Substring(0, 8)
try {
    # 1. old release, configured
    # Its last step makes icons on the Public desktop, which needs administrator rights; this rehearsal is not
    # elevated, so that one refusal is accepted once the installation itself (current.json) is complete.
    # As the provisioner does: rc.5 and rc.6 carry no payload\models, but their installer copies it unless this exists.
    if (-not (Test-Path (Join-Path $pkgOld 'payload\models\huggingface'))) { New-Item -ItemType Directory (Join-Path $data 'models\huggingface') -Force | Out-Null }
    try {
        $out = & (Join-Path $pkgOld 'tools\Install-Capstone.ps1') -InstallRoot $inst -DataRoot $data 2>&1 | Out-String
        Note "Install-Capstone.ps1 ($($mOld.version))" ($out -match '"installed"\s*:\s*true') ((($out -split "`r?`n") | Where-Object { $_ -match 'installed|version' } | Select-Object -Last 1))
    } catch {
        if ($_.Exception.Message -notmatch 'Unable to save shortcut' -or -not (Test-Path (Join-Path $inst 'current.json'))) { throw }
        Note "Install-Capstone.ps1 ($($mOld.version))" $true 'installed; its Public-desktop icons need administrator rights (rehearsal not elevated)'
    }
    $cfgDir = Join-Path $inst ('releases\{0}\application\config' -f $mOld.version)
    Copy-Item $Ca (Join-Path $gw 'inference-gateway-ca.pem')
    $boots = @{}
    foreach ($pair in @(@('capstone', 'inference-gateway.client.template.json'), @('capstone-research', 'inference-gateway.research-client.template.json'))) {
        $b = Get-Content (Join-Path $cfgDir $pair[1]) -Raw | ConvertFrom-Json
        $tok = Join-Path $gw ($pair[0] + '.token'); Set-Content -LiteralPath $tok -Value ('dummy-not-a-real-token-' + [guid]::NewGuid().ToString('N')) -Encoding ASCII
        $b.token_file = $tok; $b.ca_file = (Join-Path $gw 'inference-gateway-ca.pem')
        $bf = Join-Path $gw ($pair[0] + '.bootstrap.json'); [IO.File]::WriteAllText($bf, ($b | ConvertTo-Json), (New-Object Text.UTF8Encoding $false)); $boots[$pair[0]] = $bf
    }
    $dev = Join-Path $gw 'developer.token'; Set-Content -LiteralPath $dev -Value ('dummy-developer-' + [guid]::NewGuid().ToString('N')) -Encoding ASCII
    $out = & (Join-Path $inst 'tools\Configure-Capstone.ps1') -OperationalGatewayBootstrap $boots['capstone'] -ResearchGatewayBootstrap $boots['capstone-research'] -DeveloperTokenFile $dev -DataRoot $data -ListenHost '192.0.2.1' -ListenPort $Port 2>&1 | Out-String
    Note 'Configure-Capstone.ps1 (listens on 192.0.2.1 only, like DESKTOP-REDACT11 on 192.0.2.201)' ($out -match '"configured"\s*:\s*true') ''
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    foreach ($f in @(Get-ChildItem $gw -File) + @(Get-ChildItem (Join-Path $data 'gateway') -File -ErrorAction SilentlyContinue)) { & icacls.exe $f.FullName /grant "*${me}:(R)" /Q | Out-Null }

    # 2. a task the updater can stop and start. Capstone itself cannot start with dummy tokens (its startup asks the
    # Gateway, which refuses them: "Application startup failed", HTTP 403), so the task runs a stand-in that answers
    # /api/health only on listen_host:listen_port with current.json's version, after 20 s, as Capstone binds. Its
    # launcher is named Start-Capstone.ps1, so Capstone's own Stop-Capstone.ps1 finds and stops it like the real one.
    $si = Join-Path $Work 'standin'; New-Item -ItemType Directory $si -Force | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'capstone_standin.py') $si
    $py = Join-Path $inst 'runtime\Scripts\python.exe'
    [IO.File]::WriteAllText((Join-Path $si 'Start-Capstone.ps1'), "param([string]`$InstallRoot,[string]`$DataRoot)`r`n& '$py' -I '$(Join-Path $si 'capstone_standin.py')' --install `$InstallRoot --data `$DataRoot`r`nexit `$LASTEXITCODE`r`n", (New-Object Text.UTF8Encoding $false))
    Note 'stand-in for Capstone''s web process (dummy tokens cannot start the real one)' $true 'answers /api/health on listen_host:listen_port only, 20 s after start, with current.json''s version'
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -InstallRoot "{1}" -DataRoot "{2}"' -f (Join-Path $si 'Start-Capstone.ps1'), $inst, $data)
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $task -Action $action -Principal $principal -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew) | Out-Null
    Start-ScheduledTask -TaskName $task
    $h = WaitHealth 240
    Note "the old release's stand-in answers /api/health at 192.0.2.1:$Port (started by the task)" ([bool]$h) $(if ($h) { 'version ' + $h.version } else { 'no answer in 240 s' })
    $loop = $null; try { $loop = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 } catch { }
    Note 'nothing answers on 127.0.0.1 (Capstone binds only its own address)' (-not $loop) $(if ($loop) { 'something answered on 127.0.0.1' } else { 'no answer on 127.0.0.1, as on DESKTOP-REDACT11' })

    # 3. the updater as the provisioner runs it
    $updText = [IO.File]::ReadAllText((Join-Path $pkgNew 'tools\Update-Capstone.ps1'))
    $stopFile = Join-Path $pkgNew 'tools\Stop-Capstone.ps1'
    $fixed = Test-LabCapstoneUpdaterFixed -Text $updText -StopText $(if (Test-Path $stopFile) { [IO.File]::ReadAllText($stopFile) } else { '' })
    if ($fixed) { $upd = Join-Path $pkgNew 'tools\Update-Capstone.ps1' } else { $upd = Join-Path $Work 'Update-Capstone.lab.ps1'; [IO.File]::WriteAllText($upd, (ConvertTo-LabPatchedCapstoneUpdate -Text $updText), (New-Object Text.UTF8Encoding $true)) }
    # Port 8000 by default, as on DESKTOP-REDACT11: then only the address differs from what the shipped updater asks for.
    Note 'the provisioner''s choice' $true $(if ($fixed) { 'updater run as shipped' } else { 'lab-patched copy: ' + $upd })
    $t0 = Get-Date
    $r = Invoke-LabExternalScript -Script $upd -Arguments @('-PackageRoot', $pkgNew, '-InstallRoot', $inst, '-DataRoot', $data, '-TaskName', $task) -LogFile (Join-Path $Work 'update.log')
    $secs = ((Get-Date) - $t0).TotalSeconds
    $last = (($r.Output -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' | '
    $ok = ($r.Output -match '"updated"\s*:\s*true') -or ($r.Output -match 'failed health validation' -and $r.Output -notmatch 'within 240 seconds')
    Note ("the new release answered its updater's wait in time ({0:N0} s); with dummy tokens the authenticated gate then rolls back" -f $secs) $ok $last
    $cur = (Get-Content (Join-Path $inst 'current.json') -Raw | ConvertFrom-Json)
    $h = WaitHealth 240
    # The stand-in's journal: which release started and answered, in order. The new release must have started and
    # answered the updater's wait (the old one stopped first); after a rollback the old one starts again.
    $j = @(); $jf = Join-Path $data 'logs\standin.log'; if (Test-Path $jf) { $j = @(Get-Content $jf) }
    $newHealth = @($j | Where-Object { $_ -match "health $([regex]::Escape($mNew.version))$" }).Count
    $starts = @($j | Where-Object { $_ -match ' started ' } | ForEach-Object { ($_ -split ' started ')[1] })
    Note 'the new release itself answered the wait (the old one was stopped first)' ($newHealth -gt 0 -and $starts.Count -ge 2 -and $starts[1] -eq $mNew.version) ('starts: {0}; health answers by {1}: {2}' -f ($starts -join ' -> '), $mNew.version, $newHealth)
    if ($r.Output -match '"updated"\s*:\s*true') { Note 'the new release is active and answers' ($cur.version -eq $mNew.version -and $h -and $h.version -eq $mNew.version) ("current.json {0}; health {1}" -f $cur.version, $(if ($h) { $h.version } else { 'none' })) }
    else { Note 'after the rollback the old release is active AND the one answering (stopped before restore)' ($cur.version -eq $mOld.version -and $h -and $h.version -eq $mOld.version) ("current.json {0} (reason: {1}); health {2}" -f $cur.version, $cur.reason, $(if ($h) { $h.version } else { 'none' })) }

    # 4. control: the updater exactly as shipped
    if ($Control -and -not $fixed) {
        Remove-Item -LiteralPath (Join-Path $inst ('releases\' + $mNew.version)) -Recurse -Force -ErrorAction SilentlyContinue
        $t0 = Get-Date
        $r = Invoke-LabExternalScript -Script (Join-Path $pkgNew 'tools\Update-Capstone.ps1') -Arguments @('-PackageRoot', $pkgNew, '-InstallRoot', $inst, '-DataRoot', $data, '-TaskName', $task) -LogFile (Join-Path $Work 'update-control.log')
        $secs = ((Get-Date) - $t0).TotalSeconds
        $last = (($r.Output -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
        Note ("CONTROL: the shipped updater does not see Capstone and rolls back after its 240 s wait ({0:N0} s)" -f $secs) ($r.Output -match 'within 240 seconds') $last
    }
} finally {
    try { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue } catch { }
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$inst*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    try { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue } catch { }
}
$results | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Write-Host
$results | ConvertTo-Json | Set-Content (Join-Path $Work 'rehearsal-results.json') -Encoding UTF8
