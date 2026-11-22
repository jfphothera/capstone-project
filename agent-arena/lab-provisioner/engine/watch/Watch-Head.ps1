# Watch-Head.ps1 - WATCH-LAB on a head: this head's Ollama at work, live.
# Read-only: it follows Ollama's own server log (shared read, never locked or rotated by this window) and, every 3 s,
# asks this head's Ollama which models it holds (/api/ps: a listing, not a model call; it loads nothing). Each model
# call appears as it ends: what was asked (chat, generate, embedding), by whom (the Gateway on the hub), how long it took.
# Ollama runs with thinking off for Capstone's calls and logs no prompt or answer text: the Capstone window shows those.
param([string]$LogFile = '', [string]$Replay = '', [switch]$Once, [int]$Port = 11434)   # -Replay a saved log, -Once: tests
$ErrorActionPreference = 'Continue'
$cfgPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'config\lab-network.json'
$hub = ''                                         # the hub's address comes from config\lab-network.json
try { $cfg = Get-Content -Raw -LiteralPath $cfgPath | ConvertFrom-Json; $hub = [string]$cfg.hub; if (-not $LogFile) { $LogFile = [string]$cfg.ollama.server_settings.log_file } } catch { }
if ($Replay) { $LogFile = $Replay }
if (-not $LogFile -or -not (Test-Path -LiteralPath $LogFile)) {
    # Older heads: the server started as SYSTEM, or by a signed-in user, keeps server.log in that profile.
    $LogFile = @(@("$env:windir\System32\config\systemprofile\AppData\Local\Ollama\server.log") + @(Get-ChildItem 'C:\Users\*\AppData\Local\Ollama\server.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | ForEach-Object FullName) | Where-Object { Test-Path -LiteralPath $_ }) | Select-Object -First 1
}
$host.UI.RawUI.WindowTitle = "WATCH-LAB: $env:COMPUTERNAME Ollama (read-only)"
function Line([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Caller([string]$ip) { if ($ip -eq $hub) { 'the Gateway (hub)' } elseif ($ip -in '127.0.0.1', '::1') { 'this head' } else { $ip } }
$kinds = @{ '/api/chat' = 'model call (chat)'; '/api/generate' = 'model call (generate)'; '/api/embed' = 'embedding'; '/api/embeddings' = 'embedding'; '/api/show' = 'model details check' }

function Show-LogLine([string]$l) {
    if ($l -match '^\[GIN\] (\S+) - (\S+) \|\s*(\d+) \|\s*([^|]+?)\s*\|\s*(\S+)\s*\|\s*(\w+)\s+"([^"]+)"') {
        $time, $status, $took, $ip, $path = $Matches[2], [int]$Matches[3], $Matches[4].Trim(), $Matches[5], $Matches[7]
        if (-not $kinds.ContainsKey($path)) { return }            # version, tags and ps listings: not work
        $color = if ($status -ge 500) { 'Red' } elseif ($status -ge 400) { 'DarkYellow' } elseif ($path -match 'chat|generate') { 'Yellow' } else { 'DarkCyan' }
        Line ("{0}  {1,-22} {2,9}  from {3}{4}" -f $time, $kinds[$path], $took, (Caller $ip), $(if ($status -ne 200) { "  HTTP $status" } else { '' })) $color
        return
    }
    if ($l -match '^time=\S*T(\d\d:\d\d:\d\d)\S* level=(\w+) .*msg="([^"]+)"') {
        $time, $level, $msg = $Matches[1], $Matches[2], $Matches[3]
        if ($msg -match 'loading model') { Line "$time  loading a model into memory..." 'Cyan'; return }
        if ($msg -match 'llama runner started') { $s = if ($l -match 'started in ([\d.]+) seconds') { " ($($Matches[1]) s)" } else { '' }; Line "$time  model loaded and ready$s" 'Cyan'; return }
        if ($msg -match 'unload|evict|stopping') { Line "$time  model unloaded: $msg" 'DarkCyan'; return }
        if ($msg -match '(?i)cloud|recommendation') { return }                      # Ollama's own online features: not lab work
        if ($level -in 'ERROR', 'WARN') { Line "$time  Ollama $($level.ToLower()): $msg" $(if ($level -eq 'ERROR') { 'Red' } else { 'DarkYellow' }) }
    }
}
function Show-Loaded($last) {
    $ps = $null; try { $ps = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/ps" -TimeoutSec 2 } catch { }
    if (-not $ps) { return $last }
    $now = (@($ps.models | ForEach-Object { '{0} ({1:N1} GB, context {2})' -f $_.name, ($_.size / 1GB), $_.context_length }) | Sort-Object) -join '; '
    if ($now -ne $last) { Line ("{0}  in memory now: {1}" -f (Get-Date -Format 'HH:mm:ss'), $(if ($now) { $now } else { 'no model' })) 'Green' }
    return $now
}

Line "WATCH-LAB: $env:COMPUTERNAME's Ollama, live. Read-only: it follows Ollama's own log and changes nothing." 'Cyan'
if (-not $LogFile) { Line 'Ollama''s server log was not found on this computer. Is this a head?' 'Red'; if ($Once) { exit 2 }; Read-Host 'Press Enter to close'; exit 2 }
Line "Log: $LogFile" 'DarkGray'
try { $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/version" -TimeoutSec 2; Line ("Ollama {0} answers on port {1}." -f $v.version, $Port) 'DarkGray' } catch { Line "Ollama does not answer on port $Port right now (the log still shows what it did)." 'DarkYellow' }
$loaded = ''; $pos = -1; $lastPs = [datetime]::MinValue
while ($true) {
    try {
        $fs = [IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite, Delete')
        try {
            if ($pos -lt 0) { $pos = if ($Replay) { 0 } else { [math]::Max(0, $fs.Length - 4KB) } }      # open with the last few lines
            if ($fs.Length -lt $pos) { $pos = 0 }                                                       # the log was rotated
            $fs.Seek($pos, 'Begin') | Out-Null
            $sr = New-Object IO.StreamReader($fs)
            $text = $sr.ReadToEnd(); $pos = $fs.Length
        } finally { $fs.Close() }
        $lines = $text -split "`r?`n"
        if ($pos -gt 0 -and -not $Replay -and $lines.Count) { $lines = $lines | Select-Object -Skip 1 }   # the first may be cut
        foreach ($l in $lines) { if ($l) { Show-LogLine $l } }
    } catch { Line ((Get-Date -Format 'HH:mm:ss') + "  the log cannot be read right now ($($_.Exception.Message)); retrying") 'DarkYellow' }
    if (((Get-Date) - $lastPs).TotalSeconds -ge 3 -and -not $Replay) { $loaded = Show-Loaded $loaded; $lastPs = Get-Date }
    if ($Once) { exit 0 }
    Start-Sleep -Seconds 1
}
