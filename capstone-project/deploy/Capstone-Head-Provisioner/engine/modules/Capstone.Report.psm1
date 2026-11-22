<#
    Capstone.Report
    The operator-facing result and the machine-readable head registration.

    Design notes that exist because earlier versions failed:

      * Full success, partial success and failure are three distinct outcomes
        and are never collapsed (defect D-11). A profile with one failed tag is
        PARTIAL, with the exact tag named.

      * The plain-English summary answers the questions in spec section 24 in
        order, at the top of the file. Detailed transcripts live elsewhere.
#>

$script:SchemaVersion = 1

function Get-CapstoneOverallOutcome {
    <#
        .SYNOPSIS
            Reduces measured stage results to one outcome plus manual actions.
        .DESCRIPTION
            Pure, so the rule that decides what the operator is told is itself
            unit tested. Remote verification that is merely pending is not a
            failure, but it does prevent the result from being FULL_SUCCESS.
    #>
    [CmdletBinding()]
    param(
        [bool]$NetworkRequested = $true,
        [bool]$NetworkVerified = $false,
        [bool]$OllamaInstalled = $false,
        [bool]$LocalApiHealthy = $false,
        [int]$ModelsRequested = 0,
        [int]$ModelsInstalled = 0,
        [int]$ModelsFailed = 0,
        [bool]$InferenceVerified = $false,
        [bool]$RemoteRequested = $true,
        [bool]$RemoteConfigured = $false,
        [bool]$PrivateListenerHealthy = $false,
        [bool]$CoordinatorVerified = $false,
        [AllowEmptyCollection()][array]$PendingActions = @(),
        [AllowEmptyCollection()][array]$FailedTags = @()
    )

    $blockers = @()
    $notes = @()

    if ($NetworkRequested -and -not $NetworkVerified) { $blockers += 'The private-LAN address was not verified.' }
    if (-not $OllamaInstalled) { $blockers += 'Ollama is not installed.' }
    if (-not $LocalApiHealthy) { $blockers += 'The local Ollama API did not answer.' }
    if ($ModelsFailed -gt 0) { $blockers += ('{0} model tag(s) failed: {1}' -f $ModelsFailed, ($FailedTags -join ', ')) }
    if ($ModelsRequested -gt 0 -and $ModelsInstalled -lt $ModelsRequested) {
        $blockers += ('Only {0} of {1} requested models are installed.' -f $ModelsInstalled, $ModelsRequested)
    }
    if ($ModelsRequested -gt 0 -and -not $InferenceVerified) { $blockers += 'No installed model passed a local inference check.' }

    if ($RemoteRequested) {
        if (-not $RemoteConfigured) { $notes += 'Remote serving was not configured.' }
        elseif (-not $PrivateListenerHealthy) { $notes += 'Remote serving was configured but the private-LAN listener is not answering yet.' }
        elseif (-not $CoordinatorVerified) { $notes += 'Coordinator verification is still pending; run the coordinator utility to complete it.' }
    }

    $outcome = 'FULL_SUCCESS'
    if ($blockers.Count -gt 0) {
        $anythingWorked = ($OllamaInstalled -and $LocalApiHealthy -and $ModelsInstalled -gt 0)
        if ($anythingWorked) { $outcome = 'PARTIAL_SUCCESS' } else { $outcome = 'FAILED' }
    } elseif ($notes.Count -gt 0 -or @($PendingActions).Count -gt 0) {
        $outcome = 'PARTIAL_SUCCESS'
    }

    $ready = ($outcome -eq 'FULL_SUCCESS')
    $readyNote = 'This head is ready for Phase 4 integration.'
    if (-not $ready) {
        $readyNote = 'This head is NOT yet ready for Phase 4 integration. The items listed under WHAT STILL NEEDS ATTENTION explain why.'
    }

    return [pscustomobject]@{
        Outcome        = $outcome
        Blockers       = $blockers
        Notes          = $notes
        PendingActions = @($PendingActions)
        ReadyForPhase4 = $ready
        ReadyNote      = $readyNote
    }
}

function New-CapstoneStatusReport {
    <#
        .SYNOPSIS
            Writes LATEST-STATUS.txt and LATEST-STATUS.json.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [Parameter(Mandatory)]$Run
    )

    $outcome = $Run.Outcome
    $lines = @()
    $lines += '================================================================'
    $lines += '  CAPSTONE HEAD PROVISIONER - RESULT'
    $lines += '================================================================'
    $lines += ''
    $lines += ('  RESULT: {0}' -f $outcome.Outcome)
    $lines += ('  {0}' -f $outcome.ReadyNote)
    $lines += ''
    $lines += ('  Finished at   : {0}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
    $lines += ('  Run mode      : {0}' -f $Run.Mode)
    $lines += ('  Provisioner   : {0}' -f $Run.Version)
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  THE MACHINE'
    $lines += '----------------------------------------------------------------'
    $lines += ('  Computer name : {0}' -f $Run.ComputerName)
    $lines += ('  Windows       : {0} (build {1})' -f $Run.OsCaption, $Run.OsBuild)
    $lines += ('  CPU           : {0}' -f $Run.Cpu)
    $lines += ('  Installed RAM : {0} GB' -f $Run.RamGB)
    $lines += ('  GPU           : {0}' -f $Run.Gpu)
    $lines += ('  Model storage : {0}' -f $Run.ModelDirectory)
    $lines += ('  Free space    : {0}' -f $Run.FreeDisk)
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  NETWORK'
    $lines += '----------------------------------------------------------------'
    $lines += ('  Node number          : {0}' -f $Run.NodeNumber)
    $lines += ('  Private LAN address  : {0}' -f $Run.NodeIp)
    $lines += ('  Ethernet adapter     : {0}' -f $Run.AdapterSummary)
    $lines += ('  Address verified     : {0}' -f (Format-CapstoneYesNo $Run.NetworkVerified))
    $lines += ('  Verification detail  : {0}' -f $Run.NetworkSummary)
    $lines += ('  Coordinator contact  : {0}' -f $Run.CoordinatorNote)
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  OLLAMA'
    $lines += '----------------------------------------------------------------'
    $lines += ('  Installed            : {0}' -f (Format-CapstoneYesNo $Run.OllamaInstalled))
    $lines += ('  Version running      : {0}' -f $Run.OllamaVersion)
    $lines += ('  Executable           : {0}' -f $Run.OllamaExecutable)
    $lines += ('  Local API healthy    : {0}' -f (Format-CapstoneYesNo $Run.LocalApiHealthy))
    $lines += ('  Local API endpoint   : {0}' -f $Run.LocalApiEndpoint)
    $lines += ('  Installed this run   : {0}' -f (Format-CapstoneYesNo $Run.OllamaInstalledThisRun))
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  MODELS'
    $lines += '----------------------------------------------------------------'
    $lines += ('  Hardware profile     : {0}' -f $Run.ProfileName)
    $lines += ('  Models requested     : {0}' -f $Run.ModelsRequested)
    $lines += ('  Models installed     : {0}' -f $Run.ModelsInstalled)
    $lines += ('  Already present      : {0}' -f $Run.ModelsAlreadyPresent)
    $lines += ('  Downloaded this run  : {0}' -f $Run.ModelsDownloaded)
    $lines += ('  Models failed        : {0}' -f $Run.ModelsFailed)
    if (@($Run.FailedTags).Count -gt 0) {
        foreach ($tag in @($Run.FailedTags)) {
            $lines += ('    FAILED TAG         : {0}' -f $tag)
        }
    }
    $lines += ('  Local inference      : {0}' -f (Format-CapstoneYesNo $Run.InferenceVerified))
    $lines += ('  Models verified      : {0} of {1}' -f $Run.ModelsVerified, $Run.ModelsRequested)
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  REMOTE SERVING'
    $lines += '----------------------------------------------------------------'
    $lines += ('  Configured           : {0}' -f (Format-CapstoneYesNo $Run.RemoteConfigured))
    $lines += ('  Bound to             : {0}' -f $Run.RemoteBinding)
    $lines += ('  Private listener     : {0}' -f (Format-CapstoneYesNo $Run.PrivateListenerHealthy))
    $lines += ('  Firewall rule        : {0}' -f $Run.FirewallSummary)
    $lines += ('  Coordinator verified : {0}' -f (Format-CapstoneYesNo $Run.CoordinatorVerified))
    $lines += ('  Remote status        : {0}' -f $Run.RemoteStatus)
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  WHAT STILL NEEDS ATTENTION'
    $lines += '----------------------------------------------------------------'
    $attention = @()
    $attention += @($outcome.Blockers)
    $attention += @($outcome.Notes)
    $attention += @($outcome.PendingActions)
    if ($attention.Count -eq 0) {
        $lines += '  Nothing. No manual action is required on this machine.'
    } else {
        $number = 0
        foreach ($entry in $attention) {
            $number++
            $lines += ('  {0}. {1}' -f $number, $entry)
        }
    }
    $lines += ''
    $lines += '----------------------------------------------------------------'
    $lines += '  WHERE THE DETAIL IS'
    $lines += '----------------------------------------------------------------'
    $lines += ('  This summary    : {0}' -f $Paths.StatusText)
    $lines += ('  Machine-readable: {0}' -f $Paths.StatusJson)
    $lines += ('  Run log         : {0}' -f $Run.LogFile)
    $lines += ('  Preflight       : {0}' -f $Run.PreflightPath)
    $lines += ('  Network backup  : {0}' -f $Run.BackupPath)
    $lines += ('  Head inventory  : {0}' -f $Run.RegistrationPath)
    $lines += ''
    $lines += '================================================================'

    $text = ($lines -join [Environment]::NewLine)
    [System.IO.File]::WriteAllText($Paths.StatusText, $text, (New-Object System.Text.UTF8Encoding($false)))

    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    [System.IO.File]::WriteAllText((Join-Path $Paths.Reports ('status-{0}.txt' -f $stamp)), $text, (New-Object System.Text.UTF8Encoding($false)))

    $json = [ordered]@{
        schema_version = $script:SchemaVersion
        generated_at   = (Get-Date).ToString('o')
        result         = $outcome.Outcome
        ready_for_phase4 = $outcome.ReadyForPhase4
        run            = $Run
        attention      = $attention
    }
    Write-CapstoneJson -InputObject ([pscustomobject]$json) -Path $Paths.StatusJson

    return [pscustomobject]@{ Text = $text; TextPath = $Paths.StatusText; JsonPath = $Paths.StatusJson }
}

function Format-CapstoneYesNo {
    [CmdletBinding()]
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return 'unknown' }
    if ($Value -is [bool]) {
        if ($Value) { return 'YES' }
        return 'NO'
    }
    return [string]$Value
}

function New-CapstoneHeadRegistration {
    <#
        .SYNOPSIS
            Writes the versioned head record that Phase 4 will consume.
        .DESCRIPTION
            This file describes what was measured. It deliberately does not
            modify any Capstone routing configuration, and nothing here implies
            that Capstone is already routing work to this head.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [Parameter(Mandatory)]$Run,
        [AllowEmptyCollection()][array]$QueueItems = @()
    )

    $registration = [ordered]@{
        schema_version      = 2
        record_type         = 'capstone-head-registration'
        generated_at        = (Get-Date).ToString('o')
        provisioner_version = $Run.Version

        node_id             = $Run.NodeNumber
        hostname            = $Run.ComputerName
        ip_address          = $Run.NodeIp
        hardware_profile    = $Run.ProfileName

        cpu                 = $Run.Cpu
        ram_gb              = $Run.RamGB
        gpu                 = $Run.Gpu
        nvidia_vram_mb      = $Run.NvidiaVramMB

        ollama_version      = $Run.OllamaVersion
        ollama_endpoint     = $Run.RemoteEndpoint
        ollama_local_endpoint = $Run.LocalApiEndpoint
        model_directory     = $Run.ModelDirectory

        available_models    = @($QueueItems | Where-Object { $_.Status -in @('Verified', 'AlreadyInstalled', 'Downloaded') } | ForEach-Object {
            [ordered]@{
                tag = $_.Tag; kind = $_.Kind; family = $_.Family; role = $_.Role
                digest = $_.Digest; expected_bytes = $_.ExpectedBytes
                locally_verified = [bool]$_.InferenceOk
                inference_ms = $_.InferenceMs
            }
        })
        failed_models       = @($QueueItems | Where-Object { $_.Status -in @('Failed', 'InsufficientDisk', 'Unsupported') } | ForEach-Object {
            [ordered]@{ tag = $_.Tag; status = $_.Status; category = $_.FailureCategory; error = $_.LastError; attempts = $_.Attempts }
        })

        locally_verified_count = @($QueueItems | Where-Object { $_.InferenceOk }).Count
        remote_verification = $Run.RemoteStatus
        coordinator_verified = [bool]$Run.CoordinatorVerified
        service_health      = $Run.MachineState
        last_verification   = (Get-Date).ToString('o')

        capabilities        = @($QueueItems | Where-Object { $_.Status -in @('Verified', 'AlreadyInstalled', 'Downloaded') } | ForEach-Object { $_.Kind } | Select-Object -Unique)

        phase4_note         = 'This record is inventory and health data only. No Capstone model-routing configuration was modified by provisioning.'
    }

    $file = Join-Path $Paths.Inventory ('head-{0}.json' -f $Run.NodeNumber)
    Write-CapstoneJson -InputObject ([pscustomobject]$registration) -Path $file
    Write-CapstoneJson -InputObject ([pscustomobject]$registration) -Path (Join-Path $Paths.Inventory 'head-LATEST.json')
    return [pscustomobject]@{ Path = $file; Record = [pscustomobject]$registration }
}

Export-ModuleMember -Function @(
    'Get-CapstoneOverallOutcome', 'New-CapstoneStatusReport', 'Format-CapstoneYesNo', 'New-CapstoneHeadRegistration'
)
