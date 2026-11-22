# Repair-HubGatewayConcurrency.ps1 - run by FIX-GATEWAY-CONCURRENCY.cmd on the admin hub, as administrator (2026-11-12).
# Capstone was allowed one request at a time (max_concurrency 1 in our policy), so it used one head at a time. This sets
# it through the Gateway's own bootstrap, keeps the hub's policy copy in step, and tests head 10's untested models.
# Report: reports\<computer>\gateway-concurrency-<time>.
param([int]$Capstone = 100, [int]$Research = 0, [int]$Queue = 0, [switch]$SkipRetest, [switch]$Ask)
$ErrorActionPreference = 'Continue'
$stick = $PSScriptRoot; while ($stick -and -not (Test-Path -LiteralPath (Join-Path $stick 'START-LAB-PROVISIONER.cmd'))) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
if (-not $stick) { Write-Host '  RESULT not run: run this from the provisioner stick. Nothing was changed.' -ForegroundColor Red; exit 2 }
$engine = @(Get-ChildItem -LiteralPath $stick -Directory -Filter 'AGENT-ARENA-LAB-*' | Sort-Object Name | Select-Object -Last 1 | ForEach-Object { Join-Path $_.FullName 'engine\hub-tools\gateway-models' })
$out = Join-Path $stick ('reports\{0}\gateway-concurrency-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date)); New-Item -ItemType Directory -Path $out -Force | Out-Null
$py = 'C:\Program Files\Python312\python.exe'

Write-Host ''; Write-Host '  Agent Arena lab hub: Capstone on several heads at once, and head 10''s untested models' -ForegroundColor Green
Write-Host ("  computer {0}, user {1}" -f $env:COMPUTERNAME, $env:USERNAME)
$svc = Get-CimInstance Win32_Service -Filter "Name='InferenceGateway'" -ErrorAction SilentlyContinue
if (-not $svc -or $svc.State -ne 'Running' -or $svc.PathName -notmatch '\\releases\\([0-9][^\\]*)\\') {
    Write-Host ("  RESULT not run: the Inference Gateway service is not running here ({0}). Nothing was changed. Bring the stick back." -f $(if ($svc) { $svc.State } else { 'not installed' })) -ForegroundColor Red; exit 2
}
$release = 'C:\Program Files\InferenceGateway\releases\' + $Matches[1]
if (-not $engine -or -not (Test-Path -LiteralPath (Join-Path $engine[0] 'gateway_models.py')) -or -not (Test-Path -LiteralPath $py)) { Write-Host '  RESULT not run: the stick''s engine or Python is missing. Nothing was changed.' -ForegroundColor Red; exit 2 }
Write-Host ("  Gateway {0} is running" -f $Matches[1])
# -Ask: the operator chooses (2026-11-13: up to the Gateway's own maximum, 1024 at once; the queue up to 10000).
if ($Ask) {
    $n = (Read-Host '  Requests at once for Capstone and Capstone research, 1-1024 (Enter = 1024)').Trim(); if (-not $n) { $n = '1024' }
    $w = (Read-Host '  Requests allowed to wait for a free head, 1-10000 (Enter = 10000)').Trim(); if (-not $w) { $w = '10000' }
    if ($n -notmatch '^\d+$' -or [int]$n -lt 1 -or [int]$n -gt 1024 -or $w -notmatch '^\d+$' -or [int]$w -lt 1 -or [int]$w -gt 10000) {
        Write-Host '  RESULT not run: requests at once must be 1-1024 and the queue 1-10000. Nothing was changed.' -ForegroundColor Red; exit 2 }
    $Capstone = [int]$n; $Research = [int]$n; $Queue = [int]$w
}
$a = @('-I', (Join-Path $PSScriptRoot 'set_concurrency.py'), '--python', $py, '--release', $release, '--engine', $engine[0], '--operator', $env:USERNAME, '--capstone', $Capstone, '--report', (Join-Path $out 'concurrency.json'))
if ($Research -gt 0) { $a += @('--research', $Research) }
if ($Queue -gt 0) { $a += @('--queue', $Queue) }
if ($SkipRetest) { $a += '--skip-retest' }
& $py @a
$code = $LASTEXITCODE
Write-Host ''
if ($code -eq 0) { Write-Host ("  RESULT success: Capstone may now have {0} requests at once in the Gateway{1}{2}. How many run at the same time is each head's slots (SET-PARALLEL-PER-HEAD)." -f $Capstone, $(if ($Research -gt 0) { " and Capstone research $Research" } else { '' }), $(if ($Queue -gt 0) { "; up to $Queue may wait for a free head" } else { '' })) -ForegroundColor Green }
elseif ($code -eq 1) { Write-Host '  RESULT not everything worked (the reason is on each line above). Bring the stick back.' -ForegroundColor Yellow }
else { Write-Host '  RESULT not run. Bring the stick back.' -ForegroundColor Red }
Write-Host ("  Report: {0}" -f $out)
exit $code
