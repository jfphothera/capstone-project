# Start-Watch.ps1 - WATCH-LAB.cmd: opens the live, read-only window for the machine it runs on.
#   hub (DESKTOP-REDACT10):           the Inference Gateway's traffic   (Watch-Gateway.ps1)
#   Capstone laptop:          Capstone's agents step by step    (watch_capstone.py)
#   a head:                   this head's Ollama at work        (Watch-Head.ps1)
# The machine is known by its lab address (config\lab-network.json), as in COLLECT-LAB-EVIDENCE. Nothing is changed,
# except that SHORTCUT copies these watch files to C:\ProgramData\AgentArenaLab\watch and puts a "Watch the lab" icon
# on the desktop, so the window opens without the drive.
param([string]$Role = '', [switch]$NoQuestions)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$cfg = Get-Content -Raw -LiteralPath (Join-Path $root 'config\lab-network.json') | ConvertFrom-Json
$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object IPAddress)
$lab = @($ips | Where-Object { $_ -like "$($cfg.prefix).*" }) | Select-Object -First 1
$last = if ($lab) { [int]($lab.Split('.')[-1]) } else { 0 }
if (-not $Role) {
    $Role = if ($lab -eq [string]$cfg.hub) { 'hub' } elseif ($lab -eq [string]$cfg.roles.capstone.address) { 'capstone' } elseif ($last -ge [int]$cfg.ranges.head[0] -and $last -le [int]$cfg.ranges.head[1]) { 'head' } else { '' }
    if (-not $Role -and @($cfg.head_inventory | Where-Object { [string]$_.computer -eq $env:COMPUTERNAME }).Count) { $Role = 'head' }
}
Write-Host ''
Write-Host '  WATCH-LAB: a live, read-only window of the lab at work.' -ForegroundColor Cyan
if (-not $Role) {
    Write-Host ("  This computer ({0}, lab address {1}) is not the hub, the Capstone laptop or a head." -f $env:COMPUTERNAME, $(if ($lab) { $lab } else { 'none' }))
    $pick = Read-Host '  Type HUB, CAPSTONE or HEAD to open that window anyway, or press Enter to close'
    $Role = @{ HUB = 'hub'; CAPSTONE = 'capstone'; HEAD = 'head' }[$pick.Trim().ToUpperInvariant()]
    if (-not $Role) { exit 0 }
}
Write-Host ("  {0} ({1}): {2}" -f $env:COMPUTERNAME, $(if ($lab) { $lab } else { 'no lab address' }), @{ hub = 'the Inference Gateway''s traffic'; capstone = 'Capstone''s agents, step by step'; head = 'this head''s Ollama at work' }[$Role])
if (-not $NoQuestions -and $root -notlike 'C:\ProgramData\AgentArenaLab\watch*') {
    $answer = Read-Host '  Press Enter to watch now, or type SHORTCUT to also put a "Watch the lab" icon on this desktop (opens without the drive)'
    if ($answer.Trim() -ceq 'SHORTCUT') {
        $dest = 'C:\ProgramData\AgentArenaLab\watch'
        New-Item -ItemType Directory -Force -Path (Join-Path $dest 'engine\watch'), (Join-Path $dest 'config') | Out-Null
        Copy-Item -Path (Join-Path $PSScriptRoot '*') -Destination (Join-Path $dest 'engine\watch') -Force
        Copy-Item -LiteralPath (Join-Path $root 'config\lab-network.json') -Destination (Join-Path $dest 'config') -Force
        Copy-Item -LiteralPath (Join-Path $root 'WATCH-LAB.cmd') -Destination $dest -Force
        $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'Watch the lab.lnk'))
        $lnk.TargetPath = Join-Path $dest 'WATCH-LAB.cmd'; $lnk.WorkingDirectory = $dest; $lnk.Description = 'WATCH-LAB: live, read-only view of the lab'; $lnk.Save()
        Write-Host '  "Watch the lab" is on the desktop of every account on this computer.' -ForegroundColor Green
    }
}
Write-Host ''
switch ($Role) {
    'hub' { & (Join-Path $PSScriptRoot 'Watch-Gateway.ps1') }
    'head' { & (Join-Path $PSScriptRoot 'Watch-Head.ps1') }
    'capstone' {
        $data = if ($cfg.capstone.data_root) { [string]$cfg.capstone.data_root } else { 'C:\ProgramData\Capstone' }
        $py = @([string]$cfg.runtime.python.path, 'C:\Program Files\Python312\python.exe') | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
        $script = Join-Path $PSScriptRoot 'watch_capstone.py'
        if ($py) { & $py -I -E -s $script --root $data }
        elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -I -E -s $script --root $data }
        else { Write-Host '  Python 3.12 was not found on this laptop (the provisioner installs it at C:\Program Files\Python312).' -ForegroundColor Red; Read-Host '  Press Enter to close' }
    }
}
