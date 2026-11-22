<#
    Lab.Inspect - Stage A. Read-only discovery.

    Nothing here changes the machine. It reads settings, files, processes, the firewall,
    scheduled tasks and services, runs "ollama --version" and nvidia-smi, and makes plain GET
    requests (Ollama /api/version, /api/tags, /api/ps; Arena /v2/health; Capstone /api/health).
    Every fact says how it is known:

        observed      read directly from this machine
        inferred      derived from observed facts (for example the firewall exposure analysis)
        missing       looked for and not present
        inaccessible  could not be read (permission, a hung command, a service not answering)
#>

$script:Facts = $null

function Add-LabFact {
    param([Parameter(Mandatory)][string]$Area, [Parameter(Mandatory)][string]$Name, $Value, [ValidateSet('observed', 'inferred', 'missing', 'inaccessible')][string]$Status = 'observed', [string]$Source = '')
    $text = $Value
    if ($Value -is [array]) { $text = ($Value | ForEach-Object { [string]$_ }) -join '; ' }
    [void]$script:Facts.Add([pscustomobject][ordered]@{ area = $Area; name = $Name; value = (Protect-LabText ([string]$text)); status = $Status; source = $Source })
}

function Invoke-LabCollect {
    <#  Runs one collector; a failure becomes an 'inaccessible' fact instead of stopping inspection.  #>
    param([Parameter(Mandatory)][string]$Area, [Parameter(Mandatory)][scriptblock]$Block)
    try { return (& $Block) } catch { Add-LabFact -Area $Area -Name 'collector' -Value $_.Exception.Message -Status 'inaccessible'; return $null }
}

function Get-LabDetectedRole {
    <#  What this machine appears to be, from its addresses and installed software.  Pure.  #>
    param([AllowEmptyCollection()][string[]]$LabAddresses = @(), [bool]$OllamaInstalled = $false, [bool]$ModelsPresent = $false, [bool]$HubInstalled = $false, [bool]$CapstoneInstalled = $false)
    foreach ($a in $LabAddresses) {
        $n = Get-LabHostNumber $a
        if ($n -eq 254) { return [pscustomobject]@{ Role = 'hub'; Address = $a; Reason = ('holds the hub address {0}' -f $a) } }
        if ($n -eq 201) { return [pscustomobject]@{ Role = 'capstone'; Address = $a; Reason = 'holds the Capstone laptop address' } }
        if ($n -eq 202) { return [pscustomobject]@{ Role = 'coder'; Address = $a; Reason = 'holds the Coder laptop address' } }
        if ($n -eq 1) { return [pscustomobject]@{ Role = 'dev'; Address = $a; Reason = 'holds the development workstation address' } }
        if ($n -eq 2) { return [pscustomobject]@{ Role = 'gpu-workstation'; Address = $a; Reason = 'holds 192.0.2.2, the hand-built GPU workstation' } }
        if ($n -ge 3 -and $n -le 199) { return [pscustomobject]@{ Role = 'head'; Address = $a; Reason = ('holds head address {0}' -f $a) } }
        if ($n -ge 200 -and $n -le 249) { return [pscustomobject]@{ Role = 'app'; Address = $a; Reason = ('holds application address {0}' -f $a) } }
        if ($n -ge 250 -and $n -le 253) { return [pscustomobject]@{ Role = 'admin'; Address = $a; Reason = ('holds admin address {0}' -f $a) } }
    }
    if ($HubInstalled) { return [pscustomobject]@{ Role = 'hub'; Address = ''; Reason = 'the Arena hub is installed but no lab address is set' } }
    if ($CapstoneInstalled) { return [pscustomobject]@{ Role = 'capstone'; Address = ''; Reason = 'Capstone is installed but no lab address is set' } }
    if ($OllamaInstalled -and $ModelsPresent) { return [pscustomobject]@{ Role = 'head'; Address = ''; Reason = 'Ollama with models is installed but no lab address is set' } }
    return [pscustomobject]@{ Role = 'new'; Address = ''; Reason = 'no lab address and no lab software found' }
}

function Invoke-LabInspection {
    param([Parameter(Mandatory)]$Config, [string]$Role = '', [string]$UsbRoot = '')
    $script:Facts = New-Object System.Collections.ArrayList
    $d = [ordered]@{}
    $d.taken_at = (Get-Date).ToString('o'); $d.computer = $env:COMPUTERNAME; $d.user = "$env:USERDOMAIN\$env:USERNAME"; $d.elevated = (Test-LabElevated)
    Add-LabFact identity 'computer' $env:COMPUTERNAME
    Add-LabFact identity 'signed-in user' $d.user
    Add-LabFact identity 'running as administrator' $d.elevated

    $d.os = Invoke-LabCollect os {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $ed = ''; try { $ed = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop).EditionID } catch { }
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
        $o = [pscustomobject][ordered]@{ Caption = [string]$os.Caption; Edition = $ed; Version = [string]$os.Version; Build = [string]$os.BuildNumber; Arch = [string]$os.OSArchitecture
                                         LastBoot = $os.LastBootUpTime.ToString('o'); TimeZone = (Get-TimeZone).Id; Domain = $(if ($cs) { [string]$cs.Domain } else { '' }); PartOfDomain = $(if ($cs) { [bool]$cs.PartOfDomain } else { $false }) }
        Add-LabFact os 'Windows' ('{0} ({1}) {2} build {3} {4}' -f $o.Caption, $o.Edition, $o.Version, $o.Build, $o.Arch)
        Add-LabFact os 'last boot' $o.LastBoot
        Add-LabFact os 'domain or workgroup' ('{0} (domain joined: {1})' -f $o.Domain, $o.PartOfDomain)
        $o }

    $d.hardware = Invoke-LabCollect hardware {
        $h = Get-CapstoneHardwareFacts
        Add-LabFact hardware 'CPU' ('{0} ({1} cores, {2} threads)' -f $h.CpuName, $h.PhysicalCores, $h.LogicalCores)
        Add-LabFact hardware 'RAM' ('{0} GB' -f $h.TotalMemoryGB)
        if (@($h.Gpus).Count) { Add-LabFact hardware 'GPU' (@($h.Gpus | ForEach-Object { $_.Name })) } else { Add-LabFact hardware 'GPU' 'none reported' -Status missing }
        if ($h.NvidiaVramMB -gt 0) { Add-LabFact hardware 'NVIDIA VRAM' ('{0} MB, driver {1}' -f $h.NvidiaVramMB, $h.NvidiaDriver) -Source 'nvidia-smi' }
        elseif (@($h.Gpus | Where-Object { $_.Name -match 'NVIDIA' }).Count) { Add-LabFact hardware 'NVIDIA VRAM' 'NVIDIA GPU present but nvidia-smi gave no answer' -Status inaccessible }
        else { Add-LabFact hardware 'NVIDIA VRAM' 'no NVIDIA GPU' -Status missing }
        foreach ($v in @($h.Volumes)) { Add-LabFact storage ('volume ' + $v.Drive) ('{0} free of {1}' -f $v.FreeDisplay, (Format-CapstoneBytes -Bytes $v.SizeBytes)) }
        $h }

    $d.network = Invoke-LabCollect network {
        $adapters = @(Get-CapstoneAdapterInventory)
        $conn = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue)
        $lab = @()
        foreach ($a in $adapters) {
            $ips = @($a.IPv4Addresses | ForEach-Object { '{0}/{1}' -f $_.IPAddress, $_.PrefixLength })
            $cat = @($conn | Where-Object { $_.InterfaceIndex -eq $a.InterfaceIndex } | ForEach-Object { [string]$_.NetworkCategory }) -join ','
            Add-LabFact network ('adapter ' + $a.Name) ('{0}; {1}; MAC {2}; IPv4 {3}; gateway {4}; DHCP {5}; profile {6}' -f $a.Description, $a.Status, $a.MacAddress, ($ips -join ', '), (@($a.DefaultGateways) -join ','), $a.Dhcp, $(if ($cat) { $cat } else { 'none' }))
            foreach ($ip in @($a.IPv4Addresses)) { if (Test-LabAddressInSubnet -Address $ip.IPAddress -Prefix $Config.prefix) { $lab += [pscustomobject]@{ Address = $ip.IPAddress; Prefix = $ip.PrefixLength; Adapter = $a.Name; Index = $a.InterfaceIndex; Category = $cat } } }
        }
        if ($lab.Count) { Add-LabFact network 'lab address' (@($lab | ForEach-Object { '{0}/{1} on {2}' -f $_.Address, $_.Prefix, $_.Adapter })) }
        else { Add-LabFact network 'lab address' ('none on ' + $Config.subnet) -Status missing }
        [pscustomobject]@{ Adapters = $adapters; Lab = $lab; Connections = $conn } }
    $labAddrs = @(); if ($d.network) { $labAddrs = @($d.network.Lab | ForEach-Object { $_.Address }) }
    $headIp = ''; if ($labAddrs.Count) { $headIp = $labAddrs[0] }

    $d.ollama = Invoke-LabCollect ollama {
        $exe = Get-CapstoneOllamaExecutable
        $o = [ordered]@{ Executable = $exe; Version = ''; Processes = @(Get-CapstoneOllamaProcesses); Listeners = @(Get-CapstonePortOwner); Endpoints = @(); Base = ''
                         Models = $null; Running = $null; Env = $null; ModelDirectories = @(); Task = $null; TrayAutostart = $null }
        if ($exe) { $o.Version = Get-CapstoneExecutableVersion -Executable $exe; Add-LabFact ollama 'installed' ('{0} (version {1})' -f $exe, $o.Version) } else { Add-LabFact ollama 'installed' 'ollama.exe not found' -Status missing }
        foreach ($b in @(@('127.0.0.1') + $labAddrs)) {
            $base = 'http://{0}:11434' -f $b
            $p = Test-CapstoneOllamaApi -BaseUri $base -TimeoutSeconds 4
            $o.Endpoints += [pscustomobject]@{ Base = $base; Healthy = $p.Healthy; Version = $p.Version }
            if ($p.Healthy -and -not $o.Base) { $o.Base = $base }
            Add-LabFact ollama ('API ' + $base) $(if ($p.Healthy) { 'answers, version ' + $p.Version } else { 'no answer' }) -Status $(if ($p.Healthy) { 'observed' } else { 'missing' })
        }
        Add-LabFact ollama 'listeners on 11434' $(if ($o.Listeners.Count) { @($o.Listeners | ForEach-Object { '{0} ({1})' -f $_.LocalAddress, $_.ProcessName }) } else { 'none' })
        if ($o.Base) {
            $o.Models = Get-LabModelSet -BaseUri $o.Base
            $o.Running = Get-CapstoneRunningModels -BaseUri $o.Base
            Add-LabFact models 'installed models' ('{0}: {1}' -f @($o.Models.Models).Count, ((@($o.Models.Models) | ForEach-Object { $_.name }) -join ', ')) -Source '/api/tags'
            Add-LabFact models 'loaded now' $(if ($o.Running.Count) { $o.Running.Names } else { 'none' }) -Source '/api/ps'
        } else { Add-LabFact models 'installed models' 'no Ollama API answers, so the inventory cannot be read' -Status inaccessible }
        $o.Env = [pscustomobject]@{ MachineHost = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine'); UserHost = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User')
                                    MachineModels = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine'); UserModels = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User') }
        Add-LabFact ollama 'OLLAMA_HOST' ('machine "{0}", user "{1}"' -f $o.Env.MachineHost, $o.Env.UserHost)
        Add-LabFact ollama 'OLLAMA_MODELS' ('machine "{0}", user "{1}"' -f $o.Env.MachineModels, $o.Env.UserModels)
        $o.ModelDirectories = @(Get-LabModelDirectoryCandidates -NewDefault $Config.ollama.new_model_directory)
        foreach ($c in $o.ModelDirectories) { if ($c.Status -ne 'Missing' -or $c.Source -like 'OLLAMA_MODELS*') { Add-LabFact models ('model directory ' + $c.Path) ('{0} manifests ({1})' -f $c.Manifests, $c.Source) -Status $c.Status.ToLowerInvariant() } }
        $o.Task = Get-LabOllamaTask -TaskName $Config.ollama.task_name
        if ($o.Task.Present) { Add-LabFact ollama 'boot task' ('{0}: {1} as {2}; last run {3}, result {4}' -f $Config.ollama.task_name, $o.Task.Action, $o.Task.User, $o.Task.LastRun, $o.Task.LastResult) }
        else { Add-LabFact ollama 'boot task' ('"{0}" not present' -f $Config.ollama.task_name) -Status missing }
        if ($o.Task.Present -and $o.Task.Action -notmatch 'OLLAMA_MODELS|ollama-serve\.cmd') {
            $sys = @($o.ModelDirectories | Where-Object { $_.Source -eq 'SYSTEM profile' }) | Select-Object -First 1
            Add-LabFact ollama 'boot task model directory' ('the task does not set OLLAMA_MODELS; as SYSTEM it uses {0} unless the machine variable is set (machine OLLAMA_MODELS "{1}")' -f $(if ($sys) { $sys.Path } else { 'the SYSTEM profile' }), $o.Env.MachineModels) -Status inferred
        }
        try { $o.TrayAutostart = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name Ollama -ErrorAction Stop).Ollama } catch { }
        [pscustomobject]$o }

    $d.firewall = Invoke-LabCollect firewall {
        $snap = Get-LabFirewallSnapshot
        foreach ($p in @($snap.Profiles)) { Add-LabFact firewall ('profile ' + $p.Name) ('enabled {0}; default inbound {1}; local rules allowed {2}' -f $p.Enabled, $p.DefaultInboundAction, $p.AllowLocalFirewallRules) }
        $exposure = $null
        if ($headIp) {
            $alias = @($d.network.Lab | Where-Object { $_.Address -eq $headIp } | ForEach-Object Adapter) | Select-Object -First 1
            $paths = @(); if ($d.ollama -and $d.ollama.Executable) { $paths += $d.ollama.Executable }; if ($d.ollama) { $paths += @($d.ollama.Processes | ForEach-Object { $_.Path } | Where-Object { $_ }) }
            $sources = @($Config.ollama.client_modes.cutover) + @($Config.ollama.denied_probe_sources)
            $exposure = Get-LabOllamaExposure -Snapshot $snap -HeadIp $headIp -Sources $sources -InterfaceAlias $alias -ServerPaths $paths -Config $Config
            Add-LabFact firewall 'active profile on the lab adapter' $exposure.ActiveProfile
            foreach ($s in @($exposure.Sources)) { Add-LabFact firewall ('Ollama 11434 from ' + $s.Source) ('{0}: {1}' -f $s.Verdict, $s.Reason) -Status inferred -Source 'rule analysis' }
            foreach ($r in @($exposure.RelevantRules)) { Add-LabFact firewall ('rule ' + $r.DisplayName) ('{0}, {1}, profiles {2}, program {3}, ports {4}, remote {5}, source {6}' -f $r.Class, $r.Action, $r.Profiles, $r.Program, $r.LocalPorts, $r.RemoteAddresses, $r.PolicySource) }
            if ($exposure.PolicyNote) { Add-LabFact firewall 'policy' $exposure.PolicyNote -Status inferred }
        }
        [pscustomobject]@{ Snapshot = $snap; Exposure = $exposure } }

    # Coder's laptop: its local accounts, Remote Desktop, Wi-Fi and whether Ethernet and Wi-Fi are kept apart.
    if ($Role -eq 'coder') { $d.coder = Invoke-LabCollect coder { Get-LabCoderFacts -Config $Config } }
    $d.time = Invoke-LabCollect time { $t = Get-LabTimeState; Add-LabFact time 'time zone' $t.TimeZone; Add-LabFact time 'Windows Time' ('{0}, start {1}, source "{2}" ({3})' -f $t.Service, $t.StartMode, $t.NtpServer, $t.Type); $t }
    $alias0 = ''; if ($d.network -and @($d.network.Lab).Count) { $alias0 = $d.network.Lab[0].Adapter }
    $d.power = Invoke-LabCollect power { $p = Get-LabPowerState -Adapter $alias0; Add-LabFact power 'sleep on AC' ('standby {0} s, hibernate {1} s' -f $p.StandbyAcSeconds, $p.HibernateAcSeconds); if ($alias0) { Add-LabFact power ('adapter ' + $alias0) ('may be turned off: {0}; EEE {1}' -f $p.AdapterPowerOff, ((@($p.Eee) | ForEach-Object { '{0}={1}' -f $_.DisplayName, $_.DisplayValue }) -join ', ')) }; $p }

    # Smart App Control blocks unsigned programs without reputation, such as the Arena viewer (head 10,
    # 2026-11-07: "An Application Control policy has blocked this file").
    $d.appcontrol = Invoke-LabCollect appcontrol {
        $ac = Get-LabAppControlState
        Add-LabFact security 'Smart App Control' ('{0}{1}' -f $ac.SmartAppControl, $(if ($ac.Blocks.Count) { '; recently blocked: ' + ((@($ac.Blocks) | Select-Object -First 3 | ForEach-Object { $_.File }) -join ', ') } else { '' })) -Status $(if ($ac.SmartAppControl -eq 'unknown') { 'inaccessible' } else { 'observed' })
        $ac }

    $d.vcruntime = Invoke-LabCollect vcruntime {
        $vc = Get-LabVcRuntimeState
        Add-LabFact security 'Visual C++ 2015-2022 runtime (x64)' $(if ($vc.Ok) { 'present, version ' + $vc.Version } else { 'missing: ' + ($vc.Missing -join ', ') }) -Status $(if ($vc.Ok) { 'observed' } else { 'missing' })
        $vc }

    $d.tasks = Invoke-LabCollect tasks {
        $t = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -match '(?i)ollama|capstone|inferencegateway|arena' } | ForEach-Object { [pscustomobject]@{ Name = $_.TaskName; Path = $_.TaskPath; State = [string]$_.State; User = [string]$_.Principal.UserId } })
        Add-LabFact tasks 'relevant scheduled tasks' $(if ($t.Count) { @($t | ForEach-Object { '{0} ({1}, {2})' -f $_.Name, $_.State, $_.User }) } else { 'none' }) -Status $(if ($t.Count) { 'observed' } else { 'missing' })
        $t }
    $d.services = Invoke-LabCollect services {
        $s = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)ollama|capstone|gateway|arena' -or $_.DisplayName -match '(?i)ollama|capstone|inference gateway|agent arena' } | ForEach-Object { [pscustomobject]@{ Name = $_.Name; Status = [string]$_.Status } })
        Add-LabFact services 'relevant Windows services' $(if ($s.Count) { @($s | ForEach-Object { '{0} ({1})' -f $_.Name, $_.Status }) } else { 'none' }) -Status $(if ($s.Count) { 'observed' } else { 'missing' })
        $s }

    $d.arena = Invoke-LabCollect arena {
        $h = Get-LabArenaHealth -Endpoint $Config.arena.endpoint -ExpectedServiceId $Config.arena.service_id
        Add-LabFact arena 'event service' ('{0}: {1} {2} {3}' -f $Config.arena.endpoint, $h.Verdict, $h.ServiceId, $h.Version) -Status $(if ($h.Verdict -eq 'ok') { 'observed' } else { 'inaccessible' })
        $v = Get-LabViewerState
        Add-LabFact arena 'head viewer' $(if ($v.Installed) { 'version {0}; credential present {1}; starts at sign-in {2}; endpoints {3}' -f $v.Version, $v.CredentialPresent, $v.StartAtSignIn, ($v.Endpoints -join ',') } else { 'not installed for ' + $env:USERNAME }) -Status $(if ($v.Installed) { 'observed' } else { 'missing' })
        $t = Get-LabTicketingState
        if ($t.ProgramInstalled -or $t.CredentialPresent) { Add-LabFact arena 'Ticketing adapter' ('installed {0}; credential present {1}; running {2}' -f $t.ProgramInstalled, $t.CredentialPresent, $t.Running) }
        $hub = $null
        if ($Role -eq 'hub' -or ($labAddrs -contains $Config.hub) -or (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\AgentArenaHub\app'))) {
            $hub = Get-LabHubState -Config $Config
            Add-LabFact arena 'admin hub' ('account {0}; program {1}; data {2}{3}; listens on {4}: {5} {6} {7}; firewall rule {8}' -f $hub.Account, $hub.ProgramInstalled, $hub.DatabasePresent, $(if ($hub.MovedMarker) { ' (marked moved)' } else { '' }), $hub.ListenEndpoint, $hub.LocalHealth.Verdict, $hub.LocalHealth.ServiceId, $hub.LocalHealth.Version, $hub.FirewallRule)
        }
        $py = Get-LabPython312
        Add-LabFact arena 'Python 3.12+ for the Arena installers' $(if ($py.Found) { '{0} at {1} ({2}, for {3}; elevated {4})' -f $py.Version, $py.Executable, $py.Command, $py.Account, $py.Elevated } else { $py.Reason }) -Status $(if ($py.Found) { 'observed' } else { 'missing' })
        [pscustomobject]@{ Health = $h; Viewer = $v; Ticketing = $t; Hub = $hub; Python = $py } }

    $d.gateway = Invoke-LabCollect gateway {
        $g = Get-LabGatewayState -Config $Config
        if ($g.Observation.Present) { Add-LabFact gateway 'observation file' ('{0}; {1} s old; contract {2}' -f $Config.gateway.observation_file, $g.Observation.AgeSeconds, $g.Observation.Contract) }
        elseif ($Role -eq 'hub' -or $labAddrs -contains $Config.hub) { Add-LabFact gateway 'observation file' 'not published on this machine' -Status missing }
        # Gateway 0.3.0 publishes from inside its InferenceGateway service: a publisher task is only an
        # alternative supervisor, absent by design, so its absence is not a missing component.
        if ($Role -eq 'hub' -or $labAddrs -contains $Config.hub) {
            if ($g.PublisherTask.Present) { Add-LabFact gateway 'publisher task (alternative supervisor)' $g.PublisherTask.State }
            $zip = Find-LabGatewayPackage -Folder (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'packages\gateway')
            Add-LabFact gateway 'release on this USB' $(if ($zip) { '{0} (installed by Administration hub > 2; service {1})' -f $zip.Name, $g.Port } else { 'no inference-gateway-*.zip in packages\gateway' }) -Status $(if ($zip) { 'observed' } else { 'missing' })
        }
        $g }

    $isHub = ($Role -eq 'hub' -or $labAddrs -contains $Config.hub -or ($d.arena -and $d.arena.Hub -and $d.arena.Hub.ProgramInstalled))
    $d.outbound = $null; $d.reach = $null; $d.hubgw = $null
    if ($isHub) {
        # B. Can this host - the Gateway's host - open TCP 11434 to the heads? Policy first, then real connections.
        $alias = ''; if ($d.network -and @($d.network.Lab).Count) { $alias = $d.network.Lab[0].Adapter }
        $d.outbound = Invoke-LabCollect outbound {
            $o = Get-LabOutboundVerdict -Snapshot (Get-LabOutboundSnapshotLive) -Heads @($Config.known_heads) -Port ([int]$Config.ollama.port) -InterfaceAlias $alias -Programs @([string]$Config.runtime.python.path) -ServiceName ([string]$Config.gateway.service_name)
            Add-LabFact outbound 'default outbound action' ('{0} profile: {1}' -f $o.ActiveProfile, $(if ($o.DefaultOutbound) { $o.DefaultOutbound } else { 'not set (allow)' }))
            foreach ($h in @($o.Heads)) { Add-LabFact outbound ('TCP 11434 to ' + $h.Head) ('{0}: {1}' -f $h.Verdict, $h.Reason) -Status inferred -Source 'rule analysis' }
            $o }
        $d.reach = Invoke-LabCollect reach {
            $r = @(Get-LabHeadProbe -Heads @($Config.known_heads) -Port ([int]$Config.ollama.port))
            foreach ($p in $r) { Add-LabFact reach ('Ollama on ' + $p.Head) $(if ($p.Version) { 'answers, version {0}, {1} model(s)' -f $p.Version, @($p.Models).Count } elseif ($p.Open) { 'port open, no Ollama answer' } else { 'no connection from this host' }) -Status $(if ($p.Version) { 'observed' } else { 'missing' }) -Source 'real connection' }
            $r }
        # C and E. Arena's identity, its access to the Gateway folders, and the observation states.
        $d.hubgw = Invoke-LabCollect gateway {
            $h = Get-LabHubGatewayReadiness -Config $Config
            Add-LabFact identity 'Arena hub installed for' $(if (@($h.Identity.Installed).Count) { @($h.Identity.Installed | ForEach-Object { '{0}{1}' -f $_.Account, $(if ($_.Moved) { ' (data marked moved: rollback copy, does not start)' } else { '' }) }) } else { 'nobody' }) -Status $(if (@($h.Identity.Installed).Count) { 'observed' } else { 'missing' })
            # A venv's python.exe is a launcher whose child is the base Python: one service, two processes.
            Add-LabFact identity 'Arena running as' $(if (@($h.Identity.Running).Count) { @($h.Identity.Running | ForEach-Object { '{0}: {1}' -f $_.What, $_.Account } | Select-Object -Unique) } else { 'not running' }) -Status $(if (@($h.Identity.Running).Count) { 'observed' } else { 'missing' })
            Add-LabFact identity 'Administrators (direct members)' $(if ($h.Identity.AdministratorsStatus -eq 'observed') { @($h.Identity.AdministratorsDirect | ForEach-Object { Resolve-LabAccount $_ }) } else { 'could not be read' }) -Status $h.Identity.AdministratorsStatus
            Add-LabFact gateway 'Gateway account prerequisites' ('{0}{1}' -f $h.Accounts.State, $(if (@($h.Accounts.Blockers + $h.Accounts.Unknown).Count) { ': ' + ((@($h.Accounts.Blockers) + @($h.Accounts.Unknown)) -join '; ') } else { '' })) -Status inferred -Source 'rules of the Gateway''s Set-ObservationPublicationAcl.ps1'
            foreach ($k in 'ledger', 'state', 'logs', 'publication') { $f = $h.Access.Folders.$k; Add-LabFact gateway ("$k folder") $(if ($null -eq $f.AllowSids) { 'not present: ' + $f.Path } elseif ($f.AllowSids -is [string]) { 'ACL unreadable: ' + $f.Path } else { '{0}: allow {1}' -f $f.Path, ((@($f.AllowSids) | ForEach-Object { Resolve-LabAccount $_ }) -join ', ') }) -Status $(if ($null -eq $f.AllowSids) { 'missing' } elseif ($f.AllowSids -is [string]) { 'inaccessible' } else { 'observed' }) }
            $o = $h.Observation
            Add-LabFact gateway 'observation' ('{0} (configured {1}, file {2}, access {3}, fresh {4}, service {5})' -f $o.summary, $o.configured, $o.file_present, $o.access, $o.fresh, $o.service_running) -Status inferred
            $svc = $h.Access.Service
            Add-LabFact gateway 'InferenceGateway service' $(if ($svc.Present) { '{0}, {1}, as {2}' -f $svc.State, $svc.StartMode, $svc.Account } else { 'not installed' }) -Status $(if ($svc.Present) { 'observed' } else { 'missing' })
            if ($h.Access.AlternativeTask.Present) { Add-LabFact gateway 'publisher task (alternative supervisor)' $h.Access.AlternativeTask.State -Source 'not part of the 0.3.0 default deployment' }
            $acct = Get-LabLocalAccount -Name ([string]$Config.gateway.service_account)
            if ($acct.Exists) {
                $m = @(Get-LabPrivilegedMembership -Sids @($acct.Sid))[0]
                Add-LabFact gateway ('account ' + $Config.gateway.service_account) ('{0}; Users {1}; {2}; rights {3}' -f $acct.Sid, $acct.InUsers, $m.State, ((Get-LabUserRights -Sid $acct.Sid) -join ', '))
            } else { Add-LabFact gateway ('account ' + $Config.gateway.service_account) 'does not exist yet' -Status missing }
            foreach ($mm in @($h.Membership)) { Add-LabFact identity ('Arena account ' + (Resolve-LabAccount $mm.Sid)) ('{0}{1}' -f $mm.State, $(if (@($mm.Groups).Count) { ': ' + ($mm.Groups -join ', ') + ' (transitive)' } else { '' })) -Status inferred }
            $rt = Get-LabRuntimeState -Config $Config
            Add-LabFact gateway 'machine-wide Python' $(if ($rt.Python.Version) { '{0} at {1} (base interpreter {2})' -f $rt.Python.Version, $rt.Python.Path, $rt.Python.Base } else { 'missing: ' + $rt.Python.Path }) -Status $(if ($rt.Python.Version) { 'observed' } else { 'missing' })
            Add-LabFact gateway 'machine-wide PowerShell 7' $(if ($rt.Pwsh.Version) { '{0} at {1}' -f $rt.Pwsh.Version, $rt.Pwsh.Path } else { 'missing: ' + $rt.Pwsh.Path }) -Status $(if ($rt.Pwsh.Version) { 'observed' } else { 'missing' })
            $h }
    }

    $d.capstone = Invoke-LabCollect capstone {
        $c = Get-LabCapstoneState -Config $Config
        if ($c.Installed -or $Role -eq 'capstone') {
            Add-LabFact capstone 'installed' $(if ($c.Installed) { 'version ' + $c.Version } else { 'no ' + $Config.capstone.install_root + '\current.json' }) -Status $(if ($c.Installed) { 'observed' } else { 'missing' })
            Add-LabFact capstone 'inference mode' $(if ($c.InferenceMode) { '{0} ({1})' -f $c.InferenceMode, $c.ModeSource } else { 'no appsettings.json' }) -Status $(if ($c.InferenceMode) { 'observed' } else { 'missing' })
            foreach ($g in @(@('Gateway client bootstrap (capstone)', $c.Gateway), @('Gateway client bootstrap (capstone-research)', $c.ResearchGateway))) {
                if ($g[1]) { $b = $g[1]; Add-LabFact capstone $g[0] $(if ($b.Present) { 'url {0}; application {1}; CA {2}; token file {3} ({4}){5}' -f $b.Url, $b.ApplicationId, $b.CaFile, $b.TokenFile, $b.TokenPath, $(if ($b.NamesTokenEnvironment) { '; names a token environment variable (retired)' } else { '' }) } else { 'missing at ' + $b.Path }) }
            }
            if ($c.ArenaRegistration) { Add-LabFact capstone 'Arena adapter registration (Capstone''s record)' $c.ArenaRegistration }
            if ($c.Task) { Add-LabFact capstone 'startup task' $(if ($c.Task.Present) { '{0} as {1}' -f $c.Task.State, $c.Task.User } else { 'not registered' }) -Status $(if ($c.Task.Present) { 'observed' } else { 'missing' }) }
            if ($c.Health) { Add-LabFact capstone 'health' $(if ($c.Health.Answered) { 'ok={0} version {1}' -f $c.Health.Ok, $c.Health.Version } else { 'no answer' }) -Status $(if ($c.Health.Answered) { 'observed' } else { 'inaccessible' }) }
        }
        $c }
    if ($Role -eq 'coder') { Add-LabFact coder 'Coder deployment' 'no Coder Windows package, Gateway client or Arena publisher exists (Coder is at PHASE-00)' -Status missing -Source 'Coder project handoffs\current-state.md' }

    $d.history = Invoke-LabCollect history {
        $old = 'C:\ProgramData\CapstoneProvisioner'
        $h = [ordered]@{ Provisioner103 = (Test-Path -LiteralPath $old); NetworkBackups = @(); LastStatus = ''; LabRuns = @() }
        if ($h.Provisioner103) {
            $h.NetworkBackups = @(Get-ChildItem -LiteralPath (Join-Path $old 'backups') -Filter 'network-backup-*.json' -ErrorAction SilentlyContinue | ForEach-Object Name)
            $st = Read-LabJson (Join-Path $old 'reports\LATEST-STATUS.json'); if ($st) { $h.LastStatus = [string]$st.outcome }
            Add-LabFact backups 'provisioner 1.0.3 records' ('{0}: {1} network backup(s); last outcome {2}' -f $old, $h.NetworkBackups.Count, $h.LastStatus)
        }
        $runs = Join-Path (Get-LabRoot) 'runs'
        $h.LabRuns = @(Get-ChildItem -LiteralPath $runs -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'changes.json') } | ForEach-Object Name)
        Add-LabFact backups 'lab provisioner runs with changes' $(if ($h.LabRuns.Count) { $h.LabRuns } else { 'none' }) -Status $(if ($h.LabRuns.Count) { 'observed' } else { 'missing' })
        $rb = Read-LabJson (Join-Path (Get-LabRoot) 'state\reboot-check.json'); if ($rb) { Add-LabFact backups 'reboot checkpoint' ('saved {0} by run {1}' -f $rb.saved_at, $rb.run) }
        [pscustomobject]$h }

    $flags = @{ OllamaInstalled = [bool]($d.ollama -and $d.ollama.Executable); ModelsPresent = [bool]($d.ollama -and $d.ollama.Models -and @($d.ollama.Models.Models).Count)
                HubInstalled = [bool]($d.arena -and $d.arena.Hub -and $d.arena.Hub.ProgramInstalled); CapstoneInstalled = [bool]($d.capstone -and $d.capstone.Installed) }
    $d.detected = Get-LabDetectedRole -LabAddresses $labAddrs @flags
    Add-LabFact identity 'appears to be' ('{0} ({1})' -f $d.detected.Role, $d.detected.Reason) -Status inferred
    $d.facts = @($script:Facts)
    return [pscustomobject]$d
}

function Show-LabInspection {
    param([Parameter(Mandatory)]$Inspection)
    $area = ''
    foreach ($f in @($Inspection.facts)) {
        if ($f.area -ne $area) { $area = $f.area; Write-Host ('  [{0}]' -f $area) -ForegroundColor Cyan }
        $colour = @{ observed = 'Gray'; inferred = 'White'; missing = 'DarkYellow'; inaccessible = 'Yellow' }[$f.status]
        Write-Host ('    {0,-12} {1}: {2}' -f $f.status, $f.name, $f.value) -ForegroundColor $colour
    }
}

Export-ModuleMember -Function @('Add-LabFact', 'Invoke-LabCollect', 'Get-LabDetectedRole', 'Invoke-LabInspection', 'Show-LabInspection')
