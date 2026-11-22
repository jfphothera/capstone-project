# Repair-HubGatewayQualify.ps1 - run by FIX-GATEWAY-QUALIFY.cmd on the admin hub, as administrator (2026-11-12).
# Why: hub > 3 > 2 tested the embedding models with the Gateway's text test; they cannot pass it, and a failed test takes
# a model off the head. all-minilm (Capstone's lookups) was taken off all nine heads. This runs the Gateway's own
# embedding test again for each one that had passed it before. Report: reports\<computer>\gateway-requalify-<time>.
param([switch]$DryRun)
$ErrorActionPreference = 'Continue'
$stick = $PSScriptRoot; while ($stick -and -not (Test-Path -LiteralPath (Join-Path $stick 'START-LAB-PROVISIONER.cmd'))) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
$reportRoot = $(if ($stick) { Join-Path $stick ('reports\' + $env:COMPUTERNAME) } else { $PSScriptRoot })
$out = Join-Path $reportRoot ('gateway-requalify-{0:yyyyMMdd-HHmmss}' -f (Get-Date)); New-Item -ItemType Directory -Path $out -Force | Out-Null
$py = 'C:\Program Files\Python312\python.exe'

Write-Host ''; Write-Host '  Agent Arena lab hub: put the embedding models back on the heads' -ForegroundColor Green
Write-Host ("  computer {0}, user {1}{2}" -f $env:COMPUTERNAME, $env:USERNAME, $(if ($DryRun) { ' - DRY RUN: nothing is changed' } else { '' }))
$svc = Get-CimInstance Win32_Service -Filter "Name='InferenceGateway'" -ErrorAction SilentlyContinue
if (-not $svc -or $svc.State -ne 'Running' -or $svc.PathName -notmatch '\\releases\\([0-9][^\\]*)\\') {
    Write-Host ("  RESULT not run: the Inference Gateway service is not running here ({0}). Nothing was changed. Bring the stick back." -f $(if ($svc) { $svc.State } else { 'not installed' })) -ForegroundColor Red; exit 2
}
$release = 'C:\Program Files\InferenceGateway\releases\' + $Matches[1]
Write-Host ("  Gateway {0} is running" -f $Matches[1])
if (-not (Test-Path -LiteralPath $py)) { Write-Host "  RESULT not run: $py is missing. Nothing was changed." -ForegroundColor Red; exit 2 }
$a = @('-I', (Join-Path $PSScriptRoot 'requalify_embedding.py'), '--python', $py, '--release', $release, '--operator', $env:USERNAME, '--report', (Join-Path $out 'requalify.json'))
if ($DryRun) { $a += '--dry-run' }
& $py @a
$code = $LASTEXITCODE
Write-Host ''
if ($code -eq 0) { Write-Host '  RESULT success: the embedding models are back on the heads. Next: the Capstone laptop.' -ForegroundColor Green }
elseif ($code -eq 1) { Write-Host '  RESULT some are not back (the reason is on each line above). Bring the stick back.' -ForegroundColor Yellow }
else { Write-Host '  RESULT not run. Bring the stick back.' -ForegroundColor Red }
Write-Host ("  Report: {0}" -f $out)
exit $code
