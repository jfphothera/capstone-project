# Shared, READ-ONLY discovery for any Agent Arena display (head or orchestrator). Windows PowerShell 5.1.
# Nothing in this file changes the machine: it reads settings, files, processes
# and makes read-only HTTP requests. Results go to the USB evidence folder.
#
# Nothing here names a particular head. usb-profile.json (written by the coordinator's
# PREPARE-USB) carries the toolset version, the package, the event service and its
# service_id. Which display this computer is comes from the event service registry for
# this computer's own address, so the same USB serves every pre-addressed head.

$ErrorActionPreference = 'Continue'
$UsbRoot = Split-Path $PSScriptRoot -Parent
$UsbProfile = Get-Content (Join-Path $PSScriptRoot 'usb-profile.json') -Raw | ConvertFrom-Json
$Endpoints = @($UsbProfile.event_service_endpoints)

function Write-Banner([string]$Title) {
    Write-Host "`n AGENT ARENA $($UsbProfile.toolset_version) - $Title" -ForegroundColor Cyan
    Write-Host " Toolset folder: $UsbRoot   (prepared $($UsbProfile.prepared_at) for service $($UsbProfile.service_id))`n" -ForegroundColor DarkCyan
}

function Try-Get([scriptblock]$Block, $Default = $null) { try { & $Block } catch { $Default } }

function Test-Tcp([string]$HostName, [int]$Port, [int]$TimeoutMs = 2500) {
    $client = New-Object Net.Sockets.TcpClient
    try { $ok = $client.BeginConnect($HostName, $Port, $null, $null).AsyncWaitHandle.WaitOne($TimeoutMs) -and $client.Connected } catch { $ok = $false }
    $client.Close(); return $ok
}

# .NET HttpWebRequest with no proxy: Invoke-RestMethod in PowerShell 5.1 can stall on proxy
# auto-detection on the isolated LAN (suspected cause of field defect D2).
function Invoke-Http([string]$Url, [int]$TimeoutSec = 10) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $req = [Net.HttpWebRequest][Net.WebRequest]::Create($Url)
        $req.Proxy = $null; $req.Timeout = $req.ReadWriteTimeout = $TimeoutSec * 1000; $req.KeepAlive = $false
        $resp = $req.GetResponse()
        try { $text = (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } finally { $resp.Close() }
        return [pscustomobject]@{ answered = $true; status = 200; text = $text; error = $null; ms = $watch.ElapsedMilliseconds }
    } catch [Net.WebException] {
        $r = $_.Exception.Response
        if ($r) { $code = [int]$r.StatusCode; $r.Close(); return [pscustomobject]@{ answered = $true; status = $code; text = $null; error = $_.Exception.Message; ms = $watch.ElapsedMilliseconds } }
        return [pscustomobject]@{ answered = $false; status = $null; text = $null; error = [string]$_.Exception.Status; ms = $watch.ElapsedMilliseconds }
    } catch { return [pscustomobject]@{ answered = $false; status = $null; text = $null; error = $_.Exception.Message; ms = $watch.ElapsedMilliseconds } }
}

# One retry when nothing answered; a JSON body or $null.
function Get-Json([string]$Url, [int]$TimeoutSec = 10) {
    $r = Invoke-Http $Url $TimeoutSec
    if (-not $r.answered) { $r = Invoke-Http $Url $TimeoutSec }
    if ($r.status -eq 200) { return (Try-Get { $r.text | ConvertFrom-Json }) }
    return $null
}

# Event service check that tells "slow / no answer" apart from "something else answers".
function Get-ServiceCheck([string]$Base) {
    $uri = [Uri]$Base
    $tcp = Test-Tcp $uri.Host $uri.Port
    $first = Invoke-Http ($Base.TrimEnd('/') + '/v2/health') 10
    $r = $(if ($first.answered) { $first } else { Invoke-Http ($Base.TrimEnd('/') + '/v2/health') 15 })
    $health = $(if ($r.status -eq 200) { Try-Get { $r.text | ConvertFrom-Json } } else { $null })
    $verdict = $(if ($health -and $health.service -eq 'agent-arena-event-service' -and $health.service_id -eq $UsbProfile.service_id) { 'ok' }
                 elseif ($health -and $health.service -eq 'agent-arena-event-service') { 'other_arena_service' }
                 elseif ($r.answered) { 'wrong_service' }
                 elseif ($tcp) { 'slow_or_no_answer' } else { 'unreachable' })
    return [ordered]@{ event_service = $Base; tcp_open = $tcp; verdict = $verdict; service_id_matches = ($verdict -eq 'ok')
                       expected_service_id = $UsbProfile.service_id; health = $health; attempts = $(if ($first.answered) { 1 } else { 2 })
                       first_attempt = "answered=$($first.answered) status=$($first.status) $($first.ms) ms $($first.error)"
                       capstone_8000_open = (Test-Tcp $uri.Host 8000 1500) }
}

function Test-InNetwork([string]$Address, [string]$Cidr) {
    $parts = $Cidr.Split('/'); $bits = [int]$parts[1]
    $a = [BitConverter]::ToUInt32(([Net.IPAddress]::Parse($Address).GetAddressBytes()[3..0]), 0)
    $n = [BitConverter]::ToUInt32(([Net.IPAddress]::Parse($parts[0]).GetAddressBytes()[3..0]), 0)
    $size = [uint64][math]::Pow(2, 32 - $bits)                      # addresses in the network
    if ([math]::Floor($a / $size) -ne [math]::Floor($n / $size)) { return $false }
    $hostPart = $a % $size; return ($hostPart -ne 0 -and $hostPart -ne ($size - 1))   # not network or broadcast
}

# Same rules as the desktop app (Identity.Resolve): the registry for this computer's own address
# first; the package's display-network convention only when the registry cannot be asked.
function Resolve-Identity($Addresses) {
    $local = @($Addresses | Where-Object { $_.status -eq 'Up' -and $_.address -notlike '169.254.*' } | ForEach-Object { $_.address })
    $notes = New-Object System.Collections.ArrayList
    foreach ($ep in $Endpoints) {
        $r = Invoke-Http ($ep.TrimEnd('/') + '/v2/whoami') 10
        if (-not $r.answered) { $r = Invoke-Http ($ep.TrimEnd('/') + '/v2/whoami') 15 }
        if ($r.status -ne 200) { [void]$notes.Add("registry lookup at ${ep}: $(if ($r.answered) { "HTTP $($r.status) (service older than 2.1?)" } else { "no answer ($($r.error))" })"); continue }
        $who = Try-Get { $r.text | ConvertFrom-Json }
        if (-not $who -or $local -notcontains $who.address) { [void]$notes.Add("the event service sees this computer as $($who.address), which is not one of its own addresses"); break }
        $c = @($who.displays | ForEach-Object { [ordered]@{ display_id = $_.display_id; role = $_.kind; address = $who.address; location_id = $_.location_id; source = 'registry' } })
        if ($c.Count -eq 0) { [void]$notes.Add("the event service has no display registered at $($who.address). On the coordinator: REGISTER-DISPLAY.cmd $($who.address)") }
        return [ordered]@{ source = 'registry'; address = $who.address; candidates = $c; notes = @($notes) }
    }
    $c = @($local | Where-Object { $a = $_; @($UsbProfile.display_networks | Where-Object { Test-InNetwork $a $_ }).Count -gt 0 } | ForEach-Object {
        [ordered]@{ display_id = 'head-' + $_.Replace('.', '-'); role = 'head'; address = $_; location_id = $null; source = 'convention' } })
    if ($c.Count) { [void]$notes.Add('identity proposed from the display-network convention; the registry could not be asked') }
    return [ordered]@{ source = $(if ($c.Count) { 'convention' } else { 'none' }); address = $null; candidates = $c; notes = @($notes) }
}

# The one identity this USB run acts for: unique, or chosen by the operator when several match.
function Select-Identity($Identity, [switch]$Ask) {
    $c = @($Identity.candidates)
    if ($c.Count -eq 1) { return $c[0] }
    if ($c.Count -eq 0 -or -not $Ask) { return $null }
    Write-Host "  This computer matches several registered displays:" -ForegroundColor Yellow
    for ($i = 0; $i -lt $c.Count; $i++) { Write-Host ("   {0}. {1} ({2}, {3})" -f ($i + 1), $c[$i].display_id, $c[$i].role, $c[$i].address) }
    $n = Read-Host '  Type the number of the display this computer is (or press Enter to stop)'
    if ($n -match '^\d+$' -and [int]$n -ge 1 -and [int]$n -le $c.Count) { return $c[[int]$n - 1] }
    return $null
}

function Get-PackageInfo {
    $dir = Join-Path $UsbRoot ("package\" + $UsbProfile.package)
    $m = Try-Get { Get-Content (Join-Path $dir 'app\app-manifest.json') -Raw | ConvertFrom-Json }
    return [ordered]@{ folder = $dir; exists = (Test-Path $dir); manifest_version = $(if ($m) { $m.version } else { $null }) }
}

function Compare-Version([string]$A, [string]$B) { return ([Version]$A).CompareTo([Version]$B) }

function Get-WebView2Version {
    foreach ($k in 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                   'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                   'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') {
        $pv = Try-Get { (Get-ItemProperty $k -ErrorAction Stop).pv }
        if ($pv -and $pv -ne '0.0.0.0') { return $pv }
    }
    return $null
}

function Get-LegacyViewer {
    $dir = Join-Path $env:LOCALAPPDATA 'Capstone-Arena-Viewer'
    $result = [ordered]@{ folder = $dir; folder_exists = (Test-Path $dir); files = @(); browser_profile_bytes = $null
                          startup_shortcut = $null; scheduled_task = $null; processes = @(); config = $null; other_user_folders = @() }
    if ($result.folder_exists) {
        $result.files = @(Get-ChildItem $dir -File -Force | ForEach-Object {
            [ordered]@{ name = $_.Name; bytes = $_.Length; modified = $_.LastWriteTime.ToString('s'); sha256 = (Get-FileHash $_.FullName).Hash } })
        $profile = Join-Path $dir 'browser-profile'
        if (Test-Path $profile) { $result.browser_profile_bytes = (Get-ChildItem $profile -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }
        $cfg = Join-Path $dir 'config.json'
        if (Test-Path $cfg) { $result.config = Get-Content $cfg -Raw }
    }
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Capstone Arena.lnk'
    if (Test-Path $lnk) {
        $s = Try-Get { (New-Object -ComObject WScript.Shell).CreateShortcut($lnk) }
        $result.startup_shortcut = [ordered]@{ path = $lnk; target = $s.TargetPath; arguments = $s.Arguments }
    }
    $task = Get-ScheduledTask -TaskName ('CapstoneArenaViewer-' + $env:USERNAME) -ErrorAction SilentlyContinue
    if ($task) { $result.scheduled_task = [ordered]@{ name = $task.TaskName; state = [string]$task.State; action = ($task.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' | ' } }
    $result.processes = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe' OR Name='msedge.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*Capstone-Arena-Viewer*' } | ForEach-Object { [ordered]@{ pid = $_.ProcessId; name = $_.Name; command = $_.CommandLine } })
    $result.other_user_folders = @(Try-Get { Get-ChildItem 'C:\Users' -Directory -ErrorAction Stop } @() | ForEach-Object {
        $p = Join-Path $_.FullName 'AppData\Local\Capstone-Arena-Viewer'; if ($p -ne $dir -and (Try-Get { Test-Path $p } $false)) { $p } })
    return $result
}

function Get-AgentArenaInstall {
    $programs = Join-Path $env:LOCALAPPDATA 'Programs\AgentArena'
    $data = Join-Path $env:LOCALAPPDATA 'AgentArena'
    $pointer = Join-Path $programs 'current.txt'
    $current = $(if (Test-Path $pointer) { (Get-Content $pointer).Trim() } else { $null })
    $result = Join-Path $data 'last-setup-result.json'
    $desktop = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Agent Arena.lnk'
    $startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Agent Arena'
    $run = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
    return [ordered]@{
        programs_folder = $programs; programs_exists = (Test-Path $programs); current_version = $current
        versions = @($(if (Test-Path $programs) { Get-ChildItem $programs -Directory | ForEach-Object Name }))
        data_folder = $data; data_files = @($(if (Test-Path $data) { Get-ChildItem $data -Force | ForEach-Object Name }))
        config_exists = (Test-Path (Join-Path $data 'config.json')); credential_exists = (Test-Path (Join-Path $data 'credential.bin'))
        last_setup_result = $(if (Test-Path $result) { Get-Content $result -Raw | ConvertFrom-Json } else { $null })
        run_value = $(if ($run) { $run.AgentArena } else { $null })
        desktop_shortcut = (Test-Path $desktop); start_menu = (Test-Path $startMenu)
        running = @(Get-Process AgentArena -ErrorAction SilentlyContinue | ForEach-Object { [ordered]@{ pid = $_.Id; title = $_.MainWindowTitle } })
    }
}

function Get-UninstallVerdict($A) {
    # After "Uninstall" with default options: no program, startup, shortcuts or process; credential kept.
    if ($A.current_version -or @($A.versions).Count -or @($A.data_files).Count -eq 0) { return $null }   # installed, or never installed
    $clean = (-not $A.run_value) -and (-not $A.desktop_shortcut) -and (-not $A.start_menu) -and @($A.running).Count -eq 0
    $kept = $(if ($A.credential_exists) { 'credential and settings kept for reinstall' } else { 'credential and settings removed' })
    return [ordered]@{ clean = $clean; detail = "program files gone; startup=$([bool]$A.run_value) desktop_shortcut=$($A.desktop_shortcut) start_menu=$($A.start_menu) running=$(@($A.running).Count); $kept" }
}

function Get-Discovery([string]$Stage) {
    $os = Get-CimInstance Win32_OperatingSystem
    $addrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -ne '127.0.0.1' } | ForEach-Object {
        $a = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue
        [ordered]@{ address = $_.IPAddress; prefix = $_.PrefixLength; state = [string]$_.AddressState; interface = $_.InterfaceAlias
                    status = [string]$a.Status; mac = $a.MacAddress; description = $a.InterfaceDescription } })
    $identity = Resolve-Identity $addrs
    $service = Get-ServiceCheck $Endpoints[0]
    $ollamaVersion = Get-Json 'http://127.0.0.1:11434/api/version' 3
    $tags = Get-Json 'http://127.0.0.1:11434/api/tags' 5
    $arena = Get-AgentArenaInstall
    $ollamaProc = @(Get-Process ollama* -ErrorAction SilentlyContinue | ForEach-Object { [ordered]@{ pid = $_.Id; name = $_.ProcessName; started = (Try-Get { $_.StartTime.ToString('s') }) } })
    Add-Type -AssemblyName System.Windows.Forms
    return [ordered]@{
        stage = $Stage; taken_at = (Get-Date).ToString('o'); time_zone = [TimeZoneInfo]::Local.Id; computer = $env:COMPUTERNAME; user = "$env:USERDOMAIN\$env:USERNAME"
        toolset = [ordered]@{ version = $UsbProfile.toolset_version; folder = $UsbRoot; package = $UsbProfile.package }
        is_admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        os = [ordered]@{ caption = $os.Caption; version = $os.Version; build = $os.BuildNumber; arch = $os.OSArchitecture; last_boot = $os.LastBootUpTime.ToString('s') }
        dotnet_release = (Try-Get { (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction Stop).Release })
        webview2 = (Get-WebView2Version); powershell = $PSVersionTable.PSVersion.ToString()
        execution_policy = @(Get-ExecutionPolicy -List | ForEach-Object { "$($_.Scope)=$($_.ExecutionPolicy)" })
        network = $addrs; network_profiles = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { "$($_.InterfaceAlias)=$($_.NetworkCategory)" })
        identity = $identity
        coordinator = $service
        ollama = [ordered]@{ version = $(if ($ollamaVersion) { $ollamaVersion.version } else { $null }); model_count = $(if ($tags) { @($tags.models).Count } else { $null })
                             models = $(if ($tags) { @($tags.models | ForEach-Object name) } else { @() }); processes = $ollamaProc }
        gpu = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object Name)
        nvidia_smi = @("$env:WINDIR\System32\nvidia-smi.exe", "$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe" | Where-Object { Test-Path $_ })
        screens = @([Windows.Forms.Screen]::AllScreens | ForEach-Object { "$($_.DeviceName) $($_.Bounds.Width)x$($_.Bounds.Height) primary=$($_.Primary)" })
        power_scheme = (Try-Get { (powercfg /getactivescheme | Out-String).Trim() })
        display_timeout = (Try-Get { (powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE | Select-String 'Current (AC|DC) Power Setting Index' | ForEach-Object { $_.Line.Trim() }) -join '; ' })
        disk_c_free_gb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
        legacy_viewer = (Get-LegacyViewer); agent_arena = $arena; uninstall_verdict = (Get-UninstallVerdict $arena)
    }
}

function Save-Discovery($Discovery, [string]$Folder) {
    $json = Join-Path $Folder ("discovery-" + $Discovery.stage + ".json")
    $Discovery | ConvertTo-Json -Depth 8 | Set-Content $json -Encoding UTF8
    $d = $Discovery; $lines = @(
        "DISCOVERY ($($d.stage)) $($d.taken_at)",
        "Toolset $($d.toolset.version) from $($d.toolset.folder)",
        "Computer $($d.computer)  user $($d.user)  admin=$($d.is_admin)  time zone $($d.time_zone)",
        "Windows  $($d.os.caption) $($d.os.version) build $($d.os.build) $($d.os.arch)",
        ".NET release $($d.dotnet_release)   WebView2 $($d.webview2)   PowerShell $($d.powershell)",
        "Identity ($($d.identity.source)): $((@($d.identity.candidates) | ForEach-Object { "$($_.display_id) ($($_.role), $($_.address))" }) -join ', ')",
        "Identity notes: $((@($d.identity.notes)) -join ' | ')",
        "Event service $($d.coordinator.event_service): tcp=$($d.coordinator.tcp_open) verdict=$($d.coordinator.verdict) version=$($d.coordinator.health.version) attempts=$($d.coordinator.attempts) first: $($d.coordinator.first_attempt)",
        "Local Ollama: version $($d.ollama.version), $($d.ollama.model_count) models, pids $(($d.ollama.processes | ForEach-Object { $_.pid }) -join ',')",
        "GPU: $(($d.gpu) -join '; ')   nvidia-smi: $(@($d.nvidia_smi).Count -gt 0)",
        "Screens: $(($d.screens) -join '; ')",
        "Display timeout: $($d.display_timeout)",
        "Original viewer folder exists=$($d.legacy_viewer.folder_exists) files=$(@($d.legacy_viewer.files).Count) startup_lnk=$([bool]$d.legacy_viewer.startup_shortcut) task=$([bool]$d.legacy_viewer.scheduled_task) processes=$(@($d.legacy_viewer.processes).Count)",
        "Agent Arena installed=$([bool]$d.agent_arena.current_version) version=$($d.agent_arena.current_version) credential=$($d.agent_arena.credential_exists) startup=$([bool]$d.agent_arena.run_value) desktop_shortcut=$($d.agent_arena.desktop_shortcut) running=$(@($d.agent_arena.running).Count)",
        "Last setup result: $(if ($d.agent_arena.last_setup_result) { "$($d.agent_arena.last_setup_result.action) ok=$($d.agent_arena.last_setup_result.ok) v$($d.agent_arena.last_setup_result.version) at $($d.agent_arena.last_setup_result.finished_at)" } else { "none" })",
        "Uninstall check: $(if ($d.uninstall_verdict) { "clean=$($d.uninstall_verdict.clean) - $($d.uninstall_verdict.detail)" } else { "n/a (installed or never installed)" })")
    $lines | Set-Content (Join-Path $Folder ("discovery-" + $Discovery.stage + ".txt")) -Encoding UTF8
    return $lines
}

# The viewer window is titled 'Agent Arena <em dash> <display>'; Setup and Diagnostics windows are not.
$ViewerTitle = 'Agent Arena ' + [char]0x2014

function Save-WindowImage([string]$TitlePrefix, [string]$Path) {
    # Captures ONLY the Agent Arena window's own content (PrintWindow), never the rest of the screen.
    if (-not ('ArenaWin' -as [type])) {
        Add-Type -AssemblyName System.Drawing
        Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class ArenaWin {
  public delegate bool P(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(P p, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  public struct R { public int L, T, Rt, B; }
}
"@
    }
    $script:found = [IntPtr]::Zero; $script:title = $null
    [ArenaWin]::EnumWindows({ param($h, $l) $sb = New-Object Text.StringBuilder 256; [void][ArenaWin]::GetWindowText($h, $sb, 256)
        if ($sb.ToString().StartsWith($TitlePrefix) -and [ArenaWin]::IsWindowVisible($h)) { $script:found = $h; $script:title = $sb.ToString() }; $true }, [IntPtr]::Zero) | Out-Null
    if ($script:found -eq [IntPtr]::Zero) { return $null }
    $r = New-Object ArenaWin+R; [void][ArenaWin]::GetWindowRect($script:found, [ref]$r)
    $b = New-Object Drawing.Bitmap ([Math]::Max(1, $r.Rt - $r.L)), ([Math]::Max(1, $r.B - $r.T))
    $g = [Drawing.Graphics]::FromImage($b); $dc = $g.GetHdc(); [void][ArenaWin]::PrintWindow($script:found, $dc, 2); $g.ReleaseHdc($dc)
    $b.Save($Path); $g.Dispose(); $b.Dispose()
    return $script:title
}
