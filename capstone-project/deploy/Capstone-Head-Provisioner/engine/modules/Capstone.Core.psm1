<#
    Capstone.Core
    Shared primitives for the Capstone Head Provisioner.

    Targets Windows PowerShell 5.1. Nothing in this module may use PowerShell 7
    syntax (no ternary, no null-coalescing, no -AsHashtable), because new heads
    are provisioned with the in-box 5.1 host.

    Every interpolated variable that is followed by a colon is written as
    ${name}: so that the parser cannot read the colon as a scope qualifier.
    That exact mistake is defect D-10 in docs/DEFECT-MATRIX.md.
#>

# StrictMode is deliberately NOT enabled. This module parses JSON produced by
# Ollama and by earlier runs of this tool; under StrictMode a single absent
# property would throw mid-provisioning. Absence is checked explicitly instead.

$script:StateNames = @(
    'NOT_INSTALLED', 'INSTALLING', 'INSTALLED', 'SERVER_STARTING', 'SERVER_READY',
    'MODELS_DOWNLOADING', 'MODELS_VERIFIED', 'REMOTE_CONFIGURING', 'REMOTE_READY',
    'VERIFIED', 'PARTIAL', 'FAILED_RECOVERABLE', 'FAILED_REQUIRES_ACTION'
)

$script:TerminalStates = @('VERIFIED', 'PARTIAL', 'FAILED_REQUIRES_ACTION')

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

function Get-CapstonePaths {
    <#
        .SYNOPSIS
            Resolves the single, stable reporting root and its subdirectories.
        .PARAMETER Root
            Override for tests. Defaults to C:\ProgramData\CapstoneProvisioner.
    #>
    [CmdletBinding()]
    param([string]$Root)

    if ([string]::IsNullOrWhiteSpace($Root)) {
        $base = $env:ProgramData
        if ([string]::IsNullOrWhiteSpace($base)) { $base = 'C:\ProgramData' }
        $Root = Join-Path $base 'CapstoneProvisioner'
    }

    $paths = [ordered]@{
        Root        = $Root
        Config      = Join-Path $Root 'config'
        Inventory   = Join-Path $Root 'inventory'
        Logs        = Join-Path $Root 'logs'
        Reports     = Join-Path $Root 'reports'
        Backups     = Join-Path $Root 'backups'
        State       = Join-Path $Root 'state'
        Diagnostics = Join-Path $Root 'diagnostics'
        Downloads   = Join-Path $Root 'downloads'
    }
    $paths.StatusText = Join-Path $paths.Reports 'LATEST-STATUS.txt'
    $paths.StatusJson = Join-Path $paths.Reports 'LATEST-STATUS.json'
    $paths.StateFile  = Join-Path $paths.State   'state.json'
    $paths.Journal    = Join-Path $paths.State   'journal.jsonl'
    $paths.LockFile   = Join-Path $paths.State   'provisioner.lock'
    $paths.ConfigFile = Join-Path $paths.Config  'capstone.config.json'

    return [pscustomobject]$paths
}

function Initialize-CapstonePaths {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Paths)

    foreach ($key in @('Root', 'Config', 'Inventory', 'Logs', 'Reports', 'Backups', 'State', 'Diagnostics', 'Downloads')) {
        $dir = $Paths.$key
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
        }
    }
    return $Paths
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

$script:LogFile = $null
$script:LogLevel = 'INFO'

function Set-CapstoneLogFile {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    $script:LogFile = $Path
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null
    }
}

function Get-CapstoneLogFile { return $script:LogFile }

function Write-CapstoneLog {
    <#
        .SYNOPSIS
            Writes one operator-visible line and mirrors it to the run log.
        .DESCRIPTION
            Console output is the operator's only live feedback, so this never
            throws: a failure to append to the log must not abort provisioning.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][AllowEmptyString()][string]$Message,
        [ValidateSet('DEBUG', 'INFO', 'STEP', 'WARN', 'ERROR', 'OK')][string]$Level = 'INFO',
        [switch]$NoConsole
    )

    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = '{0} [{1,-5}] {2}' -f $stamp, $Level, $Message

    if (-not $NoConsole) {
        $colour = 'Gray'
        switch ($Level) {
            'STEP'  { $colour = 'Cyan' }
            'OK'    { $colour = 'Green' }
            'WARN'  { $colour = 'Yellow' }
            'ERROR' { $colour = 'Red' }
            'INFO'  { $colour = 'White' }
            'DEBUG' { $colour = 'DarkGray' }
        }
        if ($Level -eq 'DEBUG' -and $script:LogLevel -ne 'DEBUG') {
            # suppressed on console, still logged
        } else {
            try { Write-Host $Message -ForegroundColor $colour } catch { Write-Output $Message }
        }
    }

    if ($script:LogFile) {
        try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
    }
}

function Write-CapstoneBanner {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Text)
    $bar = '=' * 72
    Write-CapstoneLog $bar -Level STEP
    Write-CapstoneLog $Text -Level STEP
    Write-CapstoneLog $bar -Level STEP
}

# ---------------------------------------------------------------------------
# JSON helpers (5.1 safe)
# ---------------------------------------------------------------------------

function Write-CapstoneJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$InputObject,
        [Parameter(Mandatory)][string]$Path,
        [int]$Depth = 12
    )
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null
    }
    $json = $InputObject | ConvertTo-Json -Depth $Depth
    # Set-Content with UTF8 on 5.1 emits a BOM; cmd/netsh never read these files,
    # but the coordinator utility and tests do, so write BOM-less UTF8 explicitly.
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Read-CapstoneJson {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        return ($raw | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        Write-CapstoneLog ("Could not read JSON {0}: {1}" -f $Path, $_.Exception.Message) -Level WARN
        return $null
    }
}

function ConvertTo-CapstoneHashtable {
    <#
        .SYNOPSIS
            ConvertFrom-Json on 5.1 has no -AsHashtable; this fills that gap.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory, ValueFromPipeline)]$InputObject)
    process {
        if ($null -eq $InputObject) { return $null }
        if ($InputObject -is [System.Collections.IDictionary]) { return $InputObject }
        $result = @{}
        foreach ($property in $InputObject.PSObject.Properties) {
            $result[$property.Name] = $property.Value
        }
        return $result
    }
}

# ---------------------------------------------------------------------------
# Run journal and resumable state
# ---------------------------------------------------------------------------

function Write-CapstoneJournal {
    <#
        .SYNOPSIS
            Appends one append-only journal record so an interrupted run can be
            reconstructed without trusting the final report.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [Parameter(Mandatory)][string]$Stage,
        [Parameter(Mandatory)][string]$Event,
        $Detail
    )
    $record = [ordered]@{
        at    = (Get-Date).ToString('o')
        pid   = $PID
        stage = $Stage
        event = $Event
    }
    if ($PSBoundParameters.ContainsKey('Detail') -and $null -ne $Detail) {
        $record.detail = $Detail
    }
    try {
        $json = ([pscustomobject]$record | ConvertTo-Json -Depth 8 -Compress)
        Add-Content -LiteralPath $Paths.Journal -Value $json -Encoding UTF8 -ErrorAction Stop
    } catch {
        Write-CapstoneLog ("Journal append failed: {0}" -f $_.Exception.Message) -Level WARN
    }
}

function Get-CapstoneState {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Paths)

    $state = Read-CapstoneJson -Path $Paths.StateFile
    if ($null -eq $state) {
        return [pscustomobject][ordered]@{
            schema_version   = 1
            machine_state    = 'NOT_INSTALLED'
            node_number      = 0
            node_ip          = ''
            model_profile    = ''
            first_run_at     = ''
            last_run_at      = ''
            run_count        = 0
            completed_stages = @()
            models           = @()
        }
    }
    return $state
}

function Save-CapstoneState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [Parameter(Mandatory)]$State
    )
    Write-CapstoneJson -InputObject $State -Path $Paths.StateFile
    return $State
}

# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

function Enter-CapstoneLock {
    <#
        .SYNOPSIS
            Prevents two provisioner instances from editing one Ollama install.
        .DESCRIPTION
            A lock whose owning process is gone is stale and is reclaimed; this
            keeps a hard-powered-off head from needing manual cleanup.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [int]$StaleMinutes = 240
    )

    if (Test-Path -LiteralPath $Paths.LockFile) {
        $existing = Read-CapstoneJson -Path $Paths.LockFile
        if ($null -ne $existing) {
            $ownerAlive = $false
            try {
                $owner = Get-Process -Id ([int]$existing.pid) -ErrorAction Stop
                if ($owner) { $ownerAlive = $true }
            } catch { $ownerAlive = $false }

            $age = [TimeSpan]::MaxValue
            try { $age = (Get-Date) - [datetime]::Parse($existing.at) } catch { }

            if ($ownerAlive -and $age.TotalMinutes -lt $StaleMinutes) {
                return [pscustomobject]@{
                    Acquired = $false
                    OwnerPid = $existing.pid
                    Reason   = ('Another Capstone provisioner (PID {0}) is already running. Close it, or wait for it to finish.' -f $existing.pid)
                }
            }
            Write-CapstoneLog ('Reclaiming stale lock from PID {0}.' -f $existing.pid) -Level WARN
        }
    }

    $lock = [ordered]@{ pid = $PID; at = (Get-Date).ToString('o'); host = $env:COMPUTERNAME }
    Write-CapstoneJson -InputObject ([pscustomobject]$lock) -Path $Paths.LockFile -Depth 3
    return [pscustomobject]@{ Acquired = $true; OwnerPid = $PID; Reason = '' }
}

function Exit-CapstoneLock {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Paths)
    if (Test-Path -LiteralPath $Paths.LockFile) {
        $existing = Read-CapstoneJson -Path $Paths.LockFile
        if ($null -eq $existing -or [int]$existing.pid -eq $PID) {
            Remove-Item -LiteralPath $Paths.LockFile -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------

function Test-CapstoneElevated {
    [CmdletBinding()]
    param()
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return [bool]$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# HTTP stack
# ---------------------------------------------------------------------------

function Initialize-CapstoneHttp {
    <#
        .SYNOPSIS
            Removes the 15-second first-request stall from .NET Framework.
        .DESCRIPTION
            Measured on Windows 11 build 26200 under Windows PowerShell 5.1:
            the FIRST HTTP request in a process takes about 15.5 seconds, and
            every later request in that process takes under 5 ms. The cost is
            WPAD automatic proxy discovery, which .NET performs lazily on first
            use and which has to time out before the request proceeds.

            That single fact would break this tool. Health probes are given
            5-to-8 second budgets, so the first probe of every run would time
            out and the engine would conclude that a perfectly healthy Ollama
            was not answering - and might then start a second server. It is
            also exactly why an integration run reported the GPU workstation at
            192.0.2.2 as unreachable while raw TCP to it succeeded.

            The registry is read instead of probing the network: if WinINET has
            no proxy configured, the default proxy is replaced with an explicit
            direct connection, so WPAD never runs. If a proxy IS configured, it
            is left alone, because the installer download may genuinely need it.
    #>
    [CmdletBinding()]
    param([switch]$ForceDirect)

    try {
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls11
    } catch { }

    try {
        # More connections per endpoint than the default 2, so progress polling
        # cannot be blocked behind an in-flight model download.
        [System.Net.ServicePointManager]::DefaultConnectionLimit = 32
        [System.Net.ServicePointManager]::Expect100Continue = $false
    } catch { }

    $proxyConfigured = $false
    if (-not $ForceDirect) {
        try {
            $settings = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
            $enabled = 0
            if ($settings.PSObject.Properties.Name -contains 'ProxyEnable' -and $null -ne $settings.ProxyEnable) {
                $enabled = [int]$settings.ProxyEnable
            }
            $server = ''
            if ($settings.PSObject.Properties.Name -contains 'ProxyServer' -and $settings.ProxyServer) {
                $server = [string]$settings.ProxyServer
            }
            $autoConfig = ''
            if ($settings.PSObject.Properties.Name -contains 'AutoConfigURL' -and $settings.AutoConfigURL) {
                $autoConfig = [string]$settings.AutoConfigURL
            }
            if (($enabled -eq 1 -and -not [string]::IsNullOrWhiteSpace($server)) -or
                (-not [string]::IsNullOrWhiteSpace($autoConfig))) {
                $proxyConfigured = $true
            }
        } catch { }
    }

    if ($proxyConfigured) {
        return [pscustomobject]@{
            Direct = $false
            Note   = 'A WinINET proxy is configured, so the system proxy is left in place. The first HTTP request may be slow while it is discovered.'
        }
    }

    try {
        # An address-less WebProxy means "connect directly". Assigning it stops
        # .NET from ever attempting WPAD discovery in this process.
        [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy
        return [pscustomobject]@{
            Direct = $true
            Note   = 'No proxy is configured, so HTTP goes direct and automatic proxy discovery is disabled. This removes a 15-second stall on the first request.'
        }
    } catch {
        return [pscustomobject]@{
            Direct = $false
            Note   = ('Could not set a direct HTTP proxy: {0}' -f $_.Exception.Message)
        }
    }
}

# ---------------------------------------------------------------------------
# Failure classification  (pure - unit tested)
# ---------------------------------------------------------------------------

function Get-CapstoneFailureCategory {
    <#
        .SYNOPSIS
            Maps a raw failure into a named category plus a retry decision.
        .DESCRIPTION
            Defect D-07 was an installer that retried every failure, including
            permanently invalid model tags. Classification decides retry; the
            caller never retries on its own judgement.

            A 500 from the model registry is deliberately treated as transient
            but bounded. The spec is explicit that a status code alone does not
            establish a root cause, so the category records what was observed,
            not a guess about why.
    #>
    [CmdletBinding()]
    param(
        [string]$Message,
        [int]$HttpStatus = 0,
        [int]$ExitCode = 0,
        [string]$Stage = ''
    )

    if ($null -eq $Message) { $Message = '' }
    $text = $Message.ToLowerInvariant()

    $result = [ordered]@{
        Category    = 'UNEXPECTED_EXCEPTION'
        Retryable   = $false
        HttpStatus  = $HttpStatus
        ExitCode    = $ExitCode
        Stage       = $Stage
        Observed    = $Message
        Recovery    = 'Collect diagnostics and re-run the provisioner.'
    }

    if ($HttpStatus -eq 404 -or $text -match 'file does not exist|manifest unknown|pull model manifest|repository name not known|model .*not found') {
        $result.Category = 'MODEL_TAG_INVALID'
        $result.Retryable = $false
        $result.Recovery = 'The exact tag is not present in the Ollama library. Correct the catalog entry; no substitute model is installed automatically.'
    }
    elseif ($text -match 'no space left|not enough space|insufficient disk|disk full|there is not enough space') {
        $result.Category = 'DISK_SPACE_INADEQUATE'
        $result.Retryable = $false
        $result.Recovery = 'Free disk space or select a smaller profile. No existing model is deleted to make room.'
    }
    elseif ($text -match 'no such host is known|name or service not known|could not resolve|getaddrinfo') {
        $result.Category = 'NO_INTERNET_CONNECTION'
        $result.Retryable = $true
        $result.Recovery = 'DNS lookup failed. Confirm the Wi-Fi internet connection, then re-run; completed models are preserved.'
    }
    elseif ($text -match 'forcibly closed|connection was aborted|connection reset|unable to read data from the transport|operation timed out|timed out|task was canceled|the operation has timed out') {
        $result.Category = 'MODEL_DOWNLOAD_INTERRUPTED'
        $result.Retryable = $true
        $result.Recovery = 'The transfer was cut mid-stream. Ollama keeps completed layers, so a retry resumes rather than restarting.'
    }
    elseif ($HttpStatus -ge 500 -or $text -match 'internal server error|bad gateway|service unavailable|gateway timeout') {
        $result.Category = 'MODEL_PULL_SERVER_ERROR'
        $result.Retryable = $true
        $result.Recovery = 'The Ollama daemon or the model registry returned a server error. Bounded retry; if it persists the exact status is reported rather than reinterpreted.'
    }
    elseif ($HttpStatus -eq 401 -or $HttpStatus -eq 403) {
        $result.Category = 'MODEL_MANIFEST_UNAVAILABLE'
        $result.Retryable = $false
        $result.Recovery = 'The registry refused the request. This provisioner never signs in to Ollama cloud; confirm the tag is a public library tag.'
    }
    elseif ($text -match 'access is denied|requires elevation|unauthorized') {
        $result.Category = 'INSUFFICIENT_PRIVILEGE'
        $result.Retryable = $false
        $result.Recovery = 'Re-launch START-CAPSTONE-SETUP.cmd and approve the Windows UAC prompt.'
    }
    elseif ($text -match 'signature|authenticode|not digitally signed') {
        $result.Category = 'INSTALLER_SIGNATURE_FAILURE'
        $result.Retryable = $false
        $result.Recovery = 'The downloaded installer did not carry a valid Ollama Inc. signature and was not executed. Nothing was installed.'
    }
    elseif ($text -match 'connection refused|actively refused|no connection could be made') {
        $result.Category = 'LOCAL_API_UNAVAILABLE'
        $result.Retryable = $true
        $result.Recovery = 'Nothing is listening on the Ollama port yet. The provisioner waits and re-checks rather than starting a second server.'
    }
    elseif ($ExitCode -ne 0 -and $Stage -eq 'Network') {
        $result.Category = 'NETWORK_CONFIGURATION_FAILURE'
        $result.Retryable = $false
        $result.Recovery = 'The adapter was left as found or restored from the backup record. Review the backup JSON before retrying.'
    }

    return [pscustomobject]$result
}

function Get-CapstoneRetryDelaySeconds {
    <#
        .SYNOPSIS
            Bounded exponential backoff with a hard ceiling.
        .DESCRIPTION
            Attempt numbers are 1-based. The ceiling exists so a long unattended
            run cannot stall for an unbounded period on one model.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$Attempt,
        [int]$BaseSeconds = 10,
        [int]$MaxSeconds = 300
    )
    if ($Attempt -lt 1) { $Attempt = 1 }
    $delay = $BaseSeconds * [math]::Pow(2, ($Attempt - 1))
    if ($delay -gt $MaxSeconds) { $delay = $MaxSeconds }
    return [int]$delay
}

# ---------------------------------------------------------------------------
# State machine  (pure - unit tested)
# ---------------------------------------------------------------------------

function Get-CapstoneStateNames { return $script:StateNames }

function Resolve-CapstoneMachineState {
    <#
        .SYNOPSIS
            Derives the lifecycle state from observed facts only.
        .DESCRIPTION
            Defect D-11 was reporting success from the fact that a step ran.
            Every input here is something that was measured after the fact:
            an executable that exists, an API that answered, a tag that came
            back in the inventory, a listener that accepted a connection.

            Note that ModelsRequested = 0 is a valid, fully-verified outcome
            (a diagnostics-only profile), and an empty model inventory is never
            by itself evidence of a broken Ollama installation (defect D-05).
    #>
    [CmdletBinding()]
    param(
        [bool]$ExecutablePresent = $false,
        [bool]$LocalApiHealthy = $false,
        [int]$ModelsRequested = 0,
        [int]$ModelsInstalled = 0,
        [int]$ModelsFailed = 0,
        [bool]$InferenceVerified = $false,
        [bool]$RemoteConfigured = $false,
        [bool]$PrivateListenerHealthy = $false,
        [bool]$CoordinatorVerified = $false,
        [bool]$RemoteRequested = $true,
        [bool]$BlockingActionRequired = $false
    )

    if ($BlockingActionRequired) { return 'FAILED_REQUIRES_ACTION' }
    if (-not $ExecutablePresent) { return 'NOT_INSTALLED' }
    if (-not $LocalApiHealthy)   { return 'SERVER_STARTING' }

    $allModelsPresent = ($ModelsInstalled -ge $ModelsRequested)
    $modelsOk = ($allModelsPresent -and $ModelsFailed -eq 0)

    if (-not $modelsOk) {
        if ($ModelsInstalled -gt 0 -or $ModelsRequested -eq 0) { return 'PARTIAL' }
        return 'FAILED_RECOVERABLE'
    }

    # Models are complete. An inference check is required before the model
    # stage counts as verified, because presence in `ollama list` is not proof
    # that the model loads (spec section 19).
    if ($ModelsRequested -gt 0 -and -not $InferenceVerified) { return 'PARTIAL' }

    if (-not $RemoteRequested) { return 'VERIFIED' }

    if (-not $RemoteConfigured)       { return 'REMOTE_CONFIGURING' }
    if (-not $PrivateListenerHealthy) { return 'PARTIAL' }
    if (-not $CoordinatorVerified)    { return 'REMOTE_READY' }

    return 'VERIFIED'
}

function Test-CapstoneTerminalState {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$State)
    return ($script:TerminalStates -contains $State)
}

# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------

function Format-CapstoneBytes {
    [CmdletBinding()]
    param([Parameter(Mandatory)][double]$Bytes)
    if ($Bytes -lt 0) { return 'unknown' }
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

Export-ModuleMember -Function @(
    'Get-CapstonePaths', 'Initialize-CapstonePaths',
    'Set-CapstoneLogFile', 'Get-CapstoneLogFile', 'Write-CapstoneLog', 'Write-CapstoneBanner',
    'Write-CapstoneJson', 'Read-CapstoneJson', 'ConvertTo-CapstoneHashtable',
    'Write-CapstoneJournal', 'Get-CapstoneState', 'Save-CapstoneState',
    'Enter-CapstoneLock', 'Exit-CapstoneLock',
    'Test-CapstoneElevated', 'Initialize-CapstoneHttp',
    'Get-CapstoneFailureCategory', 'Get-CapstoneRetryDelaySeconds',
    'Get-CapstoneStateNames', 'Resolve-CapstoneMachineState', 'Test-CapstoneTerminalState',
    'Format-CapstoneBytes'
)
