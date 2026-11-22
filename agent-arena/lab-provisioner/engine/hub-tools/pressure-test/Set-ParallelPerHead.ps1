# Set-ParallelPerHead.ps1 - run by SET-PARALLEL-PER-HEAD.cmd on the admin hub, as administrator (2026-11-12).
param([string]$Choose = '', [string]$Release = '', [string]$Python = 'C:\Program Files\Python312\python.exe', [string]$Ledger = 'C:\ProgramData\InferenceGateway\state\capacity-ledger.db',
      [string]$PressureRoot = 'C:\ProgramData\AgentArenaLab\pressure-test', [string]$SlotsFile = 'C:\ProgramData\AgentArenaLab\gateway\head-slots.json')
$ErrorActionPreference = 'Continue'
$stick = $PSScriptRoot; while ($stick -and -not (Test-Path -LiteralPath (Join-Path $stick 'START-LAB-PROVISIONER.cmd'))) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
if (-not $stick) { Write-Host '  RESULT not run: run this from the provisioner stick. Nothing was changed.' -ForegroundColor Red; exit 2 }
$rel = @(Get-ChildItem -LiteralPath $stick -Directory -Filter 'AGENT-ARENA-LAB-*' | Sort-Object Name | Select-Object -Last 1)
$config = $(if ($rel) { Join-Path $rel[0].FullName 'config\lab-network.json' } else { '' })
Write-Host ''; Write-Host '  Agent Arena lab hub: requests at once per head, by machine class' -ForegroundColor Green
if (-not $Release) {
    $svc = Get-CimInstance Win32_Service -Filter "Name='InferenceGateway'" -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.State -ne 'Running' -or $svc.PathName -notmatch '\\releases\\([0-9][^\\]*)\\') { Write-Host '  RESULT not run: the Inference Gateway is not running here. Nothing was changed.' -ForegroundColor Red; exit 2 }
    $Release = 'C:\Program Files\InferenceGateway\releases\' + $Matches[1]
}
if (-not (Test-Path -LiteralPath $config) -or -not (Test-Path -LiteralPath $Python)) { Write-Host '  RESULT not run: the stick''s lab head list or Python is missing. Nothing was changed.' -ForegroundColor Red; exit 2 }
$work = Join-Path $stick ('reports\{0}\set-parallel-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date))
$a = @('-I', (Join-Path $PSScriptRoot 'set_parallel.py'), '--python', $Python, '--release', $Release, '--ledger', $Ledger, '--config', $config,
       '--reports', (Join-Path $stick 'reports'), '--pressure-root', $PressureRoot, '--slots-file', $SlotsFile, '--work', $work)
if ($Choose) { $a += @('--choose', $Choose) }
& $Python @a
$code = $LASTEXITCODE
Write-Host ("  Report: {0}" -f $work)
exit $code
