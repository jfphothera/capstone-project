# Collect-LabEvidence.ps1 - one double-click (COLLECT-LAB-EVIDENCE.cmd at the stick's root) gathers everything needed
# to diagnose this lab machine onto the provisioner stick, and changes nothing on the machine.
#   - which machine: by its lab address (config\lab-network.json: the hub, the Capstone laptop, the head range);
#   - every machine: network, firewall, listening ports, services, tasks, processes, Windows errors, Smart App Control;
#   - hub: the Inference Gateway (logs, observation, ledger database), the Fleet folder, the Gateway Models screen's
#     records, the model library's index, Arena (logs, settings, event database), the provisioner's last runs, and
#     every head's Ollama (version, models, loaded models) read over the lab cable;
#   - Capstone laptop: Capstone's release, settings, logs, databases, Research Lab experiments and runs (with each run's
#     own database), and, through Capstone's own API with its developer token (used here, never copied), the Research
#     Lab state, results and every recent run's trace;
#   - a head: Ollama's logs and settings, and the Arena viewer's logs.
# Token, key and password files are never copied; secret values in copied settings are masked.
#   Speed tests (they load and unload a model on every head) run only when asked: type SPEED at the first question,
#   or -SpeedTest. On the hub they are refused while the Gateway has calls in flight (lab provisioner 1.6.29: the
#   2026-11-19 collection loaded and unloaded models on all nine heads unasked; during a run that disturbs it).
param([string]$StickRoot = '', [int]$HoursBack = 48, [string]$Role = '', [string]$CapData = 'C:\ProgramData\Capstone', [string]$CapInstall = 'C:\Program Files\Capstone', [switch]$SpeedTest, [switch]$NoQuestions)   # -Role, -Cap*, -NoQuestions: tests only
$ErrorActionPreference = 'Continue'
$started = Get-Date
if (-not $StickRoot) { $StickRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$out = Join-Path $StickRoot ('reports\{0}\evidence-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, $started)
New-Item -ItemType Directory -Path $out -Force | Out-Null
$log = New-Object Collections.ArrayList
function Say([string]$t) { Write-Host "  $t"; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }
function Section([string]$name, [scriptblock]$body) {
    Write-Host ''; Write-Host "== $name" -ForegroundColor Cyan; [void]$log.Add("== $name")
    try { & $body } catch { Say ("could not collect: " + $_.Exception.Message) }
}
$SecretName = '(?i)token|secret|password|passwd|credential|\.key$|\.pfx$|\.p12$|private|\.dpapi$'
# Backups, runtimes and caches are not evidence (one Arena data folder held 5,947 files of USB backups).
$SkipDir = '(?i)\\(usb-backups|backups|__pycache__|\.venv|venv|runtime|site-packages|wheelhouse|blobs|node_modules|labels|webview-data|EBWebView)\\'
$SecretKey = '(?i)token|secret|password|passwd|credential|api_?key|private'
function Save-Text([string]$rel, $value) {
    $p = Join-Path $out $rel; New-Item -ItemType Directory -Path (Split-Path -Parent $p) -Force | Out-Null
    [IO.File]::WriteAllText($p, [string](($value | Out-String -Width 400)), (New-Object Text.UTF8Encoding $false))
}
function Save-Json([string]$rel, $value) {
    $p = Join-Path $out $rel; New-Item -ItemType Directory -Path (Split-Path -Parent $p) -Force | Out-Null
    [IO.File]::WriteAllText($p, ($value | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding $false))
}
function Mask-Json([string]$text) {
    # Secret values in a settings file become "<masked>"; the keys and the structure stay.
    return [regex]::Replace($text, '("(?<k>[^"]*)"\s*:\s*)"(?<v>[^"]*)"', { param($m) if ($m.Groups['k'].Value -match $SecretKey -and $m.Groups['k'].Value -notmatch '(?i)_file$|_path$|file$') { $m.Groups[1].Value + '"<masked>"' } else { $m.Value } })
}
function Free-MB { try { [math]::Floor((Get-PSDrive -Name ($StickRoot.Substring(0, 1))).Free / 1MB) } catch { 99999 } }
function Copy-Evidence([string]$Path, [string]$Rel, [long]$MaxBytes = 25MB) {
    # One file or a whole folder. Logs and other big text files keep their last $MaxBytes; secret files are skipped.
    if (-not (Test-Path -LiteralPath $Path)) { Say "not present: $Path"; return }
    $items = if ((Get-Item -LiteralPath $Path).PSIsContainer) { @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue) } else { @(Get-Item -LiteralPath $Path) }
    $base = if ((Get-Item -LiteralPath $Path).PSIsContainer) { (Get-Item -LiteralPath $Path).FullName.TrimEnd('\') } else { Split-Path -Parent (Get-Item -LiteralPath $Path).FullName }
    $n = 0; $skipped = 0
    foreach ($f in $items) {
        if ($f.Name -match $SecretName) { $skipped++; continue }
        if ($f.FullName.Substring($base.Length) -match $SkipDir) { continue }
        if ($f.Extension -in '.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm', '.npz', '.gguf', '.bin', '.safetensors') { continue }   # databases: Copy-Database; model files: never
        if ((Free-MB) -lt 300) { Say 'the stick has less than 300 MB free: stopped copying files'; break }
        $dst = Join-Path (Join-Path $out $Rel) $f.FullName.Substring($base.Length).TrimStart('\')
        New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
        try {
            if ($f.Extension -in '.json', '.config', '.ini', '.toml', '.yaml', '.yml') {
                [IO.File]::WriteAllText($dst, (Mask-Json ([IO.File]::ReadAllText($f.FullName))), (New-Object Text.UTF8Encoding $false))
            } elseif ($f.Length -gt $MaxBytes) {
                $fs = [IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite')
                try { $fs.Seek(-$MaxBytes, 'End') | Out-Null; $o = [IO.File]::Create($dst); try { $fs.CopyTo($o) } finally { $o.Close() } } finally { $fs.Close() }
            } else {
                $fs = [IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite'); try { $o = [IO.File]::Create($dst); try { $fs.CopyTo($o) } finally { $o.Close() } } finally { $fs.Close() }
            }
            $n++
        } catch { Say ("not copied {0}: {1}" -f $f.FullName, $_.Exception.Message) }
    }
    Say ("{0}: {1} file(s){2}" -f $Path, $n, $(if ($skipped) { ", $skipped secret file(s) not copied" } else { '' }))
}
$py = @('C:\Program Files\Python312\python.exe', 'C:\Program Files\Capstone\runtime\Scripts\python.exe') + @(Get-ChildItem 'C:\Users\*\AppData\Local\Programs\Python\Python3*\python.exe' -ErrorAction SilentlyContinue | ForEach-Object FullName) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$dbCopy = Join-Path $out 'sqlite-copy.py'
[IO.File]::WriteAllText($dbCopy, "import sqlite3,sys`nsrc=sqlite3.connect('file:'+sys.argv[1].replace('\\','/')+'?mode=ro',uri=True,timeout=30)`ndst=sqlite3.connect(sys.argv[2])`nsrc.backup(dst)`ndst.close();src.close()`nprint('ok')`n", (New-Object Text.UTF8Encoding $false))
function Copy-Database([string]$Path, [string]$Rel, [long]$MaxMB = 1500) {
    # A consistent copy of a live SQLite database (SQLite's own backup), never the file under a running program.
    if (-not (Test-Path -LiteralPath $Path)) { Say "not present: $Path"; return }
    $mb = [math]::Ceiling((Get-Item -LiteralPath $Path).Length / 1MB)
    if ($mb -gt $MaxMB) { Say "too large to bring back ($mb MB): $Path"; return }
    if ((Free-MB) -lt ($mb + 300)) { Say "not enough room on the stick for $Path ($mb MB)"; return }
    $dst = Join-Path $out $Rel; New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
    if ($py) { $r = & $py -I $dbCopy $Path $dst 2>&1 | Out-String; if ($r -match 'ok') { Say "database $Path ($mb MB) copied"; return } else { Say ("database copy failed, copying the file: " + $r.Trim()) } }
    try { $fs = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite'); try { $o = [IO.File]::Create($dst); try { $fs.CopyTo($o) } finally { $o.Close() } } finally { $fs.Close() }; Say "database $Path ($mb MB) copied as a file" } catch { Say ("database not copied: " + $_.Exception.Message) }
}
function Test-Tcp([string]$Address, [int]$Port, [int]$Ms = 2000) {
    $c = New-Object Net.Sockets.TcpClient
    try { $t = $c.BeginConnect($Address, $Port, $null, $null); if ($t.AsyncWaitHandle.WaitOne($Ms) -and $c.Connected) { return 'open' } else { return 'no answer' } } catch { return 'refused' } finally { $c.Close() }
}
function Get-Json([string]$Url, [hashtable]$Headers = @{}, [int]$Timeout = 20) {
    try { return @{ ok = $true; url = $Url; body = (Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec $Timeout -UseBasicParsing) } }
    catch { $b = ''; try { $b = $_.ErrorDetails.Message } catch { }; return @{ ok = $false; url = $Url; error = $_.Exception.Message; body = $b } }
}

# Speed test of one head's Ollama (run from the hub for every head, or on the head itself): the model Capstone uses
# (qwen3.5:4b at Capstone's num_ctx 8192, else the smallest installed chat model), once cold and once warm, with
# Ollama's own load, prompt and generation timings; then the model is unloaded, so the head is left as it was.
# Reference, head 10 on 2026-11-08 (Ollama 0.40.13, CPU, 31 GB): qwen3.5:4b cold load at num_ctx 8192 took 96 s.
$SpeedTestBlock = {
    param([string]$Address, [int]$Port, [int]$Ctx = 8192)
    $base = "http://${Address}:$Port"
    $r = [ordered]@{ address = $Address; at = (Get-Date).ToString('o') }
    try {
        $tags = @((Invoke-RestMethod "$base/api/tags" -TimeoutSec 20).models | ForEach-Object { $_ })
        $chat = @($tags | Where-Object { $_.name -notmatch '(?i)embed|minilm|bge|e5-|nomic' } | Sort-Object size)
        $pick = @($chat | Where-Object { $_.name -eq 'qwen3.5:4b' }) + @($chat | Where-Object { $_.name -eq 'qwen3.5:2b' }) + $chat | Select-Object -First 1
        if (-not $pick) { $r.error = 'no chat model installed'; return $r }
        $r.model = $pick.name; $r.model_gb = [math]::Round($pick.size / 1e9, 2)
        $loadedBefore = @((Invoke-RestMethod "$base/api/ps" -TimeoutSec 10).models | ForEach-Object name)
        $r.loaded_before = $loadedBefore
        try { $sh = Invoke-RestMethod -Method Post -Uri "$base/api/show" -Body (@{ model = $pick.name } | ConvertTo-Json) -ContentType 'application/json' -TimeoutSec 30; $r.details = $sh.details } catch { }
        # cold and warm at Capstone's context; then 2048 (Ollama reloads the model for another context size).
        foreach ($phase in 'cold', 'warm', 'ctx2048') {
            if ($phase -eq 'cold' -and $loadedBefore -contains $pick.name) { $r.cold_note = 'the model was already loaded: no cold load measured' }
            $n = if ($phase -eq 'ctx2048') { 2048 } else { $Ctx }
            $body = @{ model = $pick.name; prompt = 'Count from 1 to 20, separated by commas.'; stream = $false; think = $false; keep_alive = '60s'; options = @{ num_ctx = $n; num_predict = 48; temperature = 0 } } | ConvertTo-Json -Depth 4
            $sw = [Diagnostics.Stopwatch]::StartNew()
            try {
                $g = Invoke-RestMethod -Method Post -Uri "$base/api/generate" -Body $body -ContentType 'application/json' -TimeoutSec 900
                $r[$phase] = [ordered]@{ wall_s = [math]::Round($sw.Elapsed.TotalSeconds, 1); load_s = [math]::Round($g.load_duration / 1e9, 1); prompt_tokens = $g.prompt_eval_count; prompt_s = [math]::Round($g.prompt_eval_duration / 1e9, 2); tokens = $g.eval_count; tokens_per_s = $(if ($g.eval_duration) { [math]::Round($g.eval_count / ($g.eval_duration / 1e9), 1) } else { $null }) }
            } catch { $r[$phase] = [ordered]@{ wall_s = [math]::Round($sw.Elapsed.TotalSeconds, 1); error = $_.Exception.Message } }
        }
        try { $r.loaded_during = @((Invoke-RestMethod "$base/api/ps" -TimeoutSec 10).models | ForEach-Object { [ordered]@{ name = $_.name; gb = [math]::Round($_.size / 1e9, 2); vram_gb = [math]::Round($_.size_vram / 1e9, 2); context = $_.context_length } }) } catch { }
        if ($loadedBefore -notcontains $pick.name) { try { Invoke-RestMethod -Method Post -Uri "$base/api/generate" -Body (@{ model = $pick.name; keep_alive = 0 } | ConvertTo-Json) -ContentType 'application/json' -TimeoutSec 60 | Out-Null; $r.unloaded = $true } catch { $r.unloaded = $false } }
    } catch { $r.error = $_.Exception.Message }
    return $r
}
function Format-Speed($s) {
    if ($s.error) { return '{0}: {1}' -f $s.address, $s.error }
    $c = $s.cold; $w = $s.warm; $k = $s.ctx2048
    return '{0}: {1} ({2} GB) cold {3} s (load {4} s) | warm {5} s, {6} tokens/s | at 2048: {7} s' -f $s.address, $s.model, $s.model_gb, $c.wall_s, $c.load_s, $w.wall_s, $w.tokens_per_s, $k.wall_s
}

$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object IPAddress)
# Addresses and ports from the lab's own configuration (config\lab-network.json), as everywhere in the engine.
$cfg = Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'config\lab-network.json') | ConvertFrom-Json
$hubIp = [string]$cfg.hub; $capIp = [string]$cfg.roles.capstone.address; $prefix = [string]$cfg.prefix
$gwPort = [int]$cfg.gateway.port; $ollamaPort = [int]$cfg.ollama.port; $arenaUrl = ([string]$cfg.arena.endpoint).TrimEnd('/')
$headLo = [int]$cfg.ranges.head[0]; $headHi = [int]$cfg.ranges.head[1]
$knownHeads = @($cfg.known_heads | ForEach-Object { [string]$_ })
$services = @(foreach ($p in $cfg.business_services.PSObject.Properties) { if (-not $p.Name.StartsWith('_')) { [pscustomobject]@{ name = $p.Name.ToUpperInvariant(); head = [string]$p.Value.head; port = [int]$p.Value.port } } })
$lab = @($ips | Where-Object { $_ -like "$prefix.*" }) | Select-Object -First 1
$last = if ($lab) { [int]($lab.Split('.')[-1]) } else { 0 }
$role = if ($Role) { $Role } elseif ($lab -eq $hubIp) { 'hub' } elseif ($lab -eq $capIp) { 'capstone' } elseif ($last -ge $headLo -and $last -le $headHi) { 'head' } else { 'other' }
# A head that lost its lab address (head 5, 2026-11-09) is still a head: known by its computer name in the lab's list.
$byName = @($cfg.head_inventory | Where-Object { [string]$_.computer -eq $env:COMPUTERNAME }) | Select-Object -First 1
$labMissing = $false
if (-not $Role -and $role -eq 'other' -and $byName) { $role = 'head'; $lab = [string]$byName.address; $labMissing = $true }
Write-Host ''
Write-Host ("  Agent Arena lab evidence: {0} ({1}, lab address {2})" -f $env:COMPUTERNAME, $role, $(if ($lab) { $lab } else { 'none: is the lab cable in?' })) -ForegroundColor Green
Write-Host "  Saving to $out"
Write-Host '  Nothing on this computer is changed. This takes a few minutes.'
$wantSpeed = [bool]$SpeedTest
if (-not $wantSpeed -and -not $NoQuestions -and -not $Role -and $role -in 'hub', 'head') {
    Write-Host ''
    Write-Host '  Press Enter to collect logs only (no model is loaded or unloaded on any head: safe during an experiment).' -ForegroundColor Yellow
    $answer = Read-Host '  Or type SPEED to also time the heads (loads and unloads a model on each; only when no experiment is running)'
    $wantSpeed = ($answer -ceq 'SPEED')
}
[void]$log.Add($(if ($wantSpeed) { 'speed test: asked for' } else { 'logs only: no speed test (nothing loaded or unloaded)' }))
# The Gateway's own CA (a public certificate) for its /health and /ready; never Windows' list of public CAs.
$gatewayCa = @('C:\ProgramData\InferenceGateway\tls\inference-gateway-ca.pem', (Join-Path $StickRoot 'gateway-ca\inference-gateway-ca.pem')) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not ('LabTls' -as [type])) { Import-Module (Join-Path $PSScriptRoot 'modules\Lab.Gateway.psm1') -DisableNameChecking -ErrorAction SilentlyContinue }
function Get-GatewayJson([string]$Url) {
    if (-not $gatewayCa) { return @{ ok = $false; url = $Url; error = 'the Gateway CA certificate was not found here, so TLS was not checked (this is not a Gateway outage)'; trust = 'none' } }
    if (-not ('LabTls' -as [type])) { return @{ ok = $false; url = $Url; error = 'the TLS helper (Lab.Gateway.psm1) did not load'; trust = 'none' } }
    $st = 0; $err = ''; $cert = ''
    $text = [LabTls]::Get($Url, $gatewayCa, 15000, [ref]$st, [ref]$err, [ref]$cert)
    $body = $null; try { if ($text) { $body = $text | ConvertFrom-Json } } catch { $body = $text }
    return @{ ok = ($st -ge 200 -and $st -lt 300); url = $Url; status = $st; error = $err; body = $body; server_certificate = $cert; trust = "Gateway CA $gatewayCa" }
}

Section 'This computer' {
    Save-Text 'system\identity.txt' ([ordered]@{ computer = $env:COMPUTERNAME; user = "$env:USERDOMAIN\$env:USERNAME"; role = $role; lab_address = $lab; addresses = $ips -join ', '; time = (Get-Date).ToString('o'); time_zone = (Get-TimeZone).Id; os = (Get-CimInstance Win32_OperatingSystem | ForEach-Object { "$($_.Caption) $($_.Version) build $($_.BuildNumber)" }); last_boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime })
    Save-Text 'system\ipconfig.txt' (ipconfig /all)
    Save-Text 'system\routes.txt' (route print)
    Save-Text 'system\net-profiles.txt' (Get-NetConnectionProfile | Format-List *)
    Save-Text 'system\firewall-profiles.txt' (Get-NetFirewallProfile | Format-List Name, Enabled, DefaultInboundAction, DefaultOutboundAction, AllowLocalFirewallRules)
    $rules = Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match '(?i)ollama|arena|gateway|capstone|inference|lab|python|11434|8443|8790|8000|861' }
    Save-Text 'system\firewall-rules.txt' ($rules | ForEach-Object { $p = $_ | Get-NetFirewallPortFilter; $a = $_ | Get-NetFirewallAddressFilter; $ap = $_ | Get-NetFirewallApplicationFilter; '{0} | {1} {2} {3} | enabled {4} profile {5} | ports {6}/{7} | remote {8} | local {9} | program {10}' -f $_.DisplayName, $_.Direction, $_.Action, $_.Name, $_.Enabled, $_.Profile, $p.Protocol, $p.LocalPort, ($a.RemoteAddress -join ','), ($a.LocalAddress -join ','), $ap.Program })
    $procs = @{}; Get-CimInstance Win32_Process | ForEach-Object { $procs[[int]$_.ProcessId] = $_ }
    Save-Text 'system\listening.txt' (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Sort-Object LocalPort | ForEach-Object { '{0}:{1}  pid {2}  {3}' -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess, $(if ($procs[[int]$_.OwningProcess]) { $procs[[int]$_.OwningProcess].ExecutablePath } else { '' }) })
    Save-Text 'system\processes.txt' ($procs.Values | Where-Object { $_.Name -match '(?i)python|ollama|pwsh|powershell|arena|capstone|gateway|uvicorn|webview2|msedge' } | ForEach-Object { '{0}  {1}  {2}' -f $_.ProcessId, $_.ExecutablePath, ([regex]::Replace([string]$_.CommandLine, '(?i)(token|password|secret)\S*\s*\S+', '$1 <masked>')) })
    Save-Text 'system\services.txt' (Get-CimInstance Win32_Service | Where-Object { $_.Name -match '(?i)ollama|capstone|gateway|arena|inference' -or $_.DisplayName -match '(?i)ollama|capstone|gateway|arena|inference' } | Format-List Name, DisplayName, State, StartMode, StartName, PathName, ExitCode)
    Save-Text 'system\tasks.txt' (Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match '(?i)ollama|capstone|arena|gateway|lab|inference' } | ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; '{0}{1} | {2} | user {3} | last run {4} | last result 0x{5:X} | {6}' -f $_.TaskPath, $_.TaskName, $_.State, $_.Principal.UserId, $i.LastRunTime, [int64]$i.LastTaskResult, (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; ') })
    Save-Text 'system\disks.txt' (Get-PSDrive -PSProvider FileSystem | Format-Table Name, @{n = 'UsedGB'; e = { [math]::Round($_.Used / 1GB, 1) } }, @{n = 'FreeGB'; e = { [math]::Round($_.Free / 1GB, 1) } }, Root -AutoSize)
    $sac = try { (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -ErrorAction Stop).VerifiedAndReputablePolicyState } catch { 'unknown' }
    Save-Text 'system\smart-app-control.txt' ("VerifiedAndReputablePolicyState = $sac (0 off, 1 on, 2 evaluation)")
    $since = (Get-Date).AddHours(-$HoursBack)
    Save-Text 'system\events-application-errors.txt' (Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Level = 1, 2; StartTime = $since } -MaxEvents 300 -ErrorAction SilentlyContinue | Format-List TimeCreated, ProviderName, Id, Message)
    Save-Text 'system\events-system-errors.txt' (Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1, 2; StartTime = $since } -MaxEvents 200 -ErrorAction SilentlyContinue | Format-List TimeCreated, ProviderName, Id, Message)
    Save-Text 'system\events-smart-app-control-blocks.txt' (Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; StartTime = $since } -MaxEvents 500 -ErrorAction SilentlyContinue | Where-Object { $_.Id -in 3033, 3034, 3077, 3089 } | Format-List TimeCreated, Id, Message)
    Say 'network, firewall, ports, services, tasks, processes, Windows errors saved'
}

Section 'Lab network from here' {
    $arenaPort = ([uri]$arenaUrl).Port
    $targets = @(@('hub Gateway', $hubIp, $gwPort), @('hub Arena', $hubIp, $arenaPort), @('Capstone', $capIp, 8000))
    foreach ($s in $services) { $targets += , @("$($s.name) on $($s.head)", $s.head, $s.port) }
    foreach ($h in $knownHeads) { $targets += , @("head $($h.Split('.')[-1]) Ollama", $h, $ollamaPort) }
    $rows = foreach ($t in $targets) { $ping = Test-Connection -ComputerName $t[1] -Count 1 -Quiet -ErrorAction SilentlyContinue; [ordered]@{ what = $t[0]; address = $t[1]; port = $t[2]; ping = $ping; tcp = (Test-Tcp $t[1] $t[2]) } }
    Save-Json 'network\reach.json' @($rows)
    Say (($rows | ForEach-Object { '{0} {1}' -f $_.what, $_.tcp }) -join '; ')
}

# Facts for the cross-machine report (tools/analyze_lab_evidence.py): one facts.json per machine, the same keys on all.
$facts = [ordered]@{ schema = 'agent-arena.lab-facts/1'; computer = $env:COMPUTERNAME; role = $role; lab_address = $lab; collected_at = (Get-Date).ToString('o'); provisioner = (Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'VERSION') -ErrorAction SilentlyContinue).Trim(); expectations = @() }
function Expect([string]$what, [bool]$ok, [string]$detail) { $script:facts.expectations += [ordered]@{ check = $what; ok = $ok; detail = $detail } }
if ($labMissing) { Expect "this head holds its lab address $lab" $false "no adapter has ${lab}: see network\network-devices.txt and adapters_all (disabled or hidden adapters)" }

Section 'Memory: what holds it (committed memory per process and program, pools)' {
    # The 2026-11-09 sweep saw commit at its limit on five heads with small working sets: committed memory, not RAM
    # in use, says which program holds it.
    $perf = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction SilentlyContinue
    $wmi = @{}; Get-CimInstance Win32_Process | ForEach-Object { $wmi[[int]$_.ProcessId] = $_ }
    $top = @(Get-Process | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First 30 | ForEach-Object { $c = $wmi[[int]$_.Id]; [ordered]@{ pid = $_.Id; name = $_.ProcessName; type = $(if ($c -and $c.CommandLine -match '--type=([a-z-]+)') { $Matches[1] } else { '' }); committed_mb = [math]::Round($_.PrivateMemorySize64 / 1MB); in_ram_mb = [math]::Round($_.WorkingSet64 / 1MB); cpu_s = [math]::Round($_.CPU); started = $(try { $_.StartTime.ToString('o') } catch { '' }); path = $(if ($c) { $c.ExecutablePath } else { '' }) } })
    $byProgram = @(Get-Process | Group-Object ProcessName | ForEach-Object { [ordered]@{ name = $_.Name; count = $_.Count; committed_mb = [math]::Round((($_.Group | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB); cpu_s = [math]::Round((($_.Group | Measure-Object CPU -Sum).Sum)) } } | Sort-Object { $_.committed_mb } -Descending | Select-Object -First 20)
    $facts.memory = [ordered]@{ available_mb = $perf.AvailableMBytes; committed_gb = [math]::Round($perf.CommittedBytes / 1GB, 1); commit_limit_gb = [math]::Round($perf.CommitLimit / 1GB, 1); pool_paged_mb = [math]::Round($perf.PoolPagedBytes / 1MB); pool_nonpaged_mb = [math]::Round($perf.PoolNonpagedBytes / 1MB); cache_mb = [math]::Round($perf.CacheBytes / 1MB); modified_mb = [math]::Round($perf.ModifiedPageListBytes / 1MB); top_processes = $top; by_program = $byProgram }
    # Head 7 (2026-11-10): the programs held 6 GB of 43.9; the kernel's pools held 27. Their owners: pool tags and handles.
    try {
        Import-Module (Join-Path $PSScriptRoot 'modules\Lab.Memory.psm1') -Force -DisableNameChecking
        $tags = @(Get-LabPoolTags -Top 12 -FindDrivers); $hand = Get-LabHandleHolders -Top 12
        $facts.memory.pool_tags = $tags; $facts.memory.handles_total = $hand.total; $facts.memory.handle_holders = $hand.top
        Save-Text 'system\kernel-memory.txt' ((Format-LabKernelMemory -Tags $tags -Handles $hand) -join "`r`n")
    } catch { $facts.memory.pool_tags_error = $_.Exception.Message }
    Save-Json 'system\memory.json' $facts.memory
    Say ('committed {0} of {1} GB; most by: {2}' -f $facts.memory.committed_gb, $facts.memory.commit_limit_gb, (($byProgram | Select-Object -First 3 | ForEach-Object { "$($_.name) $($_.committed_mb) MB" }) -join ', '))
    $kp = @($facts.memory.pool_tags | Where-Object { $_.pool -eq 'paged' } | Select-Object -First 1)
    if ($kp) { Say ('kernel: paged pool {0} MB, nonpaged {1} MB; biggest paged tag {2} {3} MB {4}' -f $facts.memory.pool_paged_mb, $facts.memory.pool_nonpaged_mb, $kp[0].tag, $kp[0].mb, $(if ($kp[0].what) { $kp[0].what } else { $kp[0].drivers })) }
    # The hourly memory log FIX-LAB-HEAD starts, and every memory trace run here.
    $mw = 'C:\ProgramData\AgentArenaLab\memory-watch\memory-log.csv'
    if (Test-Path -LiteralPath $mw) { Copy-Evidence -Path (Split-Path -Parent $mw) -Rel 'memory-watch' }
    foreach ($t in @(Get-ChildItem 'C:\Users\*\AppData\Local\AgentArenaLab' -Directory -Filter 'memory-trace-*' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 4)) { Copy-Evidence -Path $t.FullName -Rel ('memory-trace\' + $t.Name) }
}

Section 'Health facts: network links, collisions, clock, duplicates, crashes, power' {
    # Every adapter, also disabled or hidden ones, and the network devices Windows has (head 5's wired adapter vanished).
    $facts.adapters_all = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | ForEach-Object { [ordered]@{ name = $_.Name; description = $_.InterfaceDescription; status = [string]$_.Status; admin = [string]$_.AdminStatus; media = [string]$_.MediaConnectionState; hidden = [bool]$_.Hidden; mac = $_.MacAddress } })
    $facts.network_devices = @(Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -notmatch '(?i)WAN Miniport|Kernel Debug|Bluetooth Device \(RFCOMM' } | ForEach-Object { [ordered]@{ name = $_.FriendlyName; status = [string]$_.Status; problem = [string]$_.Problem; present = [bool]$_.Present; id = $_.InstanceId } })
    Save-Text 'network\network-devices.txt' (Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Format-Table Status, Problem, Present, FriendlyName, InstanceId -AutoSize)
    Save-Text 'network\network-device-events.txt' (Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-PnP', 'e2fexpress', 'e1iexpress', 'Microsoft-Windows-NDIS', 'Netwtw10', 'Microsoft-Windows-UserPnp'; StartTime = (Get-Date).AddDays(-14) } -MaxEvents 200 -ErrorAction SilentlyContinue | Format-List TimeCreated, ProviderName, Id, Message)
    $os = Get-CimInstance Win32_OperatingSystem; $cs = Get-CimInstance Win32_ComputerSystem; $cpu = @(Get-CimInstance Win32_Processor)
    $facts.hardware = [ordered]@{ cpu = (($cpu | ForEach-Object Name) -join '; '); cores = ($cpu | Measure-Object NumberOfCores -Sum).Sum; threads = ($cpu | Measure-Object NumberOfLogicalProcessors -Sum).Sum; max_mhz = ($cpu | Select-Object -First 1).MaxClockSpeed; ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1); free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 1); model = "$($cs.Manufacturer) $($cs.Model)"; last_boot = $os.LastBootUpTime.ToString('o'); uptime_h = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
        gpu = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { [ordered]@{ name = $_.Name; driver_version = $_.DriverVersion; driver_date = $(if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' }) } }) }
    # Network: link speed, errors, power saving on the adapter, duplicate addresses, the lab's address table.
    $facts.adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | ForEach-Object {
        $st = $_ | Get-NetAdapterStatistics -ErrorAction SilentlyContinue
        $eee = @($_ | Get-NetAdapterAdvancedProperty -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match '(?i)energy|green|eee|power sav|wake' } | ForEach-Object { "$($_.DisplayName)=$($_.DisplayValue)" })
        [ordered]@{ name = $_.Name; description = $_.InterfaceDescription; link = $_.LinkSpeed; full_duplex = $_.FullDuplex; mac = $_.MacAddress; rx_errors = $st.ReceivedPacketErrors; rx_discarded = $st.ReceivedDiscardedPackets; tx_errors = $st.OutboundPacketErrors; power_settings = $eee; addresses = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength) $($_.AddressState)" }) } })
    $facts.duplicate_address = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.AddressState -eq 'Duplicate' } | ForEach-Object IPAddress)
    $facts.neighbours = @(Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like "$prefix.*" -and $_.State -ne 'Unreachable' } | ForEach-Object { [ordered]@{ ip = $_.IPAddress; mac = $_.LinkLayerAddress; state = [string]$_.State } })
    Save-Text 'network\arp.txt' (arp -a)
    # Round trips to the hub and the heads, and the clock against the hub (time source of the lab).
    $facts.ping = [ordered]@{}
    foreach ($t in @($hubIp) + $knownHeads) { if ($t -eq $lab) { continue }; $p = @(Test-Connection -ComputerName $t -Count 3 -ErrorAction SilentlyContinue); $facts.ping[$t] = $(if ($p.Count) { [ordered]@{ received = $p.Count; avg_ms = [math]::Round(($p | Measure-Object ResponseTime -Average).Average, 1); max_ms = ($p | Measure-Object ResponseTime -Maximum).Maximum } } else { [ordered]@{ received = 0 } }) }
    if ($lab -ne $hubIp) { $w = (w32tm /stripchart /computer:$hubIp /samples:3 /dataonly 2>&1) -join "`n"; $m = [regex]::Matches($w, '([+-]\d+\.\d+)s'); $facts.clock_offset_s = $(if ($m.Count) { [double]$m[$m.Count - 1].Groups[1].Value } else { $null }); Save-Text 'network\clock-vs-hub.txt' $w }
    Save-Text 'network\time-source.txt' (w32tm /query /status 2>&1)
    # Duplicates and collisions: Ollama installed or running more than once, more than one listener on a lab port,
    # tasks and firewall rules for the same thing, several Pythons.
    $installs = @(@("$env:ProgramFiles\Ollama\ollama.exe", "${env:ProgramFiles(x86)}\Ollama\ollama.exe") + @(Get-ChildItem 'C:\Users\*\AppData\Local\Programs\Ollama\ollama.exe' -ErrorAction SilentlyContinue | ForEach-Object FullName) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)
    $ollamaProcs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '(?i)^ollama' } | ForEach-Object { [ordered]@{ pid = $_.ProcessId; name = $_.Name; path = $_.ExecutablePath; owner = $(try { $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop; "$($o.Domain)\$($o.User)" } catch { '' }); started = $(if ($_.CreationDate) { $_.CreationDate.ToString('o') } else { '' }) } })
    $l = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
    $portUse = [ordered]@{}; foreach ($port in @($ollamaPort, $gwPort, 8790, 8791, 8000, 8611, 8612, 11500)) { $x = @($l | Where-Object LocalPort -eq $port); if ($x.Count) { $portUse["$port"] = @($x | ForEach-Object { [ordered]@{ address = $_.LocalAddress; pid = $_.OwningProcess; program = $(try { (Get-Process -Id $_.OwningProcess -ErrorAction Stop).Path } catch { '' }) } }) } }
    $runKeys = @(foreach ($k in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run') { $p = Get-ItemProperty $k -ErrorAction SilentlyContinue; if ($p) { $p.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' -and "$($_.Name) $($_.Value)" -match '(?i)ollama|arena|capstone|python' } | ForEach-Object { "$k $($_.Name) = $($_.Value)" } } })
    $startupLinks = @(Get-ChildItem "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup", 'C:\Users\*\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup' -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)ollama|arena|capstone' } | ForEach-Object FullName)
    $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match '(?i)ollama|arena|capstone|gateway|inference' } | ForEach-Object { [ordered]@{ name = "$($_.TaskPath)$($_.TaskName)"; state = [string]$_.State; user = $_.Principal.UserId; action = (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; ') } })
    $fw = @(Get-NetFirewallRule -Direction Inbound -ErrorAction SilentlyContinue | Where-Object Enabled -eq 'True' | ForEach-Object { $pf = $_ | Get-NetFirewallPortFilter; if ("$($pf.LocalPort)" -match "(^|,)($ollamaPort|$gwPort|8790|8000|8611|8612|11500)(,|$)") { $af = $_ | Get-NetFirewallAddressFilter; [ordered]@{ name = $_.DisplayName; action = [string]$_.Action; port = "$($pf.LocalPort)"; remote = ($af.RemoteAddress -join ','); profile = [string]$_.Profile } } })
    $pythons = @(@(Get-ChildItem 'C:\Program Files\Python3*\python.exe', 'C:\Users\*\AppData\Local\Programs\Python\Python3*\python.exe' -ErrorAction SilentlyContinue | ForEach-Object FullName) + @(Get-ChildItem 'C:\Users\*\AppData\Local\Microsoft\WindowsApps\python*.exe' -ErrorAction SilentlyContinue | ForEach-Object FullName))
    $facts.duplicates = [ordered]@{ ollama_installs = $installs; ollama_processes = $ollamaProcs; ports = $portUse; run_keys = $runKeys; startup_links = $startupLinks; tasks = $tasks; inbound_rules_on_lab_ports = $fw; pythons = $pythons }
    # Crashes and unclean stops in the last 14 days.
    $d14 = (Get-Date).AddDays(-14)
    $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 41, 6008, 1001; StartTime = $d14 } -MaxEvents 100 -ErrorAction SilentlyContinue)
    $app = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1001, 1002; StartTime = $d14 } -MaxEvents 400 -ErrorAction SilentlyContinue | Where-Object { $_.Message -match '(?i)ollama|python|llama|capstone|arena' })
    $wer = @(Get-ChildItem 'C:\ProgramData\Microsoft\Windows\WER\ReportArchive', 'C:\ProgramData\Microsoft\Windows\WER\ReportQueue' -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)ollama|python|llama' -and $_.LastWriteTime -gt $d14 } | ForEach-Object { '{0:yyyy-MM-dd HH:mm} {1}' -f $_.LastWriteTime, $_.Name })
    $facts.crashes = [ordered]@{ unexpected_restarts = @($ev | ForEach-Object { '{0:yyyy-MM-dd HH:mm} event {1}' -f $_.TimeCreated, $_.Id }); app_crashes = @($app | Select-Object -First 30 | ForEach-Object { '{0:yyyy-MM-dd HH:mm} {1}' -f $_.TimeCreated, ((([string]$_.Message) -split "`r?`n")[0]) }); wer_reports = $wer }
    Save-Text 'system\crash-events.txt' ($ev + $app | Format-List TimeCreated, ProviderName, Id, Message)
    # Power: plan, the Windows 11 power mode (best efficiency / balanced / best performance), mains or battery.
    $overlayKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes'
    $ov = (Get-ItemProperty $overlayKey -ErrorAction SilentlyContinue)
    $modes = @{ '961cc777-2547-4f9d-8174-7d86181b8a7a' = 'best power efficiency'; '00000000-0000-0000-0000-000000000000' = 'balanced'; 'ded574b5-45a0-4f42-8737-46345c09c238' = 'best performance'; '3af9b8d9-7c97-431d-ad78-34a8bfea439f' = 'better performance' }
    $acMode = [string]$ov.ActiveOverlayAcPowerScheme
    $bat = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue)
    $facts.power = [ordered]@{ plan = ((powercfg /getactivescheme) -join ' '); mode_on_mains = $(if ($modes.ContainsKey($acMode)) { $modes[$acMode] } elseif ($acMode) { $acMode } else { 'not set (balanced)' }); battery_present = [bool]$bat.Count; on_mains = $(if ($bat.Count) { [bool](@($bat | Where-Object { $_.BatteryStatus -eq 2 }).Count) } else { $true }); battery_percent = $(if ($bat.Count) { $bat[0].EstimatedChargeRemaining } else { $null }) }
    $cpuPerf = Get-Counter '\Processor Information(_Total)\% Processor Performance', '\Processor(_Total)\% Processor Time' -SampleInterval 2 -MaxSamples 3 -ErrorAction SilentlyContinue
    if ($cpuPerf) { $facts.power.cpu_performance_pct = [math]::Round((($cpuPerf.CounterSamples | Where-Object Path -like '*performance*' | Measure-Object CookedValue -Average).Average), 0); $facts.power.cpu_busy_pct = [math]::Round((($cpuPerf.CounterSamples | Where-Object Path -like '*processor time*' | Measure-Object CookedValue -Average).Average), 0) }
    # Security that slows model loads: real-time scanning of the models folder.
    $mp = Get-MpComputerStatus -ErrorAction SilentlyContinue; $pref = Get-MpPreference -ErrorAction SilentlyContinue
    $facts.defender = [ordered]@{ realtime = $mp.RealTimeProtectionEnabled; exclusions = @($pref.ExclusionPath); excluded_processes = @($pref.ExclusionProcess) }
    $facts.reboot_pending = [bool]((Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'))
    $facts.disks = @(Get-Volume -ErrorAction SilentlyContinue | Where-Object DriveLetter | ForEach-Object { [ordered]@{ drive = "$($_.DriveLetter):"; free_gb = [math]::Round($_.SizeRemaining / 1GB, 1); size_gb = [math]::Round($_.Size / 1GB, 1); health = [string]$_.HealthStatus } })
    $facts.physical_disks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | ForEach-Object { [ordered]@{ name = $_.FriendlyName; media = [string]$_.MediaType; bus = [string]$_.BusType; health = [string]$_.HealthStatus } })
    Say ('{0} adapter(s) up ({1}); {2} Ollama process(es), {3} install(s); power mode {4}; Defender real-time {5}' -f @($facts.adapters).Count, (($facts.adapters | ForEach-Object { "$($_.name) $($_.link)" }) -join ', '), $ollamaProcs.Count, $installs.Count, $facts.power.mode_on_mains, $facts.defender.realtime)
}

Section 'Lab provisioner runs on this computer' {
    $runs = 'C:\ProgramData\AgentArenaLab\runs'
    if (Test-Path $runs) { foreach ($r in @(Get-ChildItem $runs -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 6)) { Copy-Evidence -Path $r.FullName -Rel ('provisioner-runs\' + $r.Name) -MaxBytes 10MB } } else { Say 'none' }
}

if ($role -eq 'hub') {
    Section 'Inference Gateway' {
        Copy-Evidence -Path 'C:\ProgramData\InferenceGateway\logs' -Rel 'gateway\logs'
        Copy-Evidence -Path 'C:\ProgramData\InferenceGateway\observation' -Rel 'gateway\observation'
        Copy-Evidence -Path 'C:\ProgramData\InferenceGateway\observation-state' -Rel 'gateway\observation-state'
        foreach ($f in @(Get-ChildItem 'C:\ProgramData\InferenceGateway' -File -ErrorAction SilentlyContinue)) { Copy-Evidence -Path $f.FullName -Rel 'gateway\root' }
        Copy-Evidence -Path 'C:\ProgramData\InferenceGateway\config' -Rel 'gateway\config'
        foreach ($db in @(Get-ChildItem 'C:\ProgramData\InferenceGateway\state' -Filter '*.db' -ErrorAction SilentlyContinue)) { Copy-Database -Path $db.FullName -Rel ('gateway\state\' + $db.Name) }
        # 1.6.29: checked against the Gateway's own CA. Before, Windows' public CA list refused the lab certificate and
        # the evidence read "Could not establish trust relationship" while the Gateway was up.
        $gh = @((Get-GatewayJson "https://${hubIp}:$gwPort/health"), (Get-GatewayJson "https://${hubIp}:$gwPort/ready"))
        Save-Json 'gateway\health.json' $gh
        Say ('Gateway /health {0}, /ready {1} (TLS: {2})' -f $(if ($gh[0].ok) { 'ok' } else { "not ok: status $($gh[0].status) $($gh[0].error)" }), $(if ($gh[1].ok) { 'ok' } else { "not ok: status $($gh[1].status) $($gh[1].error)" }), $gh[0].trust)
    }
    Section 'Fleet, Gateway Models screen and model library' {
        Copy-Evidence -Path 'C:\ProgramData\AgentArenaLab\fleet' -Rel 'fleet'
        Copy-Evidence -Path 'C:\ProgramData\AgentArenaLab\gateway' -Rel 'gateway-models-data'
        Save-Text 'gateway-models-program.txt' (Get-ChildItem 'C:\Program Files\AgentArenaLab' -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { '{0}  {1}  {2:yyyy-MM-dd HH:mm}  {3}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.Substring(0, 16), $_.Length, $_.LastWriteTime, $_.FullName })
        $ml = 'C:\Program Files\AgentArenaLab\GatewayModels\model_library.py'
        if ($py -and (Test-Path $ml)) { Save-Text 'model-library-index.txt' (& $py -I $ml list --library 'C:\ProgramData\AgentArenaLab\model-library' 2>&1) }
    }
    Section 'Arena (admin hub)' {
        foreach ($u in @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue)) {
            $a = Join-Path $u.FullName 'AppData\Local\AgentArenaService'
            if (-not (Test-Path $a)) { continue }
            Copy-Evidence -Path $a -Rel ("arena\$($u.Name)\data")
            foreach ($db in @(Get-ChildItem $a -Recurse -Filter '*.db' -ErrorAction SilentlyContinue)) { Copy-Database -Path $db.FullName -Rel ("arena\$($u.Name)\db\" + $db.Name) }
            $v = Join-Path $u.FullName 'AppData\Local\Programs\AgentArenaHub\app\arena_service\__init__.py'
            if (Test-Path $v) { Save-Text ("arena\$($u.Name)\program-version.txt") (Select-String -LiteralPath $v -Pattern '__version__') }
        }
        Save-Json 'arena\health.json' (Get-Json "$arenaUrl/health")
    }
    Section 'Every head, over the lab cable' {
        $script:heads = foreach ($n in $headLo..$headHi) {
            $a = "$prefix.$n"
            if ($knownHeads -notcontains $a -and (Test-Tcp $a $ollamaPort 300) -ne 'open') { continue }
            $h = [ordered]@{ address = $a; tcp_11434 = (Test-Tcp $a $ollamaPort) }
            if ($h.tcp_11434 -eq 'open') { $h.version = (Get-Json "http://${a}:$ollamaPort/api/version" @{} 10); $h.models = (Get-Json "http://${a}:$ollamaPort/api/tags" @{} 20); $h.loaded = (Get-Json "http://${a}:$ollamaPort/api/ps" @{} 10) }
            $h
        }
        Save-Json 'heads.json' @($script:heads)
        Say (($script:heads | ForEach-Object { '{0}: {1}' -f $_.address, $_.tcp_11434 }) -join '; ')
    }
    Section 'Gateway work in flight (read from its observation file)' {
        $script:inFlight = $null
        try {
            $o = Get-Content -Raw -LiteralPath ([string]$cfg.gateway.observation_file) | ConvertFrom-Json
            $script:inFlight = @($o.leases).Count
            Say ("{0} call(s) in flight at the Gateway (observation of {1})" -f $script:inFlight, $o.generated_at)
        } catch { Say ('the Gateway observation file could not be read: ' + $_.Exception.Message) }
    }
    Section 'Speed of every head (only when asked; cold and warm, in parallel; each model unloaded after)' {
        if (-not $wantSpeed) { Say 'not run: logs only (run again and type SPEED, when no experiment is running, to time the heads)'; return }
        if ($null -eq $script:inFlight) { Say 'not run: the Gateway''s work in flight is unknown, so a speed test could disturb an experiment'; return }
        if ($script:inFlight -gt 0) { Say ("not run: the Gateway has {0} call(s) in flight (an experiment is running)" -f $script:inFlight); return }
        $up = @($script:heads | Where-Object { $_.tcp_11434 -eq 'open' } | ForEach-Object { $_.address })
        Say ('testing {0} head(s); a cold load can take minutes on a CPU head' -f $up.Count)
        $jobs = foreach ($a in $up) { Start-Job -ScriptBlock $SpeedTestBlock -ArgumentList $a, $ollamaPort, 8192 }
        $speeds = @($jobs | Wait-Job -Timeout 1500 | Receive-Job)
        $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
        Save-Json 'head-speed.json' $speeds
        $facts.head_speed = $speeds
        try {
            $obs = Get-Content -Raw -LiteralPath ([string]$cfg.gateway.observation_file) | ConvertFrom-Json
            $facts.gateway_heads = @($obs.resources | Where-Object { $_.resource_type -eq 'shared_inference' } | ForEach-Object { [ordered]@{ resource_id = $_.resource_id; health = $_.health; fresh = $_.fresh; qualified = @($_.qualified_models).Count } })
            $facts.gateway_open = @($obs.leases).Count
        } catch { }
        foreach ($s in ($speeds | Sort-Object { [int]($_.address.Split('.')[-1]) })) { Say (Format-Speed $s) }
    }
}

if ($role -eq 'capstone') {
    $settingsPath = Join-Path $CapData 'config\appsettings.json'
    $settings = $null; try { $settings = Get-Content -Raw $settingsPath | ConvertFrom-Json } catch { }
    Section 'Capstone release and settings' {
        Copy-Evidence -Path (Join-Path $CapInstall 'current.json') -Rel 'capstone\program'
        Save-Text 'capstone\program\releases.txt' (Get-ChildItem (Join-Path $CapInstall 'releases') -Directory -ErrorAction SilentlyContinue | Format-Table Name, LastWriteTime -AutoSize)
        Copy-Evidence -Path (Join-Path $CapInstall 'tools') -Rel 'capstone\program\tools'
        Copy-Evidence -Path (Join-Path $CapData 'config') -Rel 'capstone\config'
        Copy-Evidence -Path (Join-Path $CapData 'gateway') -Rel 'capstone\gateway'
        Copy-Evidence -Path (Join-Path $CapData 'services') -Rel 'capstone\services'
        Save-Text 'capstone\acl.txt' $(foreach ($p in (Join-Path $CapData 'config'), (Join-Path $CapData 'gateway'), (Join-Path $CapData 'data'), (Join-Path $CapData 'services')) { "== $p"; icacls $p })
    }
    Section 'Capstone logs and databases' {
        Copy-Evidence -Path (Join-Path $CapData 'logs') -Rel 'capstone\logs'
        Copy-Database -Path (Join-Path $CapData 'data\ai_configuration.db') -Rel 'capstone\data\ai_configuration.db'
        Copy-Database -Path (Join-Path $CapData 'data\operations.db') -Rel 'capstone\data\operations.db'
        Save-Text 'capstone\data\files.txt' (Get-ChildItem (Join-Path $CapData 'data') -File -ErrorAction SilentlyContinue | Format-Table Name, Length, LastWriteTime -AutoSize)
    }
    Section 'Capstone Research Lab experiments' {
        $exp = (Join-Path $CapData 'data\experiments')
        $recent = @(Get-ChildItem $exp -Directory -Filter 'EXP-*' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt (Get-Date).AddHours(-$HoursBack) } | Sort-Object LastWriteTime -Descending)
        Save-Text 'capstone\experiments\list.txt' (Get-ChildItem $exp -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Format-Table Name, LastWriteTime -AutoSize)
        $i = 0
        foreach ($e in $recent) {
            Copy-Evidence -Path $e.FullName -Rel ('capstone\experiments\' + $e.Name) -MaxBytes 20MB
            if ($i -lt 12) { foreach ($db in @(Get-ChildItem $e.FullName -Recurse -Filter '*.db' -ErrorAction SilentlyContinue)) { Copy-Database -Path $db.FullName -Rel ('capstone\experiments\' + $db.FullName.Substring($exp.Length + 1)) -MaxMB 400 } }
            $i++
        }
        Say ("{0} experiment(s) of the last {1} h; the databases of the newest 12" -f $recent.Count, $HoursBack)
    }
    Section 'Capstone through its own API (developer token used here, not copied)' {
        if (-not $settings) { Say 'no appsettings.json'; return }
        $base = 'http://{0}:{1}' -f $settings.listen_host, $settings.listen_port
        $h = @{}
        try { $h['X-Capstone-Developer-Token'] = (Get-Content -Raw -LiteralPath $settings.developer_token_file).Trim() } catch { Say ('developer token not readable: ' + $_.Exception.Message) }
        Save-Json 'capstone\api\health.json' (Get-Json "$base/api/health")
        Save-Json 'capstone\api\lab-state.json' (Get-Json "$base/api/lab/state?refresh=true" $h 120)
        $res = Get-Json "$base/api/lab/results" $h 60
        Save-Json 'capstone\api\lab-results.json' $res
        foreach ($p in '/api/lab/mode', '/api/experiments', '/api/ai/active', '/api/ai/profiles', '/api/ai/endpoints') { Save-Json ('capstone\api\' + ($p.Trim('/') -replace '/', '-') + '.json') (Get-Json "$base$p" $h 30) }
        $runs = @(); if ($res.ok) { $runs = @($res.body.experiments | Select-Object -First 15 | ForEach-Object { $x = $_; $_.runs | ForEach-Object { [pscustomobject]@{ eid = $x.id; rid = $_.id } } }) }
        foreach ($r in $runs) {
            Save-Json ("capstone\api\runs\{0}-trace.json" -f $r.rid) (Get-Json "$base/api/experiments/runs/$($r.rid)/trace" $h 60)
            Save-Json ("capstone\api\experiments\{0}.json" -f $r.eid) (Get-Json "$base/api/experiments/$($r.eid)?offset=0&limit=100" $h 60)
        }
        Say ("{0} run trace(s) read through Capstone's API" -f $runs.Count)
    }
    Section 'Arena Ticketing adapter on this laptop' {
        foreach ($u in @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue)) {
            foreach ($d in @(Get-ChildItem (Join-Path $u.FullName 'AppData\Local') -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)arena' })) { Copy-Evidence -Path $d.FullName -Rel ("arena-adapter\$($u.Name)\$($d.Name)") -MaxBytes 10MB }
        }
    }
}

if ($role -eq 'head') {
    Section 'How this head performs (60 s sample, memory, power, Defender, updates, disks)' {
        $os = Get-CimInstance Win32_OperatingSystem; $cs = Get-CimInstance Win32_ComputerSystem; $cpu = @(Get-CimInstance Win32_Processor)
        Save-Text 'performance\hardware.txt' ([ordered]@{ cpu = ($cpu | ForEach-Object { "$($_.Name) ($($_.NumberOfCores) cores, $($_.NumberOfLogicalProcessors) threads, max $($_.MaxClockSpeed) MHz, now $($_.CurrentClockSpeed) MHz)" }) -join '; '; ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1); free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 1); commit_used_gb = [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / 1MB, 1); commit_limit_gb = [math]::Round($os.TotalVirtualMemorySize / 1MB, 1); manufacturer = $cs.Manufacturer; model = $cs.Model })
        Save-Text 'performance\pagefile.txt' ((Get-CimInstance Win32_PageFileUsage | Format-List Name, AllocatedBaseSize, CurrentUsage, PeakUsage), (Get-CimInstance Win32_PageFileSetting | Format-List *), "automatic: $($cs.AutomaticManagedPagefile)")
        $counters = '\Processor(_Total)\% Processor Time', '\Processor Information(_Total)\% Processor Performance', '\Memory\Available MBytes', '\Memory\Pages/sec', '\PhysicalDisk(_Total)\% Disk Time', '\PhysicalDisk(_Total)\Avg. Disk Queue Length'
        $sample = Get-Counter -Counter $counters -SampleInterval 5 -MaxSamples 12 -ErrorAction SilentlyContinue
        Save-Text 'performance\counters-60s.txt' ($sample | ForEach-Object { $t = $_.Timestamp; $_.CounterSamples | ForEach-Object { '{0:HH:mm:ss}  {1,-60} {2,10:N1}' -f $t, $_.Path, $_.CookedValue } })
        Save-Text 'performance\top-processes.txt' ('== by CPU time', (Get-Process | Sort-Object CPU -Descending | Select-Object -First 25 | Format-Table Id, ProcessName, @{n = 'CPU_s'; e = { [math]::Round($_.CPU, 0) } }, @{n = 'WorkingSetMB'; e = { [math]::Round($_.WorkingSet64 / 1MB) } }, Path -AutoSize), '== by memory', (Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 25 | Format-Table Id, ProcessName, @{n = 'WorkingSetMB'; e = { [math]::Round($_.WorkingSet64 / 1MB) } }, @{n = 'CPU_s'; e = { [math]::Round($_.CPU, 0) } }, Path -AutoSize))
        Save-Text 'performance\power.txt' ((powercfg /getactivescheme), (powercfg /query SCHEME_CURRENT SUB_PROCESSOR), (powercfg /availablesleepstates))
        Save-Text 'performance\thermal.txt' (Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue | ForEach-Object { '{0}: {1:N1} C' -f $_.InstanceName, ($_.CurrentTemperature / 10 - 273.15) })
        $mp = Get-MpComputerStatus -ErrorAction SilentlyContinue; $pref = Get-MpPreference -ErrorAction SilentlyContinue
        Save-Text 'performance\defender.txt' ([ordered]@{ realtime = $mp.RealTimeProtectionEnabled; on_access = $mp.OnAccessProtectionEnabled; last_quick_scan = $mp.QuickScanEndTime; last_full_scan = $mp.FullScanEndTime; signatures = $mp.AntivirusSignatureLastUpdated; exclusion_paths = ($pref.ExclusionPath -join '; '); exclusion_processes = ($pref.ExclusionProcess -join '; '); scan_avg_cpu_load = $pref.ScanAvgCPULoadFactor }, (Get-MpThreatDetection -ErrorAction SilentlyContinue | Select-Object -First 20 | Format-List))
        Save-Text 'performance\updates.txt' ((Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 15 | Format-Table HotFixID, Description, InstalledOn -AutoSize), (Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WindowsUpdateClient'; StartTime = (Get-Date).AddDays(-14) } -MaxEvents 60 -ErrorAction SilentlyContinue | Format-Table TimeCreated, Id, Message -AutoSize -Wrap), ('reboot pending: ' + [bool]((Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'))))
        Save-Text 'performance\disks.txt' ((Get-PhysicalDisk | Format-Table FriendlyName, MediaType, BusType, HealthStatus, OperationalStatus, @{n = 'SizeGB'; e = { [math]::Round($_.Size / 1GB) } } -AutoSize), (Get-Volume | Where-Object DriveLetter | Format-Table DriveLetter, FileSystem, HealthStatus, @{n = 'FreeGB'; e = { [math]::Round($_.SizeRemaining / 1GB, 1) } }, @{n = 'SizeGB'; e = { [math]::Round($_.Size / 1GB, 1) } } -AutoSize), (Get-PhysicalDisk | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue | Format-List Temperature, Wear, ReadErrorsTotal, WriteErrorsTotal, PowerOnHours))
        Save-Text 'performance\resource-exhaustion.txt' (Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Resource-Exhaustion-Detector'; StartTime = (Get-Date).AddDays(-14) } -MaxEvents 50 -ErrorAction SilentlyContinue | Format-List TimeCreated, Id, Message)
        Save-Text 'performance\installed-programs.txt' (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object DisplayName | Sort-Object InstallDate -Descending | Format-Table DisplayName, DisplayVersion, InstallDate, Publisher -AutoSize)
        Save-Text 'performance\startup.txt' ((Get-CimInstance Win32_StartupCommand | Format-Table Name, Command, User, Location -AutoSize), (Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.State -ne 'Disabled' -and $_.TaskPath -notlike '\Microsoft\*' } | Format-Table TaskPath, TaskName, State -AutoSize))
        Say ('{0}; {1} GB RAM, {2} GB free' -f (($cpu | ForEach-Object Name) -join ', '), [math]::Round($cs.TotalPhysicalMemory / 1GB, 1), [math]::Round($os.FreePhysicalMemory / 1MB, 1))
    }
    Section 'Ollama on this head (settings, how it starts, logs, speed)' {
        $ollamaEnv = [ordered]@{}; foreach ($scope in 'Machine', 'User') { foreach ($k in @([Environment]::GetEnvironmentVariables($scope).Keys | Where-Object { $_ -like 'OLLAMA*' })) { $ollamaEnv["$scope $k"] = [Environment]::GetEnvironmentVariable($k, $scope) } }
        Save-Text 'ollama\all-ollama-settings.txt' $ollamaEnv
        Save-Text 'ollama\how-it-starts.txt' ((Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match '(?i)ollama' } | ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; '{0} | {1} | user {2} | last run {3} | last result 0x{4:X} | {5}' -f $_.TaskName, $_.State, $_.Principal.UserId, $i.LastRunTime, [int64]$i.LastTaskResult, (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; ') }), (Get-CimInstance Win32_Process | Where-Object { $_.Name -match '(?i)ollama' } | Format-List ProcessId, Name, ExecutablePath, CommandLine, CreationDate), (Get-ChildItem "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup", 'C:\Users\*\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup' -ErrorAction SilentlyContinue | Format-Table FullName -AutoSize))
        foreach ($d in @([string][Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine'), 'C:\ProgramData\Ollama\models') + @(Get-ChildItem 'C:\Users\*\.ollama\models' -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique) {
            $s = Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum
            Save-Text ('ollama\models-folder-' + ($d -replace '[:\\]', '_') + '.txt') ('{0}: {1} files, {2:N1} GB, on drive {3}' -f $d, $s.Count, ($s.Sum / 1GB), $d.Substring(0, 1))
        }
        if ($wantSpeed) {
            $speed = & $SpeedTestBlock $(if ($lab) { $lab } else { '127.0.0.1' }) $ollamaPort 8192
            Save-Json 'ollama\speed.json' $speed
            Say (Format-Speed $speed)
            $facts.speed = $speed
        } else { Say 'speed test not run: logs only (run again and type SPEED, when no experiment is running)' }
        # The same model stored in more than one models folder (disk twice, and two Ollamas may each load their own).
        $dirs = @(@([string][Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine'), [string]$cfg.ollama.new_model_directory) + @(Get-ChildItem 'C:\Users\*\.ollama\models', "$env:windir\System32\config\systemprofile\.ollama\models" -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName) | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ 'blobs')) } | Select-Object -Unique)
        $blobDirs = @{}; $md = @()
        foreach ($d in $dirs) {
            $b = @(Get-ChildItem (Join-Path $d 'blobs') -File -ErrorAction SilentlyContinue)
            foreach ($x in $b) { $blobDirs[$x.Name] = @($blobDirs[$x.Name]) + $d | Where-Object { $_ } }
            $md += [ordered]@{ folder = $d; manifests = @(Get-ChildItem (Join-Path $d 'manifests') -Recurse -File -ErrorAction SilentlyContinue).Count; blobs = $b.Count; gb = [math]::Round((($b | Measure-Object Length -Sum).Sum) / 1GB, 1) }
        }
        $dupBlobs = @($blobDirs.Keys | Where-Object { @($blobDirs[$_]).Count -gt 1 })
        $facts.models_folders = $md; $facts.duplicate_blobs = $dupBlobs.Count
        $tags = (Get-Json "http://$(if ($lab) { $lab } else { '127.0.0.1' }):$ollamaPort/api/tags" @{} 20)
        $ver = (Get-Json "http://$(if ($lab) { $lab } else { '127.0.0.1' }):$ollamaPort/api/version" @{} 10)
        $facts.ollama = [ordered]@{ version = $ver.body.version; models = @($tags.body.models | ForEach-Object name) }
        # Against the lab's own configuration.
        $lis = @(Get-NetTCPConnection -State Listen -LocalPort $ollamaPort -ErrorAction SilentlyContinue)
        Expect 'one Ollama listens on its port' ($lis.Count -ge 1 -and @($lis | Select-Object -ExpandProperty OwningProcess -Unique).Count -eq 1) ('{0} listener(s): {1}' -f $lis.Count, (($lis | ForEach-Object { "$($_.LocalAddress) pid $($_.OwningProcess)" }) -join ', '))
        Expect "Ollama listens on every address ($($cfg.ollama.bind))" ([bool]@($lis | Where-Object { $_.LocalAddress -in '0.0.0.0', '::' }).Count) (($lis | ForEach-Object LocalAddress) -join ', ')
        $serve = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ollama.exe' -and $_.CommandLine -match '\bserve\b' })
        Expect 'exactly one "ollama serve"' ($serve.Count -eq 1) ('{0}: {1}' -f $serve.Count, (($serve | ForEach-Object { "$($_.ProcessId) $($_.ExecutablePath)" }) -join '; '))
        $tray = @(Get-Process -Name 'ollama app', 'ollama app.exe' -ErrorAction SilentlyContinue)
        Expect 'no Ollama tray app competing with the boot task' ($tray.Count -eq 0) ('{0} tray process(es)' -f $tray.Count)
        $task = Get-ScheduledTask -TaskName ([string]$cfg.ollama.task_name) -ErrorAction SilentlyContinue
        Expect "boot task '$($cfg.ollama.task_name)' runs" ($task -and [string]$task.State -eq 'Running') $(if ($task) { [string]$task.State } else { 'not present' })
        $mm = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine')
        Expect "models folder is $($cfg.ollama.new_model_directory)" ($mm -eq [string]$cfg.ollama.new_model_directory) "OLLAMA_MODELS (machine) = '$mm'"
        Expect 'no model stored twice' ($dupBlobs.Count -eq 0) ("{0} file(s) in more than one folder: {1}" -f $dupBlobs.Count, (($md | ForEach-Object { "$($_.folder) $($_.gb) GB" }) -join '; '))
        $managed = @(Get-NetFirewallRule -DisplayName ([string]$cfg.ollama.managed_rule_name) -ErrorAction SilentlyContinue)
        $legacy = @(Get-NetFirewallRule -DisplayName ("$($cfg.ollama.legacy_rule_prefix)*") -ErrorAction SilentlyContinue)
        Expect "firewall rule '$($cfg.ollama.managed_rule_name)'" ($managed.Count -eq 1) ("{0} managed, {1} legacy '$($cfg.ollama.legacy_rule_prefix)*' rule(s)" -f $managed.Count, $legacy.Count)
        $inv = @($cfg.head_inventory | Where-Object { $_.address -eq $lab }) | Select-Object -First 1
        if ($inv) { Expect "RAM as in the lab's head list ($($inv.ram_gb) GB)" ([math]::Abs($facts.hardware.ram_gb - [double]$inv.ram_gb) -lt 1.5) "$($facts.hardware.ram_gb) GB" }
        $ex = @($facts.defender.exclusions)
        Expect 'Defender does not scan the models folder on every load' (-not $facts.defender.realtime -or [bool]@($ex | Where-Object { $_ -and ([string]$cfg.ollama.new_model_directory).StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count) ("real-time {0}; exclusions: {1}" -f $facts.defender.realtime, ($ex -join '; '))
        Expect 'power mode is not "best power efficiency"' ($facts.power.mode_on_mains -ne 'best power efficiency') $facts.power.mode_on_mains
        Expect 'on mains power' ([bool]$facts.power.on_mains) $(if ($facts.power.battery_present) { "battery $($facts.power.battery_percent)%" } else { 'no battery' })
        $labNic = @($facts.adapters | Where-Object { @($_.addresses | Where-Object { $_ -like "$lab/*" }).Count }) | Select-Object -First 1
        if ($labNic) { Expect 'lab cable at 1 Gbps or more' ([string]$labNic.link -match '(\d+(\.\d+)?)\s*Gbps') ("{0}: {1}" -f $labNic.name, $labNic.link) }
        Expect 'no reboot pending' (-not $facts.reboot_pending) "$($facts.reboot_pending)"
        # 1.6.14: the boot wrapper, HEAD-OLLAMA's settings record and backups, the server's own log (its "server config"
        # line proves the effective settings) and the pinned runtime folder; the running server against the pin.
        $od = 'C:\ProgramData\AgentArenaLab\ollama'
        foreach ($f in @(Get-ChildItem -LiteralPath $od -File -ErrorAction SilentlyContinue) + @(Get-ChildItem -LiteralPath (Join-Path $od 'backups') -File -ErrorAction SilentlyContinue)) { Copy-Evidence -Path $f.FullName -Rel ('ollama\managed\' + $(if ($f.Directory.Name -eq 'backups') { 'backups' } else { '' })) }
        Copy-Evidence -Path (Join-Path $od 'logs') -Rel 'ollama\managed\logs' -MaxBytes 20MB
        Save-Text 'ollama\managed\runtime-folders.txt' (Get-ChildItem -LiteralPath (Join-Path $od 'runtime') -Directory -ErrorAction SilentlyContinue | ForEach-Object { '{0}  ollama.exe {1}' -f $_.FullName, (Test-Path -LiteralPath (Join-Path $_.FullName 'ollama.exe')) })
        if ($cfg.ollama.runtime) {
            $pin = [string]$cfg.ollama.runtime.version
            Expect "the running Ollama server is the pinned $pin" ([string]$ver.body.version -eq $pin) ("/api/version = '{0}'; serving program {1}" -f $ver.body.version, (($serve | ForEach-Object ExecutablePath) -join '; '))
            $st = $null; try { $st = Get-Content -LiteralPath (Join-Path $od 'ollama-settings.json') -Raw | ConvertFrom-Json } catch { }
            Expect 'HEAD-OLLAMA settings active (qualified)' ($st -and $st.state -eq 'active') $(if ($st) { "state $($st.state); OLLAMA_NUM_PARALLEL=$($st.env.OLLAMA_NUM_PARALLEL), OLLAMA_MAX_LOADED_MODELS=$($st.env.OLLAMA_MAX_LOADED_MODELS), qualified $($st.qualified_at)" } else { 'no settings record' })
        }
    }
    Section 'Ollama on this head' {
        Save-Text 'ollama\environment.txt' ([ordered]@{ OLLAMA_HOST_machine = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine'); OLLAMA_MODELS_machine = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine'); OLLAMA_KEEP_ALIVE = [Environment]::GetEnvironmentVariable('OLLAMA_KEEP_ALIVE', 'Machine') })
        foreach ($u in @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue)) { Copy-Evidence -Path (Join-Path $u.FullName 'AppData\Local\Ollama') -Rel ("ollama\$($u.Name)") -MaxBytes 20MB }
        # Ollama started by the boot task as SYSTEM keeps its server.log in SYSTEM's profile.
        Copy-Evidence -Path "$env:windir\System32\config\systemprofile\AppData\Local\Ollama" -Rel 'ollama\SYSTEM' -MaxBytes 20MB
        Copy-Evidence -Path 'C:\ProgramData\Ollama' -Rel 'ollama\ProgramData' -MaxBytes 20MB
        Save-Json 'ollama\api.json' @((Get-Json 'http://127.0.0.1:11434/api/version'), (Get-Json "http://${lab}:11434/api/tags"), (Get-Json "http://${lab}:11434/api/ps"))
        foreach ($p in 'C:\ProgramData\CapstoneQMS\logs', 'C:\ProgramData\CapstoneERP\logs') { if (Test-Path $p) { Copy-Evidence -Path $p -Rel ('business-service\' + (Split-Path -Leaf (Split-Path -Parent $p))) } }
        foreach ($u in @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue)) { foreach ($d in @(Get-ChildItem (Join-Path $u.FullName 'AppData\Local') -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)arena' })) { Copy-Evidence -Path $d.FullName -Rel ("arena-viewer\$($u.Name)\$($d.Name)") -MaxBytes 10MB } }
    }
}

if ($role -eq 'capstone') {
    try { $facts.capstone = [ordered]@{ release = (Get-Content -Raw (Join-Path $CapInstall 'current.json') | ConvertFrom-Json).version; health = (Get-Json "http://$($settings.listen_host):$($settings.listen_port)/api/health").body } } catch { }
}
Save-Json 'facts.json' $facts
Remove-Item -LiteralPath $dbCopy -Force -ErrorAction SilentlyContinue
$files = @(Get-ChildItem $out -Recurse -File)
$mb = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
[void]$log.Add(('done: {0} files, {1} MB, {2:N0} s' -f $files.Count, $mb, ((Get-Date) - $started).TotalSeconds))
[IO.File]::WriteAllText((Join-Path $out 'COLLECTION-LOG.txt'), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false))
Write-Host ''
Write-Host ("  DONE: {0} files ({1} MB) saved to {2}" -f $files.Count, $mb, $out) -ForegroundColor Green
Write-Host '  Close this window, then remove the stick with "Safely Remove Hardware" (the USB icon by the clock).'
