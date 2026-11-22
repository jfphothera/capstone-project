# Start-PressureTest.ps1 - run by PRESSURE-TEST.cmd on the admin hub, as administrator (2026-11-12).
# 1 = start a pressure test (it runs by itself as a SYSTEM task), 2 = status, 0 = stop and put every head back.
# The test and its logs live in C:\ProgramData\AgentArenaLab\pressure-test; status and stop copy them to the stick.
param([string]$Choice = '', [string]$Heads = '', [string]$Minutes = '', [string]$Top = '', [string]$Funnel = '',
      [string]$Root = 'C:\ProgramData\AgentArenaLab\pressure-test', [string]$Ledger = 'C:\ProgramData\InferenceGateway\state\capacity-ledger.db',
      [string]$Release = '', [string]$Python = 'C:\Program Files\Python312\python.exe', [switch]$NoTask)
$ErrorActionPreference = 'Continue'
$taskName = 'Agent Arena Lab - pressure test'; $app = 'capstone-research'
$stick = $PSScriptRoot; while ($stick -and -not (Test-Path -LiteralPath (Join-Path $stick 'START-LAB-PROVISIONER.cmd'))) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
function Ask([string]$q, [string]$default) { $a = Read-Host ("  {0} (Enter = {1})" -f $q, $default); if ([string]::IsNullOrWhiteSpace($a)) { $default } else { $a.Trim() } }
function Py([string[]]$a) { $o = & $Python -I (Join-Path $Root 'bin\pressure_test.py') @a 2>&1; [pscustomobject]@{ Code = $LASTEXITCODE; Text = (($o | Out-String).Trim()) } }
function Current { $f = Join-Path $Root 'current.txt'; if (Test-Path -LiteralPath $f) { $d = (Get-Content -LiteralPath $f -Raw).Trim(); if (Test-Path -LiteralPath $d) { return $d } }; $null }
function State([string]$d) { try { (Get-Content -LiteralPath (Join-Path $d 'status.json') -Raw | ConvertFrom-Json) } catch { $null } }
function ToStick([string]$d) {
    if (-not $stick) { Write-Host "  (no provisioner stick: the logs stay in $d)"; return }
    $t = Join-Path $stick ('reports\{0}\pressure-test-{1}' -f $env:COMPUTERNAME, (Split-Path -Leaf $d)); New-Item -ItemType Directory -Path $t -Force | Out-Null
    Get-ChildItem -LiteralPath $d -File | Where-Object { $_.Name -notlike 'policy-*' } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $t -Force }
    Write-Host "  logs copied to the stick: $t"
}
function Show([string]$d) {
    $s = State $d
    if ($s) {
        Write-Host ("  state: {0}{1}{2}" -f $s.state, $(if ($s.step) { ", step $($s.step) (from $($s.step_started))" } else { '' }), $(if ($s.finished) { " - $($s.finished)" } else { '' })) -ForegroundColor Cyan
        if ($s.heads) { foreach ($p in $s.heads.PSObject.Properties) { Write-Host ("    {0}: {1} at once - {2}{3}" -f $p.Name, $p.Value.step, $p.Value.decision, $(if ($p.Value.out_tok_s) { " ($($p.Value.out_tok_s) out tok/s)" } else { '' })) } }
    }
    $log = Join-Path $d 'run.log'; if (Test-Path -LiteralPath $log) { Write-Host '  last lines of the log:'; Get-Content -LiteralPath $log -Tail 8 | ForEach-Object { Write-Host "    $_" } }
}

Write-Host ''; Write-Host '  Agent Arena lab hub: pressure test' -ForegroundColor Green
if (-not $Release) {
    $svc = Get-CimInstance Win32_Service -Filter "Name='InferenceGateway'" -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.State -ne 'Running' -or $svc.PathName -notmatch '\\releases\\([0-9][^\\]*)\\') { Write-Host '  The Inference Gateway is not running here: nothing was changed.' -ForegroundColor Red; exit 2 }
    $Release = 'C:\Program Files\InferenceGateway\releases\' + $Matches[1]
}
if (-not (Test-Path -LiteralPath $Python)) { Write-Host "  $Python is missing: nothing was changed." -ForegroundColor Red; exit 2 }
$cur = Current; $st = $(if ($cur) { State $cur } else { $null })
$busy = $cur -and $st -and $st.state -ne 'done'
if (-not $Choice) {
    Write-Host $(if ($busy) { "  A test is running ($cur)." } else { '  No test is running.' })
    Write-Host '    1  start a test'; Write-Host '    2  status (and copy the logs to the stick)'; Write-Host '    0  stop the test and put every head back'
    $Choice = (Read-Host '  Type 1, 2 or 0').Trim()
}

if ($Choice -eq '1') {
    if ($busy) { Write-Host '  A test is already running: type 2 for its status, or 0 to stop it first.' -ForegroundColor Yellow; Show $cur; exit 1 }
    # One head of each machine class: 2 (GPU, 64 GB), 5 (16 GB), 6 (8 GB), 7 (32 GB).
    if (-not $Heads) { $Heads = Ask 'Test heads, by number' '2,5,6,7' }
    $ids = @($Heads -split '[,\s]+' | Where-Object { $_ -match '^\d{1,3}$' -and [int]$_ -ge 2 -and [int]$_ -le 199 } | ForEach-Object { 'head-192-0-2-' + [int]$_ } | Select-Object -Unique)
    if (-not $ids.Count) { Write-Host '  No head number given: nothing was changed.' -ForegroundColor Red; exit 1 }
    if (-not $Minutes) { $Minutes = Ask 'Minutes per step' '10' }
    if (-not $Top) { $Top = Ask 'Most requests at once to try per head' '10' }
    if (-not $Funnel) { $Funnel = Ask ("Send {0} only to the test heads while it runs? Y/N" -f $app) 'Y' }
    if ($Minutes -notmatch '^\d+(\.\d+)?$' -or [double]$Minutes -lt 1 -or $Top -notmatch '^\d+$' -or [int]$Top -lt 1 -or [int]$Top -gt 64) { Write-Host '  Minutes must be 1 or more and the top 1-64: nothing was changed.' -ForegroundColor Red; exit 1 }
    # The application's own limit must not cap the test (FIX-GATEWAY-CONCURRENCY sets it to 100).
    $cl = & $Python -I (Join-Path $Release 'inference_gateway\service_launcher.py') credentials list --database $Ledger 2>$null | Out-String
    $limit = 0; try { $limit = [int]((($cl | ConvertFrom-Json).applications | Where-Object app_id -eq $app).max_concurrency) } catch { }
    if ($limit -lt $ids.Count * [int]$Top) { Write-Host ("  {0} may run only {1} at once, fewer than {2} heads x {3}: run FIX-GATEWAY-CONCURRENCY first. Nothing was changed." -f $app, $limit, $ids.Count, $Top) -ForegroundColor Red; exit 1 }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'; $d = Join-Path $Root "runs\$stamp"
    New-Item -ItemType Directory -Path $d, (Join-Path $Root 'bin') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'pressure_test.py') -Destination (Join-Path $Root 'bin\pressure_test.py') -Force
    $cfg = [ordered]@{ heads = $ids; minutes = [double]$Minutes; top = [int]$Top; gain = 0.10; funnel = ($Funnel -match '^[Yy]'); app = $app; release = $Release
                       python = $Python; ledger = $Ledger; started = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); prepared_at = [double]([DateTimeOffset]::Now.ToUnixTimeMilliseconds() / 1000) }
    [IO.File]::WriteAllText((Join-Path $d 'config.json'), ($cfg | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))
    $p = Py @('prepare', '--dir', $d)
    if ($p.Code -ne 0) { Write-Host ('  Not started: ' + $p.Text) -ForegroundColor Red; exit 1 }
    [IO.File]::WriteAllText((Join-Path $Root 'current.txt'), $d, (New-Object Text.UTF8Encoding $false))
    if (-not $NoTask) {
        $action = New-ScheduledTaskAction -Execute $Python -Argument ('-I "{0}" run --dir "{1}"' -f (Join-Path $Root 'bin\pressure_test.py'), $d)
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force -Description 'Agent Arena Lab: pressure test (PRESSURE-TEST.cmd; 0 stops it and restores every head)' | Out-Null
        Start-ScheduledTask -TaskName $taskName
    }
    Write-Host ''
    Write-Host ('  STARTED: heads {0}; {1} min per step; 1 at once up to {2}; {3}' -f ($ids -join ', '), $Minutes, $Top, $(if ($cfg.funnel) { "$app only on these heads" } else { 'other heads unchanged' })) -ForegroundColor Green
    Write-Host '  Now start the Research Lab work on the Capstone laptop. The clock starts with its first request on a test head.'
    Write-Host '  Each head stops by itself at its point of diminishing returns; then every head is put back.'
    Write-Host '  Any time: PRESSURE-TEST.cmd > 2 for status, > 0 to stop. The stick need not stay in the hub.'
    exit 0
}
if (-not $cur) { Write-Host '  No test has been started on this hub.'; exit 0 }
if ($Choice -eq '2') { [void](Py @('summary', '--dir', $cur)); Show $cur; ToStick $cur; exit 0 }
if ($Choice -eq '0') {
    if (-not $busy) { Write-Host '  The test has already ended; every head was put back.'; Show $cur; ToStick $cur; exit 0 }
    [void](Py @('stop', '--dir', $cur)); Write-Host '  Stopping (up to 90 s)...'
    $until = (Get-Date).AddSeconds(90); while ((Get-Date) -lt $until -and (State $cur).state -ne 'done') { Start-Sleep -Seconds 3 }
    if ((State $cur).state -ne 'done') {
        if (-not $NoTask) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
        $r = Py @('restore', '--dir', $cur); if ($r.Code -ne 0) { Write-Host ('  RESTORE FAILED: ' + $r.Text + ' - bring the stick back.') -ForegroundColor Red; ToStick $cur; exit 1 }
    }
    Show $cur; ToStick $cur
    Write-Host '  STOPPED: every head is back as before the test.' -ForegroundColor Green
    exit 0
}
Write-Host '  Type 1, 2 or 0.'; exit 1
