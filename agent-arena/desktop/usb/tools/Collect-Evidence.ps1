# 3-COLLECT-EVIDENCE: read-only re-check while the Arena runs (no changes).
# Takes window-only snapshots every 10 seconds for two minutes, so activity sent
# from the coordinator during that time is captured.
. (Join-Path $PSScriptRoot 'HeadCommon.ps1')
$ev = Join-Path $UsbRoot ("evidence\" + $env:COMPUTERNAME + "\" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-collect")
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript (Join-Path $ev 'transcript.txt') | Out-Null
Write-Banner 'EVIDENCE COLLECTION (read-only)'
$d = Get-Discovery 'collect'
Save-Discovery $d $ev | ForEach-Object { Write-Host "  $_" }
$ver = $d.agent_arena.current_version
if ($ver) {
    $exe = Join-Path $env:LOCALAPPDATA "Programs\AgentArena\$ver\AgentArena.exe"
    $dp = Start-Process $exe -ArgumentList '--diagnostics-report', ('"' + (Join-Path $ev 'diagnostics.txt') + '"') -PassThru -WindowStyle Hidden; $dp.WaitForExit()
}
if (@(Get-Process AgentArena -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "$ViewerTitle*" }).Count -eq 0) { Write-Host "  Agent Arena is not running, so there is no window to capture (start it from the Start menu)." -ForegroundColor Yellow }
else {
    Write-Host "  Capturing the Arena window every 10 s for 2 minutes. Leave it visible." -ForegroundColor Yellow
    for ($i = 1; $i -le 12; $i++) { $t = Save-WindowImage $ViewerTitle (Join-Path $ev ("arena-{0:D2}.png" -f $i)); Write-Host "   $i/12  $t"; Start-Sleep 10 }
}
$log = Join-Path $env:LOCALAPPDATA 'AgentArena\logs\agent-arena.log'
if (Test-Path $log) { Copy-Item $log (Join-Path $ev 'agent-arena.log') }
Write-Host "`n  Evidence saved to $ev`n"
Stop-Transcript | Out-Null
