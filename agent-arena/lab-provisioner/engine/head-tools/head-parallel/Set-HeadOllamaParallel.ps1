# Set-HeadOllamaParallel.ps1 - run by HEAD-PARALLEL.cmd on an inference head, as administrator (2026-11-12).
# Ollama runs one request at a time unless OLLAMA_NUM_PARALLEL says more. On the lab heads Ollama is started at boot
# by the task 'Capstone Ollama Server' (as SYSTEM) through a small wrapper file, ollama-serve.cmd, that sets its
# environment (Lab.Head New-LabOllamaWrapperText). 1 = on: the wrapper also sets OLLAMA_NUM_PARALLEL (a copy of the
# wrapper is kept first); 0 = off: the wrapper as it was before. Then Ollama is restarted the way the provisioner
# does it (its own processes stopped, the boot task started, the API waited for). Report: reports\<computer>.
param([string]$Choice = '', [string]$Parallel = '', [string]$TaskName = 'Capstone Ollama Server', [switch]$NoRestart, [string]$WrapperPath = '')
$ErrorActionPreference = 'Continue'

function Set-WrapperParallel([string]$Text, [int]$N) {
    <#  The wrapper text with OLLAMA_NUM_PARALLEL=N set just before the serve line (N = 0: the line removed). Pure.  #>
    $lines = @($Text -split "`r?`n" | Where-Object { $_ -notmatch '^\s*set "?OLLAMA_NUM_PARALLEL=' })
    while ($lines.Count -and $lines[-1] -eq '') { $lines = @($lines[0..($lines.Count - 2)]) }
    if ($N -gt 0) {
        $i = -1; for ($k = 0; $k -lt $lines.Count; $k++) { if ($lines[$k] -match '"\s+serve(\s|$)|ollama(\.exe)?"?\s+serve(\s|$)') { $i = $k; break } }
        if ($i -lt 0) { throw 'the wrapper has no "... serve" line: it is not the provisioner''s wrapper' }
        $set = ('set "OLLAMA_NUM_PARALLEL={0}"' -f $N)
        $lines = @($(if ($i -gt 0) { $lines[0..($i - 1)] }) + $set + $lines[$i..($lines.Count - 1)])
    }
    ($lines -join "`r`n") + "`r`n"
}
if ($Choice -eq 'selftest') { return }   # the test suite loads Set-WrapperParallel only

$stick = $PSScriptRoot; while ($stick -and -not (Test-Path -LiteralPath (Join-Path $stick 'START-LAB-PROVISIONER.cmd'))) { $up = Split-Path -Parent $stick; if (-not $up -or $up -eq $stick) { $stick = $null } else { $stick = $up } }
$log = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }
function Finish([int]$code) {
    if ($stick) { $d = Join-Path $stick ('reports\' + $env:COMPUTERNAME); New-Item -ItemType Directory -Path $d -Force | Out-Null
                  [IO.File]::WriteAllText((Join-Path $d ('head-parallel-{0:yyyyMMdd-HHmmss}.txt' -f (Get-Date))), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false)) }
    exit $code
}

Write-Host ''; Write-Host '  Agent Arena lab head: Ollama runs several requests at once' -ForegroundColor Green
$ip = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -match '^192\.0\.2\.(\d{1,3})$' -and [int]$Matches[1] -ge 2 -and [int]$Matches[1] -le 199 } | Select-Object -First 1).IPAddress
if (-not $ip -and -not $WrapperPath) { Say 'This computer has no head address (192.0.2.2-199): it is not an inference head. Nothing was changed.' 'Red'; Finish 2 }
Say ("computer {0}, head {1}, user {2}" -f $env:COMPUTERNAME, $ip, $env:USERNAME)
$wrapper = $WrapperPath
if (-not $wrapper) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Say "The boot task '$TaskName' is missing: this head was not set up by the provisioner. Nothing was changed." 'Red'; Finish 2 }
    $args0 = (@($t.Actions)[0]).Arguments
    if ($args0 -notmatch '"([^"]+ollama-serve\.cmd)"') { Say "The boot task does not run the provisioner's wrapper ($args0). Nothing was changed." 'Red'; Finish 2 }
    $wrapper = $Matches[1]
}
if (-not (Test-Path -LiteralPath $wrapper)) { Say "The wrapper $wrapper is missing. Nothing was changed." 'Red'; Finish 2 }
# 1.6.14: a head HEAD-OLLAMA qualified keeps its measured OLLAMA_NUM_PARALLEL (and reprovisioning re-applies it), so
# this older toggle would only drift from the evidence the Gateway uses.
$managed = Join-Path (Split-Path -Parent $wrapper) 'ollama-settings.json'
if ((Test-Path -LiteralPath $managed) -and ((Get-Content -LiteralPath $managed -Raw | ConvertFrom-Json).state -eq 'active')) { Say 'This head''s Ollama settings are managed by HEAD-OLLAMA (1.6.14), from its qualification. Use HEAD-OLLAMA (2 = qualify again, 0 = roll back). Nothing was changed.' 'Yellow'; Finish 1 }
$text = [IO.File]::ReadAllText($wrapper)
$now = $(if ($text -match 'set "?OLLAMA_NUM_PARALLEL=(\d+)') { [int]$Matches[1] } else { 0 })
$bind = $(if ($text -match 'set "?OLLAMA_HOST=([^"\r\n]+)') { $Matches[1].Trim() } else { '127.0.0.1:11434' })
$exe = $(if ($text -match '"([^"]+ollama(\.exe)?)"\s+serve') { $Matches[1] } else { '' })
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
Say ("now: {0}; memory {1} GB; Ollama at {2}" -f $(if ($now) { "up to $now requests at once" } else { 'one request at a time (Ollama''s default)' }), $ramGb, $bind)

if (-not $Choice) { Write-Host '    1  on: Ollama runs several requests at once'; Write-Host '    0  off: back to how it was'; $Choice = (Read-Host '  Type 1 or 0').Trim() }
if ($Choice -eq '1') {
    $default = $(if ($ramGb -le 12) { '4' } else { '10' })
    if (-not $Parallel) { $Parallel = Read-Host ("  Most requests at once (Enter = {0})" -f $default); if ([string]::IsNullOrWhiteSpace($Parallel)) { $Parallel = $default } }
    if ($Parallel -notmatch '^\d+$' -or [int]$Parallel -lt 2 -or [int]$Parallel -gt 64) { Say 'It must be 2-64. Nothing was changed.' 'Red'; Finish 1 }
    # Ollama reserves memory for every parallel request each time it loads a model, even with no traffic: too high and
    # models stop loading on this head. The pressure test (or Capstone's stair-step) shows where this head stops gaining.
    if ([int]$Parallel -gt [int]$default) { Say ("note: $Parallel is above the suggested $default for $ramGb GB; if models stop loading on this head, run this again with a lower number") 'Yellow' }
    $new = Set-WrapperParallel $text ([int]$Parallel)
} elseif ($Choice -eq '0') {
    $keep = @(Get-ChildItem -LiteralPath (Split-Path -Parent $wrapper) -Filter 'ollama-serve.before-parallel-*.cmd' -ErrorAction SilentlyContinue | Sort-Object Name)
    $new = $(if ($keep.Count) { [IO.File]::ReadAllText($keep[0].FullName) } else { Set-WrapperParallel $text 0 })
    Say $(if ($keep.Count) { "back to the wrapper kept before the first change ($($keep[0].Name))" } else { 'no kept copy: the parallel line is taken out' })
} else { Say 'Type 1 or 0. Nothing was changed.' 'Yellow'; Finish 1 }
if ($new -eq $text) { Say 'Already so: nothing to change.' 'Green'; Finish 0 }

# A model running now would be cut off by the restart.
$psUri = 'http://{0}/api/ps' -f $(if ($bind -match '^0\.0\.0\.0') { $bind -replace '^0\.0\.0\.0', '127.0.0.1' } else { $bind })
$loaded = @(); try { $loaded = @((Invoke-RestMethod -Uri $psUri -TimeoutSec 5).models | ForEach-Object { $_.name }) } catch { }
if ($loaded.Count -and -not $NoRestart) {
    Say ("Ollama has {0} loaded now ({1}): restarting it cuts off any request running on it." -f $loaded.Count, ($loaded -join ', ')) 'Yellow'
    if ((Read-Host '  Type RESTART to go on, or Enter to stop here').Trim() -cne 'RESTART') { Say 'Nothing was changed.'; Finish 1 }
}
if ($Choice -eq '1' -and -not @(Get-ChildItem -LiteralPath (Split-Path -Parent $wrapper) -Filter 'ollama-serve.before-parallel-*.cmd' -ErrorAction SilentlyContinue).Count) {
    $copy = Join-Path (Split-Path -Parent $wrapper) ('ollama-serve.before-parallel-{0:yyyyMMdd-HHmmss}.cmd' -f (Get-Date)); Copy-Item -LiteralPath $wrapper -Destination $copy; Say "kept a copy of the wrapper: $copy"
}
[IO.File]::WriteAllText($wrapper, $new, (New-Object Text.ASCIIEncoding))
Say $(if ($Choice -eq '1') { "wrapper: OLLAMA_NUM_PARALLEL=$Parallel" } else { 'wrapper: as before' }) 'Green'
if ($NoRestart) { Say 'RESULT wrapper changed; Ollama not restarted (-NoRestart).'; Finish 0 }

# Restart as Lab.Head Restart-LabOllamaServer does: only this installation's Ollama processes, then the boot task.
# 1.6.18: the boot task and its cmd.exe end first: cmd.exe reads the wrapper by byte position and would otherwise read on
# in the rewritten file and start Ollama with only part of the new settings.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
foreach ($c in @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($wrapper, [StringComparison]::OrdinalIgnoreCase) -ge 0 })) { try { Stop-Process -Id $c.ProcessId -Force -ErrorAction Stop } catch { } }
$dir = $(if ($exe) { Split-Path -Parent $exe } else { '' }); $stopped = @()
foreach ($p in @(Get-Process -Name 'ollama*' -ErrorAction SilentlyContinue)) {
    $mine = $false; try { $mine = ($dir -and $p.Path -and ([IO.Path]::GetFullPath((Split-Path -Parent $p.Path)) -ieq [IO.Path]::GetFullPath($dir))) } catch { }
    if ($mine) { try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; $stopped += ('{0} ({1})' -f $p.Name, $p.Id) } catch { } }
}
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName $TaskName
$verUri = $psUri -replace '/api/ps$', '/api/version'; $ok = $null; $until = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $until -and -not $ok) { try { $ok = Invoke-RestMethod -Uri $verUri -TimeoutSec 5 } catch { Start-Sleep -Seconds 3 } }
Say ("stopped: {0}; boot task started" -f $(if ($stopped.Count) { $stopped -join ', ' } else { 'nothing' }))
if ($ok) {
    Say ("RESULT success: Ollama {0} answers; OLLAMA_NUM_PARALLEL is {1}." -f $ok.version, $(if ($Choice -eq '1') { $Parallel } else { 'as before' })) 'Green'
    # 1.6.14: Ollama 0.40.15 (pinned, HEAD-OLLAMA) lifts that limit; HEAD-OLLAMA measures it instead of assuming it.
    # 2026-11-14: Ollama ignores OLLAMA_NUM_PARALLEL for some model families and runs them one request at a time
    # (qwen3.5 = family qwen35, and qwen35moe; ollama issue 18938). Measured: 0 of 194 qwen3.5:4b calls overlapped.
    # Gateway 0.4.3 limits those models to one call per head by itself (placement_policy.serial_families).
    if ($Choice -eq '1') { Say 'NOTE: this is a setting, not a measurement. Ollama still runs qwen3.5 models (family qwen35, qwen35moe) one request at a time; the Gateway (0.4.3) sends them one per head.' 'Yellow' }
    Finish 0
}
Say 'RESULT Ollama did not answer within 3 minutes after the restart. Restart this head, then bring the stick back.' 'Red'; Finish 1
