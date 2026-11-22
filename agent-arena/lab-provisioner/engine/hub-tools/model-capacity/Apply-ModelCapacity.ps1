# Apply-ModelCapacity.ps1 - run by APPLY-MODEL-CAPACITY.cmd on the admin hub, as administrator (provisioner 1.6.14).
param([switch]$Yes, [string]$Release = '', [string]$Python = 'C:\Program Files\Python312\python.exe', [string]$Ledger = 'C:\ProgramData\InferenceGateway\state\capacity-ledger.db',
      [string]$PressureRoot = 'C:\ProgramData\AgentArenaLab\pressure-test', [string]$CapacityFile = 'C:\ProgramData\AgentArenaLab\gateway\model-capacity.json')
$ErrorActionPreference = 'Continue'
# 1.6.18: the outermost folder with START-LAB-PROVISIONER.cmd (the stick root), where HEAD-OLLAMA writes its reports.
$stick = $null; $d = $PSScriptRoot; while ($d) { if (Test-Path -LiteralPath (Join-Path $d 'START-LAB-PROVISIONER.cmd')) { $stick = $d }; $up = Split-Path -Parent $d; if (-not $up -or $up -eq $d) { $d = $null } else { $d = $up } }
if (-not $stick) { Write-Host '  RESULT not run: run this from the provisioner stick. Nothing was changed.' -ForegroundColor Red; exit 2 }
Write-Host ''; Write-Host '  Agent Arena lab hub: each head''s measured per-model capacity for the Gateway' -ForegroundColor Green
if (-not $Release) {
    $svc = Get-CimInstance Win32_Service -Filter "Name='InferenceGateway'" -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.State -ne 'Running' -or $svc.PathName -notmatch '\\releases\\([0-9][^\\]*)\\') { Write-Host '  RESULT not run: the Inference Gateway is not running here. Nothing was changed.' -ForegroundColor Red; exit 2 }
    $Release = 'C:\Program Files\InferenceGateway\releases\' + $Matches[1]
}
if (-not (Test-Path -LiteralPath $Python)) { Write-Host '  RESULT not run: Python is missing. Nothing was changed.' -ForegroundColor Red; exit 2 }
$work = Join-Path $stick ('reports\{0}\model-capacity-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date))
$a = @('-I', (Join-Path $PSScriptRoot 'model_capacity.py'), '--python', $Python, '--release', $Release, '--ledger', $Ledger, '--reports', (Join-Path $stick 'reports'),
       '--pressure-root', $PressureRoot, '--capacity-file', $CapacityFile, '--work', $work)
if ($Yes) { $a += '--yes' }
& $Python @a
$code = $LASTEXITCODE
Write-Host ("  Report and matrix: {0}" -f $work)
exit $code
