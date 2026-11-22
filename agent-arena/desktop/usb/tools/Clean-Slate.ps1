# 0-CLEAN-SLATE: remove everything Agent Arena or the original Phase 4.2 viewer ever placed for
# this Windows user, after showing the list and asking. Re-scans and reports anything left.
# Never touches Ollama, models, network, firewall, power settings or other applications
# (for example the head provisioner folder on the desktop).
. (Join-Path $PSScriptRoot 'HeadCommon.ps1')
$ev = Join-Path $UsbRoot ("evidence\" + $env:COMPUTERNAME + "\" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-clean-slate")
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript (Join-Path $ev 'transcript.txt') | Out-Null
Write-Banner 'CLEAN SLATE (removes Agent Arena from this Windows user, after you confirm)'

$programs = Join-Path $env:LOCALAPPDATA 'Programs\AgentArena'
$data = Join-Path $env:LOCALAPPDATA 'AgentArena'
$legacyDir = Join-Path $env:LOCALAPPDATA 'Capstone-Arena-Viewer'
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$legacyOwned = 'Install.ps1','Viewer.ps1','Diagnostics.ps1','Metrics.ps1','Uninstall.ps1','README.md','VERIFY-ON-EACH-HEAD.md','config.json','prior-power-scheme.txt','diagnostics.json','maintenance.stop'

function Get-LinkTarget([string]$Path) { Try-Get { (New-Object -ComObject WScript.Shell).CreateShortcut($Path).TargetPath } '' }

# Everything this tool may remove. Each item: kind, path/detail, and how to remove it.
function Get-Inventory {
    $items = New-Object System.Collections.ArrayList
    function Add($Kind, $Detail, [scriptblock]$Remove) { [void]$items.Add([pscustomobject]@{ Kind = $Kind; Detail = $Detail; Remove = $Remove }) }
    $session = (Get-Process -Id $PID).SessionId
    foreach ($p in @(Get-Process AgentArena -ErrorAction SilentlyContinue | Where-Object SessionId -eq $session)) {
        $path = Try-Get { $p.MainModule.FileName } ''
        if ($path -and $path.StartsWith($UsbRoot, [StringComparison]::OrdinalIgnoreCase)) { continue }   # the setup window from this USB
        $id = $p.Id; Add 'process (viewer, tray icon or setup)' "PID $id $path" { Stop-Process -Id $id -Force -ErrorAction Stop }.GetNewClosure()
    }
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe' OR Name='msedge.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*Capstone-Arena-Viewer*' })) {
        $id = $p.ProcessId; Add 'legacy viewer process' "PID $id $($p.Name)" { Stop-Process -Id $id -Force -ErrorAction Stop }.GetNewClosure()
    }
    $run = Try-Get { (Get-ItemProperty $runKey -ErrorAction Stop).AgentArena }
    if ($run) { Add 'sign-in startup (HKCU Run AgentArena)' $run { Remove-ItemProperty $runKey -Name AgentArena -ErrorAction Stop } }
    if (Test-Path $programs) {
        foreach ($d in @(Get-ChildItem $programs -Force)) { $f = $d.FullName; Add $(if ($d.Name -like '*.partial') { 'interrupted install (.partial)' } elseif ($d.PSIsContainer) { 'program version folder' } else { 'program file' }) $f { Remove-Item -LiteralPath $f -Recurse -Force -ErrorAction Stop }.GetNewClosure() }
        Add 'program folder' $programs { if (Test-Path $programs) { Remove-Item -LiteralPath $programs -Recurse -Force -ErrorAction Stop } }
    }
    if (Test-Path $data) {
        foreach ($d in @(Get-ChildItem $data -Force)) {
            $kind = switch ($d.Name) { 'credential.bin' { 'viewer credential (DPAPI)' } 'config.json' { 'configuration' } 'webview-data' { 'WebView2 browser data' } 'logs' { 'logs (copied to evidence first)' } 'window.json' { 'window position and zoom' } default { 'data file' } }
            $f = $d.FullName; Add $kind $f { Remove-Item -LiteralPath $f -Recurse -Force -ErrorAction Stop }.GetNewClosure()
        }
        Add 'data folder' $data { if (Test-Path $data) { Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction Stop } }
    }
    $startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Agent Arena'
    if (Test-Path $startMenu) { Add 'Start menu folder' $startMenu { Remove-Item -LiteralPath $startMenu -Recurse -Force -ErrorAction Stop }.GetNewClosure() }
    $lnk = Join-Path $desktop 'Agent Arena.lnk'
    if ((Test-Path $lnk) -and (Get-LinkTarget $lnk) -like "$programs*") { Add 'desktop shortcut' $lnk { Remove-Item -LiteralPath $lnk -Force -ErrorAction Stop }.GetNewClosure() }
    # Toolset and package copies the operator dragged to the desktop (never this USB, never other tools).
    foreach ($d in @(Get-ChildItem $desktop -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^(AGENT-ARENA(-\d+\.\d+\.\d+)?|Agent-Arena-\d+\.\d+\.\d+|Capstone-Arena-Viewer)(\.zip)?$' })) {
        if ($UsbRoot.StartsWith($d.FullName, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $f = $d.FullName; Add 'Arena copy on the desktop (its evidence is copied first)' $f { Remove-Item -LiteralPath $f -Recurse -Force -ErrorAction Stop }.GetNewClosure()
    }
    $legacyLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Capstone Arena.lnk'
    if ((Test-Path $legacyLnk) -and (((Get-LinkTarget $legacyLnk) -like '*Capstone-Arena-Viewer*') -or ((Try-Get { [IO.File]::ReadAllText($legacyLnk) } '') -match 'Capstone-Arena-Viewer'))) {
        Add 'legacy startup shortcut' $legacyLnk { Remove-Item -LiteralPath $legacyLnk -Force -ErrorAction Stop }.GetNewClosure()
    }
    $task = Get-ScheduledTask -TaskName ('CapstoneArenaViewer-' + $env:USERNAME) -ErrorAction SilentlyContinue
    if ($task) { $tn = $task.TaskName; Add 'legacy scheduled task' $tn { Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction Stop }.GetNewClosure() }
    if (Test-Path $legacyDir) {
        $entries = @(Get-ChildItem $legacyDir -Force)
        $owned = @($entries | Where-Object { $_.Name -eq 'browser-profile' -or $legacyOwned -contains $_.Name })
        foreach ($d in $owned) { $f = $d.FullName; Add 'legacy viewer file' $f { Remove-Item -LiteralPath $f -Recurse -Force -ErrorAction Stop }.GetNewClosure() }
        # Files the operator put there are not Agent Arena's: they are kept and reported, and so is the folder.
        if ($owned.Count -eq $entries.Count) { Add 'legacy folder' $legacyDir { if (-not @(Get-ChildItem $legacyDir -Force).Count) { Remove-Item -LiteralPath $legacyDir -Force -ErrorAction Stop } } }
    }
    return $items
}

function Get-Untouchables {
    $o = Get-Json 'http://127.0.0.1:11434/api/version' 3
    return [ordered]@{ ollama_version = $(if ($o) { $o.version } else { $null })
                       ollama_pids = (@(Get-Process ollama* -ErrorAction SilentlyContinue | ForEach-Object Id | Sort-Object) -join ',')
                       power_scheme = (Try-Get { (powercfg /getactivescheme | Out-String).Trim() })
                       addresses = (@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object IPAddress | Sort-Object) -join ',') }
}

$items = Get-Inventory
$guard = Get-Untouchables
$installedVersion = $(if (Test-Path (Join-Path $programs 'current.txt')) { (Get-Content (Join-Path $programs 'current.txt')).Trim() } else { $null })
if ($items.Count -eq 0) {
    Write-Host "  Nothing from Agent Arena or the original viewer was found for $env:USERNAME. Nothing to do." -ForegroundColor Green
    "nothing found" | Set-Content (Join-Path $ev 'SUMMARY.txt'); Stop-Transcript | Out-Null; return
}
Write-Host "  Found for Windows user $env:USERNAME (installed version: $(if ($installedVersion) { $installedVersion } else { 'none' })):" -ForegroundColor Yellow
$items | ForEach-Object { Write-Host ("   - {0}: {1}" -f $_.Kind, $_.Detail) }
$others = @(Try-Get { Get-ChildItem 'C:\Users' -Directory -ErrorAction Stop } @() | ForEach-Object { Join-Path $_.FullName 'AppData\Local\AgentArena' } | Where-Object { $_ -ne $data -and (Try-Get { Test-Path $_ } $false) })
if ($others.Count) { Write-Host "  NOTE: other Windows users also have Agent Arena data (not touched; sign in as them to clean): $($others -join ', ')" -ForegroundColor Yellow }
Write-Host "`n  Not touched: Ollama, models, network, firewall, power settings and other applications." -ForegroundColor DarkCyan
Write-Host "  The viewer credential is deleted, so a NEW enrollment code is needed to install again." -ForegroundColor Yellow
$answer = Read-Host "`n  Type REMOVE to delete all of the above, or press Enter to stop"
if ($answer -ne 'REMOVE') {
    Write-Host "  Stopped. Nothing was changed." -ForegroundColor Green
    "operator stopped; nothing changed" | Set-Content (Join-Path $ev 'SUMMARY.txt'); Stop-Transcript | Out-Null; return
}

# Keep what explains the past before removing it.
foreach ($f in 'logs', 'last-setup-result.json', 'config.json') { $p = Join-Path $data $f; if (Test-Path $p) { Copy-Item $p (Join-Path $ev $f) -Recurse -Force } }
foreach ($d in @(Get-ChildItem $desktop -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^AGENT-ARENA' -and -not $UsbRoot.StartsWith($_.FullName, [StringComparison]::OrdinalIgnoreCase) })) {
    $e = Join-Path $d.FullName 'evidence'; if (Test-Path $e) { Copy-Item $e (Join-Path $ev ("desktop-copy-" + $d.Name)) -Recurse -Force }
}

$log = New-Object System.Collections.ArrayList
foreach ($i in $items) {
    try { & $i.Remove; [void]$log.Add("removed  $($i.Kind): $($i.Detail)"); Write-Host "  removed  $($i.Kind)" -ForegroundColor Green }
    catch { [void]$log.Add("FAILED   $($i.Kind): $($i.Detail) - $($_.Exception.Message)"); Write-Host "  FAILED   $($i.Kind): $($_.Exception.Message)" -ForegroundColor Red }
    if ($i.Kind -like 'process*') { Start-Sleep -Milliseconds 800 }   # let the files it held be released
}

$left = @(Get-Inventory)
$after = Get-Untouchables
$untouched = ($guard.ollama_version -eq $after.ollama_version) -and ($guard.ollama_pids -eq $after.ollama_pids) -and ($guard.power_scheme -eq $after.power_scheme) -and ($guard.addresses -eq $after.addresses)
$legacyKept = $(if (Test-Path $legacyDir) { @(Get-ChildItem $legacyDir -Force | ForEach-Object Name) } else { @() })
$lines = @("CLEAN SLATE on $env:COMPUTERNAME as $env:USERNAME, toolset $($UsbProfile.toolset_version) from $UsbRoot", "previously installed: $installedVersion") + $log +
         @("", "re-scan: $(if ($left.Count) { "$($left.Count) item(s) remain: " + (($left | ForEach-Object { "$($_.Kind) $($_.Detail)" }) -join '; ') } else { 'nothing from Agent Arena remains' })",
           "kept (not Agent Arena's): $(if ($legacyKept.Count) { "$legacyDir contains " + ($legacyKept -join ', ') } else { 'nothing' })",
           "Ollama, power plan and addresses unchanged: $untouched (Ollama $($after.ollama_version) pids $($after.ollama_pids))")
$lines | Set-Content (Join-Path $ev 'SUMMARY.txt') -Encoding UTF8
Write-Host ""
$lines | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
if ($left.Count -eq 0 -and $untouched) { Write-Host "`n  CLEAN: this computer is ready for 2-INSTALL-AND-VERIFY with a new enrollment code.`n  Evidence: $ev`n" -ForegroundColor Cyan }
else { Write-Host "`n  NOT FULLY CLEAN: read the lines above (close any Agent Arena window and run 0-CLEAN-SLATE again).`n  Evidence: $ev`n" -ForegroundColor Red }
Stop-Transcript | Out-Null
