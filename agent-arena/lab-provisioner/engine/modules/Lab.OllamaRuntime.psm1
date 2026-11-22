<#
    Lab.OllamaRuntime (provisioner 1.6.14) - the pinned Ollama runtime on a head, the explicit server settings in the
    boot wrapper, and the pure measurement rules of the parallel qualification. Standalone: no other Lab module is
    needed, so the head one-click (engine\head-tools\head-ollama) and the provisioner share the same code.

    * The runtime is the official Windows archive pinned in config ollama.runtime (version, file, sha256). It unpacks
      into C:\ProgramData\AgentArenaLab\ollama\runtime\<version> beside the old per-user install, which stays as it
      is for rollback. Nothing is ever deleted.
    * The wrapper keeps its OLLAMA_HOST and OLLAMA_MODELS lines; only the managed keys below are written, just before
      the serve line, and the server's output goes to a log so its own "server config" line proves what it applied.
    * Overlap is real only when the compute windows overlap: created_at - (prompt_eval_duration + eval_duration)
      to created_at, from each reply. Simultaneous HTTP requests are not proof.
#>

$script:ManagedKeys = @('OLLAMA_CONTEXT_LENGTH', 'OLLAMA_NUM_PARALLEL', 'OLLAMA_MAX_LOADED_MODELS', 'OLLAMA_MAX_QUEUE', 'OLLAMA_KEEP_ALIVE', 'OLLAMA_FLASH_ATTENTION', 'OLLAMA_KV_CACHE_TYPE')

function Get-LabOllamaPaths {
    param([string]$Root = 'C:\ProgramData\AgentArenaLab')
    $dir = Join-Path $Root 'ollama'
    return [pscustomobject][ordered]@{ Root = $Root; Dir = $dir; Wrapper = (Join-Path $dir 'ollama-serve.cmd'); Settings = (Join-Path $dir 'ollama-settings.json')
                                       Runtime = (Join-Path $dir 'runtime'); Backups = (Join-Path $dir 'backups'); Logs = (Join-Path $dir 'logs') }
}

# ---------------------------------------------------------------------------
# The wrapper (pure)
# ---------------------------------------------------------------------------

function Test-LabWrapperServeLine { param([string]$Line) return [bool]($Line -match '^\s*"[^"]*ollama(\.exe)?"\s+serve(\s|$)') }

function Get-LabWrapperFacts {
    <#  Pure. The executable, every 'set "K=V"' and the log target of a provisioner wrapper.  #>
    param([string]$Text)
    $envMap = [ordered]@{}; $exe = ''; $log = ''
    foreach ($l in @($Text -split "`r?`n")) {
        if ($l -match '^\s*set\s+"?([A-Za-z_][A-Za-z0-9_]*)=([^"]*)"?\s*$') { $envMap[$Matches[1]] = $Matches[2] }
        elseif (Test-LabWrapperServeLine $l) {
            if ($l -match '^\s*"([^"]+)"') { $exe = $Matches[1] }
            if ($l -match '>>\s*"([^"]+)"') { $log = $Matches[1] }
        }
    }
    return [pscustomobject][ordered]@{ Executable = $exe; Env = $envMap; LogFile = $log }
}

function Set-LabWrapperContent {
    <#  Pure. The wrapper with the managed keys of $Settings set just before the serve line (a key with an empty value
        is taken out: the runtime default), the serve line on $Executable (unchanged when empty), and its output appended
        to $LogFile (unchanged when $LogFile is $null; '' takes the redirection out). OLLAMA_HOST, OLLAMA_MODELS and every
        other line are kept as they are. Throws for a file that is not the provisioner's wrapper.  #>
    param([Parameter(Mandatory)][string]$Text, [System.Collections.IDictionary]$Settings = @{}, [string]$Executable = '', $LogFile = $null)
    foreach ($k in @($Settings.Keys)) { if ($script:ManagedKeys -notcontains $k) { throw "the wrapper only manages $($script:ManagedKeys -join ', '), not $k" } }
    $lines = @($Text -split "`r?`n")
    while ($lines.Count -and $lines[-1] -eq '') { $lines = @($lines | Select-Object -First ($lines.Count - 1)) }
    $serve = @($lines | Where-Object { Test-LabWrapperServeLine $_ })
    if ($serve.Count -ne 1) { throw 'the wrapper has no single "... serve" line: it is not the provisioner''s wrapper' }
    $facts = Get-LabWrapperFacts -Text $Text
    $exe = $(if ($Executable) { $Executable } else { $facts.Executable })
    $log = $(if ($null -eq $LogFile) { $facts.LogFile } else { [string]$LogFile })
    $keep = @()
    foreach ($l in $lines) {
        if (Test-LabWrapperServeLine $l) { continue }
        if ($l -match '^\s*set\s+"?([A-Za-z_][A-Za-z0-9_]*)=' -and $Settings.Contains($Matches[1])) { continue }
        if ($l -match '^\s*if not exist "[^"]+" mkdir "[^"]+"\s*$') { continue }
        $keep += $l
    }
    foreach ($k in $script:ManagedKeys) { if ($Settings.Contains($k) -and -not [string]::IsNullOrEmpty([string]$Settings[$k])) { $keep += ('set "{0}={1}"' -f $k, $Settings[$k]) } }
    if ($log) { $d = Split-Path -Parent $log; $keep += ('if not exist "{0}" mkdir "{0}"' -f $d); $keep += ('"{0}" serve >> "{1}" 2>&1' -f $exe, $log) }
    else { $keep += ('"{0}" serve' -f $exe) }
    return ($keep -join "`r`n") + "`r`n"
}

function Get-LabMaxLoadedModels {
    <#  Pure. OLLAMA_MAX_LOADED_MODELS to try on a head: 1 on a small head (both 4B models would not fit beside Windows
        at num_ctx 8192 with parallel slots), else the default (the worker and the reviewer model resident together).  #>
    param([double]$RamGb, $Rule)
    $small = $(if ($Rule -and $null -ne $Rule.small_head_ram_gb) { [double]$Rule.small_head_ram_gb } else { 12 })
    if ($RamGb -le $small) { return $(if ($Rule -and $Rule.small_head) { [int]$Rule.small_head } else { 1 }) }
    return $(if ($Rule -and $Rule.default) { [int]$Rule.default } else { 2 })
}

function Get-LabMachineClass {
    <#  Pure. 1.6.19: the head's machine class from config ollama.server_settings.machine_classes (first match, in order):
        a class with min_vram_gb matches a head whose NVIDIA card has at least that much memory; otherwise a class matches
        when the head's RAM is at least min_ram_gb. Each class says which requests-at-once levels are tested, how many
        models may stay loaded, which models are qualified, which one is the worker (it sets OLLAMA_NUM_PARALLEL), and the
        Gateway's slots and memory_gb for the head. Decided with the lab operator on 2026-11-17 from head 8's measurements.  #>
    param([double]$RamGb, $VramGb = $null, $Classes)
    foreach ($c in @($Classes)) {
        if ($null -ne $c.min_vram_gb) { if ($null -ne $VramGb -and [double]$VramGb -ge [double]$c.min_vram_gb) { return $c } ; continue }
        if ($RamGb -ge [double]$c.min_ram_gb) { return $c }
    }
    return $null
}

function New-LabServerSettings {
    <#  Pure. The managed wrapper keys from config ollama.server_settings, for one parallel level and resident count.  #>
    param([Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][int]$Parallel, [Parameter(Mandatory)][int]$MaxLoaded)
    $s = [ordered]@{}
    $s['OLLAMA_CONTEXT_LENGTH'] = [string][int]$Spec.context_length
    $s['OLLAMA_NUM_PARALLEL'] = [string]$Parallel
    $s['OLLAMA_MAX_LOADED_MODELS'] = [string]$MaxLoaded
    $s['OLLAMA_MAX_QUEUE'] = [string][int]$Spec.max_queue
    $s['OLLAMA_KEEP_ALIVE'] = [string]$Spec.keep_alive
    $s['OLLAMA_FLASH_ATTENTION'] = [string]$Spec.flash_attention
    $s['OLLAMA_KV_CACHE_TYPE'] = [string]$Spec.kv_cache_type
    return $s
}

# ---------------------------------------------------------------------------
# What the server itself says (pure)
# ---------------------------------------------------------------------------

function Get-LabServerConfigFromLog {
    <#  Pure. The last 'msg="server config" env="map[K:V ...]"' line of an Ollama server log, as a map, plus the
        backend from the 'inference compute' lines after it (library=cpu / cuda / rocm). $null when absent.  #>
    param([string]$Text)
    $lines = @($Text -split "`r?`n")
    $at = -1; for ($i = $lines.Count - 1; $i -ge 0; $i--) { if ($lines[$i] -match 'msg="server config"') { $at = $i; break } }
    if ($at -lt 0) { return $null }
    $map = [ordered]@{}
    if ($lines[$at] -match 'env="map\[(.*)\]"') {
        foreach ($m in [regex]::Matches($Matches[1], '(OLLAMA_[A-Z_]+|HTTPS?_PROXY|NO_PROXY|CUDA_VISIBLE_DEVICES|ROCR_VISIBLE_DEVICES|GPU_DEVICE_ORDINAL|HIP_VISIBLE_DEVICES):(.*?)(?=\s+[A-Z_]+:|$)')) { $map[$m.Groups[1].Value] = $m.Groups[2].Value.Trim() }
    }
    $libs = @(); for ($i = $at + 1; $i -lt $lines.Count; $i++) { if ($lines[$i] -match 'msg="inference compute".*\blibrary=([A-Za-z0-9_]+)') { $libs += $Matches[1].ToLowerInvariant() } }
    $version = $(if ($lines[$at] -match '\bversion=([0-9][0-9.]*)') { $Matches[1] } else { '' })
    for ($i = $at; $i -lt $lines.Count -and -not $version; $i++) { if ($lines[$i] -match 'msg="Listening on .*\(version ([0-9][0-9.]*)\)') { $version = $Matches[1] } }
    return [pscustomobject][ordered]@{ Env = $map; Backend = $(if ($libs.Count) { ($libs | Select-Object -Unique) -join '+' } else { '' }); Version = $version; Line = $lines[$at] }
}

function Test-LabServerSettingsApplied {
    <#  Pure. Which wanted managed keys the server's own config line shows with another value. Ollama prints every
        known key, so a key it shows empty/default when we set it, or with another value, is a mismatch.  #>
    param([System.Collections.IDictionary]$Wanted, [System.Collections.IDictionary]$Seen)
    $bad = @()
    foreach ($k in @($Wanted.Keys)) {
        $w = [string]$Wanted[$k]; if ([string]::IsNullOrEmpty($w)) { continue }
        $s = $(if ($Seen -and $Seen.Contains($k)) { [string]$Seen[$k] } else { $null })
        if ($null -eq $s) { $bad += "$k not shown"; continue }
        $wn = $w; $sn = $s
        if ($k -eq 'OLLAMA_KEEP_ALIVE') { $wn = ConvertTo-LabSeconds $w; $sn = ConvertTo-LabSeconds $s }
        if ([string]$wn -ne [string]$sn) { $bad += "$k=$s (wanted $w)" }
    }
    return $bad
}

function ConvertTo-LabSeconds {
    <#  Pure. '30m', '1h0m0s', '1800', '30m0s' -> seconds; anything else unchanged.  #>
    param([string]$Value)
    $v = $Value.Trim()
    if ($v -match '^\d+$') { return [int]$v }
    if ($v -match '^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$' -and $v) { return [int]([int]$Matches[1] * 3600 + [int]$Matches[2] * 60 + [double]$(if ($Matches[3]) { $Matches[3] } else { 0 })) }
    return $v
}

# ---------------------------------------------------------------------------
# Measurement (pure)
# ---------------------------------------------------------------------------

function ConvertFrom-LabOllamaTime {
    <#  Pure. Ollama's created_at (RFC 3339 with up to 9 fraction digits) -> Unix seconds (double).  #>
    param([Parameter(Mandatory)]$Value)
    if ($Value -is [DateTime]) { return ($Value.ToUniversalTime().Ticks - 621355968000000000) / 1e7 }   # PowerShell 7's ConvertFrom-Json
    $v = [string]$Value -replace '(\.\d{7})\d+', '$1'
    $d = [DateTimeOffset]::Parse($v, [Globalization.CultureInfo]::InvariantCulture)
    return ($d.UtcTicks - 621355968000000000) / 1e7
}

function Get-LabComputeWindow {
    <#  Pure. One reply's compute window: created_at - (prompt_eval_duration + eval_duration) to created_at.  #>
    param([Parameter(Mandatory)]$Reply)
    $end = ConvertFrom-LabOllamaTime $Reply.created_at
    $ns = [double]$(if ($Reply.prompt_eval_duration) { $Reply.prompt_eval_duration } else { 0 }) + [double]$(if ($Reply.eval_duration) { $Reply.eval_duration } else { 0 })
    return [pscustomobject]@{ Start = $end - $ns / 1e9; End = $end }
}

function Get-LabWindowSweep {
    <#  Pure. The most windows open at one instant and the time at least one is open (seconds). Each window is trimmed
        by Tolerance/2 at both ends, so back-to-back requests (one ends as the next starts, rounded to microseconds in
        created_at) do not count as overlapping; real overlap in parallel generation lasts seconds.  #>
    param([AllowEmptyCollection()][array]$Windows, [double]$Tolerance = 0.05)
    $ev = New-Object System.Collections.ArrayList
    foreach ($w in $Windows) {
        if ($null -eq $w) { continue }
        $s = [double]$w.Start + $Tolerance / 2; $e = [double]$w.End - $Tolerance / 2
        if ($e -gt $s) { [void]$ev.Add(@($s, 1)); [void]$ev.Add(@($e, -1)) }
    }
    $sorted = @($ev | Sort-Object @{ Expression = { $_[0] } }, @{ Expression = { $_[1] } })
    $n = 0; $max = 0; $busy = 0.0; $from = 0.0
    foreach ($x in $sorted) {
        if ($n -eq 0 -and $x[1] -gt 0) { $from = $x[0] }
        $n += $x[1]; if ($n -gt $max) { $max = $n }
        if ($n -eq 0) { $busy += $x[0] - $from }
    }
    return [pscustomobject]@{ MaxOverlap = $max; BusySeconds = ($busy + $Tolerance * [math]::Min(1, $max)) }
}

function Get-LabMaxOverlap {
    <#  Pure. The most compute windows open at one instant (Get-LabWindowSweep).  #>
    param([AllowEmptyCollection()][array]$Windows, [double]$Tolerance = 0.05)
    return (Get-LabWindowSweep -Windows $Windows -Tolerance $Tolerance).MaxOverlap
}

function Test-LabConversationIsolation {
    <#  Pure. Every conversation's answer carries its own marker and no other conversation's marker.  #>
    param([AllowEmptyCollection()][array]$Results)
    $markers = @($Results | ForEach-Object { [string]$_.Marker })
    $fail = @()
    foreach ($r in $Results) {
        $t = [string]$r.Text
        if (-not $t.Contains([string]$r.Marker)) { $fail += ('{0}: its own marker is missing' -f $r.Marker) }
        foreach ($m in $markers) { if ($m -ne $r.Marker -and $t.Contains($m)) { $fail += ('{0}: carries {1}' -f $r.Marker, $m) } }
    }
    return [pscustomobject]@{ Passed = ($fail.Count -eq 0 -and $Results.Count -gt 0); Failures = $fail }
}

function Get-LabPercentile {
    param([AllowEmptyCollection()][double[]]$Values, [double]$P)
    $v = @($Values | Sort-Object); if (-not $v.Count) { return $null }
    $i = [math]::Min($v.Count - 1, [math]::Max(0, [int][math]::Ceiling($P * $v.Count) - 1)); return [math]::Round($v[$i], 2)
}

function Get-LabBatchMetrics {
    <#  Pure. Throughput, latency, queue and overlap of one batch. Each result: Ok, Valid, Reply (Ollama's JSON),
        Sent and Received (Unix seconds, client side).  #>
    param([AllowEmptyCollection()][array]$Results)
    $ok = @($Results | Where-Object { $_.Ok -and $_.Reply })
    $win = @($ok | ForEach-Object { Get-LabComputeWindow -Reply $_.Reply })
    $tokens = [double](($ok | ForEach-Object { [double]$_.Reply.eval_count } | Measure-Object -Sum).Sum)
    # Output tokens per second of the time the server was computing (union of the windows): serial requests are not
    # charged for the client's gaps between them, parallel ones are charged for their common wall time.
    $sweep = Get-LabWindowSweep -Windows $win; $span = $sweep.BusySeconds
    $lat = @($ok | ForEach-Object { [double]($_.Received - $_.Sent) })
    $queue = @($ok | ForEach-Object { [math]::Max(0, [double]($_.Received - $_.Sent) * 1000 - [double]$_.Reply.total_duration / 1e6) })
    $load = @($ok | ForEach-Object { [double]$_.Reply.load_duration / 1e6 })
    return [pscustomobject][ordered]@{
        requests = $Results.Count; failed = @($Results | Where-Object { -not $_.Ok }).Count; invalid = @($ok | Where-Object { -not $_.Valid }).Count
        measured_overlap = $sweep.MaxOverlap; output_tokens = $tokens
        tokens_per_s = $(if ($span -gt 0) { [math]::Round($tokens / $span, 2) } else { 0 })
        latency_p50_s = (Get-LabPercentile $lat 0.5); latency_p95_s = (Get-LabPercentile $lat 0.95)
        queue_ms_avg = $(if ($queue.Count) { [math]::Round(($queue | Measure-Object -Average).Average) } else { $null })
        load_ms_max = $(if ($load.Count) { [math]::Round(($load | Measure-Object -Maximum).Maximum) } else { $null })
    }
}

function Test-LabLevelPassed {
    <#  Pure. A parallel level passes when every request answered, every answer is valid and isolated, the compute
        windows really overlapped at that level, and memory stayed above the floor.  #>
    param([Parameter(Mandatory)][int]$Level, [Parameter(Mandatory)]$Metrics, [Parameter(Mandatory)][bool]$Isolated, $MinFreeGb, [double]$Floor = 1.0)
    $why = @()
    if ($Metrics.failed) { $why += "$($Metrics.failed) request(s) failed" }
    if ($Metrics.invalid) { $why += "$($Metrics.invalid) answer(s) invalid" }
    if (-not $Isolated) { $why += 'conversation isolation failed' }
    if ($Metrics.measured_overlap -lt $Level) { $why += ('compute windows overlapped {0}, not {1}: requests waited in a queue' -f $Metrics.measured_overlap, $Level) }
    if ($null -ne $MinFreeGb -and [double]$MinFreeGb -lt $Floor) { $why += ('free memory fell to {0} GB (floor {1} GB)' -f $MinFreeGb, $Floor) }
    return [pscustomobject]@{ Passed = ($why.Count -eq 0); Reasons = $why }
}

function Select-LabParallelLevel {
    <#  Pure. The highest useful level that passed: levels are tried in order; a level counts only when it and every
        lower level passed and it adds at least $Gain over the level below (the serial baseline below the first).
        1 when the first level fails. Levels: [{Level, Passed, TokensPerS}].  #>
    param([AllowEmptyCollection()][array]$Levels, [double]$SerialTokensPerS, [double]$Gain = 0.10)
    $best = 1; $prev = $SerialTokensPerS
    foreach ($l in @($Levels | Sort-Object Level)) {
        if (-not $l.Passed) { break }
        if ($prev -gt 0 -and [double]$l.TokensPerS -lt $prev * (1 + $Gain)) { break }
        $best = [int]$l.Level; $prev = [double]$l.TokensPerS
    }
    return $best
}

function Test-LabRuntimeVersion {
    <#  Pure. True when $Have is at least $Want (dotted numbers).  #>
    param([string]$Have, [string]$Want)
    try { return ([version]($Have -replace '[^0-9.].*$', '')) -ge ([version]$Want) } catch { return $false }
}

# ---------------------------------------------------------------------------
# The runtime on disk (not pure)
# ---------------------------------------------------------------------------

function Test-LabOllamaSignature {
    param([Parameter(Mandatory)][string]$Path, [string]$Publisher = 'Ollama Inc.')
    try { $s = Get-AuthenticodeSignature -FilePath $Path -ErrorAction Stop } catch { return [pscustomobject]@{ Ok = $false; Detail = $_.Exception.Message } }
    $subject = $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' })
    $ok = ([string]$s.Status -eq 'Valid' -and $subject -match ('CN=' + [regex]::Escape($Publisher) + '(,|$)'))
    return [pscustomobject]@{ Ok = $ok; Detail = ('{0}, {1}' -f $s.Status, $subject) }
}

function Find-LabOllamaArchive {
    <#  The pinned archive on the stick (packages\ollama\<file>) whose SHA-256 is the pinned one; '' when absent.  #>
    param([Parameter(Mandatory)][string]$StickRelease, [Parameter(Mandatory)]$Spec)
    $p = Join-Path $StickRelease ('packages\ollama\' + $Spec.file)
    if (-not (Test-Path -LiteralPath $p)) { return '' }
    return $p
}

function Install-LabOllamaRuntime {
    <#  Unpacks the pinned archive into <install_root>\<version> (once; a verified copy is reused). Checks the archive's
        SHA-256 first and ollama.exe's publisher after; never deletes (an interrupted unpack stays as .partial-*).  #>
    param([Parameter(Mandatory)][string]$Archive, [Parameter(Mandatory)]$Spec)
    $dest = Join-Path $Spec.install_root $Spec.version
    $exe = Join-Path $dest 'ollama.exe'
    if (Test-Path -LiteralPath $exe) {
        $sig = Test-LabOllamaSignature -Path $exe -Publisher $Spec.publisher
        if ($sig.Ok) { return [pscustomobject]@{ Executable = $exe; Method = 'already unpacked'; Detail = $sig.Detail } }
        throw "$exe exists but its signature is not Ollama's ($($sig.Detail)); not used, not removed"
    }
    $hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne ([string]$Spec.sha256).ToLowerInvariant()) { throw "$Archive has SHA-256 $hash, not the pinned $($Spec.sha256); not used" }
    $drive = (Split-Path -Qualifier $Spec.install_root).TrimEnd(':')
    $free = (Get-PSDrive -Name $drive -ErrorAction SilentlyContinue).Free
    $need = [int64]$Spec.size * 2
    if ($free -and $free -lt $need) { throw ('drive {0}: has {1:N1} GB free; unpacking Ollama {2} needs {3:N1} GB' -f $drive, ($free / 1GB), $Spec.version, ($need / 1GB)) }
    New-Item -ItemType Directory -Path $Spec.install_root -Force | Out-Null
    $tmp = Join-Path $Spec.install_root ('{0}.partial-{1:yyyyMMdd-HHmmss}' -f $Spec.version, (Get-Date))
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $tmp)
    $sig = Test-LabOllamaSignature -Path (Join-Path $tmp 'ollama.exe') -Publisher $Spec.publisher
    if (-not $sig.Ok) { throw "the unpacked ollama.exe is not signed by $($Spec.publisher) ($($sig.Detail)); left in $tmp, not used" }
    Rename-Item -LiteralPath $tmp -NewName $Spec.version
    return [pscustomobject]@{ Executable = $exe; Method = 'unpacked from ' + (Split-Path -Leaf $Archive); Detail = $sig.Detail }
}

function Read-LabOllamaSettings {
    <#  The head's active settings record (written by HEAD-OLLAMA), or $null when absent, unreadable or rolled back.  #>
    param([string]$Path = (Get-LabOllamaPaths).Settings)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { $s = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $null }
    if ([string]$s.state -ne 'active' -or -not $s.executable -or -not (Test-Path -LiteralPath ([string]$s.executable))) { return $null }
    return $s
}

function ConvertTo-LabSettingsMap {
    param($Env)
    $m = [ordered]@{}; if ($Env) { foreach ($p in $Env.PSObject.Properties) { $m[$p.Name] = [string]$p.Value } }; return $m
}

function Get-LabServingOllamaExecutable {
    <#  The ollama.exe the boot task serves: the active settings record, else the wrapper's serve line, else the pinned
        runtime when it is unpacked, else the per-user install (Capstone.Ollama). Reprovisioning keeps what HEAD-OLLAMA
        qualified instead of going back to the per-user install.  #>
    param([string]$Root = 'C:\ProgramData\AgentArenaLab', $Spec = $null)
    $p = Get-LabOllamaPaths -Root $Root
    $s = Read-LabOllamaSettings -Path $p.Settings
    if ($s) { return [string]$s.executable }
    if (Test-Path -LiteralPath $p.Wrapper) { $e = (Get-LabWrapperFacts -Text ([IO.File]::ReadAllText($p.Wrapper))).Executable; if ($e -and (Test-Path -LiteralPath $e)) { return $e } }
    if ($Spec) { $e = Join-Path (Join-Path $Spec.install_root $Spec.version) 'ollama.exe'; if (Test-Path -LiteralPath $e) { return $e } }
    if (Get-Command Get-CapstoneOllamaExecutable -ErrorAction SilentlyContinue) { return (Get-CapstoneOllamaExecutable) }
    return $null
}

function Stop-LabOllamaBootHost {
    <#  1.6.18: ends the boot task and every cmd.exe running the wrapper, BEFORE Ollama itself is stopped. cmd.exe reads
        a batch file by byte position: when Ollama stops under a wrapper cmd.exe is still running, that cmd.exe reads on
        in the rewritten file from its old position and starts the new server with only the last few settings (head 8,
        2026-11-17: Ollama 0.40.15 came up with the old OLLAMA_NUM_PARALLEL=10 and no context length). Ends nothing else.  #>
    param([Parameter(Mandatory)][string]$TaskName, [Parameter(Mandatory)][string]$Wrapper)
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $ended = @()
    foreach ($c in @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Wrapper, [StringComparison]::OrdinalIgnoreCase) -ge 0 })) {
        try { Stop-Process -Id $c.ProcessId -Force -ErrorAction Stop; $ended += ('cmd.exe ({0})' -f $c.ProcessId) } catch { }
    }
    $until = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $until -and [string](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -eq 'Running') { Start-Sleep -Milliseconds 500 }
    return $ended
}

Export-ModuleMember -Function @(
    'Stop-LabOllamaBootHost', 'Get-LabOllamaPaths', 'Test-LabWrapperServeLine', 'Get-LabWrapperFacts', 'Set-LabWrapperContent', 'Get-LabMaxLoadedModels', 'Get-LabMachineClass', 'New-LabServerSettings',
    'Get-LabServerConfigFromLog', 'Test-LabServerSettingsApplied', 'ConvertTo-LabSeconds', 'ConvertFrom-LabOllamaTime', 'Get-LabComputeWindow', 'Get-LabWindowSweep', 'Get-LabMaxOverlap',
    'Test-LabConversationIsolation', 'Get-LabPercentile', 'Get-LabBatchMetrics', 'Test-LabLevelPassed', 'Select-LabParallelLevel', 'Test-LabRuntimeVersion',
    'Test-LabOllamaSignature', 'Find-LabOllamaArchive', 'Install-LabOllamaRuntime', 'Read-LabOllamaSettings', 'ConvertTo-LabSettingsMap', 'Get-LabServingOllamaExecutable')
