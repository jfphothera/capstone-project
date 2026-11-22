# Update-HeadOllama.ps1 - run by HEAD-OLLAMA.cmd on an inference head, as administrator (provisioner 1.6.14, 2026-11-17).
#   1 = update Ollama to the pinned version from this stick, set the explicit server settings, restart, prove the running
#       server's version, then qualify parallel inference (about an hour) and keep the best tested settings;
#   2 = qualify again (the pinned Ollama is already in place);
#   0 = roll back to the Ollama and boot wrapper this head had before its first update.
# The pinned archive is on the stick (packages\ollama): no internet is needed. Models, OLLAMA_HOST, OLLAMA_MODELS, the
# firewall and the lab cable are left as they are (checked before and after). Deletes nothing: the old ollama.exe stays
# where it is, every wrapper version is kept in C:\ProgramData\AgentArenaLab\ollama\backups.
# Reports: reports\<computer>\ollama-qualify-<time>.json (the hub's APPLY-MODEL-CAPACITY reads it) and head-ollama-<time>.txt.
param([string]$Choice = '', [string]$TaskName = 'Capstone Ollama Server', [string]$Root = 'C:\ProgramData\AgentArenaLab', [switch]$AllowProtected)
$ErrorActionPreference = 'Continue'
Import-Module (Join-Path $PSScriptRoot '..\..\modules\Lab.OllamaRuntime.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'HeadOllama.Qualify.psm1') -Force -DisableNameChecking
if ($Choice -eq 'selftest') { return }

$stamp = '{0:yyyyMMdd-HHmmss}' -f (Get-Date)
$release = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
# 1.6.18: the outermost folder with START-LAB-PROVISIONER.cmd (the stick root), not the release folder that has one too.
$stick = $null; $d = $PSScriptRoot; while ($d) { if (Test-Path -LiteralPath (Join-Path $d 'START-LAB-PROVISIONER.cmd')) { $stick = $d }; $up = Split-Path -Parent $d; if (-not $up -or $up -eq $d) { $d = $null } else { $d = $up } }
$reportDir = $(if ($stick) { Join-Path $stick ('reports\' + $env:COMPUTERNAME) } else { Join-Path $Root 'reports' })
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$log = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }
function Finish([int]$code) { [IO.File]::WriteAllText((Join-Path $reportDir "head-ollama-$stamp.txt"), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false)); exit $code }
function Save-Json($obj, [string]$path) { [IO.File]::WriteAllText($path, ($obj | ConvertTo-Json -Depth 14), (New-Object Text.UTF8Encoding $false)) }

Write-Host ''; Write-Host '  Agent Arena lab head: pinned Ollama and verified parallel inference (1.6.18)' -ForegroundColor Green
$cfgPath = Join-Path $release 'config\lab-network.json'
if (-not (Test-Path -LiteralPath $cfgPath)) { Say "The lab configuration is missing ($cfgPath): run this from the provisioner stick. Nothing was changed." 'Red'; Finish 2 }
$cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
$spec = $cfg.ollama.runtime; $ss = $cfg.ollama.server_settings
$paths = Get-LabOllamaPaths -Root $Root
$ip = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -match '^192\.0\.2\.(\d{1,3})$' -and [int]$Matches[1] -ge 2 -and [int]$Matches[1] -le 199 } | Select-Object -First 1).IPAddress
if (-not $ip) { Say 'This computer has no head address (192.0.2.2-199): it is not an inference head. Nothing was changed.' 'Red'; Finish 2 }
$prot = @($cfg.protected_addresses | Where-Object { $_.address -eq $ip }) | Select-Object -First 1
if ($prot -and -not $AllowProtected) { Say ("Head {0} is protected in the lab plan ({1}). HEAD-OLLAMA changes it only under a separately approved plan (HEAD-OLLAMA.cmd -AllowProtected). Nothing was changed." -f $ip, $prot.reason) 'Yellow'; Finish 2 }
$rid = 'head-' + ($ip -replace '\.', '-')
$base = 'http://127.0.0.1:{0}' -f [int]$cfg.ollama.port
Say ("computer {0}, head {1}, user {2}" -f $env:COMPUTERNAME, $ip, $env:USERNAME)

# ---------------------------------------------------------------- what is there now
function Get-Api([string]$p, [int]$t = 10) { try { Invoke-RestMethod -Uri ($base + $p) -TimeoutSec $t } catch { $null } }
function Get-ServerLogLength { $f = $(if ($script:logFile) { $script:logFile } else { [string]$ss.log_file }); if (Test-Path -LiteralPath $f) { (Get-Item -LiteralPath $f).Length } else { 0 } }
function Read-ServerLog([int64]$From = -1) {
    # 1.6.18: -From = the log length before a restart, so only the restarted server's own lines count.
    $f = $(if ($script:logFile) { $script:logFile } else { [string]$ss.log_file }); if (-not (Test-Path -LiteralPath $f)) { return '' }
    try { $fs = [IO.File]::Open($f, 'Open', 'Read', 'ReadWrite'); $len = $fs.Length; $fs.Seek($(if ($From -ge 0 -and $From -le $len) { $From } else { [math]::Max(0, $len - 2MB) }), 'Begin') | Out-Null
          $sr = New-Object IO.StreamReader($fs); $t = $sr.ReadToEnd(); $sr.Close(); return $t } catch { return '' }
}
function Get-FirewallFacts {
    @(Get-NetFirewallRule -Direction Inbound -ErrorAction SilentlyContinue | ForEach-Object {
        $pf = $_ | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        if ("$($pf.LocalPort)" -match "(^|,)$([int]$cfg.ollama.port)(,|$)" -or $_.DisplayName -match '(?i)ollama') {
            $af = $_ | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue; $ap = $_ | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
            '{0} | enabled {1} | {2} | port {3} | local {4} | remote {5} | program {6}' -f $_.DisplayName, $_.Enabled, $_.Action, $pf.LocalPort, ($af.LocalAddress -join ','), ($af.RemoteAddress -join ','), $ap.Program }
    } | Sort-Object)
}
function Get-Inventory {
    $cs = Get-CimInstance Win32_ComputerSystem; $os = Get-CimInstance Win32_OperatingSystem
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $wrapperText = $(if (Test-Path -LiteralPath $paths.Wrapper) { [IO.File]::ReadAllText($paths.Wrapper) } else { '' })
    $listen = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$cfg.ollama.port) -ErrorAction SilentlyContinue)
    $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '(?i)^(ollama|llama)' } | ForEach-Object {
        [ordered]@{ pid = $_.ProcessId; name = $_.Name; path = $_.ExecutablePath; command = $_.CommandLine
                    owner = $(try { $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop; "$($o.Domain)\$($o.User)" } catch { '' })
                    listens = [bool]@($listen | Where-Object OwningProcess -eq $_.ProcessId).Count } })
    $tags = Get-Api '/api/tags' 20
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    $cfgLine = Get-LabServerConfigFromLog -Text (Read-ServerLog)
    # 1.6.19: the NVIDIA card's own memory (Win32_VideoController stops at 4 GB), for the machine class.
    $vram = $null; $smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
    if (-not (Test-Path -LiteralPath $smi)) { $c = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue; $smi = $(if ($c) { $c.Source } else { '' }) }
    if ($smi) { try { $o = @(& $smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null); if ($o.Count -and [string]$o[0] -match '^\s*(\d+)') { $vram = [math]::Round([double]$Matches[1] / 1024, 1) } } catch { } }
    $machineEnv = [ordered]@{}; foreach ($k in @([Environment]::GetEnvironmentVariables('Machine').Keys | Where-Object { $_ -like 'OLLAMA*' })) { $machineEnv[$k] = [Environment]::GetEnvironmentVariable($k, 'Machine') }
    return [ordered]@{
        at = (Get-Date).ToString('o'); computer = $env:COMPUTERNAME; address = $ip
        hardware = [ordered]@{ cpu = (@(Get-CimInstance Win32_Processor | ForEach-Object Name) -join ', '); cores = [int](@(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum)
                               ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1); free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
                               gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { '{0} (driver {1})' -f $_.Name, $_.DriverVersion }); vram_gb = $vram }
        os = ('{0} {1} build {2}' -f $os.Caption, $os.Version, $os.BuildNumber)
        boot_task = $(if ($task) { [ordered]@{ state = [string]$task.State; user = $task.Principal.UserId; action = (($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; ') } } else { $null })
        wrapper = [ordered]@{ path = $paths.Wrapper; text = $wrapperText; facts = $(if ($wrapperText) { Get-LabWrapperFacts -Text $wrapperText } else { $null }) }
        processes = $procs
        server_version = [string](Get-Api '/api/version').version
        cli = [ordered]@{ path = $(if ($cmd) { $cmd.Source } else { '' }); file_version = $(if ($cmd) { (Get-Item $cmd.Source).VersionInfo.FileVersion } else { '' }) }
        models = @($tags.models | ForEach-Object { [ordered]@{ name = $_.name; digest = $_.digest; size_gb = [math]::Round([double]$_.size / 1GB, 2); family = $_.details.family; families = @($_.details.families); quantization = $_.details.quantization_level } })
        loaded = @((Get-Api '/api/ps').models | ForEach-Object { [ordered]@{ name = $_.name; size_gb = [math]::Round([double]$_.size / 1GB, 2); vram_gb = [math]::Round([double]$_.size_vram / 1GB, 2); context_length = $_.context_length; expires_at = [string]$_.expires_at } })
        machine_env = $machineEnv
        server_config = $(if ($cfgLine) { [ordered]@{ env = $cfgLine.Env; backend = $cfgLine.Backend; line = $cfgLine.Line } } else { $null })
        firewall = @(Get-FirewallFacts)
    }
}

# ---------------------------------------------------------------- restart and prove
function Restart-Server([string[]]$Dirs) {
    # 1.6.18: the boot task's cmd.exe ends first, so it cannot read on in the rewritten wrapper (Stop-LabOllamaBootHost).
    $stopped = @(Stop-LabOllamaBootHost -TaskName $TaskName -Wrapper $paths.Wrapper)
    foreach ($p in @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '(?i)^(ollama|llama)' })) {
        $mine = $false; foreach ($d in $Dirs) { if ($d -and $p.ExecutablePath -and $p.ExecutablePath.StartsWith($d.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { $mine = $true } }
        if ($mine) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped += ('{0} ({1})' -f $p.Name, $p.ProcessId) } catch { } }
    }
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $v = $null; $until = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $until -and -not $v) { $v = Get-Api '/api/version' 5; if (-not $v) { Start-Sleep -Seconds 3 } }
    $listener = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$cfg.ollama.port) -ErrorAction SilentlyContinue | Select-Object -First 1)
    $lpath = $(if ($listener) { try { (Get-Process -Id $listener[0].OwningProcess -ErrorAction Stop).Path } catch { '' } } else { '' })
    return [pscustomobject]@{ Version = $(if ($v) { [string]$v.version } else { '' }); ListenerPath = $lpath; Stopped = $stopped }
}
function Write-Wrapper([string]$Exe, $Settings, [string]$LogFile) {
    $text = [IO.File]::ReadAllText($paths.Wrapper)
    $new = Set-LabWrapperContent -Text $text -Settings $Settings -Executable $Exe -LogFile $LogFile
    if ($new -ne $text) { [IO.File]::WriteAllText($paths.Wrapper, $new, (New-Object Text.ASCIIEncoding)) }
}
function Set-AndProve([string]$Exe, $Settings, [string]$Why) {
    # Writes the wrapper, restarts, and proves from the server itself: /api/version, the listening program and its own
    # "server config" line. Returns $null when the server does not run the pinned version.
    Write-Wrapper $Exe $Settings ([string]$ss.log_file)
    $from = Get-ServerLogLength
    $r = Restart-Server @((Split-Path -Parent $Exe), $script:oldDir)
    $seen = Get-LabServerConfigFromLog -Text (Read-ServerLog $from)
    $bad = $(if ($seen) { Test-LabServerSettingsApplied -Wanted $Settings -Seen $seen.Env } else { @('the server log has no "server config" line') })
    $okVer = ($r.Version -eq [string]$spec.version); $okPath = ($r.ListenerPath -and $r.ListenerPath -ieq $Exe)
    Say ('{0}: Ollama {1} answers; listening program {2}; settings {3}' -f $Why, $(if ($r.Version) { $r.Version } else { '(none)' }), $(if ($r.ListenerPath) { $r.ListenerPath } else { '(unknown)' }), $(if ($bad.Count) { 'NOT proven: ' + ($bad -join '; ') } else { 'proven by the server''s own config line' })) $(if ($okVer -and $okPath -and -not $bad.Count) { 'Green' } else { 'Yellow' })
    if (-not $okVer -or -not $okPath) { return $null }
    return [pscustomobject]@{ Version = $r.Version; Proven = (-not $bad.Count); Mismatch = $bad; Backend = $(if ($seen) { $seen.Backend } else { '' }); ConfigEnv = $(if ($seen) { $seen.Env } else { $null }) }
}

# ---------------------------------------------------------------- qualification
function Invoke-Qualification([string]$Exe, [string]$Mode) {
    $inv = $script:inventory
    $ram = [double]$inv.hardware.ram_gb
    # 1.6.19: the machine class (GPU / 32 GB / 16 GB / 8 GB) sets the levels, loaded models, models and the worker.
    $cls = Get-LabMachineClass -RamGb $ram -VramGb $inv.hardware.vram_gb -Classes $ss.machine_classes
    $script:machineClass = $cls
    $maxLoaded = $(if ($cls) { [int]$cls.max_loaded_models } else { Get-LabMaxLoadedModels -RamGb $ram -Rule $ss.max_loaded_models })
    $floor = [double]$ss.min_free_gb; $gain = [double]$ss.useful_gain; $ctx = [int]$ss.context_length; $rounds = [math]::Max(1, [int]$ss.rounds)
    $ladder = @($(if ($cls) { $cls.parallel_ladder } else { $ss.parallel_ladder }) | ForEach-Object { [int]$_ } | Sort-Object)
    $qualify = @($(if ($cls) { $cls.qualify_models } else { $ss.qualify_models }))
    $worker = $(if ($cls) { [string]$cls.worker_model } else { '' })
    $tag = -join ((48..57) + (65..90) | Get-Random -Count 4 | ForEach-Object { [char]$_ })
    $have = @{}; foreach ($m in $inv.models) { $have[[string]$m.name] = $m }
    $models = @($qualify | Where-Object { $have.ContainsKey([string]$_) })
    $missing = @($qualify | Where-Object { -not $have.ContainsKey([string]$_) })
    foreach ($m in $missing) { Say "model $m is not on this head: not qualified here (no model is pulled by this tool)" 'Yellow' }
    Say ("machine: {0} ({1} GB RAM{2}); the Gateway gets {3} slots and memory_gb {4}" -f $(if ($cls) { $cls.name } else { '(no class)' }), $ram, $(if ($inv.hardware.vram_gb) { ", $($inv.hardware.vram_gb) GB on the graphics card" } else { '' }), $(if ($cls) { $cls.gateway_slots } else { '-' }), $(if ($cls) { $cls.gateway_memory_gb } else { '-' })) 'Cyan'
    Say ("plan: models {0}; worker {1}; num_ctx {2}; parallel levels {3}; up to {4} model(s) resident; {5} round(s) per level; memory floor {6} GB" -f ($models -join ', '), $(if ($worker) { $worker } else { '-' }), $ctx, ($ladder -join ', '), $maxLoaded, $rounds, $floor)
    $conv = @{}; foreach ($m in $models) { $conv[$m] = @(1..($ladder[-1]) | ForEach-Object { New-LabQualifyConversation -Model $m -Index $_ -RunTag $tag -ReviewerModel $ss.reviewer_model -NumCtx $ctx -ReviewerNumPredict ([int]$ss.reviewer_num_predict) -KeepAlive $ss.keep_alive }) }
    $per = @{}; foreach ($m in $models) { $per[$m] = [ordered]@{ model = $m; serial = $null; levels = @(); stopped = '' ; warm = @() } }
    $levelRuns = @()
    foreach ($L in $ladder) {
        if (-not @($models | Where-Object { -not $per[$_].stopped }).Count) { break }
        Say ''; Say ("level {0}: OLLAMA_NUM_PARALLEL={0}" -f $L) 'Cyan'
        $proof = Set-AndProve $Exe (New-LabServerSettings -Spec $ss -Parallel $L -MaxLoaded $maxLoaded) "level $L"
        if (-not $proof) { Say 'the pinned server is not the one answering: qualification stopped' 'Red'; return $null }
        if ($proof.Mismatch.Count -and $proof.ConfigEnv) { Say 'the server shows other settings than the wrapper sets: qualification stopped' 'Red'; return $null }
        $levelRuns += [ordered]@{ level = $L; version = $proof.Version; settings_proven = $proof.Proven; mismatch = $proof.Mismatch; backend = $proof.Backend }
        foreach ($m in $models) {
            if ($per[$m].stopped) { continue }
            $w = Invoke-LabQualifyBatch -Base $base -Conversations @((New-LabQualifyConversation -Model $m -Index 1 -RunTag ('W' + $tag.Substring(1)) -ReviewerModel $ss.reviewer_model -NumCtx $ctx -KeepAlive $ss.keep_alive)) -TimeoutSeconds ([int]$ss.request_timeout_s) -Serial
            $ps = @((Get-Api '/api/ps').models | Where-Object { $_.name -eq $m }) | Select-Object -First 1
            $free = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 2)
            $wr = $w.Results[0]
            $per[$m].warm += [ordered]@{ level = $L; ok = $wr.Ok; error = $wr.Error; load_ms = $(if ($wr.Reply) { [math]::Round([double]$wr.Reply.load_duration / 1e6) } else { $null })
                                         size_gb = $(if ($ps) { [math]::Round([double]$ps.size / 1GB, 2) } else { $null }); vram_gb = $(if ($ps) { [math]::Round([double]$ps.size_vram / 1GB, 2) } else { $null })
                                         context_length = $(if ($ps) { $ps.context_length } else { $null }); free_gb_after_load = $free }
            if (-not $wr.Ok) { $per[$m].stopped = "level ${L}: the model did not load ($($wr.Error))"; Say ("{0}: did not load: {1}" -f $m, $wr.Error) 'Red'; continue }
            Say ("{0}: loaded in {1} s, {2} GB resident (context {3}), {4} GB free" -f $m, [math]::Round([double]$wr.Reply.load_duration / 1e9, 1), $per[$m].warm[-1].size_gb, $per[$m].warm[-1].context_length, $free)
            if (-not $per[$m].serial) {
                $s = Invoke-LabQualifyBatch -Base $base -Conversations $conv[$m] -TimeoutSeconds ([int]$ss.request_timeout_s) -Serial
                $sm = Get-LabBatchMetrics -Results $s.Results
                $per[$m].serial = [ordered]@{ metrics = $sm; min_free_gb = $s.MinFreeGb; results = @($s.Results | ForEach-Object { Get-ResultRecord $_ }) ; all_valid = (-not $sm.failed -and -not $sm.invalid) }
                Say ("{0}: serial baseline {1} answers, {2} invalid, {3} failed, {4} tok/s" -f $m, $sm.requests, $sm.invalid, $sm.failed, $sm.tokens_per_s) $(if ($per[$m].serial.all_valid) { 'Gray' } else { 'Yellow' })
            }
            $all = @(); $tps = @(); $iso = $true; $minFree = $null; $isoFail = @()
            for ($r = 1; $r -le $rounds; $r++) {
                $b = Invoke-LabQualifyBatch -Base $base -Conversations @($conv[$m] | Select-Object -First $L) -TimeoutSeconds ([int]$ss.request_timeout_s)
                $bm = Get-LabBatchMetrics -Results $b.Results; $tps += [double]$bm.tokens_per_s
                $i = Test-LabConversationIsolation -Results $b.Results; if (-not $i.Passed) { $iso = $false; $isoFail += $i.Failures }
                if ($null -ne $b.MinFreeGb -and ($null -eq $minFree -or $b.MinFreeGb -lt $minFree)) { $minFree = $b.MinFreeGb }
                $all += $b.Results
            }
            $met = Get-LabBatchMetrics -Results $all
            $met.tokens_per_s = [math]::Round(($tps | Measure-Object -Average).Average, 2)
            $verdict = Test-LabLevelPassed -Level $L -Metrics $met -Isolated $iso -MinFreeGb $minFree -Floor $floor
            $agree = Get-LabAgreement -Results @($all | ForEach-Object { [pscustomobject]@{ Marker = $_.Marker; Key = $_.Key } }) -Serial @($per[$m].serial.results | ForEach-Object { [pscustomobject]@{ Marker = $_.marker; Key = $_.key; Valid = $_.valid } })
            $per[$m].levels += [ordered]@{ level = $L; passed = $verdict.Passed; reasons = $verdict.Reasons; metrics = $met; min_free_gb = $minFree; isolation_passed = $iso; isolation_failures = $isoFail
                                           agreement_with_serial = $agree; results = @($all | ForEach-Object { Get-ResultRecord $_ }) }
            Say ("{0} at {1}: {2}; compute overlap {3}; {4} tok/s (serial {5}); p95 {6} s; queue {7} ms; min free {8} GB; same answers as serial {9}" -f $m, $L, $(if ($verdict.Passed) { 'PASS' } else { 'FAIL (' + ($verdict.Reasons -join '; ') + ')' }), $met.measured_overlap, $met.tokens_per_s, $per[$m].serial.metrics.tokens_per_s, $met.latency_p95_s, $met.queue_ms_avg, $minFree, $agree) $(if ($verdict.Passed) { 'Green' } else { 'Yellow' })
            if (-not $verdict.Passed) { $per[$m].stopped = "level ${L}: " + ($verdict.Reasons -join '; ') }
        }
    }
    # The highest useful level that passed, per model; the server-wide setting is the lowest of those.
    $adopt = @{}
    foreach ($m in $models) {
        $lv = @($per[$m].levels | ForEach-Object { [pscustomobject]@{ Level = $_.level; Passed = $_.passed; TokensPerS = $_.metrics.tokens_per_s } })
        $adopt[$m] = $(if ($per[$m].serial -and $per[$m].serial.all_valid) { Select-LabParallelLevel -Levels $lv -SerialTokensPerS ([double]$per[$m].serial.metrics.tokens_per_s) -Gain $gain } else { 1 })
    }
    # 1.6.19: the worker model sets the server-wide OLLAMA_NUM_PARALLEL; every other model keeps its own measured limit
    # (the Gateway holds it there: measured_parallel). Before, the lowest model set it, so a reviewer that ran out of
    # output room held the worker at 1 (head 8, 2026-11-17).
    $setting = $(if ($worker -and $adopt.ContainsKey($worker)) { $adopt[$worker] } elseif ($models.Count) { ($models | ForEach-Object { $adopt[$_] } | Measure-Object -Minimum).Minimum } else { 1 })
    foreach ($m in $models) { if ($adopt[$m] -gt $setting) { $adopt[$m] = $setting } }
    Say ''; Say ("adopted: OLLAMA_NUM_PARALLEL={0} from the worker {1}; per model: {2}" -f $setting, $(if ($worker) { $worker } else { '(lowest model)' }), (($models | ForEach-Object { "$_ $($adopt[$_])" }) -join ', ')) 'Cyan'
    # Mixed models: both resident and answering at once, or one resident model at a time.
    $mixed = $null
    if ($maxLoaded -ge 2 -and $models.Count -ge 2) {
        $proof = Set-AndProve $Exe (New-LabServerSettings -Spec $ss -Parallel $setting -MaxLoaded $maxLoaded) 'mixed-model test'
        if ($proof) {
            $cs = @(); foreach ($m in $models) { $cs += @($conv[$m] | Select-Object -First $setting) }
            foreach ($m in $models) { [void](Invoke-LabQualifyBatch -Base $base -Conversations @((New-LabQualifyConversation -Model $m -Index 2 -RunTag ('W' + $tag.Substring(1)) -ReviewerModel $ss.reviewer_model -NumCtx $ctx -KeepAlive $ss.keep_alive)) -Serial) }
            $b = Invoke-LabQualifyBatch -Base $base -Conversations $cs -TimeoutSeconds ([int]$ss.request_timeout_s)
            $bm = Get-LabBatchMetrics -Results $b.Results; $i = Test-LabConversationIsolation -Results $b.Results
            # 1.6.19: a memory question: both resident, answering, isolated, above the memory floor. An answer the checker
            # rejects (for example a thinking model out of output room) is reported, but does not unload models.
            $ok = (-not $bm.failed -and $i.Passed -and $b.MaxResident -ge 2 -and ($null -eq $b.MinFreeGb -or $b.MinFreeGb -ge $floor))
            if ($bm.invalid) { Say ("mixed models: {0} answer(s) rejected by the answer check (reported; not a memory limit)" -f $bm.invalid) 'Yellow' }
            $mixed = [ordered]@{ max_loaded_models = $maxLoaded; passed = $ok; resident_at_once = $b.MaxResident; min_free_gb = $b.MinFreeGb; metrics = $bm; isolation_passed = $i.Passed; isolation_failures = $i.Failures; results = @($b.Results | ForEach-Object { Get-ResultRecord $_ }) }
            Say ("mixed models at once: {0}; {1} resident together; compute overlap {2}; min free {3} GB" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $b.MaxResident, $bm.measured_overlap, $b.MinFreeGb) $(if ($ok) { 'Green' } else { 'Yellow' })
            if (-not $ok) { $maxLoaded = 1; Say 'OLLAMA_MAX_LOADED_MODELS=1: one model resident at a time on this head' 'Yellow' }
        }
    } elseif ($models.Count -ge 2) { Say ("OLLAMA_MAX_LOADED_MODELS={0}: {1} GB is too little to keep both models resident; the Gateway should send the reviewer model elsewhere" -f $maxLoaded, $ram) 'Yellow' }
    # Final settings, proven once more after the restart.
    $final = New-LabServerSettings -Spec $ss -Parallel $setting -MaxLoaded $maxLoaded
    $proof = Set-AndProve $Exe $final 'final settings'
    $tags = Get-Api '/api/tags' 20
    $entries = @()
    foreach ($m in @($qualify)) {
        $p = $per[$m]
        $own = $(if ($adopt.ContainsKey($m)) { [int]$adopt[$m] } else { 1 })   # 1.6.19: this model's own level
        $lvl = $(if ($p) { @($p.levels | Where-Object { $_.level -eq $own }) | Select-Object -First 1 } else { $null })
        $warm = $(if ($p -and $p.warm.Count) { $p.warm[-1] } else { $null })
        $digest = [string](@($tags.models | Where-Object { $_.name -eq $m }) | Select-Object -First 1).digest
        $serialOk = [bool]($p -and $p.serial -and $p.serial.all_valid)
        $passed = [bool]($proof -and $serialOk -and ($own -eq 1 -or ($lvl -and $lvl.passed)))
        $measured = $(if ($own -gt 1 -and $lvl) { [math]::Min($own, [int]$lvl.metrics.measured_overlap) } else { 1 })
        $backend = $(if ($proof -and $proof.Backend) { $proof.Backend } elseif ($warm -and $warm.vram_gb -gt 0) { 'gpu' } else { 'cpu' })
        $entries += [ordered]@{
            resource_id = $rid; address = $ip; model = $m; digest = $digest; runtime_version = $(if ($proof) { $proof.Version } else { '' }); backend = $backend
            num_ctx = $ctx; num_parallel_setting = [int]$setting; measured_parallel = [int]$measured; max_loaded_models = [int]$maxLoaded; max_queue = [int]$ss.max_queue; keep_alive = [string]$ss.keep_alive
            flash_attention = $(if ($ss.flash_attention) { [string]$ss.flash_attention } else { 'runtime default (not set)' + $(if ($proof -and $proof.ConfigEnv -and $proof.ConfigEnv.Contains('OLLAMA_FLASH_ATTENTION')) { '; server shows ' + $(if ($proof.ConfigEnv['OLLAMA_FLASH_ATTENTION']) { $proof.ConfigEnv['OLLAMA_FLASH_ATTENTION'] } else { 'empty (runtime decides per model)' }) } else { '' }) })
            kv_cache_type = $(if ($ss.kv_cache_type) { [string]$ss.kv_cache_type } else { 'runtime default (not set)' + $(if ($proof -and $proof.ConfigEnv -and $proof.ConfigEnv.Contains('OLLAMA_KV_CACHE_TYPE')) { '; server shows ' + $(if ($proof.ConfigEnv['OLLAMA_KV_CACHE_TYPE']) { $proof.ConfigEnv['OLLAMA_KV_CACHE_TYPE'] } else { 'empty (f16)' }) } else { '' }) })
            memory = [ordered]@{ ram_gb = $ram; model_size_gb = $(if ($warm) { $warm.size_gb } else { $null }); model_vram_gb = $(if ($warm) { $warm.vram_gb } else { $null }); context_length_seen = $(if ($warm) { $warm.context_length } else { $null })
                                 free_gb_after_load = $(if ($warm) { $warm.free_gb_after_load } else { $null }); min_free_gb = $(if ($lvl) { $lvl.min_free_gb } else { $null }); mixed_models = $(if ($mixed) { $mixed.passed } else { $null }) }
            throughput = [ordered]@{ serial_tokens_per_s = $(if ($p -and $p.serial) { $p.serial.metrics.tokens_per_s } else { $null }); parallel_tokens_per_s = $(if ($lvl) { $lvl.metrics.tokens_per_s } else { $null })
                                     gain = $(if ($lvl -and $p.serial -and $p.serial.metrics.tokens_per_s -gt 0) { [math]::Round($lvl.metrics.tokens_per_s / $p.serial.metrics.tokens_per_s, 2) } else { $null }) }
            latency = [ordered]@{ p50_s = $(if ($lvl) { $lvl.metrics.latency_p50_s } elseif ($p -and $p.serial) { $p.serial.metrics.latency_p50_s } else { $null }); p95_s = $(if ($lvl) { $lvl.metrics.latency_p95_s } elseif ($p -and $p.serial) { $p.serial.metrics.latency_p95_s } else { $null })
                                  queue_ms_avg = $(if ($lvl) { $lvl.metrics.queue_ms_avg } else { $null }); load_ms = $(if ($warm) { $warm.load_ms } else { $null }) }
            isolation_passed = $(if ($lvl) { [bool]$lvl.isolation_passed } else { $serialOk })
            passed = $passed
            evidence = Limit-Text 400 ('reports\{0}\ollama-qualify-{1}.json: {2}' -f $env:COMPUTERNAME, $stamp, $(if (-not $p) { 'model not on this head' } elseif (-not $proof) { 'the pinned server did not answer after the final restart' } elseif (-not $serialOk) { 'serial baseline invalid' } elseif ($own -eq 1) { 'parallel levels did not pass or add throughput (' + $p.stopped + '); one request at a time' } else { "level $own passed: compute windows overlapped $($lvl.metrics.measured_overlap), $($lvl.metrics.requests) answers valid and isolated" }))
        }
    }
    return [ordered]@{ mode = $Mode; run_tag = $tag; levels_run = $levelRuns; models = @($models | ForEach-Object { $per[$_] }); missing_models = $missing
                       adopted = [ordered]@{ num_parallel = [int]$setting; per_model = $adopt; max_loaded_models = [int]$maxLoaded; worker_model = $worker }; mixed = $mixed; final = $final; final_proof = $proof; entries = $entries }
}
function Limit-Text([int]$n, [string]$t) { if ($t.Length -le $n) { $t } else { $t.Substring(0, $n - 3) + '...' } }   # the Gateway takes evidence up to 400 characters
function Get-ResultRecord($r) {
    $rep = $r.Reply
    [ordered]@{ marker = $r.Marker; kind = $r.Kind; ok = $r.Ok; valid = $r.Valid; why = $r.Why; key = $r.Key; sent = $r.Sent; received = $r.Received
                created_at = $(if ($rep) { [string]$rep.created_at } else { '' }); prompt_eval_count = $(if ($rep) { $rep.prompt_eval_count } else { $null }); eval_count = $(if ($rep) { $rep.eval_count } else { $null })
                prompt_eval_ms = $(if ($rep) { [math]::Round([double]$rep.prompt_eval_duration / 1e6) } else { $null }); eval_ms = $(if ($rep) { [math]::Round([double]$rep.eval_duration / 1e6) } else { $null })
                total_ms = $(if ($rep) { [math]::Round([double]$rep.total_duration / 1e6) } else { $null }); load_ms = $(if ($rep) { [math]::Round([double]$rep.load_duration / 1e6) } else { $null })
                done_reason = $(if ($rep) { [string]$rep.done_reason } else { '' }); text = $(if ($r.Text) { ([string]$r.Text).Substring(0, [math]::Min(4000, ([string]$r.Text).Length)) } else { '' }) }
}

# ---------------------------------------------------------------- the run
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task -or -not (Test-Path -LiteralPath $paths.Wrapper) -or (@($task.Actions)[0]).Arguments -notmatch [regex]::Escape($paths.Wrapper)) { Say "The boot task '$TaskName' does not run $($paths.Wrapper): this head was not set up by the provisioner. Nothing was changed." 'Red'; Finish 2 }
$script:inventory = Get-Inventory
$inv = $script:inventory
$active = Read-LabOllamaSettings -Path $paths.Settings
$script:oldDir = $(if ($inv.wrapper.facts.Executable) { Split-Path -Parent $inv.wrapper.facts.Executable } else { '' })
Say ("now: Ollama {0} from {1}; {2} GB RAM ({3} GB free); GPU {4}; {5} model(s)" -f $(if ($inv.server_version) { $inv.server_version } else { '(not answering)' }), $inv.wrapper.facts.Executable, $inv.hardware.ram_gb, $inv.hardware.free_gb, (($inv.hardware.gpus) -join '; '), @($inv.models).Count)
Say ("pinned: Ollama {0} ({1}); no internet is needed: the archive is on this stick" -f $spec.version, $spec.file)
if (-not $Choice) {
    Write-Host '    1  update Ollama to the pinned version and qualify parallel inference (about an hour)'
    Write-Host '    2  qualify again (the pinned version is already in place)'
    Write-Host '    0  roll back to the Ollama this head had before its first update'
    $Choice = (Read-Host '  Type 1, 2 or 0').Trim()
}
$fwBefore = @($inv.firewall)
$modelsBefore = @($inv.models | ForEach-Object { '{0} {1}' -f $_.name, $_.digest } | Sort-Object)

if ($Choice -eq '0') {
    $records = @(Get-ChildItem -LiteralPath $paths.Backups -Filter 'rollback-*.json' -ErrorAction SilentlyContinue | Sort-Object Name)
    if (-not $records.Count) { Say 'No rollback record: this head was never updated by HEAD-OLLAMA. Nothing was changed.' 'Yellow'; Finish 1 }
    $rec = Get-Content -LiteralPath $records[0].FullName -Raw | ConvertFrom-Json
    if (-not (Test-Path -LiteralPath $rec.wrapper_backup) -or -not (Test-Path -LiteralPath $rec.previous_executable)) { Say "The kept wrapper ($($rec.wrapper_backup)) or the old ollama.exe ($($rec.previous_executable)) is missing. Nothing was changed." 'Red'; Finish 2 }
    if (@($inv.loaded).Count -and (Read-Host ('  {0} model(s) loaded now; the restart cuts off any running request. Type RESTART to go on' -f @($inv.loaded).Count)).Trim() -cne 'RESTART') { Say 'Nothing was changed.'; Finish 1 }
    Copy-Item -LiteralPath $paths.Wrapper -Destination (Join-Path $paths.Backups "ollama-serve.before-rollback-$stamp.cmd")
    if (Test-Path -LiteralPath $paths.Settings) { Copy-Item -LiteralPath $paths.Settings -Destination (Join-Path $paths.Backups "ollama-settings.before-rollback-$stamp.json")
        $s = Get-Content -LiteralPath $paths.Settings -Raw | ConvertFrom-Json; $s.state = 'rolled_back'; Save-Json $s $paths.Settings }
    [IO.File]::WriteAllText($paths.Wrapper, [IO.File]::ReadAllText($rec.wrapper_backup), (New-Object Text.ASCIIEncoding))
    $r = Restart-Server @((Split-Path -Parent $rec.previous_executable), $script:oldDir)
    $okBack = ($r.Version -eq [string]$rec.previous_version)
    # A report that passes nothing, so the hub's APPLY-MODEL-CAPACITY clears this head's per-model limits.
    Save-Json ([ordered]@{ schema = 'agent-arena.ollama-qualify/1'; generated_at = (Get-Date).ToString('o'); computer = $env:COMPUTERNAME; address = $ip; resource_id = $rid; rolled_back = $true; qualification = $null
                           entries = @($ss.qualify_models | ForEach-Object { [ordered]@{ resource_id = $rid; address = $ip; model = [string]$_; digest = ''; runtime_version = $r.Version; num_ctx = [int]$ss.context_length
                                                                                         num_parallel_setting = $null; measured_parallel = 1; passed = $false; isolation_passed = $false
                                                                                         evidence = "reports\$env:COMPUTERNAME\ollama-qualify-$stamp.json: rolled back to Ollama $($r.Version)" } }) }) (Join-Path $reportDir "ollama-qualify-$stamp.json")
    Say ("RESULT {0}: Ollama {1} answers from {2} (before the update: {3}); the wrapper is the one kept in {4}; Ollama {5} stays unpacked in {6}" -f $(if ($okBack) { 'rolled back' } else { 'rolled back, but the version differs' }), $r.Version, $r.ListenerPath, $rec.previous_version, $rec.wrapper_backup, $spec.version, (Join-Path $spec.install_root $spec.version)) $(if ($okBack) { 'Green' } else { 'Yellow' })
    Finish $(if ($okBack) { 0 } else { 1 })
}
if ($Choice -ne '1' -and $Choice -ne '2') { Say 'Type 1, 2 or 0. Nothing was changed.' 'Yellow'; Finish 1 }
if (@($inv.loaded).Count) {
    Say ("Ollama has {0} loaded now ({1}): the restarts cut off any request running on it. Stop Capstone's work on this head first." -f @($inv.loaded).Count, ((@($inv.loaded) | ForEach-Object name) -join ', ')) 'Yellow'
    if ((Read-Host '  Type RESTART to go on, or Enter to stop here').Trim() -cne 'RESTART') { Say 'Nothing was changed.'; Finish 1 }
}
$exe = Join-Path (Join-Path $spec.install_root $spec.version) 'ollama.exe'
if ($Choice -eq '2') {
    if (-not $active -or [string]$active.executable -ne $exe) { Say 'This head does not run the pinned Ollama yet: choose 1. Nothing was changed.' 'Yellow'; Finish 1 }
} else {
    $archive = Find-LabOllamaArchive -StickRelease $release -Spec $spec
    if (-not $archive) { Say ("The pinned archive packages\ollama\{0} is not on this stick. Nothing was changed (no download is attempted)." -f $spec.file) 'Red'; Finish 2 }
    Say "checking and unpacking $($spec.file) (SHA-256 and Ollama's signature; a few minutes)..."
    try { $inst = Install-LabOllamaRuntime -Archive $archive -Spec $spec } catch { Say ("RESULT not updated: {0}. Nothing else was changed." -f $_.Exception.Message) 'Red'; Finish 1 }
    Say ("Ollama {0}: {1} ({2}; {3})" -f $spec.version, $inst.Executable, $inst.Method, $inst.Detail) 'Green'
    # Kept for rollback before the wrapper is first changed: the wrapper as it was and the Ollama it ran.
    New-Item -ItemType Directory -Path $paths.Backups -Force | Out-Null
    $wb = Join-Path $paths.Backups "ollama-serve.before-1.6.14-$stamp.cmd"; Copy-Item -LiteralPath $paths.Wrapper -Destination $wb
    if ([string]$inv.wrapper.facts.Executable -ine $exe) {
        Save-Json ([ordered]@{ schema = 'agent-arena.ollama-rollback/1'; made_at = (Get-Date).ToString('o'); wrapper_backup = $wb; previous_executable = $inv.wrapper.facts.Executable
                               previous_version = $inv.server_version; previous_wrapper_env = $inv.wrapper.facts.Env; firewall = $fwBefore; models = $modelsBefore }) (Join-Path $paths.Backups "rollback-$stamp.json")
        Say "kept for rollback: $wb and rollback-$stamp.json (HEAD-OLLAMA > 0 puts them back)"
    }
}
$q = Invoke-Qualification -Exe $exe -Mode $(if ($Choice -eq '1') { 'update and qualify' } else { 'qualify again' })
$after = Get-Inventory
$fwSame = ((@($after.firewall) -join "`n") -eq ($fwBefore -join "`n"))
$modelsAfter = @($after.models | ForEach-Object { '{0} {1}' -f $_.name, $_.digest } | Sort-Object)
$modelsSame = (($modelsAfter -join "`n") -eq ($modelsBefore -join "`n"))
$wf = Get-LabWrapperFacts -Text $after.wrapper.text
$hostSame = ([string]$wf.Env['OLLAMA_HOST'] -eq [string]$inv.wrapper.facts.Env['OLLAMA_HOST'] -and [string]$wf.Env['OLLAMA_MODELS'] -eq [string]$inv.wrapper.facts.Env['OLLAMA_MODELS'])
if ($q -and $q.final_proof) {
    Save-Json ([ordered]@{ schema = 'agent-arena.ollama-settings/1'; state = 'active'; executable = $exe; version = $spec.version; env = $q.final; log_file = [string]$ss.log_file
                           qualified_at = (Get-Date).ToString('o'); report = "reports\$env:COMPUTERNAME\ollama-qualify-$stamp.json" }) $paths.Settings
}
$report = [ordered]@{
    schema = 'agent-arena.ollama-qualify/1'; provisioner = (Get-Content -LiteralPath (Join-Path $release 'VERSION') -Raw -ErrorAction SilentlyContinue).Trim(); generated_at = (Get-Date).ToString('o')
    computer = $env:COMPUTERNAME; address = $ip; resource_id = $rid
    runtime = [ordered]@{ version = $spec.version; file = $spec.file; sha256 = $spec.sha256; source = $spec.source; executable = $exe }
    measurement_rule = 'overlap = compute windows created_at - (prompt_eval_duration + eval_duration) to created_at overlapping; simultaneous HTTP requests are not counted'
    before = $inv; after = $after
    preserved = [ordered]@{ firewall = $fwSame; models = $modelsSame; ollama_host_and_models = $hostSame }
    # 1.6.19: read by the hub's APPLY-MODEL-CAPACITY, which gives the Gateway this head's slots and memory_gb.
    machine_class = $(if ($script:machineClass -and $q -and $q.final_proof) { [ordered]@{ name = [string]$script:machineClass.name; ram_gb = $inv.hardware.ram_gb; vram_gb = $inv.hardware.vram_gb
                                                                             gateway_slots = [int]$script:machineClass.gateway_slots; gateway_memory_gb = [double]$script:machineClass.gateway_memory_gb } } else { $null })
    qualification = $q
    # 1.6.22: @(...) outside, so one entry stays a JSON array. $(...) unrolled a one-element array into a single
    # object: heads 3-6 (one qualified model) wrote "entries": {...} and APPLY-MODEL-CAPACITY stopped (2026-11-18).
    entries = @(if ($q) { $q.entries })
}
$rp = Join-Path $reportDir "ollama-qualify-$stamp.json"; Save-Json $report $rp
Say ''
Say ("firewall rules unchanged: {0}; models unchanged: {1}; OLLAMA_HOST and OLLAMA_MODELS unchanged: {2}" -f $fwSame, $modelsSame, $hostSame) $(if ($fwSame -and $modelsSame -and $hostSame) { 'Green' } else { 'Red' })
if (-not $q -or -not $q.final_proof) { Say "RESULT not qualified: the pinned server did not prove itself (see above). Report: $rp. HEAD-OLLAMA > 0 rolls back." 'Red'; Finish 1 }
foreach ($e in $q.entries) { Say ("{0}: {1}; parallel setting {2}, measured {3}; {4}" -f $e.model, $(if ($e.passed) { 'PASS' } else { 'not passed' }), $e.num_parallel_setting, $e.measured_parallel, $e.evidence) $(if ($e.passed) { 'Green' } else { 'Yellow' }) }
if ($script:machineClass) { Say ("machine {0}: the hub's APPLY-MODEL-CAPACITY gives the Gateway {1} slots and memory_gb {2} for this head" -f $script:machineClass.name, $script:machineClass.gateway_slots, $script:machineClass.gateway_memory_gb) 'Green' }
Say ("RESULT done: Ollama {0} runs with OLLAMA_NUM_PARALLEL={1}, OLLAMA_MAX_LOADED_MODELS={2}, OLLAMA_CONTEXT_LENGTH={3}, OLLAMA_MAX_QUEUE={4}, OLLAMA_KEEP_ALIVE={5}. Report: {6}. Bring the stick to the hub (APPLY-MODEL-CAPACITY)." -f $q.final_proof.Version, $q.adopted.num_parallel, $q.adopted.max_loaded_models, $ss.context_length, $ss.max_queue, $ss.keep_alive, $rp) 'Green'
Finish $(if ($fwSame -and $modelsSame -and $hostSame) { 0 } else { 1 })
