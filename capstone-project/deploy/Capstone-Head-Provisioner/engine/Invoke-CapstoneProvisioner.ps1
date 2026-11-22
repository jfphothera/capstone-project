<#
    Invoke-CapstoneProvisioner
    The single deployment engine for every Capstone head.

    Every launcher in this package calls THIS script. There is no second
    implementation for a particular node, and no separate utility for
    networking, installation, downloads or remote serving: they are stages of
    one resumable run (defect D-13).

    Stage order is deliberate and is the main correction to the earlier
    versions:

        preflight -> network -> install Ollama -> healthy LOCAL api
                  -> download models -> verify models
                  -> configure remote serving -> verify remote serving

    Remote serving is last. A remote-serving problem can never invalidate
    models that were downloaded and verified successfully (defect D-06).
#>
[CmdletBinding()]
param(
    [ValidateRange(0, 254)]
    [int]$NodeNumber = 0,

    # Deliberately NOT named -Profile: $Profile is an automatic PowerShell
    # variable, and binding a parameter over it is the root of defect D-10.
    [ValidateSet('', 'CPU8', 'CPU16', 'CPU32', 'GPU', 'EMBEDDINGS')]
    [string]$ModelProfile = '',

    [ValidateSet('Provision', 'VerifyOnly', 'DiagnosticsOnly', 'Rollback', 'GpuInspect')]
    [string]$Mode = 'Provision',

    [ValidateSet('PrivateLan', 'PrivateOnly')]
    [string]$BindMode = 'PrivateLan',

    [string]$ConfigFile = '',
    [string]$DataRoot = '',
    [string]$BackupFile = '',

    [switch]$SkipNetwork,
    [switch]$SkipModels,
    [switch]$SkipRemote,
    [switch]$SkipInference,
    [switch]$NonInteractive,
    [switch]$AllowUndersizedProfile
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:Version = '1.0.3'
$script:CoordinatorIp = '192.0.2.1'
$script:GpuWorkstationIp = '192.0.2.2'

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$moduleRoot = Join-Path $engineRoot 'modules'
foreach ($moduleName in @('Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Remote', 'Capstone.Preflight', 'Capstone.Report')) {
    $modulePath = Join-Path $moduleRoot ('{0}.psm1' -f $moduleName)
    if (-not (Test-Path -LiteralPath $modulePath)) {
        Write-Host ('FATAL: required module {0} is missing from {1}.' -f $moduleName, $moduleRoot) -ForegroundColor Red
        exit 3
    }
    Import-Module -Name $modulePath -Force -DisableNameChecking -Global
}

$paths = Initialize-CapstonePaths -Paths (Get-CapstonePaths -Root $DataRoot)
$runStamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
Set-CapstoneLogFile -Path (Join-Path $paths.Logs ('run-{0}.log' -f $runStamp))

# MUST run before the first HTTP request of the process. Without it, WPAD
# proxy discovery adds about 15 seconds to that first request under Windows
# PowerShell 5.1, which is longer than every health-probe budget here and
# would make a healthy Ollama look dead. See Initialize-CapstoneHttp.
$httpSetup = Initialize-CapstoneHttp

# ---------------------------------------------------------------------------
# Helpers local to the engine
# ---------------------------------------------------------------------------

function Get-ProposedProfileName {
    param($Hardware)
    if ($Hardware.NvidiaVramMB -ge 8000) { return 'GPU' }
    $ram = [double]$Hardware.TotalMemoryGB
    if ($ram -lt 11) { return 'CPU8' }
    if ($ram -lt 24) { return 'CPU16' }
    return 'CPU32'
}

function Get-ExistingPrivateAddress {
    $matches = @(Get-CapstoneAdapterInventory | ForEach-Object {
        $adapter = $_
        @($adapter.IPv4Addresses | Where-Object { $_.IPAddress -match '^192\.0\.2\.\d{1,3}$' -and $_.PrefixLength -eq 24 }) |
            ForEach-Object { [pscustomobject]@{ IPAddress = $_.IPAddress; Adapter = $adapter } }
    })
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Write-StageHeader {
    param([string]$Text)
    Write-CapstoneLog '' -Level INFO
    Write-CapstoneLog ('--- {0} ---' -f $Text) -Level STEP
}

# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------

$run = [ordered]@{
    Version = $script:Version
    Mode = $Mode
    ComputerName = $env:COMPUTERNAME
    StartedAt = (Get-Date).ToString('o')
    LogFile = (Get-CapstoneLogFile)
    PreflightPath = ''
    BackupPath = '(none created)'
    RegistrationPath = ''

    OsCaption = ''; OsBuild = ''; Cpu = ''; RamGB = 0.0; Gpu = ''; NvidiaVramMB = 0
    ModelDirectory = ''; FreeDisk = ''

    NodeNumber = $NodeNumber; NodeIp = ''
    AdapterSummary = '(not evaluated)'
    NetworkVerified = $false; NetworkSummary = '(not evaluated)'
    CoordinatorNote = '(not evaluated)'

    OllamaInstalled = $false; OllamaVersion = ''; OllamaExecutable = ''
    LocalApiHealthy = $false; LocalApiEndpoint = ''; OllamaInstalledThisRun = $false

    ProfileName = $ModelProfile
    ModelsRequested = 0; ModelsInstalled = 0; ModelsAlreadyPresent = 0
    ModelsDownloaded = 0; ModelsFailed = 0; ModelsVerified = 0
    FailedTags = @(); InferenceVerified = $false

    RemoteConfigured = $false; RemoteBinding = '(not configured)'
    PrivateListenerHealthy = $false; FirewallSummary = '(not configured)'
    CoordinatorVerified = $false; RemoteStatus = 'REMOTE_NOT_CONFIGURED'
    RemoteEndpoint = ''

    MachineState = 'NOT_INSTALLED'
}

$pendingActions = @()
$queueItems = @()
$exitCode = 1
$lockAcquired = $false

try {
    Write-CapstoneBanner ('CAPSTONE HEAD PROVISIONER {0}   mode: {1}' -f $script:Version, $Mode)
    Write-CapstoneLog ('Run log: {0}' -f (Get-CapstoneLogFile)) -Level INFO
    Write-CapstoneLog ('HTTP: {0}' -f $httpSetup.Note) -Level DEBUG
    Write-CapstoneLog 'Once this run starts, no further input is required. Leave the machine running.' -Level INFO

    # -----------------------------------------------------------------------
    # Lock
    # -----------------------------------------------------------------------
    if ($Mode -ne 'DiagnosticsOnly') {
        $lock = Enter-CapstoneLock -Paths $paths
        if (-not $lock.Acquired) {
            Write-CapstoneLog $lock.Reason -Level ERROR
            exit 4
        }
        $lockAcquired = $true
    }

    # -----------------------------------------------------------------------
    # Configuration: a selection made once must survive every later run
    # -----------------------------------------------------------------------
    $configPath = $ConfigFile
    if ([string]::IsNullOrWhiteSpace($configPath)) { $configPath = $paths.ConfigFile }
    $config = Read-CapstoneJson -Path $configPath
    if ($null -eq $config) {
        $packaged = Join-Path (Split-Path -Parent $engineRoot) 'capstone.config.json'
        if (Test-Path -LiteralPath $packaged) { $config = Read-CapstoneJson -Path $packaged }
    }

    $preferMac = ''
    $preferName = ''
    if ($null -ne $config) {
        if ($NodeNumber -eq 0 -and $config.PSObject.Properties.Name -contains 'node_number' -and $config.node_number) {
            $NodeNumber = [int]$config.node_number
        }
        if ([string]::IsNullOrWhiteSpace($ModelProfile) -and $config.PSObject.Properties.Name -contains 'model_profile' -and $config.model_profile) {
            $ModelProfile = [string]$config.model_profile
        }
        if ($config.PSObject.Properties.Name -contains 'ethernet_mac') { $preferMac = [string]$config.ethernet_mac }
        if ($config.PSObject.Properties.Name -contains 'ethernet_name') { $preferName = [string]$config.ethernet_name }
        if ($config.PSObject.Properties.Name -contains 'bind_mode' -and $config.bind_mode) { $BindMode = [string]$config.bind_mode }
    }

    $savedState = Get-CapstoneState -Paths $paths
    if ($NodeNumber -eq 0 -and $savedState.node_number) { $NodeNumber = [int]$savedState.node_number }
    if ([string]::IsNullOrWhiteSpace($ModelProfile) -and $savedState.model_profile) { $ModelProfile = [string]$savedState.model_profile }

    # -----------------------------------------------------------------------
    # Preflight (always, and always before any change)
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 1 of 7: read-only machine discovery'

    $existingPrivate = Get-ExistingPrivateAddress
    if ($NodeNumber -eq 0 -and $null -ne $existingPrivate) {
        $NodeNumber = [int]($existingPrivate.IPAddress.Split('.')[3])
        Write-CapstoneLog ('This machine already holds {0}; treating it as node {1}.' -f $existingPrivate.IPAddress, $NodeNumber) -Level INFO
    }

    $provisionalIp = ''
    if ($NodeNumber -gt 0) { $provisionalIp = ('192.0.2.{0}' -f $NodeNumber) }

    $preflight = New-CapstonePreflightReport -Paths $paths -PrivateIp $provisionalIp -NodeNumber $NodeNumber -ProfileName $ModelProfile
    $run.PreflightPath = $preflight.Path
    $facts = $preflight.Report

    $run.OsCaption = $facts.operating_system.Caption
    $run.OsBuild = $facts.operating_system.BuildNumber
    $run.Cpu = $facts.hardware.CpuName
    $run.RamGB = $facts.hardware.TotalMemoryGB
    $run.NvidiaVramMB = $facts.hardware.NvidiaVramMB
    $run.Gpu = (@($facts.hardware.Gpus | ForEach-Object { $_.Name }) -join '; ')
    if ([string]::IsNullOrWhiteSpace($run.Gpu)) { $run.Gpu = 'none detected' }
    $run.ModelDirectory = $facts.ollama.ModelDirectory.Path
    $run.FreeDisk = $facts.ollama.Disk.FreeDisplay
    $run.OllamaInstalled = [bool]$facts.ollama.Installed
    $run.OllamaExecutable = [string]$facts.ollama.Executable
    $run.OllamaVersion = [string]$facts.ollama.Version
    $run.LocalApiHealthy = [bool]$facts.ollama.LocalApi.Healthy
    $run.CoordinatorNote = $facts.coordinator.Note

    Write-CapstoneLog ('Machine      : {0}, {1} GB RAM, {2}' -f $run.Cpu, $run.RamGB, $run.Gpu) -Level INFO
    Write-CapstoneLog ('Windows      : {0} build {1}, PowerShell {2}' -f $run.OsCaption, $run.OsBuild, $facts.operating_system.PowerShellVersion) -Level INFO
    Write-CapstoneLog ('Elevated     : {0}' -f $facts.operating_system.Elevated) -Level INFO
    Write-CapstoneLog ('Ollama       : {0}' -f $(if ($run.OllamaInstalled) { 'installed, ' + $run.OllamaVersion } else { 'not installed' })) -Level INFO
    Write-CapstoneLog ('Models now   : {0}' -f $facts.ollama.ModelCount) -Level INFO
    Write-CapstoneLog ('Model storage: {0} ({1} free)' -f $run.ModelDirectory, $run.FreeDisk) -Level INFO
    Write-CapstoneLog ('Internet     : {0}' -f $facts.internet.Note) -Level INFO

    if (-not $facts.operating_system.Supported) {
        Write-CapstoneLog ('UNSUPPORTED_WINDOWS_VERSION: {0}' -f $facts.operating_system.SupportNote) -Level ERROR
        $pendingActions += ('Unsupported Windows version: {0}' -f $facts.operating_system.SupportNote)
        throw 'This Windows version is not supported by the provisioner.'
    }

    if ($Mode -eq 'DiagnosticsOnly') {
        Write-CapstoneLog '' -Level INFO
        Write-CapstoneLog 'Diagnostics-only run. Nothing was changed.' -Level OK
        Write-CapstoneLog ('Full inventory: {0}' -f $preflight.Path) -Level OK
        $exitCode = 0
        # `return` at script scope would skip the `exit $exitCode` at the end of
        # this file and the process would report 0 regardless of the real
        # result. `exit` still runs the finally block below.
        exit $exitCode
    }

    # -----------------------------------------------------------------------
    # Protected machines
    # -----------------------------------------------------------------------
    $heldAddresses = @()
    foreach ($adapter in @($facts.adapters)) {
        foreach ($address in @($adapter.IPv4Addresses)) { $heldAddresses += [string]$address.IPAddress }
    }

    if ($heldAddresses -contains $script:CoordinatorIp) {
        Write-CapstoneLog 'This machine holds 192.0.2.1 and is the Capstone coordinator. Its network will not be reconfigured.' -Level WARN
        $SkipNetwork = $true
        if ($Mode -eq 'Provision' -and $NodeNumber -eq 1) {
            throw 'The coordinator is not provisioned as a head by this tool.'
        }
    }

    $isGpuWorkstation = ($heldAddresses -contains $script:GpuWorkstationIp)
    if ($isGpuWorkstation) {
        Write-CapstoneLog 'This machine holds 192.0.2.2 and is the existing GPU inference workstation.' -Level WARN
        Write-CapstoneLog 'Its network configuration, Ollama installation and model storage will not be altered. Model additions are additive only.' -Level WARN
        $SkipNetwork = $true
        if ($Mode -eq 'Provision' -and $ModelProfile -ne 'GPU' -and $ModelProfile -ne 'EMBEDDINGS' -and -not [string]::IsNullOrWhiteSpace($ModelProfile)) {
            throw ('Refusing to apply the CPU profile "{0}" to the GPU workstation. Use the GPU profile, or VerifyOnly mode.' -f $ModelProfile)
        }
        if ([string]::IsNullOrWhiteSpace($ModelProfile)) { $ModelProfile = 'GPU' }
    }

    # -----------------------------------------------------------------------
    # Rollback mode
    # -----------------------------------------------------------------------
    if ($Mode -eq 'Rollback') {
        Write-StageHeader 'Network rollback'
        $backupPath = $BackupFile
        if ([string]::IsNullOrWhiteSpace($backupPath)) {
            $backupPath = Join-Path $paths.Backups 'network-backup-LATEST.json'
        }
        $backup = Read-CapstoneJson -Path $backupPath
        if ($null -eq $backup) {
            Write-CapstoneLog ('No network backup record was found at {0}. Nothing was changed.' -f $backupPath) -Level ERROR
            $exitCode = 5
            exit $exitCode
        }

        $plan = Resolve-CapstoneRollbackPlan -Backup $backup
        Write-CapstoneLog ('Backup from {0} for {1} (index {2}).' -f $backup.captured_at, $backup.interface_alias, $backup.interface_index) -Level INFO
        Write-CapstoneLog ('Plan: {0} - {1}' -f $plan.Action, $plan.Reason) -Level INFO

        $failures = @()
        if ($plan.Action -eq 'RestoreDhcp') {
            $r1 = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'set', 'address', ('name={0}' -f $plan.InterfaceIndex), 'source=dhcp')
            if ($r1.ExitCode -ne 0) { $failures += ('set address source=dhcp: {0}' -f $r1.Output) }
            $r2 = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'set', 'dnsservers', ('name={0}' -f $plan.InterfaceIndex), 'source=dhcp')
            if ($r2.ExitCode -ne 0) { $failures += ('set dnsservers source=dhcp: {0}' -f $r2.Output) }
        } elseif ($plan.Action -eq 'RestoreStatic') {
            $first = @($plan.RestoreAddresses)[0]
            $maskBits = [int]$first.prefix
            $maskValue = ([ipaddress](([uint32]::MaxValue -shl (32 - $maskBits)) -band [uint32]::MaxValue)).ToString()
            # The shift above yields host byte order; rebuild the dotted mask directly instead.
            $maskOctets = @()
            $remaining = $maskBits
            for ($i = 0; $i -lt 4; $i++) {
                $bits = [math]::Min(8, [math]::Max(0, $remaining))
                $maskOctets += (256 - [math]::Pow(2, 8 - $bits)) % 256
                $remaining -= $bits
            }
            $maskValue = ($maskOctets -join '.')

            $arguments = @('interface', 'ipv4', 'set', 'address', ('name={0}' -f $plan.InterfaceIndex), 'source=static', ('address={0}' -f $first.ip), ('mask={0}' -f $maskValue))
            if (-not [string]::IsNullOrWhiteSpace($plan.RestoreGateway)) { $arguments += ('gateway={0}' -f $plan.RestoreGateway) } else { $arguments += 'gateway=none' }
            $r1 = Invoke-CapstoneNetsh -Arguments $arguments
            if ($r1.ExitCode -ne 0) { $failures += ('restore static address: {0}' -f $r1.Output) }

            if (@($plan.RestoreDns).Count -gt 0) {
                $index = 1
                foreach ($server in @($plan.RestoreDns)) {
                    $r = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'add', 'dnsservers', ('name={0}' -f $plan.InterfaceIndex), ('address={0}' -f $server), ('index={0}' -f $index), 'validate=no')
                    if ($r.ExitCode -ne 0) { $failures += ('restore DNS {0}: {1}' -f $server, $r.Output) }
                    $index++
                }
            }
        }

        Start-Sleep -Seconds 3
        $after = Get-CapstoneNetworkFacts -InterfaceIndex $plan.InterfaceIndex -TargetIp ([string]$backup.target_ip)
        Write-CapstoneLog '' -Level INFO
        Write-CapstoneLog ('Adapter now: addresses={0} dhcp={1} gateways={2} dns={3}' -f
            ((@($after.Addresses | ForEach-Object { '{0}/{1}' -f $_.IPAddress, $_.PrefixLength }) -join ', ')),
            $after.Dhcp,
            ((@($after.DefaultGateways) -join ', ')),
            ((@($after.DnsServers) -join ', '))) -Level INFO

        if ($failures.Count -eq 0) {
            Write-CapstoneLog 'Rollback completed. Compare the values above with the backup record.' -Level OK
            $exitCode = 0
        } else {
            Write-CapstoneLog 'Rollback was INCOMPLETE. These settings could not be restored:' -Level ERROR
            foreach ($failure in $failures) { Write-CapstoneLog ('  - {0}' -f $failure) -Level ERROR }
            $exitCode = 6
        }
        exit $exitCode
    }

    # -----------------------------------------------------------------------
    # Node and profile selection: at most one prompt, then remembered forever
    # -----------------------------------------------------------------------
    $proposedProfile = Get-ProposedProfileName -Hardware $facts.hardware

    if ($NodeNumber -eq 0 -or [string]::IsNullOrWhiteSpace($ModelProfile)) {
        if ($NonInteractive) {
            $missing = @()
            if ($NodeNumber -eq 0) { $missing += 'node number' }
            if ([string]::IsNullOrWhiteSpace($ModelProfile)) { $missing += 'model profile' }
            throw ('Running non-interactively but the {0} is not set. Provide it on the command line or in {1}.' -f ($missing -join ' and '), $configPath)
        }

        Write-CapstoneLog '' -Level INFO
        Write-CapstoneBanner 'ONE-TIME SETUP SELECTION'
        Write-CapstoneLog ('  Machine            : {0}' -f $env:COMPUTERNAME) -Level INFO
        Write-CapstoneLog ('  CPU                : {0} ({1} cores)' -f $facts.hardware.CpuName, $facts.hardware.PhysicalCores) -Level INFO
        Write-CapstoneLog ('  Installed RAM      : {0} GB' -f $facts.hardware.TotalMemoryGB) -Level INFO
        Write-CapstoneLog ('  GPU                : {0}' -f $run.Gpu) -Level INFO
        Write-CapstoneLog ('  Free disk          : {0} on {1}' -f $run.FreeDisk, $facts.ollama.Disk.Volume) -Level INFO
        Write-CapstoneLog ('  Ollama installed   : {0}' -f $run.OllamaInstalled) -Level INFO
        Write-CapstoneLog ('  Models present     : {0}' -f $facts.ollama.ModelCount) -Level INFO
        Write-CapstoneLog ('  Internet available : {0}' -f $facts.internet.Available) -Level INFO
        Write-CapstoneLog ('  Suggested profile  : {0}' -f $proposedProfile) -Level INFO
        Write-CapstoneLog '' -Level INFO
        Write-CapstoneLog 'This is the only question. The answers are saved and reused on every later run.' -Level INFO
        Write-CapstoneLog '' -Level INFO

        if ($NodeNumber -eq 0) {
            while ($true) {
                $answer = Read-Host '  Node number for this computer (3 to 15, giving 192.0.2.N)'
                if ($answer -match '^\s*(\d{1,2})\s*$' -and [int]$Matches[1] -ge 3 -and [int]$Matches[1] -le 15) {
                    $NodeNumber = [int]$Matches[1]; break
                }
                Write-CapstoneLog '  Enter a number from 3 to 15. (1 is the coordinator, 2 is the GPU workstation.)' -Level WARN
            }
        }
        if ([string]::IsNullOrWhiteSpace($ModelProfile)) {
            $answer = Read-Host ('  Model profile [CPU8 / CPU16 / CPU32 / GPU / EMBEDDINGS], or Enter for {0}' -f $proposedProfile)
            if ([string]::IsNullOrWhiteSpace($answer)) {
                $ModelProfile = $proposedProfile
            } else {
                $normalised = $answer.Trim().ToUpperInvariant()
                if (@('CPU8', 'CPU16', 'CPU32', 'GPU', 'EMBEDDINGS') -contains $normalised) {
                    $ModelProfile = $normalised
                } else {
                    Write-CapstoneLog ('  "{0}" is not a known profile; using {1}.' -f $answer, $proposedProfile) -Level WARN
                    $ModelProfile = $proposedProfile
                }
            }
        }
    }

    $run.NodeNumber = $NodeNumber
    $run.ProfileName = $ModelProfile
    $run.NodeIp = ('192.0.2.{0}' -f $NodeNumber)
    $privateIp = $run.NodeIp

    Write-CapstoneLog '' -Level INFO
    Write-CapstoneLog ('Provisioning node {0} as {1} with the {2} model profile.' -f $NodeNumber, $privateIp, $ModelProfile) -Level OK

    Write-CapstoneJson -InputObject ([pscustomobject][ordered]@{
        schema_version = 1
        node_number    = $NodeNumber
        model_profile  = $ModelProfile
        bind_mode      = $BindMode
        ethernet_mac   = $preferMac
        ethernet_name  = $preferName
        saved_at       = (Get-Date).ToString('o')
    }) -Path $paths.ConfigFile

    $savedState.node_number = $NodeNumber
    $savedState.model_profile = $ModelProfile
    $savedState.run_count = [int]$savedState.run_count + 1
    $savedState.last_run_at = (Get-Date).ToString('o')
    if ([string]::IsNullOrWhiteSpace([string]$savedState.first_run_at)) { $savedState.first_run_at = $savedState.last_run_at }
    Save-CapstoneState -Paths $paths -State $savedState | Out-Null
    Write-CapstoneJournal -Paths $paths -Stage 'Start' -Event 'run-begin' -Detail ([pscustomobject]@{ node = $NodeNumber; profileName = $ModelProfile; mode = $Mode })

    # -----------------------------------------------------------------------
    # Stage 2: private Ethernet
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 2 of 7: private Ethernet configuration'

    if ($SkipNetwork) {
        Write-CapstoneLog 'Network configuration skipped for this machine.' -Level INFO
        $existing = @($facts.adapters | Where-Object {
            @($_.IPv4Addresses | Where-Object { $_.IPAddress -eq $privateIp }).Count -gt 0
        })
        if ($existing.Count -gt 0) {
            $run.NetworkVerified = $true
            $run.AdapterSummary = ('{0} (index {1})' -f $existing[0].Name, $existing[0].InterfaceIndex)
            $run.NetworkSummary = 'Pre-existing configuration was left untouched.'
        } else {
            $run.NetworkSummary = 'Not evaluated; network configuration was skipped.'
        }
    } elseif ($Mode -eq 'VerifyOnly') {
        $decision = Select-CapstoneEthernetAdapter -Adapters @($facts.adapters) -TargetIp $privateIp -PreferMacAddress $preferMac -PreferName $preferName
        if ($decision.Status -eq 'AlreadyConfigured') {
            $verdict = Test-CapstoneNetworkState -InterfaceIndex $decision.Adapter.InterfaceIndex -TargetIp $privateIp
            $run.NetworkVerified = $verdict.Pass
            $run.NetworkSummary = $verdict.Summary
            $run.AdapterSummary = ('{0} (index {1})' -f $decision.Adapter.Name, $decision.Adapter.InterfaceIndex)
        } else {
            $run.NetworkSummary = ('Address not present. {0}' -f $decision.Reason)
        }
    } else {
        $decision = Select-CapstoneEthernetAdapter -Adapters @($facts.adapters) -TargetIp $privateIp -PreferMacAddress $preferMac -PreferName $preferName
        Write-CapstoneLog ('Adapter decision: {0}' -f $decision.Status) -Level INFO
        Write-CapstoneLog ('  {0}' -f $decision.Reason) -Level INFO

        if ($decision.Status -eq 'AlreadyConfigured') {
            $run.AdapterSummary = ('{0} (index {1})' -f $decision.Adapter.Name, $decision.Adapter.InterfaceIndex)
            $verdict = Test-CapstoneNetworkState -InterfaceIndex $decision.Adapter.InterfaceIndex -TargetIp $privateIp
            $run.NetworkVerified = $verdict.Pass
            $run.NetworkSummary = $verdict.Summary
            if ($verdict.Pass) {
                Write-CapstoneLog 'The private-LAN address is already correct. Nothing was changed.' -Level OK
            } else {
                Write-CapstoneLog ('The address is present but these checks fail: {0}. Correcting only those settings.' -f ($verdict.Failed -join ', ')) -Level WARN
                $backup = New-CapstoneNetworkBackup -Paths $paths -Adapter $decision.Adapter -TargetIp $privateIp -NodeNumber $NodeNumber
                $run.BackupPath = $backup.Path
                $applied = Set-CapstonePrivateAddress -InterfaceIndex $decision.Adapter.InterfaceIndex -TargetIp $privateIp
                $run.NetworkVerified = $applied.Success
                $run.NetworkSummary = $applied.Verification.Summary
            }
        } elseif ($decision.Status -eq 'Selected') {
            $adapter = $decision.Adapter
            $run.AdapterSummary = ('{0} (index {1}, {2})' -f $adapter.Name, $adapter.InterfaceIndex, $adapter.Description)

            $conflict = Test-CapstoneAddressInUse -IPAddress $privateIp
            Write-CapstoneLog ('Address conflict check: {0} - {1}' -f $conflict.Verdict, $conflict.Reason) -Level INFO
            if ($conflict.Verdict -eq 'Conflict') {
                $pendingActions += ('DUPLICATE_IP_OR_NETWORK_CONFLICT: {0} is already in use. {1} Nothing was changed.' -f $privateIp, $conflict.Reason)
                Write-CapstoneLog ('Refusing to assign {0}. {1}' -f $privateIp, $conflict.Reason) -Level ERROR
                $run.NetworkSummary = ('Not applied: {0}' -f $conflict.Reason)
            } else {
                $backup = New-CapstoneNetworkBackup -Paths $paths -Adapter $adapter -TargetIp $privateIp -NodeNumber $NodeNumber
                $run.BackupPath = $backup.Path
                Write-CapstoneLog ('Current state saved for rollback: {0}' -f $backup.Path) -Level OK
                Write-CapstoneJournal -Paths $paths -Stage 'Network' -Event 'backup-written' -Detail ([pscustomobject]@{ path = $backup.Path })

                Write-CapstoneLog ('Applying {0}/24, no gateway, no IPv4 DNS, to index {1}.' -f $privateIp, $adapter.InterfaceIndex) -Level STEP
                $applied = Set-CapstonePrivateAddress -InterfaceIndex $adapter.InterfaceIndex -TargetIp $privateIp
                $run.NetworkVerified = $applied.Success
                $run.NetworkSummary = $applied.Verification.Summary

                foreach ($check in @($applied.Verification.Checks)) {
                    $level = 'OK'
                    if (-not $check.Pass) { $level = 'ERROR' }
                    Write-CapstoneLog ('  {0,-18} {1}  (expected {2}; observed {3})' -f $check.Name, $(if ($check.Pass) { 'pass' } else { 'FAIL' }), $check.Expected, $check.Actual) -Level $level
                }

                if ($applied.Success) {
                    Write-CapstoneLog ('Private-LAN address verified on {0}.' -f $adapter.Name) -Level OK
                    $record = Read-CapstoneJson -Path $backup.Path
                    if ($null -ne $record) {
                        $record.applied = $true
                        Write-CapstoneJson -InputObject $record -Path $backup.Path
                    }
                } else {
                    $pendingActions += ('NETWORK_CONFIGURATION_FAILURE: the address did not verify ({0}). Use RECOVER-NETWORK-ROLLBACK.cmd with {1} to restore the previous settings.' -f $applied.Verification.Summary, $backup.Path)
                }

                # Wi-Fi and every other adapter must be exactly as they were.
                $afterAdapters = Get-CapstoneAdapterInventory
                foreach ($before in @($facts.adapters)) {
                    if ($before.InterfaceIndex -eq $adapter.InterfaceIndex) { continue }
                    $after = @($afterAdapters | Where-Object { $_.InterfaceIndex -eq $before.InterfaceIndex })
                    if ($after.Count -ne 1) { continue }
                    $beforeList = (@($before.IPv4Addresses | ForEach-Object { '{0}/{1}' -f $_.IPAddress, $_.PrefixLength }) | Sort-Object) -join ','
                    $afterList = (@($after[0].IPv4Addresses | ForEach-Object { '{0}/{1}' -f $_.IPAddress, $_.PrefixLength }) | Sort-Object) -join ','
                    if ($beforeList -ne $afterList) {
                        $message = ('Adapter "{0}" changed during provisioning (before: {1}; after: {2}). This was not intended.' -f $before.Name, $beforeList, $afterList)
                        Write-CapstoneLog $message -Level ERROR
                        $pendingActions += $message
                    }
                }
                Write-CapstoneLog 'Confirmed that no other adapter changed.' -Level OK
            }
        } else {
            $category = 'AMBIGUOUS_NETWORK_ADAPTER'
            if ($decision.Status -eq 'NoCandidate') { $category = 'NO_ETHERNET_ADAPTER' }
            if ($decision.Status -eq 'SoleInternetPath') { $category = 'ETHERNET_IS_SOLE_INTERNET_PATH' }
            Write-CapstoneLog ('{0}: stopping before any network change.' -f $category) -Level ERROR
            $pendingActions += ('{0}: {1}' -f $category, $decision.Reason)
            $run.NetworkSummary = ('Not applied. {0}' -f $decision.Reason)
        }
    }

    $coordinator = Test-CapstoneCoordinatorReachable
    $run.CoordinatorNote = $coordinator.Note
    Write-CapstoneLog ('Coordinator: {0}' -f $coordinator.Note) -Level $(if ($coordinator.LinkProven) { 'OK' } else { 'WARN' })

    # -----------------------------------------------------------------------
    # Stage 3: Ollama
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 3 of 7: Ollama installation'

    if (-not $run.OllamaInstalled) {
        if ($Mode -eq 'VerifyOnly') {
            Write-CapstoneLog 'Ollama is not installed and this is a verification-only run.' -Level WARN
        } else {
            # The preflight internet probe is ADVISORY, never a gate. Measured
            # on node 3 (2026-10-30): the probe reported "could not be reached"
            # on a machine that then downloaded three models successfully. A
            # probe that is wrong in that direction must not be allowed to
            # refuse an installation the machine is perfectly able to perform.
            # The download itself is the authority: if it genuinely cannot
            # reach the host, it fails with the real transport error.
            if (-not $facts.internet.Available) {
                Write-CapstoneLog 'The internet probe did not get a response from the official Ollama hosts. Attempting the download anyway; the download itself will report the real result.' -Level WARN
            }
            $install = Install-CapstoneOllama -Paths $paths
            if ($install.Success) {
                $run.OllamaInstalled = $true
                $run.OllamaExecutable = $install.Executable
                $run.OllamaVersion = $install.Version
                $run.OllamaInstalledThisRun = ($install.Method -ne 'existing-installation')
            } else {
                $category = Get-CapstoneFailureCategory -Message $install.Error -Stage 'Install'
                $pendingActions += ('{0}: {1} {2}' -f $category.Category, $install.Error, $category.Recovery)
                Write-CapstoneLog ('Ollama installation did not complete: {0}' -f $install.Error) -Level ERROR
            }
        }
    } else {
        Write-CapstoneLog ('Reusing the existing Ollama {0} at {1}. It was not reinstalled.' -f $run.OllamaVersion, $run.OllamaExecutable) -Level OK
    }

    # -----------------------------------------------------------------------
    # Stage 4: healthy local API
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 4 of 7: local Ollama API'

    $baseUri = ''
    if ($run.OllamaInstalled -and -not [string]::IsNullOrWhiteSpace($run.OllamaExecutable)) {
        $server = Start-CapstoneOllamaServer -Executable $run.OllamaExecutable -PrivateIp $privateIp
        if ($server.Success) {
            $baseUri = $server.BaseUri
            $run.LocalApiHealthy = $true
            $run.LocalApiEndpoint = $baseUri
            if (-not [string]::IsNullOrWhiteSpace($server.Version)) { $run.OllamaVersion = $server.Version }
            Write-CapstoneLog ('Local Ollama API is healthy at {0} (version {1}).' -f $baseUri, $run.OllamaVersion) -Level OK
        } else {
            $message = 'LOCAL_API_UNAVAILABLE: the Ollama API did not answer.'
            if ($server.Strategy -and $server.Strategy.Action -eq 'StopWithPortConflict') {
                $message = ('EXISTING_PORT_CONFLICT: {0}' -f $server.Strategy.Reason)
            }
            $pendingActions += $message
            Write-CapstoneLog $message -Level ERROR
        }
    }

    # -----------------------------------------------------------------------
    # Stage 5: models
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 5 of 7: model downloads and verification'

    if (-not $run.LocalApiHealthy) {
        Write-CapstoneLog 'Skipping model downloads: there is no healthy local Ollama API to download into.' -Level WARN
    } elseif ($SkipModels) {
        Write-CapstoneLog 'Model downloads skipped by request.' -Level INFO
    } else {
        $catalog = Get-CapstoneCatalog -Path (Join-Path $engineRoot 'catalog\model-catalog.json')
        $wanted = @(Get-CapstoneProfileModels -Catalog $catalog -ProfileName $ModelProfile -IncludeEmbeddings)
        $run.ModelsRequested = $wanted.Count

        $profileEntry = $catalog.profiles.PSObject.Properties | Where-Object { $_.Name -eq $ModelProfile }
        $minRam = 0
        if ($profileEntry -and $profileEntry.Value.PSObject.Properties.Name -contains 'min_ram_gb') { $minRam = [int]$profileEntry.Value.min_ram_gb }
        if ($minRam -gt 0 -and $run.RamGB -lt $minRam) {
            $warning = ('The {0} profile expects about {1} GB of RAM and this machine has {2} GB. Models will download, but the larger ones may be slow or fail to load.' -f $ModelProfile, $minRam, $run.RamGB)
            Write-CapstoneLog $warning -Level WARN
            if (-not $AllowUndersizedProfile) { $pendingActions += ('UNDERSIZED_PROFILE: {0}' -f $warning) }
        }

        $installed = Get-CapstoneInstalledModels -BaseUri $baseUri
        $installedNames = @($installed.Models | ForEach-Object { $_.Name })
        $disk = Get-CapstoneDiskFacts -Path (Get-CapstoneModelDirectory).Path

        $plan = Get-CapstoneModelPlan -CatalogModels $wanted -InstalledNames $installedNames -FreeBytes $disk.FreeBytes
        Write-CapstoneLog ('Profile {0}: {1} tags, {2} already present, {3} to download.' -f $ModelProfile, $plan.TotalCount, $plan.AlreadyInstalled, $plan.PendingCount) -Level INFO
        Write-CapstoneLog ('Disk: {0}' -f $plan.DiskNote) -Level $(if ($plan.DiskSufficient) { 'INFO' } else { 'ERROR' })

        if (-not $plan.DiskSufficient) {
            $pendingActions += ('DISK_SPACE_INADEQUATE: {0}' -f $plan.DiskNote)
        }

        if ($Mode -eq 'VerifyOnly') {
            foreach ($item in $plan.Items) {
                if ($item.Status -eq 'AlreadyInstalled' -and -not $SkipInference) {
                    $check = Test-CapstoneModelInference -BaseUri $baseUri -Tag $item.Tag -Kind $item.Kind
                    $item.InferenceOk = $check.Success
                    $item.InferenceMs = $check.ElapsedMs
                    $item.InferenceNote = $(if ($check.Success) { $check.Note } else { $check.Error })
                    Write-CapstoneLog ('  {0}: {1}' -f $item.Tag, $(if ($check.Success) { 'inference pass' } else { 'INFERENCE FAIL - ' + $check.Error })) -Level $(if ($check.Success) { 'OK' } else { 'ERROR' })
                }
            }
            $queueItems = @($plan.Items)
            $summary = Get-CapstoneQueueSummary -Items $queueItems
        } else {
            Write-CapstoneLog 'Downloads start now. Progress is reported continuously; the window is not frozen when a large layer is in flight.' -Level INFO
            $queue = Invoke-CapstoneModelQueue -Paths $paths -BaseUri $baseUri -Plan $plan -SkipInference:$SkipInference
            $queueItems = @($queue.Items)
            $summary = $queue.Summary
        }

        $run.ModelsInstalled = $summary.Installed
        $run.ModelsFailed = $summary.Failed
        $run.FailedTags = @($summary.FailedTags)
        $run.ModelsVerified = $summary.InferenceOk
        $run.ModelsAlreadyPresent = @($queueItems | Where-Object { $_.Status -eq 'AlreadyInstalled' }).Count
        $run.ModelsDownloaded = @($queueItems | Where-Object { $_.Status -in @('Verified', 'Downloaded') -and $_.Attempts -gt 0 }).Count
        $run.InferenceVerified = ($summary.InferenceOk -gt 0)
        if ($SkipInference) { $run.InferenceVerified = $false }

        Write-CapstoneLog '' -Level INFO
        Write-CapstoneLog ('Model result: {0} of {1} installed, {2} verified by real inference, {3} failed.' -f $run.ModelsInstalled, $run.ModelsRequested, $run.ModelsVerified, $run.ModelsFailed) -Level $(if ($run.ModelsFailed -eq 0) { 'OK' } else { 'WARN' })
        foreach ($tag in @($run.FailedTags)) {
            $item = @($queueItems | Where-Object { $_.Tag -eq $tag })[0]
            Write-CapstoneLog ('  FAILED {0}  [{1}]  {2}' -f $tag, $item.FailureCategory, $item.LastError) -Level ERROR
        }
    }

    # -----------------------------------------------------------------------
    # Stage 6: remote serving  (never earlier than this)
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 6 of 7: remote serving on the private LAN'

    if ($SkipRemote) {
        Write-CapstoneLog 'Remote serving skipped by request.' -Level INFO
        $run.RemoteStatus = 'REMOTE_NOT_REQUESTED'
    } elseif ($isGpuWorkstation) {
        Write-CapstoneLog 'This is the existing GPU workstation; its already-working remote endpoint is left exactly as it is.' -Level OK
        $run.RemoteConfigured = $true
        $run.RemoteBinding = '(pre-existing, unchanged)'
        $privateProbe = Test-CapstoneOllamaApi -BaseUri (Get-CapstoneApiBase -HostAddress $privateIp) -TimeoutSeconds 8
        $run.PrivateListenerHealthy = $privateProbe.Healthy
        $run.RemoteEndpoint = (Get-CapstoneApiBase -HostAddress $privateIp)
    } elseif (-not $run.LocalApiHealthy) {
        Write-CapstoneLog 'Skipping remote serving: there is no healthy local server to expose.' -Level WARN
    } elseif (-not $run.NetworkVerified) {
        Write-CapstoneLog 'Skipping remote serving: the private-LAN address is not verified, so there is no interface to bind.' -Level WARN
        $run.RemoteStatus = 'REMOTE_NOT_CONFIGURED'
    } elseif ($Mode -eq 'VerifyOnly') {
        $privateProbe = Test-CapstoneOllamaApi -BaseUri (Get-CapstoneApiBase -HostAddress $privateIp) -TimeoutSeconds 8
        $run.PrivateListenerHealthy = $privateProbe.Healthy
        $run.RemoteConfigured = $privateProbe.Healthy
        $run.RemoteEndpoint = (Get-CapstoneApiBase -HostAddress $privateIp)
        $run.FirewallSummary = (@(Get-CapstoneFirewallRuleFacts | ForEach-Object { $_.DisplayName }) -join '; ')
    } else {
        # Note that models having failed does NOT block this stage. A head with
        # 17 of 18 models is still worth serving to the coordinator.
        $binding = Resolve-CapstoneRemoteBinding -PrivateIp $privateIp -BindMode $BindMode
        Write-CapstoneLog ('Binding: OLLAMA_HOST={0} ({1})' -f $binding.Value, $binding.Rationale) -Level INFO
        $run.RemoteBinding = $binding.Value
        $run.RemoteEndpoint = (Get-CapstoneApiBase -HostAddress $privateIp)

        $hostVariable = Set-CapstoneOllamaHostVariable -Value $binding.Value -Paths $paths
        if (-not [string]::IsNullOrWhiteSpace($hostVariable.PendingAction)) { $pendingActions += $hostVariable.PendingAction }

        $firewall = Set-CapstoneFirewallRule -PrivateIp $privateIp -CoordinatorIp $script:CoordinatorIp
        if ($firewall.Applied) {
            $run.FirewallSummary = ('{0} (inbound TCP 11434 from {1} only)' -f $firewall.DisplayName, $script:CoordinatorIp)
        } else {
            $run.FirewallSummary = ('NOT APPLIED: {0}' -f $firewall.Error)
            $pendingActions += ('FIREWALL_RULE_FAILURE: {0}' -f $firewall.Error)
        }

        $expected = Get-CapstoneApiBase -HostAddress $privateIp

        # The server is registered to start at BOOT, as SYSTEM, via a
        # scheduled task running "ollama serve". The Ollama tray application is
        # deliberately not used:
        #
        #   * it only starts at interactive logon, so a head sitting at the
        #     lock screen after a reboot serves nothing at all, and
        #   * on nodes 5 and 6 a clean install left it running with no
        #     listener and no amount of waiting produced one.
        #
        # If the task does not produce a listener, the tray autostart is put
        # back and the old restart path is tried, so a head can never end up
        # worse off than before.
        if ($hostVariable.Applied) {
            $service = Register-CapstoneOllamaService -Executable $run.OllamaExecutable `
                -BindValue $binding.Value -ModelDirectory $run.ModelDirectory

            if ($service.Registered) {
                $run.RemoteConfigured = $true
                $started = Start-CapstoneOllamaService -ExpectedBaseUri $expected -TimeoutSeconds 180
                $run.PrivateListenerHealthy = $started.Success

                if (-not $started.Success) {
                    Write-CapstoneLog ('The boot task did not bring up a listener: {0}' -f $started.Error) -Level WARN
                    [void](Restore-CapstoneTrayAutostart -PreviousTrayCommand $service.PreviousTrayCommand)
                    $restart = Restart-CapstoneOllamaForBinding -Executable $run.OllamaExecutable -CurrentBaseUri $baseUri -ExpectedBaseUri $expected
                    $run.PrivateListenerHealthy = $restart.Success
                    if (-not [string]::IsNullOrWhiteSpace($restart.PendingAction)) { $pendingActions += $restart.PendingAction }
                    if (-not $restart.Success) {
                        $pendingActions += ('REMOTE_LISTENER_FAILURE: {0}' -f $started.Error)
                    }
                }
            } else {
                $pendingActions += ('OLLAMA_SERVICE_REGISTRATION_FAILED: {0}' -f $service.Error)
                $restart = Restart-CapstoneOllamaForBinding -Executable $run.OllamaExecutable -CurrentBaseUri $baseUri -ExpectedBaseUri $expected
                $run.RemoteConfigured = $true
                $run.PrivateListenerHealthy = $restart.Success
                if (-not [string]::IsNullOrWhiteSpace($restart.PendingAction)) { $pendingActions += $restart.PendingAction }
            }

            if ($run.PrivateListenerHealthy) {
                Write-CapstoneLog ('The private-LAN listener is live at {0}.' -f $expected) -Level OK
                # Loopback must keep working in PrivateLan mode; confirm it.
                if ($binding.LoopbackWorks) {
                    $loopback = Test-CapstoneOllamaApi -BaseUri (Get-CapstoneApiBase) -TimeoutSeconds 10
                    $run.LocalApiHealthy = $loopback.Healthy
                    if (-not $loopback.Healthy) {
                        $pendingActions += 'After rebinding, the loopback API stopped answering. Local tooling on this head may be affected.'
                    }
                }
            }
        } else {
            $run.RemoteStatus = 'REMOTE_LISTENER_NOT_BOUND'
        }
    }

    # -----------------------------------------------------------------------
    # Stage 7: verification and reporting
    # -----------------------------------------------------------------------
    Write-StageHeader 'Stage 7 of 7: verification and report'

    $remoteVerification = Get-CapstoneRemoteVerification `
        -LocalApiHealthy $run.LocalApiHealthy `
        -PrivateListenerHealthy $run.PrivateListenerHealthy `
        -FirewallRuleApplied ($run.FirewallSummary -notlike 'NOT APPLIED*') `
        -CoordinatorLinkProven $coordinator.LinkProven `
        -CoordinatorApiProven $false `
        -CoordinatorInferenceProven $false

    $run.RemoteStatus = $remoteVerification.Overall
    $run.CoordinatorVerified = ($remoteVerification.Overall -eq 'REMOTE_VERIFIED_END_TO_END')

    foreach ($level in @($remoteVerification.Levels)) {
        Write-CapstoneLog ('  {0,-24} {1}' -f $level.Level, $(if ($level.Pass) { 'pass' } else { 'not proven' })) -Level $(if ($level.Pass) { 'OK' } else { 'WARN' })
    }
    if (-not $run.CoordinatorVerified -and -not $SkipRemote) {
        Write-CapstoneLog $remoteVerification.Note -Level WARN
        $pendingActions += ('REMOTE_COORDINATOR_VERIFICATION_PENDING: {0}' -f $remoteVerification.Note)
    }

    $run.MachineState = Resolve-CapstoneMachineState `
        -ExecutablePresent $run.OllamaInstalled `
        -LocalApiHealthy $run.LocalApiHealthy `
        -ModelsRequested $run.ModelsRequested `
        -ModelsInstalled $run.ModelsInstalled `
        -ModelsFailed $run.ModelsFailed `
        -InferenceVerified $run.InferenceVerified `
        -RemoteConfigured $run.RemoteConfigured `
        -PrivateListenerHealthy $run.PrivateListenerHealthy `
        -CoordinatorVerified $run.CoordinatorVerified `
        -RemoteRequested (-not $SkipRemote)

    $run.Outcome = Get-CapstoneOverallOutcome `
        -NetworkRequested (-not $SkipNetwork) `
        -NetworkVerified $run.NetworkVerified `
        -OllamaInstalled $run.OllamaInstalled `
        -LocalApiHealthy $run.LocalApiHealthy `
        -ModelsRequested $run.ModelsRequested `
        -ModelsInstalled $run.ModelsInstalled `
        -ModelsFailed $run.ModelsFailed `
        -InferenceVerified $run.InferenceVerified `
        -RemoteRequested (-not $SkipRemote) `
        -RemoteConfigured $run.RemoteConfigured `
        -PrivateListenerHealthy $run.PrivateListenerHealthy `
        -CoordinatorVerified $run.CoordinatorVerified `
        -PendingActions $pendingActions `
        -FailedTags @($run.FailedTags)

    $registration = New-CapstoneHeadRegistration -Paths $paths -Run ([pscustomobject]$run) -QueueItems $queueItems
    $run.RegistrationPath = $registration.Path

    $savedState.machine_state = $run.MachineState
    Save-CapstoneState -Paths $paths -State $savedState | Out-Null
    Write-CapstoneJournal -Paths $paths -Stage 'Finish' -Event 'run-end' -Detail ([pscustomobject]@{ outcome = $run.Outcome.Outcome; state = $run.MachineState })

    $report = New-CapstoneStatusReport -Paths $paths -Run ([pscustomobject]$run)

    Write-CapstoneLog '' -Level INFO
    Write-Host $report.Text
    Write-CapstoneLog '' -Level INFO

    switch ($run.Outcome.Outcome) {
        'FULL_SUCCESS'    { $exitCode = 0 }
        'PARTIAL_SUCCESS' { $exitCode = 10 }
        default           { $exitCode = 20 }
    }
}
catch {
    $message = $_.Exception.Message
    Write-CapstoneLog '' -Level ERROR
    Write-CapstoneLog ('The run stopped: {0}' -f $message) -Level ERROR
    if ($_.ScriptStackTrace) {
        Write-CapstoneLog $_.ScriptStackTrace -Level DEBUG -NoConsole
    }
    $pendingActions += $message

    try {
        $run.Outcome = Get-CapstoneOverallOutcome `
            -NetworkRequested (-not $SkipNetwork) -NetworkVerified $run.NetworkVerified `
            -OllamaInstalled $run.OllamaInstalled -LocalApiHealthy $run.LocalApiHealthy `
            -ModelsRequested $run.ModelsRequested -ModelsInstalled $run.ModelsInstalled `
            -ModelsFailed $run.ModelsFailed -InferenceVerified $run.InferenceVerified `
            -RemoteRequested (-not $SkipRemote) -RemoteConfigured $run.RemoteConfigured `
            -PrivateListenerHealthy $run.PrivateListenerHealthy -CoordinatorVerified $run.CoordinatorVerified `
            -PendingActions $pendingActions -FailedTags @($run.FailedTags)
        $report = New-CapstoneStatusReport -Paths $paths -Run ([pscustomobject]$run)
        Write-Host $report.Text
    } catch {
        Write-CapstoneLog ('A result report could not be written: {0}' -f $_.Exception.Message) -Level ERROR
    }
    $exitCode = 20
}
finally {
    if ($lockAcquired) { Exit-CapstoneLock -Paths $paths }
    Write-CapstoneLog ('Status summary: {0}' -f $paths.StatusText) -Level INFO
    Write-CapstoneLog ('Full run log  : {0}' -f (Get-CapstoneLogFile)) -Level INFO
}

exit $exitCode
