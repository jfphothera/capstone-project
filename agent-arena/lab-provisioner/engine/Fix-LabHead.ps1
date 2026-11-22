# Fix-LabHead.ps1 - one double-click on a lab head (FIX-LAB-HEAD.cmd at the stick's root):
#   1. records what holds the memory (every process's committed memory, the pools) BEFORE anything changes;
#   2. Windows Defender: excludes the models folder Ollama serves from and Ollama itself from real-time scanning
#      (every cold load of a 3-9 GB model was scanned first: 2026-11-09 sweep);
#   3. stops Ollama's tray app and its start-up shortcut (renamed, not deleted), which starts a second server;
#   4. REPORTS a second models copy (C:\Users\<user>\.ollama\models) and never deletes it. 1.6.0 and 1.6.1 offered to
#      delete it: on head 7 (2026-11-10) the listing returned the store in use's own subfolders (blobs, manifests) as
#      "copies", the check found nothing to compare in them and passed, and the models Ollama serves from were deleted.
#      This fix deletes nothing: a second copy costs disk space only.
#   5. after the word RESTART typed, restarts the head (Ollama comes back by itself: its boot task).
# 1.6.1 also: a head with no lab address (found by its computer name in the head inventory) gets its wired adapter
# back (enabled, restarted, its pre-11-07 settings restored) and its inventory address; and Agent Arena is updated to
# the stick's viewer (2.1.5: the graphics memory leak of the room's ticker signs fixed, the display rebuilt if kernel memory still grows, the last known room shown when the hub is off) for the signed-in user, keeping its credential.
# Every step is written to the stick (reports\<computer>\head-fix-<date>), with how to undo it.
param([string]$StickRoot = '')
$ErrorActionPreference = 'Continue'
if (-not $StickRoot) { $StickRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$cfg = Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'config\lab-network.json') | ConvertFrom-Json
$prefix = [string]$cfg.prefix; $lo = [int]$cfg.ranges.head[0]; $hi = [int]$cfg.ranges.head[1]
$lab = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like "$prefix.*" } | ForEach-Object IPAddress) | Select-Object -First 1
$out = Join-Path $StickRoot ('reports\{0}\head-fix-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date))
New-Item -ItemType Directory -Path $out -Force | Out-Null
$log = New-Object Collections.ArrayList
function Say([string]$t, [string]$c = 'Gray') { Write-Host "  $t" -ForegroundColor $c; [void]$log.Add(('{0:HH:mm:ss} {1}' -f (Get-Date), $t)) }
function Save([string]$name, $v) { [IO.File]::WriteAllText((Join-Path $out $name), [string]($v | Out-String -Width 300), (New-Object Text.UTF8Encoding $false)) }
function Ask([string]$prompt, [string]$word) { Write-Host ''; Write-Host "  $prompt" -ForegroundColor Yellow; $a = Read-Host "  Type $word to go ahead, or press Enter to skip"; return ($a -ceq $word) }

foreach ($m in 'Lab.Core', 'Lab.Arena', 'Lab.Memory') { Import-Module (Join-Path $PSScriptRoot "modules\$m.psm1") -Force -DisableNameChecking }
# A head that lost its lab address (head 5, 2026-11-09) is still known by its computer name in the inventory.
$inv = @($cfg.head_inventory | Where-Object { [string]$_.computer -eq $env:COMPUTERNAME }) | Select-Object -First 1
$target = if ($lab) { $lab } elseif ($inv) { [string]$inv.address } else { '' }
Write-Host ''; Write-Host "  Agent Arena lab head fix: $env:COMPUTERNAME ($(if ($lab) { $lab } elseif ($target) { "should be $target; no lab address now" } else { 'no lab address' }))" -ForegroundColor Green
$n = if ($target) { [int]$target.Split('.')[-1] } else { 0 }
if ($n -lt $lo -or $n -gt $hi) {
    Say "This computer has no head address ($prefix.$lo-$hi) on the lab cable and is not a head in the inventory: nothing was changed." 'Red'
    Save 'ipconfig.txt' (ipconfig /all); Save 'adapters.txt' (Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Format-Table Name, InterfaceDescription, Status, MediaConnectionState, AdminStatus, LinkSpeed -AutoSize); Save 'network-devices.txt' (Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Format-Table Status, Problem, FriendlyName, InstanceId -AutoSize)
    [IO.File]::WriteAllText((Join-Path $out 'FIX-LOG.txt'), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false)); exit 1
}

# 1. What holds the memory, before anything changes.
function Snapshot([string]$name) {
    $os = Get-CimInstance Win32_OperatingSystem
    $perf = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction SilentlyContinue
    $top = Get-Process | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First 30
    $lines = @(('RAM {0:N1} GB, available {1:N0} MB, committed {2:N1} of {3:N1} GB' -f ((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB), $perf.AvailableMBytes, ($perf.CommittedBytes / 1GB), ($perf.CommitLimit / 1GB)),
        ('pools: paged {0:N0} MB, nonpaged {1:N0} MB; cache {2:N0} MB; modified {3:N0} MB; standby {4:N0} MB' -f ($perf.PoolPagedBytes / 1MB), ($perf.PoolNonpagedBytes / 1MB), ($perf.CacheBytes / 1MB), ($perf.ModifiedPageListBytes / 1MB), (($perf.StandbyCacheNormalPriorityBytes + $perf.StandbyCacheReserveBytes + $perf.StandbyCacheCoreBytes) / 1MB)), '',
        'committed MB  in RAM MB  CPU s    pid    process (command)')
    $wmi = @{}; Get-CimInstance Win32_Process | ForEach-Object { $wmi[[int]$_.ProcessId] = $_ }
    foreach ($p in $top) { $c = $wmi[[int]$p.Id]; $type = if ($c -and $c.CommandLine -match '--type=([a-z-]+)') { " --type=$($Matches[1])" } else { '' }; $lines += ('{0,12:N0}  {1,9:N0}  {2,7:N0}  {3,6}  {4}{5}  {6}' -f ($p.PrivateMemorySize64 / 1MB), ($p.WorkingSet64 / 1MB), $p.CPU, $p.Id, $p.ProcessName, $type, $(if ($c) { $c.ExecutablePath } else { '' })) }
    $groups = Get-Process | Group-Object ProcessName | ForEach-Object { [pscustomobject]@{ name = $_.Name; count = $_.Count; committed_mb = [math]::Round((($_.Group | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB) } } | Sort-Object committed_mb -Descending | Select-Object -First 15
    $lines += '', 'by program (all its processes together):'; $lines += @($groups | ForEach-Object { '{0,12:N0} MB  {1,3} x {2}' -f $_.committed_mb, $_.count, $_.name })
    # Head 7: the programs held 6 GB of 43.9; the kernel pools held 27. Who owns kernel memory: its pool tags and the handles.
    $tags = @(); try { $tags = @(Get-LabPoolTags -Top 12 -FindDrivers); $lines += Format-LabKernelMemory -Tags $tags -Handles (Get-LabHandleHolders -Top 12) } catch { $lines += '', ('kernel pool tags could not be read: ' + $_.Exception.Message) }
    Save $name $lines
    $kernel = @($tags | Where-Object pool -eq 'paged' | Select-Object -First 1 | ForEach-Object { 'paged pool {0:N0} MB, biggest tag {1} {2:N0} MB{3}' -f ($perf.PoolPagedBytes / 1MB), $_.tag, $_.mb, $(if ($_.what) { " ($($_.what))" } elseif ($_.drivers) { " (in $($_.drivers))" } else { '' }) })
    return [pscustomobject]@{ committed_gb = [math]::Round($perf.CommittedBytes / 1GB, 1); limit_gb = [math]::Round($perf.CommitLimit / 1GB, 1); top = ($groups | Select-Object -First 3 | ForEach-Object { "$($_.name) $($_.committed_mb) MB" }) -join ', '; kernel = ($kernel -join '') }
}
$before = Snapshot 'memory-before.txt'
Say ("memory now: committed {0} of {1} GB; most by: {2}" -f $before.committed_gb, $before.limit_gb, $before.top) 'Cyan'
if ($before.kernel) { Say ("kernel: {0}" -f $before.kernel) 'Cyan' }

# 1a. The hourly memory log (read-only): every hour one line with the pools, their biggest tags, the handles, Agent
#     Arena and Ollama, in C:\ProgramData\AgentArenaLab\memory-watch. The collector brings it back. Remove with
#     Unregister-ScheduledTask -TaskName 'Agent Arena Lab memory log'.
try {
    $wd = 'C:\ProgramData\AgentArenaLab\memory-watch'; New-Item -ItemType Directory -Path $wd -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'modules\Lab.Memory.psm1') -Destination (Join-Path $wd 'Lab.Memory.psm1') -Force
    $ws = Join-Path $wd 'Watch-LabMemory.ps1'
    [IO.File]::WriteAllText($ws, (New-LabMemoryWatchScript -ModulePath (Join-Path $wd 'Lab.Memory.psm1') -CsvPath (Join-Path $wd 'memory-log.csv')), (New-Object Text.UTF8Encoding $false))
    $act = New-ScheduledTaskAction -Execute "$env:windir\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ws)
    $trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName 'Agent Arena Lab memory log' -Action $act -Trigger $trg -Settings $set -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName 'Agent Arena Lab memory log'
    Say "hourly memory log on: $wd\memory-log.csv (undo: Unregister-ScheduledTask -TaskName 'Agent Arena Lab memory log')" 'Green'
} catch { Say "could not start the hourly memory log: $($_.Exception.Message)" 'Red' }

# 1b. The lab cable, only when this head has lost its address. Head 5 (2026-11-09): no adapter up at all, two days after
#     our 11-07 run turned the wired adapter's power saving and Energy Efficient Ethernet off without restarting its
#     driver. Find the wired adapter even when hidden or disabled, bring it back, put back the settings it worked
#     with before 11-07 (from our own backup), and give it this head's address from the inventory.
function Get-Wired { @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object { $_.HardwareInterface -and [string]$_.PhysicalMediaType -match '802\.3' -and $_.InterfaceDescription -notmatch 'Wi-?Fi|Wireless|WLAN|Bluetooth|Virtual|Hyper-V|VPN|TAP-|Loopback' }) }
function Get-WiredDevices { @(Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'Ethernet|GbE|Gigabit|2\.5G|Realtek PCIe|Killer E|I21\d|I22\d' -and $_.FriendlyName -notmatch 'Wi-?Fi|Wireless|WLAN|Bluetooth|Virtual|WAN Miniport|Hyper-V' }) }
if (-not $lab) {
    Say "no lab address: repairing the lab cable's adapter for $target (from the head inventory)" 'Yellow'
    Save 'network-before.txt' @((ipconfig /all), '', (Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Format-Table Name, InterfaceDescription, Status, MediaConnectionState, AdminStatus, LinkSpeed, MacAddress -AutoSize), (Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Format-Table Status, Problem, Present, FriendlyName, InstanceId -AutoSize))
    $devs = Get-WiredDevices
    if (-not (Get-Wired).Count -or @($devs | Where-Object { $_.Status -ne 'OK' }).Count) { & pnputil.exe /scan-devices | Out-Null; Start-Sleep -Seconds 5; Say 'asked Windows to look for hardware changes (pnputil /scan-devices)'; $devs = Get-WiredDevices }
    foreach ($d in @($devs | Where-Object { $_.Status -eq 'Error' -or ($_.Status -ne 'OK' -and $_.Present) })) {
        $prob = [string]$d.Problem
        try {
            if ($prob -match '22|DISABLED') { Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop; Say "enabled the network device $($d.FriendlyName) (it was turned off in Device Manager)" 'Green' }
            else { & pnputil.exe /restart-device "$($d.InstanceId)" | Out-Null; Say "restarted the network device $($d.FriendlyName) (it showed problem $prob)" 'Green' }
        } catch { Say "could not bring back $($d.FriendlyName): $($_.Exception.Message)" 'Red' }
    }
    foreach ($w in @(Get-Wired | Where-Object { [string]$_.AdminStatus -eq 'Down' -or [string]$_.Status -eq 'Disabled' })) {
        try { Enable-NetAdapter -Name $w.Name -Confirm:$false -ErrorAction Stop; Say "turned the adapter '$($w.Name)' back on (it was disabled; undo: Disable-NetAdapter -Name '$($w.Name)')" 'Green' } catch { Say "could not turn on '$($w.Name)': $($_.Exception.Message)" 'Red' }
    }
    # The settings this adapter worked with before our 11-07 run, applied WITH a driver restart this time.
    $pb = @(Get-ChildItem 'C:\ProgramData\AgentArenaLab\runs\*\backups\power-before.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime) | Select-Object -First 1
    if ($pb) {
        $old = Get-Content -Raw -LiteralPath $pb.FullName | ConvertFrom-Json
        $nic = @(Get-Wired | Where-Object { $_.Name -eq [string]$old.Adapter }) | Select-Object -First 1
        if (-not $nic -and (Get-Wired).Count -eq 1) { $nic = (Get-Wired)[0] }
        if ($nic) {
            foreach ($p in @($old.Eee)) {
                $nowValue = (Get-NetAdapterAdvancedProperty -Name $nic.Name -DisplayName $p.DisplayName -ErrorAction SilentlyContinue).DisplayValue
                if ($nowValue -and $nowValue -ne $p.DisplayValue) { try { Set-NetAdapterAdvancedProperty -Name $nic.Name -DisplayName $p.DisplayName -DisplayValue $p.DisplayValue -ErrorAction Stop; Say "'$($nic.Name)': $($p.DisplayName) back to '$($p.DisplayValue)' as before 11-07 (was '$nowValue'; undo: Set-NetAdapterAdvancedProperty -Name '$($nic.Name)' -DisplayName '$($p.DisplayName)' -DisplayValue '$nowValue')" 'Green' } catch { Say "could not restore $($p.DisplayName): $($_.Exception.Message)" 'Red' } }
            }
            if ([string]$old.AdapterPowerOff -eq 'Enabled') { try { Enable-NetAdapterPowerManagement -Name $nic.Name -ErrorAction Stop; Say "'$($nic.Name)': power management back as before 11-07 (undo: Disable-NetAdapterPowerManagement -Name '$($nic.Name)')" 'Green' } catch { Say "could not restore power management on '$($nic.Name)': $($_.Exception.Message)" 'Red' } }
        }
    }
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline -and -not @(Get-Wired | Where-Object { [string]$_.Status -eq 'Up' }).Count) { Start-Sleep -Seconds 2 }
    $up = @(Get-Wired | Where-Object { [string]$_.Status -eq 'Up' })
    if (-not (Get-Wired).Count) { Say 'Windows sees no wired network adapter on this computer, even after looking for hardware changes. The restart below may bring it back; if not, the network port needs its driver or is turned off in the BIOS.' 'Red' }
    elseif (-not $up.Count) { Say ("the wired adapter '{0}' is on but has no link: check that the lab cable is in this computer's network port and that the port's lights are on" -f ((Get-Wired | ForEach-Object Name) -join "', '")) 'Red' }
    elseif ($up.Count -gt 1) { Say ("more than one wired adapter has a link ({0}): not choosing one; the lab address is not set" -f (($up | ForEach-Object Name) -join ', ')) 'Yellow' }
    else {
        $nic = $up[0]
        $has = @(Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like "$prefix.*" })
        if ($has.Count) { Say "'$($nic.Name)' has its lab address again: $($has[0].IPAddress)" 'Green' }
        elseif (Test-Connection -ComputerName $target -Count 2 -Quiet -ErrorAction SilentlyContinue) { Say "$target already answers on the lab network: another computer holds it, so it was not set here" 'Red' }
        else {
            Save 'address-before.txt' (Get-NetIPConfiguration -InterfaceIndex $nic.ifIndex -Detailed -ErrorAction SilentlyContinue)
            try {
                Set-NetIPInterface -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
                Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
                New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $target -PrefixLength ([int]$cfg.prefix_length) -ErrorAction Stop | Out-Null
                Say "'$($nic.Name)' set to $target/$($cfg.prefix_length), as in the head inventory (undo: Set-NetIPInterface -InterfaceIndex $($nic.ifIndex) -Dhcp Enabled)" 'Green'
            } catch { Say "could not set $target on '$($nic.Name)': $($_.Exception.Message)" 'Red' }
        }
    }
    Start-Sleep -Seconds 3
    $lab = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like "$prefix.*" } | ForEach-Object IPAddress) | Select-Object -First 1
    $hubIp = ([uri][string]$cfg.arena.endpoint).Host
    Say ("lab cable now: {0}; the hub {1} {2}" -f $(if ($lab) { $lab } else { 'still no lab address' }), $hubIp, $(if ($lab -and (Test-Connection -ComputerName $hubIp -Count 2 -Quiet -ErrorAction SilentlyContinue)) { 'answers' } else { 'does not answer' })) $(if ($lab) { 'Green' } else { 'Red' })
    Save 'network-after.txt' @((ipconfig /all), '', (Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Format-Table Name, InterfaceDescription, Status, MediaConnectionState, AdminStatus, LinkSpeed -AutoSize))
}

# Ollama: which one serves, and from which store.
$serve = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ollama.exe' -and $_.CommandLine -match '\bserve\b' }) | Select-Object -First 1
$machineModels = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine')
$active = if ($machineModels -and ((Test-Path -LiteralPath (Join-Path $machineModels 'manifests')) -or (Test-Path -LiteralPath (Join-Path $machineModels 'manifests-v2')))) { (Resolve-Path -LiteralPath $machineModels).Path } else { $null }
Say ("Ollama serving: {0}; models store in use: {1}" -f $(if ($serve) { "$($serve.ExecutablePath) (pid $($serve.ProcessId))" } else { 'none found' }), $(if ($active) { $active } else { 'unknown' }))

# 2. Defender exclusions.
$mp = Get-MpComputerStatus -ErrorAction SilentlyContinue
if ($mp -and $mp.RealTimeProtectionEnabled) {
    $paths = @($active, [string]$cfg.ollama.new_model_directory) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
    $procs = @($(if ($serve) { $serve.ExecutablePath }), "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") + @(Get-ChildItem 'C:\Users\*\AppData\Local\Programs\Ollama\ollama.exe', "$env:ProgramFiles\Ollama\ollama.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
    $have = Get-MpPreference
    foreach ($p in $paths) { if (@($have.ExclusionPath) -notcontains $p) { try { Add-MpPreference -ExclusionPath $p -ErrorAction Stop; Say "Defender: models folder excluded: $p (undo: Remove-MpPreference -ExclusionPath '$p')" 'Green' } catch { Say "Defender: could not exclude ${p}: $($_.Exception.Message)" 'Red' } } else { Say "Defender: already excluded: $p" } }
    foreach ($p in $procs) { if (@($have.ExclusionProcess) -notcontains $p) { try { Add-MpPreference -ExclusionProcess $p -ErrorAction Stop; Say "Defender: Ollama excluded: $p (undo: Remove-MpPreference -ExclusionProcess '$p')" 'Green' } catch { Say "Defender: could not exclude ${p}: $($_.Exception.Message)" 'Red' } } else { Say "Defender: already excluded: $p" } }
} else { Say 'Defender real-time scanning is off here: nothing to exclude' }

# 3. Ollama's tray app (it starts its own server beside the boot task's).
$tray = @(Get-Process -Name 'ollama app' -ErrorAction SilentlyContinue)
foreach ($t in $tray) { try { Stop-Process -Id $t.Id -Force -ErrorAction Stop; Say "Ollama tray app stopped (pid $($t.Id))" 'Green' } catch { Say "could not stop the tray app: $($_.Exception.Message)" 'Red' } }
foreach ($l in @(Get-ChildItem 'C:\Users\*\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\Ollama*.lnk', "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\Ollama*.lnk" -ErrorAction SilentlyContinue)) {
    try { Rename-Item -LiteralPath $l.FullName -NewName ($l.Name + '.disabled-by-lab') -ErrorAction Stop; Say "Ollama tray start-up turned off: $($l.FullName) (undo: rename it back to $($l.Name))" 'Green' } catch { Say "could not turn off $($l.FullName): $($_.Exception.Message)" 'Red' }
}
if (-not $tray.Count) { Say 'no Ollama tray app running' }

# 4. A second models copy: reported only, never deleted (head 7, 2026-11-10: see the top of this file).
function Get-StoreFolders {
    # The folders themselves (Get-Item), never their contents: Get-ChildItem on a folder lists what is inside it.
    param([string[]]$Paths = @('C:\Users\*\.ollama\models', "$env:windir\System32\config\systemprofile\.ollama\models"))
    @(Get-Item -Path $Paths -Force -ErrorAction SilentlyContinue | Where-Object { $_.PSIsContainer } | ForEach-Object { $_.FullName.TrimEnd('\') })
}
function Test-SamePath([string]$a, [string]$b) { return ([IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [IO.Path]::GetFullPath($b).TrimEnd('\')) }
function Test-Within([string]$inner, [string]$outer) { $i = [IO.Path]::GetFullPath($inner).TrimEnd('\') + '\'; $o = [IO.Path]::GetFullPath($outer).TrimEnd('\') + '\'; return $i.StartsWith($o, [StringComparison]::OrdinalIgnoreCase) }
if ($active) {
    # Ollama 0.40 records the models it copies or pulls in manifests-v2, older ones in manifests: count both. Head 7's
    # 26 restored models (2026-11-11 14:55) were all in manifests-v2, and 1.6.5 said "holds 0 model(s)".
    $activeManifests = @('manifests', 'manifests-v2' | ForEach-Object { Get-ChildItem -LiteralPath (Join-Path $active $_) -Recurse -File -ErrorAction SilentlyContinue }).Count
    $activeBlobs = @(Get-ChildItem -LiteralPath (Join-Path $active 'blobs') -File -ErrorAction SilentlyContinue).Count
    if (-not $activeManifests -or -not $activeBlobs) { Say ("the store Ollama serves from ({0}) holds {1} model(s) and {2} file(s): this head has no models to serve" -f $active, $activeManifests, $activeBlobs) 'Red' }
    else { Say ("models store in use: {0} model(s), {1} file(s) in {2}" -f $activeManifests, $activeBlobs, $active) }
    foreach ($st in Get-StoreFolders) {
        if ((Test-SamePath $st $active) -or (Test-Within $st $active) -or (Test-Within $active $st)) { continue }
        $n = @('manifests', 'manifests-v2' | ForEach-Object { Get-ChildItem -LiteralPath (Join-Path $st $_) -Recurse -File -ErrorAction SilentlyContinue }).Count
        if (-not $n) { continue }
        $sz = [math]::Round(((Get-ChildItem -LiteralPath $st -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1GB, 1)
        Say ("a second models copy: {0} ({1} model(s), {2} GB). Kept: Ollama does not use it; it only takes disk space." -f $st, $n, $sz) 'Cyan'
    }
} else { Say 'the store Ollama serves from is unknown' 'Yellow' }

# 4b. Agent Arena: 2.1.2 and earlier kept every finished task in the page and redrew the room 30 times a second with
#     new GPU resources each frame, so the viewer grew for as long as it ran. Updated in place for the signed-in user:
#     no window, no enrollment code; the credential is kept. Without the hub it installs itself at the next sign-in.
$tool = Get-LabViewerToolset -Payload (Join-Path (Split-Path -Parent $PSScriptRoot) 'payload')
if ($tool) {
    try { $u = Update-LabViewerQuiet -Toolset $tool -EvidenceFolder $out -Hub ([string]$cfg.arena.endpoint); Say ("Agent Arena: {0}" -f $u.Detail) $(if ($u.Outcome -in 'DONE', 'UNCHANGED') { 'Green' } else { 'Yellow' }) }
    catch { Say ("Agent Arena update failed: {0}" -f $_.Exception.Message) 'Red' }
} else { Say 'this stick carries no Agent Arena viewer package: the viewer was not updated' 'Yellow' }

$after = Snapshot 'memory-after-fix.txt'
Save 'defender-after.txt' (Get-MpPreference | Format-List ExclusionPath, ExclusionProcess)
[IO.File]::WriteAllText((Join-Path $out 'FIX-LOG.txt'), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false))
Write-Host ''; Write-Host "  Saved to $out" -ForegroundColor Green

# 5. Restart.
if (Ask "Restart $env:COMPUTERNAME now? It clears the exhausted memory and finishes pending Windows updates; Ollama starts again by itself." 'RESTART') {
    [void]$log.Add(('{0:HH:mm:ss} restart requested' -f (Get-Date))); [IO.File]::WriteAllText((Join-Path $out 'FIX-LOG.txt'), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false))
    Write-Host '  Restarting in 15 seconds. Remove the stick now if you need it elsewhere (use "Safely Remove Hardware").' -ForegroundColor Yellow
    Start-Sleep -Seconds 15; Restart-Computer -Force
} else { Say 'not restarted: restart this head yourself to clear the memory' 'Yellow'; [IO.File]::WriteAllText((Join-Path $out 'FIX-LOG.txt'), ($log -join "`r`n"), (New-Object Text.UTF8Encoding $false)) }
