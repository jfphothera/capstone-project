# Get-CapstoneLogs.ps1 - run by GET-CAPSTONE-LOGS.cmd on the Capstone laptop (2026-11-14).
# Copies C:\ProgramData\Capstone\logs\capstone.log and capstone.error.log to the stick (reports\<computer>\capstone-logs-
# <time>), plus Capstone's installed version and what its Testing Lab endpoints answer right now (read-only GETs on
# Capstone's own address, timed). No databases, nothing changed, nothing deleted.
$ErrorActionPreference = 'Continue'
$stick = $PSScriptRoot; while ($stick -and -not (Test-Path -LiteralPath (Join-Path $stick 'START-LAB-PROVISIONER.cmd'))) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
if (-not $stick) { Write-Host '  Run this from the provisioner stick. Nothing was copied.' -ForegroundColor Red; exit 2 }
$out = Join-Path $stick ('reports\{0}\capstone-logs-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date)); New-Item -ItemType Directory -Path $out -Force | Out-Null
$lines = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$lines.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }

Write-Host ''; Write-Host '  Agent Arena lab: Capstone''s logs to the stick' -ForegroundColor Green
$logs = 'C:\ProgramData\Capstone\logs'
foreach ($n in 'capstone.log', 'capstone.error.log') {
    $f = Join-Path $logs $n
    if (Test-Path -LiteralPath $f) { Copy-Item -LiteralPath $f -Destination $out -Force; Say ('{0}: copied ({1:N1} MB)' -f $n, ((Get-Item -LiteralPath $f).Length / 1MB)) }
    else { Say "$n not found in $logs" 'Yellow' }
}
try { $v = (Get-Content -Raw 'C:\Program Files\Capstone\current\VERSION' -ErrorAction Stop).Trim(); Say "installed Capstone: $v" } catch {
    $rel = @(Get-ChildItem 'C:\Program Files\Capstone\releases' -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1)
    Say ('newest Capstone release folder: ' + $(if ($rel) { $rel[0].Name } else { 'none found' })) }

# What Capstone answers now (its own address; read-only). A login wall answers 401/303 quickly; a hang shows as a timeout.
# The address is where Capstone listens (its appsettings.json), else the lab config's Capstone address on this stick.
$listen = $null; $port = 8000
try { $s = Get-Content -Raw 'C:\ProgramData\Capstone\config\appsettings.json' -ErrorAction Stop | ConvertFrom-Json; $listen = [string]$s.listen_host; if ($s.listen_port) { $port = [int]$s.listen_port } } catch { }
if (-not $listen) {
    $rel = @(Get-ChildItem -LiteralPath $stick -Directory -Filter 'AGENT-ARENA-LAB-*' | Sort-Object Name | Select-Object -Last 1)
    try { $listen = [string]((Get-Content -Raw (Join-Path $rel[0].FullName 'config\lab-network.json') | ConvertFrom-Json).roles.capstone.address) } catch { }
}
if (-not $listen) { Say 'Capstone''s address is in neither its appsettings.json nor the lab config: endpoint checks skipped' 'Yellow' }
$base = 'http://{0}:{1}' -f $listen, $port
foreach ($p in '/api/health', '/api/lab/mode', '/api/lab/state', '/api/lab/state', '/pressure-testing', '/static/js/testing_lab.js?v=rc12') {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri ($base + $p) -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0 -ErrorAction Stop
        $body = [string]$r.Content; if ($body.Length -gt 300) { $body = $body.Substring(0, 300) + ' ...' }
        Say ('{0}: HTTP {1} in {2:N1} s; cache {3}; {4}' -f $p, $r.StatusCode, $sw.Elapsed.TotalSeconds, $r.Headers['Cache-Control'], ($body -replace '\s+', ' '))
    } catch {
        $code = $(if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'no answer' })
        Say ('{0}: {1} after {2:N1} s ({3})' -f $p, $code, $sw.Elapsed.TotalSeconds, $_.Exception.Message) 'Yellow'
    }
    if ($p -eq '/api/lab/state') { Start-Sleep -Seconds 5 }
}
[IO.File]::WriteAllText((Join-Path $out 'checks.txt'), ($lines -join "`r`n"), (New-Object Text.UTF8Encoding $false))
Write-Host ''
Write-Host ("  DONE: saved to {0}" -f $out) -ForegroundColor Green
