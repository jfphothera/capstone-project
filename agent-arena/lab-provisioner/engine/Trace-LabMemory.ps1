# Trace-LabMemory.ps1 - MEMORY-TRACE.cmd at the stick's root, on a lab head, as the signed-in user (no administrator).
# Head 7 (2026-11-10) had 43.9 of 44.7 GB committed while all its programs together held about 6 GB: the kernel's
# paged pool held 24 GB and its nonpaged pool 3.1 GB. A restart clears it; it comes back. This finds out what makes it
# grow, on the head itself, by running the head four ways and measuring each:
#   1 quiet       Agent Arena closed, no inference
#   2 Arena       Agent Arena open (its live window), no inference
#   3 inference   Agent Arena closed, a steady loop of small requests to this head's own Ollama (127.0.0.1, no Gateway)
#   4 both        Agent Arena open and the inference loop
# Every 2 minutes: committed memory, both kernel pools and their biggest tags, handles, Agent Arena's WebView2 and app
# memory, Ollama's and Defender's. At the start and end of every phase every pool tag and every program is recorded,
# so the summary names what grew, how fast, in which phase, and which driver file owns the tag. Agent Arena is left as
# it was found (open again if it was open). Nothing is changed or deleted; results go to this stick
# (reports\<computer>\memory-trace-<date>) and to %LOCALAPPDATA%\AgentArenaLab.
param([int]$Minutes = 30, [int]$SampleSeconds = 120, [string]$StickRoot = '', [string]$ViewerExe = '', [string[]]$ViewerArgs = @(),
      [string]$OllamaUrl = 'http://127.0.0.1:11434')
$ErrorActionPreference = 'Continue'
if (-not $StickRoot) { $StickRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
Import-Module (Join-Path $PSScriptRoot 'modules\Lab.Memory.psm1') -Force -DisableNameChecking
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$local = Join-Path $env:LOCALAPPDATA "AgentArenaLab\memory-trace-$stamp"
New-Item -ItemType Directory -Path $local -Force | Out-Null
$stickOut = Join-Path $StickRoot "reports\$env:COMPUTERNAME\memory-trace-$stamp"
$log = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)); [IO.File]::WriteAllText((Join-Path $local 'TRACE-LOG.txt'), ($log -join "`r`n")) }
function Save-Json([string]$name, $obj) { [IO.File]::WriteAllText((Join-Path $local $name), ($obj | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false)) }
function Copy-ToStick { try { if (Test-Path -LiteralPath $StickRoot) { New-Item -ItemType Directory -Path $stickOut -Force | Out-Null; Copy-Item -Path (Join-Path $local '*') -Destination $stickOut -Recurse -Force -ErrorAction Stop } } catch { Write-Host "  (the stick is not reachable: everything is kept in $local)" -ForegroundColor Yellow } }
$inv = [Globalization.CultureInfo]::InvariantCulture

# ---- Agent Arena: the installed viewer of this user, closed and opened by its own exe.
if (-not $ViewerExe) {
    $cur = Join-Path $env:LOCALAPPDATA 'Programs\AgentArena\current.txt'
    if (Test-Path -LiteralPath $cur) { $v = ([string](Get-Content -LiteralPath $cur -TotalCount 1)).Trim(); $ViewerExe = Join-Path $env:LOCALAPPDATA "Programs\AgentArena\$v\AgentArena.exe"; if (-not $ViewerArgs.Count) { $ViewerArgs = @() } }
}
$haveViewer = [bool]($ViewerExe -and (Test-Path -LiteralPath $ViewerExe))
$viewerVersion = if ($haveViewer) { (Get-Item -LiteralPath $ViewerExe).VersionInfo.ProductVersion } else { '' }
$viewerWasOpen = @(Get-Process AgentArena -ErrorAction SilentlyContinue).Count -gt 0
function Get-ArenaWeb { @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'webview-exe-name=AgentArena\.exe' }) }
function Stop-Viewer {
    foreach ($p in @(Get-Process AgentArena -ErrorAction SilentlyContinue)) { try { [void]$p.CloseMainWindow(); if (-not $p.WaitForExit(15000)) { $p.Kill() } } catch { } }
    $deadline = (Get-Date).AddSeconds(30); while ((Get-Date) -lt $deadline -and (Get-ArenaWeb).Count) { Start-Sleep -Seconds 2 }
}
function Start-Viewer {
    if (-not $haveViewer) { return $false }
    if (-not @(Get-Process AgentArena -ErrorAction SilentlyContinue).Count) { if ($ViewerArgs.Count) { Start-Process -FilePath $ViewerExe -ArgumentList $ViewerArgs | Out-Null } else { Start-Process -FilePath $ViewerExe | Out-Null } }
    $deadline = (Get-Date).AddSeconds(60); while ((Get-Date) -lt $deadline -and -not (Get-ArenaWeb).Count) { Start-Sleep -Seconds 2 }
    return [bool](Get-ArenaWeb).Count
}

# ---- Inference: the smallest chat model this head's Ollama has, one request after another.
$model = ''
try {
    $tags = @((Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 10).models)
    $chat = @($tags | Where-Object { $_.name -notmatch '(?i)embed|minilm|bge|e5-|nomic|granite-embedding' } | Sort-Object size)
    if ($chat.Count) { $model = [string]$chat[0].name }
} catch { }
$loadFile = Join-Path $local 'inference-count.txt'
$loadScript = {
    param($url, $model, $file)
    $n = 0; $bad = 0
    while ($true) {
        try { $body = @{ model = $model; prompt = "Write one short sentence about the number $n."; stream = $false; options = @{ num_predict = 48 } } | ConvertTo-Json
              Invoke-RestMethod -Uri "$url/api/generate" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 300 | Out-Null; $n++ }
        catch { $bad++; Start-Sleep -Seconds 5 }
        [IO.File]::WriteAllText($file, "$n $bad")
    }
}
$job = $null
function Start-Load { if (-not $model) { return $false }; [IO.File]::WriteAllText($loadFile, '0 0'); $script:job = Start-Job -ScriptBlock $loadScript -ArgumentList $OllamaUrl, $model, $loadFile; return $true }
function Stop-Load { if ($script:job) { Stop-Job $script:job -ErrorAction SilentlyContinue; Remove-Job $script:job -Force -ErrorAction SilentlyContinue; $script:job = $null } }
function Get-LoadCount { if (Test-Path -LiteralPath $loadFile) { $p = ([IO.File]::ReadAllText($loadFile)).Split(' '); return [pscustomobject]@{ ok = [int]$p[0]; failed = [int]$p[1] } }; return [pscustomobject]@{ ok = 0; failed = 0 } }

# ---- Measuring.
$csv = Join-Path $local 'samples.csv'
'time,phase,committed_gb,limit_gb,paged_mb,nonpaged_mb,handles,arena_webview_mb,arena_webview_procs,arena_app_mb,ollama_mb,defender_mb,paged_top,nonpaged_top,requests_ok,requests_failed' | Set-Content -LiteralPath $csv -Encoding ascii
function Get-Programs {
    $t = @{}
    foreach ($g in @(Get-Process -ErrorAction SilentlyContinue | Group-Object ProcessName)) { $t[$g.Name] = [long[]]@(($g.Group | Measure-Object PrivateMemorySize64 -Sum).Sum, ($g.Group | Measure-Object HandleCount -Sum).Sum, $g.Count) }
    return $t
}
function Sample([string]$phase) {
    $m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $web = @(Get-ArenaWeb); $webMb = 0; foreach ($w in $web) { $p = Get-Process -Id $w.ProcessId -ErrorAction SilentlyContinue; if ($p) { $webMb += $p.PrivateMemorySize64 / 1MB } }
    $sum = { param($n) [math]::Round(((@(Get-Process -Name $n -ErrorAction SilentlyContinue) | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB) }
    $top = @(Get-LabPoolTags -Top 3); $lc = Get-LoadCount
    $tg = { param($pool) (@($top | Where-Object pool -eq $pool | ForEach-Object { '{0}={1}' -f $_.tag.Trim(), $_.mb.ToString($inv) }) -join ' ') }
    $row = @((Get-Date).ToString('s'), $phase, [math]::Round($m.CommittedBytes / 1GB, 2).ToString($inv), [math]::Round($m.CommitLimit / 1GB, 1).ToString($inv),
        [math]::Round($m.PoolPagedBytes / 1MB), [math]::Round($m.PoolNonpagedBytes / 1MB), [long]((Get-Process | Measure-Object HandleCount -Sum).Sum),
        [math]::Round($webMb), $web.Count, (& $sum 'AgentArena'), (& $sum 'ollama'), (& $sum 'MsMpEng'), (& $tg 'paged'), (& $tg 'nonpaged'), $lc.ok, $lc.failed) -join ','
    Add-Content -LiteralPath $csv -Value $row -Encoding ascii
    return [pscustomobject]@{ at = Get-Date; paged = [math]::Round($m.PoolPagedBytes / 1MB); nonpaged = [math]::Round($m.PoolNonpagedBytes / 1MB); committed = [math]::Round($m.CommittedBytes / 1GB, 2); web = [math]::Round($webMb) }
}
function Get-Slope($samples) {
    # MB per hour of paged + nonpaged pool: the least-squares line through every sample of the phase, so one jump
    # (a model loading, memory compression) does not decide the phase the way its first and last samples would.
    $pts = @($samples); if ($pts.Count -lt 3) { return $null }
    $t0 = $pts[0].at; $xs = @($pts | ForEach-Object { ($_.at - $t0).TotalHours }); $ys = @($pts | ForEach-Object { [double]($_.paged + $_.nonpaged) })
    $n = $pts.Count; $mx = ($xs | Measure-Object -Average).Average; $my = ($ys | Measure-Object -Average).Average
    $num = 0.0; $den = 0.0; for ($i = 0; $i -lt $n; $i++) { $num += ($xs[$i] - $mx) * ($ys[$i] - $my); $den += ($xs[$i] - $mx) * ($xs[$i] - $mx) }
    if ($den -le 0) { return $null }; return [math]::Round($num / $den)
}
function Compare-Tables($a, $b, [double]$hours) {
    $rows = foreach ($k in $b.Keys) { $o = if ($a.ContainsKey($k)) { $a[$k] } else { [long[]]@(0, 0) }; $n = $b[$k]
        [pscustomobject]@{ tag = $k; paged_mb = [math]::Round(($n[0] - $o[0]) / 1MB, 1); nonpaged_mb = [math]::Round(($n[1] - $o[1]) / 1MB, 1) } }
    $rows = @($rows | ForEach-Object { $_ | Add-Member -NotePropertyName total_mb -NotePropertyValue ($_.paged_mb + $_.nonpaged_mb) -PassThru } | Sort-Object total_mb -Descending | Select-Object -First 8)
    foreach ($r in $rows) { $r | Add-Member -NotePropertyName per_hour_mb -NotePropertyValue ([math]::Round($r.total_mb / [math]::Max($hours, 0.01), 0)) }
    return $rows
}
function Compare-Programs($a, $b) {
    @(foreach ($k in $b.Keys) { $o = if ($a.ContainsKey($k)) { $a[$k] } else { [long[]]@(0, 0, 0) }; $n = $b[$k]
        [pscustomobject]@{ program = $k; private_mb = [math]::Round(($n[0] - $o[0]) / 1MB, 1); handles = $n[1] - $o[1]; processes = "$($o[2])->$($n[2])" } }) | Sort-Object { [math]::Abs($_.private_mb) + [math]::Abs($_.handles) / 100 } -Descending | Select-Object -First 8
}

# ---- Run.
Write-Host ''; Write-Host "  Agent Arena Lab: memory trace on $env:COMPUTERNAME ($(4 * $Minutes) minutes in four phases)" -ForegroundColor Green
Write-Host '  Leave this window open and this computer on the lab cable; it changes nothing and deletes nothing.' -ForegroundColor Yellow
Say ("Agent Arena: {0}; inference model: {1}" -f $(if ($haveViewer) { "$viewerVersion ($ViewerExe), open now: $viewerWasOpen" } else { 'not installed for this user (phases 2 and 4 run without it)' }), $(if ($model) { $model } else { 'none (phases 3 and 4 run without inference)' }))
foreach ($v in @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue)) { Say ("graphics: {0}, driver {1}" -f $v.Name, $v.DriverVersion) }
$open = @(Format-LabKernelMemory -Tags (Get-LabPoolTags -Top 12 -FindDrivers) -Handles (Get-LabHandleHolders -Top 12))
$m0 = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; $os = Get-CimInstance Win32_OperatingSystem
$open = @(('{0}: committed {1:N1} of {2:N1} GB; paged pool {3:N0} MB, nonpaged {4:N0} MB; up since {5:s}' -f $env:COMPUTERNAME, ($m0.CommittedBytes / 1GB), ($m0.CommitLimit / 1GB), ($m0.PoolPagedBytes / 1MB), ($m0.PoolNonpagedBytes / 1MB), $os.LastBootUpTime)) + $open
[IO.File]::WriteAllText((Join-Path $local 'opening-snapshot.txt'), ($open -join "`r`n"))
Say ("now: committed {0:N1} of {1:N1} GB; paged pool {2:N0} MB; nonpaged {3:N0} MB (opening-snapshot.txt names the biggest holders)" -f ($m0.CommittedBytes / 1GB), ($m0.CommitLimit / 1GB), ($m0.PoolPagedBytes / 1MB), ($m0.PoolNonpagedBytes / 1MB)) 'Cyan'
Copy-ToStick
$phases = @(@{ n = 1; name = 'quiet'; arena = $false; load = $false }, @{ n = 2; name = 'Arena'; arena = $true; load = $false },
            @{ n = 3; name = 'inference'; arena = $false; load = $true }, @{ n = 4; name = 'both'; arena = $true; load = $true })
$results = @()
try {
    foreach ($ph in $phases) {
        Stop-Load
        if ($ph.arena) { $on = Start-Viewer } else { Stop-Viewer; $on = $false }
        $loading = if ($ph.load) { Start-Load } else { $false }
        $label = "phase $($ph.n) of 4 ($($ph.name))"
        Say ("{0}: Agent Arena {1}; inference {2}" -f $label, $(if ($on) { 'open' } else { 'closed' }), $(if ($loading) { "running ($model)" } else { 'off' })) 'Green'
        Start-Sleep -Seconds ([math]::Min(120, [math]::Max(20, $Minutes * 6)))   # settle: memory handed back after a change must not count against the phase
        $t0 = Get-Date; $tags0 = Get-LabPoolTagTable; $prog0 = Get-Programs; $s0 = Sample $ph.name; $lc0 = Get-LoadCount; $all = @($s0)
        $end = $t0.AddMinutes($Minutes)
        while ((Get-Date) -lt $end) {
            Start-Sleep -Seconds ([math]::Min($SampleSeconds, [math]::Max(1, ($end - (Get-Date)).TotalSeconds)))
            $s = Sample $ph.name; $all += $s
            if ($ph.arena -and -not (Get-ArenaWeb).Count) { $on = $false }      # Agent Arena closed or crashed during the phase
            Write-Host ("    {0:HH:mm}  {1}, {2:N0} of {3} min: paged {4:N0} MB ({5:+#,0;-#,0;0}), nonpaged {6:N0} MB ({7:+#,0;-#,0;0}), Agent Arena display {8:N0} MB" -f (Get-Date), $label, ((Get-Date) - $t0).TotalMinutes, $Minutes, $s.paged, ($s.paged - $s0.paged), $s.nonpaged, ($s.nonpaged - $s0.nonpaged), $s.web)
        }
        $hours = ((Get-Date) - $t0).TotalHours; $tags1 = Get-LabPoolTagTable; $prog1 = Get-Programs; $s1 = Sample $ph.name; $lc1 = Get-LoadCount; $all += $s1
        if ($loading -and ($lc1.ok - $lc0.ok) -lt 1) { $loading = $false }   # no request answered: inference did not run
        $grew = Compare-Tables $tags0 $tags1 $hours
        foreach ($g in $grew) { $w = Get-LabTagMeaning $g.tag; if (-not $w -and $g.total_mb -ge 5) { $w = (Find-LabTagDrivers -Tag $g.tag); if ($w) { $w = 'in ' + $w } }; $g | Add-Member -NotePropertyName what -NotePropertyValue $w }
        $r = [pscustomobject][ordered]@{ phase = $ph.n; name = $ph.name; arena_open = $on; inference = $loading; minutes = [math]::Round($hours * 60, 1)
            pool_mb_per_hour = (Get-Slope $all); samples = $all.Count
            paged_mb = $s1.paged - $s0.paged; nonpaged_mb = $s1.nonpaged - $s0.nonpaged; committed_gb = [math]::Round($s1.committed - $s0.committed, 2)
            requests = ($lc1.ok - $lc0.ok); failed_requests = ($lc1.failed - $lc0.failed); tags = $grew; programs = @(Compare-Programs $prog0 $prog1) }
        $results += $r; Save-Json 'phases.json' $results
        Say ("{0} done (Agent Arena {7}, inference {8}): kernel pools {1:+#,0;-#,0;0} MB in {2} min, trend {3} MB/h; fastest tag {4} {5:+0.0;-0.0} MB{6}" -f $label, ($r.paged_mb + $r.nonpaged_mb), $r.minutes, $(if ($null -eq $r.pool_mb_per_hour) { 'n/a' } else { '{0:+#,0;-#,0;0}' -f $r.pool_mb_per_hour }), $grew[0].tag, $grew[0].total_mb, $(if ($grew[0].what) { " ($($grew[0].what))" } else { '' }), $(if ($on) { 'open' } else { 'NOT open' }), $(if ($loading) { "ran ($($r.requests) requests)" } else { 'off' })) 'Cyan'
        Copy-ToStick
    }
} finally {
    Stop-Load
    if ($viewerWasOpen) { [void](Start-Viewer) } else { Stop-Viewer }
}

# ---- Verdict: what Agent Arena and inference each add, against the quiet phase.
$lines = @("MEMORY TRACE  $env:COMPUTERNAME  $stamp", "Agent Arena $(if ($haveViewer) { $viewerVersion } else { 'not installed' }); inference model $(if ($model) { $model } else { 'none' }); $Minutes min per phase", '')
$lines += $open; $lines += '', 'PHASES (kernel paged + nonpaged pool growth):'
foreach ($r in $results) {
    $lines += ('  {0} {1,-9} Arena {2,-6} inference {3,-6} {4,6:+#,0;-#,0;0} MB/h  ({5:+#,0;-#,0;0} MB paged, {6:+#,0;-#,0;0} MB nonpaged; committed {7:+0.00;-0.00} GB; {8} requests, {9} failed)' -f $r.phase, $r.name, $r.arena_open, $r.inference, $r.pool_mb_per_hour, $r.paged_mb, $r.nonpaged_mb, $r.committed_gb, $r.requests, $r.failed_requests)
    foreach ($g in @($r.tags | Select-Object -First 4)) { $lines += ('        tag {0}  {1:+0.0;-0.0} MB ({2:+#,0;-#,0;0} MB/h)  {3}' -f $g.tag, $g.total_mb, $g.per_hour_mb, $g.what) }
    foreach ($p in @($r.programs | Select-Object -First 3)) { $lines += ('        program {0}: {1:+0.0;-0.0} MB, {2:+#,0;-#,0;0} handles, processes {3}' -f $p.program, $p.private_mb, $p.handles, $p.processes) }
}
$lines += ''; $lines += @(Get-LabTraceVerdict -Phases $results -Minutes $Minutes)
[IO.File]::WriteAllText((Join-Path $local 'SUMMARY.txt'), ($lines -join "`r`n"), (New-Object Text.UTF8Encoding $false))
Copy-ToStick
Write-Host ''; $lines | Select-Object -Last 6 | ForEach-Object { Write-Host "  $_" -ForegroundColor Cyan }
Write-Host ''; Write-Host "  Saved to $stickOut" -ForegroundColor Green
