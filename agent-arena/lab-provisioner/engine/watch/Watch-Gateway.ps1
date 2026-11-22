# Watch-Gateway.ps1 - WATCH-LAB on the hub: the Inference Gateway's traffic, live, from its published observation file.
# Read-only: it reads the file the Gateway already publishes every few seconds (the one the Arena hub reads). It never
# calls the Gateway, never opens its ledger and changes nothing. Every line is a request's step through the Gateway:
# queued, admitted to a head, finished or failed; every 20 s a table of the heads (health, calls running, leases).
# The observation carries no model names for requests (the Capstone window and each head's window show those).
param([string]$ObservationFile = '', [string]$Replay = '', [switch]$Once)   # -Replay a saved observation, -Once: tests
$ErrorActionPreference = 'Continue'
$cfgPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'config\lab-network.json'
if (-not $ObservationFile) {
    try { $ObservationFile = [string](Get-Content -Raw -LiteralPath $cfgPath | ConvertFrom-Json).gateway.observation_file } catch { }
    if (-not $ObservationFile) { $ObservationFile = 'C:\ProgramData\InferenceGateway\observation\shared-inference-observation.json' }
}
if ($Replay) { $ObservationFile = $Replay }
$host.UI.RawUI.WindowTitle = 'WATCH-LAB: Inference Gateway (read-only)'
function Line([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Short([string]$id) { if ($id -and $id.Length -gt 12) { $id.Substring(0, 12) } else { $id } }
function HeadName([string]$r) { if ($r -match 'head-192-0-2-(\d+)$') { 'head ' + $Matches[1] } elseif ($r) { $r } else { '?' } }
function At($epoch) { try { ([DateTimeOffset]::FromUnixTimeMilliseconds([int64]([double]$epoch * 1000))).ToLocalTime().ToString('HH:mm:ss') } catch { '--:--:--' } }
function Read-Observation {
    for ($i = 0; $i -lt 3; $i++) {
        try {
            $fs = [IO.File]::Open($ObservationFile, 'Open', 'Read', 'ReadWrite, Delete')
            try { $sr = New-Object IO.StreamReader($fs); $text = $sr.ReadToEnd() } finally { $fs.Close() }
            return ($text | ConvertFrom-Json)
        } catch { Start-Sleep -Milliseconds 300 }
    }
    return $null
}

Line 'WATCH-LAB: the Inference Gateway, live. Read-only: it reads the Gateway''s published observation file and changes nothing.' 'Cyan'
Line ("Observation file: $ObservationFile") 'DarkGray'
$lastEvent = -1; $where = @{}; $lastTable = [datetime]::MinValue; $seenFailures = @{}; $first = $true
while ($true) {
    $o = Read-Observation
    if (-not $o) { Line ((Get-Date -Format 'HH:mm:ss') + '  the observation file cannot be read right now; retrying') 'DarkYellow'; if ($Once) { exit 2 }; Start-Sleep -Seconds 3; continue }
    foreach ($l in @($o.leases)) { if ($l.request_id) { $where[[string]$l.request_id] = [string]$l.resource_id } }
    $events = @($o.events | Sort-Object { [int64]$_.event_id })
    if ($first) {
        # Start from the last few events, so the window opens with context.
        $lastEvent = if ($events.Count -gt 12) { [int64]$events[-13].event_id } else { -1 }
        $first = $false
    }
    foreach ($e in $events) {
        if ([int64]$e.event_id -le $lastEvent) { continue }
        $lastEvent = [int64]$e.event_id
        $rid = [string]$e.request_id; $head = HeadName ([string]$(if ($e.resource_id) { $e.resource_id } else { $where[$rid] }))
        $who = "{0} {1}" -f $e.app_id, (Short $rid)
        switch -Regex ([string]$e.event_type) {
            '^queued$' { Line ("{0}  QUEUED     {1}  waiting for a head" -f (At $e.at), $who) 'DarkGray' }
            '^admitted$' { Line ("{0}  ADMITTED   {1}  placed on {2}" -f (At $e.at), $who, $(if ($head -ne '?') { $head } else { 'a head' })) 'Yellow' }
            '^completed$' { Line ("{0}  FINISHED   {1}{2}" -f (At $e.at), $who, $(if ($head -ne '?') { "  on $head" } else { '' })) 'Green' }
            '^invocation\.failed$' { Line ("{0}  FAILED     {1}{2}" -f (At $e.at), $who, $(if ($head -ne '?') { "  on $head" } else { '' })) 'Red' }
            '^cancelled$' { Line ("{0}  CANCELLED  {1}" -f (At $e.at), $who) 'DarkYellow' }
            '^model\.suspended$' { Line ("{0}  SUSPENDED  a model on {1} (the Gateway holds it until a probe passes)" -f (At $e.at), $head) 'Red' }
            '^model\.recovered$|^probe\.passed$' { Line ("{0}  RECOVERED  {1} on {2}" -f (At $e.at), $e.event_type, $head) 'Green' }
            '^resource\.health_changed$' { Line ("{0}  HEAD       {1} health changed" -f (At $e.at), $head) 'Magenta' }
            default { Line ("{0}  {1,-10} {2}" -f (At $e.at), $e.event_type, $who) 'DarkGray' }
        }
    }
    foreach ($f in @($o.last_hour.invocation_failures)) {
        if (-not $f) { continue }
        $k = '{0}|{1}|{2}|{3}' -f $f.resource_id, $f.model, $f.class, $f.count
        if (-not $seenFailures.ContainsKey($k)) { $seenFailures[$k] = $true; Line ("{0}  LAST HOUR  {1} {2} failure(s) of {3} on {4} ({5})" -f (At $f.last_at), $f.count, $(if ($f.class) { $f.class } else { 'unclassified' }), $f.model, (HeadName $f.resource_id), $f.app_id) 'Red' }
    }
    if (((Get-Date) - $lastTable).TotalSeconds -ge 20 -or $Once) {
        $lastTable = Get-Date
        $busy = @{}; foreach ($l in @($o.leases)) { $h = HeadName $l.resource_id; if (-not $busy[$h]) { $busy[$h] = @(0, 0) }; $busy[$h][0]++; if ([int]$l.executing -gt 0) { $busy[$h][1]++ } }
        $q = $o.requests
        Line ''
        Line ("---- {0}  heads (admission {1}; requests queued {2}, leased {3}) ----" -f (Get-Date -Format 'HH:mm:ss'), $(if ($o.admission_open) { 'open' } else { 'CLOSED' }), [int]$q.queued, [int]$q.leased) 'Cyan'
        foreach ($r in @($o.resources | Where-Object { $_.resource_type -eq 'shared_inference' } | Sort-Object { if ([string]$_.resource_id -match '(\d+)$') { [int]$Matches[1] } else { 999 } })) {
            $h = HeadName $r.resource_id; $b = $busy[$h]; if (-not $b) { $b = @(0, 0) }
            $color = if ($r.health -ne 'healthy') { 'Red' } elseif ($b[1] -gt 0) { 'Yellow' } else { 'Gray' }
            Line ("  {0,-8} {1,-10} running {2,2}   leased {3,2}   models qualified {4}" -f $h, $r.health, $b[1], $b[0], @($r.qualified_models).Count) $color
        }
        foreach ($s in @($o.suspensions)) { if ($s) { Line ("  SUSPENDED {0} on {1}: {2}" -f $s.model, (HeadName $s.resource_id), $s.reason) 'Red' } }
        foreach ($a in @($o.alerts)) { if ($a) { Line ("  ALERT {0}" -f (($a | ConvertTo-Json -Compress -Depth 4)).Substring(0, [Math]::Min(160, ($a | ConvertTo-Json -Compress -Depth 4).Length))) 'Red' } }
        Line ''
    }
    if ($Once) { exit 0 }
    Start-Sleep -Seconds 2
}
