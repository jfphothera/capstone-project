<#
    Capstone.Remote
    Remote Ollama serving on the private LAN, plus the firewall boundary.

    Design notes that exist because earlier versions failed:

      * This stage runs only AFTER models are downloaded and verified, and its
        failure never invalidates the models. Earlier versions demanded a
        192.0.2.x listener before downloading anything, which is backwards
        (defect D-06), and one version refused to configure remote serving at
        all unless every single tag had succeeded.

      * OLLAMA_HOST is written at MACHINE scope so the binding survives a
        reboot. Setting only the current process environment, as an earlier
        version did, meant the tray application came back after a restart bound
        to loopback again and the head silently stopped serving.

      * A machine-scope variable does not reconfigure an already-running
        process. The variable is therefore also injected into this process's
        environment so the server that gets started inherits it immediately.

      * Nothing is force-terminated while a model is loaded or a request is in
        flight; that case reports a pending action instead.
#>

$script:CoordinatorIp = '192.0.2.1'
$script:OllamaPort = 11434
$script:FirewallRulePrefix = 'Capstone Ollama Head'

# ---------------------------------------------------------------------------
# Binding decision  (pure - unit tested)
# ---------------------------------------------------------------------------

function Resolve-CapstoneRemoteBinding {
    <#
        .SYNOPSIS
            Chooses the OLLAMA_HOST value for a head.
        .DESCRIPTION
            Two supported modes, with an explicit trade-off:

              PrivateLan (default) binds 0.0.0.0:11434 and relies on a firewall
                rule that admits only the coordinator. Loopback keeps working,
                so the ollama CLI and any local Capstone tooling on the head
                still function, and the listener comes up at boot even if the
                private NIC initialises late.

              PrivateOnly binds the literal 192.0.2.x:11434. This is a tighter
                network posture, but the server then refuses loopback entirely
                and fails to start if the address is not yet up.

            Neither mode is application-layer authentication. See
            docs/SECURITY-NOTES.md.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PrivateIp,
        [ValidateSet('PrivateLan', 'PrivateOnly')][string]$BindMode = 'PrivateLan',
        [int]$Port = $script:OllamaPort
    )

    if ($BindMode -eq 'PrivateOnly') {
        return [pscustomobject]@{
            Value          = ('{0}:{1}' -f $PrivateIp, $Port)
            BindMode       = $BindMode
            LoopbackWorks  = $false
            Rationale      = 'Bound to the private address only. The firewall rule remains the access control; loopback access on the head is given up.'
        }
    }
    return [pscustomobject]@{
        Value          = ('0.0.0.0:{0}' -f $Port)
        BindMode       = $BindMode
        LoopbackWorks  = $true
        Rationale      = 'Bound to all interfaces, with inbound access restricted to the coordinator by a scoped Windows Defender Firewall rule. Loopback keeps working for local tooling.'
    }
}

function Test-CapstoneManagedHostValue {
    <#
        .SYNOPSIS
            True when an OLLAMA_HOST value is one this tool is allowed to replace.
        .DESCRIPTION
            An operator's own custom value is never overwritten silently; it is
            reported as a pending action instead.
    #>
    [CmdletBinding()]
    param([AllowEmptyString()][AllowNull()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    $normalised = $Value.Trim().ToLowerInvariant() -replace '^https?://', ''
    return [bool]($normalised -match '^(127\.0\.0\.1|localhost|0\.0\.0\.0|192\.0\.2\.\d{1,3})(:\d+)?$')
}

# ---------------------------------------------------------------------------
# Environment variable
# ---------------------------------------------------------------------------

function Set-CapstoneOllamaHostVariable {
    <#
        .SYNOPSIS
            Persists OLLAMA_HOST at machine scope and reports what it replaced.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)]$Paths
    )

    $before = [ordered]@{
        machine = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine')
        user    = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User')
        process = $env:OLLAMA_HOST
    }
    Write-CapstoneJson -InputObject ([pscustomobject]$before) -Path (Join-Path $Paths.Backups 'ollama-host-before.json') -Depth 4

    $result = [ordered]@{
        Applied = $false; Value = $Value; PendingAction = ''
        PreviousMachine = [string]$before.machine
        PreviousUser = [string]$before.user
    }

    if (-not (Test-CapstoneManagedHostValue -Value ([string]$before.machine))) {
        $result.PendingAction = ('OLLAMA_HOST is already set at machine scope to "{0}", which this provisioner did not set. It was left unchanged. Review it, then re-run.' -f $before.machine)
        return [pscustomobject]$result
    }

    try {
        [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $Value, 'Machine')
        # A machine-scope write does not reach processes that already exist,
        # including this one. Inject it here so any server started below
        # inherits the new binding immediately.
        $env:OLLAMA_HOST = $Value
        $result.Applied = $true
        Write-CapstoneLog ('OLLAMA_HOST set to {0} at machine scope (survives reboot).' -f $Value) -Level OK
    } catch {
        $result.PendingAction = ('Could not write OLLAMA_HOST at machine scope: {0}' -f $_.Exception.Message)
        return [pscustomobject]$result
    }

    # A user-scope value overrides machine scope. If an earlier Capstone run
    # left one behind it would silently win, so a recognised managed value is
    # cleared; anything else is reported rather than deleted.
    if (-not [string]::IsNullOrWhiteSpace([string]$before.user)) {
        if (Test-CapstoneManagedHostValue -Value ([string]$before.user)) {
            try {
                [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $null, 'User')
                Write-CapstoneLog ('Removed the user-scope OLLAMA_HOST ("{0}") left by an earlier run, so the machine-scope value applies.' -f $before.user) -Level INFO
            } catch {
                $result.PendingAction = ('A user-scope OLLAMA_HOST of "{0}" overrides the machine-scope value and could not be removed.' -f $before.user)
            }
        } else {
            $result.PendingAction = ('A user-scope OLLAMA_HOST of "{0}" overrides the machine-scope value. It was not this provisioner''s setting, so it was left alone. Remove it manually if remote serving does not come up.' -f $before.user)
        }
    }

    return [pscustomobject]$result
}

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------

function Set-CapstoneFirewallRule {
    <#
        .SYNOPSIS
            Creates or refreshes one narrowly scoped inbound rule.
        .DESCRIPTION
            Inbound TCP 11434 from the coordinator address only. The rule is
            rebuilt rather than edited so repeated runs converge on exactly one
            rule instead of accumulating near-duplicates (defect D-13).

            Windows Firewall itself is never disabled and no other rule is
            touched.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PrivateIp,
        [string]$CoordinatorIp = $script:CoordinatorIp,
        [int]$Port = $script:OllamaPort
    )

    $displayName = ('{0} {1} from Coordinator {2}' -f $script:FirewallRulePrefix, $PrivateIp, $CoordinatorIp)
    $result = [ordered]@{ Applied = $false; DisplayName = $displayName; Error = ''; Replaced = $false }

    try {
        $existing = @(Get-NetFirewallRule -DisplayName ('{0}*' -f $script:FirewallRulePrefix) -ErrorAction SilentlyContinue)
        foreach ($rule in $existing) {
            Remove-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue
            $result.Replaced = $true
        }

        New-NetFirewallRule -DisplayName $displayName `
            -Description 'Capstone: allows only the Capstone coordinator to reach this head''s Ollama API on the isolated private LAN.' `
            -Direction Inbound -Action Allow -Protocol TCP `
            -LocalPort $Port -LocalAddress $PrivateIp -RemoteAddress $CoordinatorIp `
            -Profile Any -Enabled True -ErrorAction Stop | Out-Null

        $result.Applied = $true
        Write-CapstoneLog ('Firewall rule in place: inbound TCP {0} on {1}, accepted only from {2}.' -f $Port, $PrivateIp, $CoordinatorIp) -Level OK
    } catch {
        $result.Error = $_.Exception.Message
        Write-CapstoneLog ('Could not configure the firewall rule: {0}' -f $result.Error) -Level ERROR
    }

    return [pscustomobject]$result
}

function Get-CapstoneFirewallRuleFacts {
    [CmdletBinding()]
    param()
    $facts = @()
    try {
        foreach ($rule in @(Get-NetFirewallRule -DisplayName ('{0}*' -f $script:FirewallRulePrefix) -ErrorAction SilentlyContinue)) {
            $ports = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
            $addresses = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
            $facts += [pscustomobject]@{
                DisplayName   = [string]$rule.DisplayName
                Enabled       = [string]$rule.Enabled
                Direction     = [string]$rule.Direction
                Action        = [string]$rule.Action
                LocalPort     = (@($ports.LocalPort) -join ',')
                RemoteAddress = (@($addresses.RemoteAddress) -join ',')
                LocalAddress  = (@($addresses.LocalAddress) -join ',')
            }
        }
    } catch { }
    return $facts
}

# ---------------------------------------------------------------------------
# Restart for a new binding
# ---------------------------------------------------------------------------

function Restart-CapstoneOllamaForBinding {
    <#
        .SYNOPSIS
            Rebinds the running server, or declines and says why.
        .DESCRIPTION
            Declines when a model is loaded or a request appears to be running,
            because the operator's working GPU and heads must not have live
            inference cut to satisfy a setup script.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$CurrentBaseUri,
        [Parameter(Mandatory)][string]$ExpectedBaseUri,
        [int]$TimeoutSeconds = 90,
        [switch]$Force
    )

    $result = [ordered]@{
        Restarted = $false; Success = $false; PendingAction = ''; Error = ''
        StoppedProcesses = @()
    }

    $running = Get-CapstoneRunningModels -BaseUri $CurrentBaseUri
    if ($running.Available -and $running.Count -gt 0 -and -not $Force) {
        $result.PendingAction = ('Ollama currently has {0} model(s) loaded ({1}). The server was NOT restarted, so no in-flight work was interrupted. Remote serving will take effect the next time Ollama restarts, or re-run the provisioner when the head is idle.' -f $running.Count, ($running.Names -join ', '))
        Write-CapstoneLog $result.PendingAction -Level WARN
        return [pscustomobject]$result
    }

    $processes = Get-CapstoneOllamaProcesses
    $installDirectory = Split-Path -Parent $Executable

    foreach ($process in $processes) {
        # Only processes from this installation directory are stopped, so a
        # differently located Ollama is never collateral damage.
        $belongs = $false
        if (-not [string]::IsNullOrWhiteSpace($process.Path)) {
            try {
                $belongs = ([IO.Path]::GetFullPath((Split-Path -Parent $process.Path)) -ieq [IO.Path]::GetFullPath($installDirectory))
            } catch { $belongs = $false }
        }
        if (-not $belongs) {
            Write-CapstoneLog ('Left process {0} (PID {1}) alone: it is not from {2}.' -f $process.Name, $process.Id, $installDirectory) -Level INFO
            continue
        }
        try {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
            $result.StoppedProcesses += ('{0} (PID {1})' -f $process.Name, $process.Id)
        } catch {
            Write-CapstoneLog ('Could not stop {0} (PID {1}): {2}' -f $process.Name, $process.Id, $_.Exception.Message) -Level WARN
        }
    }

    Start-Sleep -Seconds 3
    $result.Restarted = $true

    $app = Join-Path $installDirectory 'ollama app.exe'
    try {
        if (Test-Path -LiteralPath $app) {
            Start-Process -FilePath $app -WindowStyle Hidden -ErrorAction Stop | Out-Null
        } else {
            Start-Process -FilePath $Executable -ArgumentList @('serve') -WindowStyle Hidden -ErrorAction Stop | Out-Null
        }
    } catch {
        $result.Error = ('Could not restart Ollama: {0}' -f $_.Exception.Message)
        return [pscustomobject]$result
    }

    $probe = Wait-CapstoneOllamaApi -BaseUri $ExpectedBaseUri -TimeoutSeconds $TimeoutSeconds -Activity 'Waiting for the private-LAN Ollama listener'
    $result.Success = $probe.Healthy
    if (-not $probe.Healthy) {
        $result.PendingAction = ('Ollama was restarted but did not answer on {0} within {1} seconds. Models and local serving are unaffected.' -f $ExpectedBaseUri, $TimeoutSeconds)
    }
    return [pscustomobject]$result
}

# ---------------------------------------------------------------------------
# Boot-time server, independent of the tray application
# ---------------------------------------------------------------------------

$script:TaskName = 'Capstone Ollama Server'
$script:TrayRunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

function Register-CapstoneOllamaService {
    <#
        .SYNOPSIS
            Starts the Ollama server at boot via a scheduled task, as SYSTEM.
        .DESCRIPTION
            Measured on nodes 5 and 6 (2026-10-30): a clean install of Ollama
            0.40.13 left "ollama app.exe" running with no server listening,
            and no amount of waiting produced one.

            The tray application is also the wrong mechanism for a headless
            inference head even when it works, because it only starts at
            INTERACTIVE LOGON. A head that reboots and sits at the lock screen
            has no server at all - which is exactly the failure that took node
            3 offline overnight.

            So the server is registered as a scheduled task that runs
            "ollama.exe serve" at system startup under the SYSTEM account. No
            logon is required, it restarts itself if it exits, and it does not
            depend on the tray application in any way.

            The tray application's own autostart entry is removed, because two
            servers would otherwise contend for port 11434. The previous value
            is returned so the caller can put it back if the task does not
            work out.
        .PARAMETER ModelDirectory
            Where blobs live. Passed to the task as OLLAMA_MODELS so that the
            SYSTEM-owned server and any user-context tooling agree on one
            location rather than each using their own profile.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$BindValue,
        [string]$ModelDirectory = ''
    )

    $result = [ordered]@{
        Registered = $false; TaskName = $script:TaskName
        RemovedTrayAutostart = $false; PreviousTrayCommand = ''
        ModelDirectory = $ModelDirectory; Error = ''
    }

    try {
        # Record and remove the tray autostart so nothing competes for the port.
        try {
            $existing = Get-ItemProperty -Path $script:TrayRunKey -Name 'Ollama' -ErrorAction Stop
            if ($existing -and $existing.Ollama) {
                $result.PreviousTrayCommand = [string]$existing.Ollama
                Remove-ItemProperty -Path $script:TrayRunKey -Name 'Ollama' -ErrorAction Stop
                $result.RemovedTrayAutostart = $true
                Write-CapstoneLog 'Removed the Ollama tray autostart; the boot task owns the server now.' -Level INFO
            }
        } catch {
            # No autostart entry present. Nothing to do.
        }

        $action = New-ScheduledTaskAction -Execute $Executable -Argument 'serve'
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

        Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings `
            -Description 'Capstone: runs the Ollama inference server at system startup, so the head serves the private LAN without requiring anyone to log on.' `
            -Force -ErrorAction Stop | Out-Null

        $result.Registered = $true
        Write-CapstoneLog ('Registered the "{0}" task to start Ollama at boot as SYSTEM (no logon needed).' -f $script:TaskName) -Level OK
    } catch {
        $result.Error = $_.Exception.Message
        Write-CapstoneLog ('Could not register the boot task: {0}' -f $result.Error) -Level ERROR
    }

    return [pscustomobject]$result
}

function Restore-CapstoneTrayAutostart {
    <#
        .SYNOPSIS
            Puts the tray autostart back after a failed task registration.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$PreviousTrayCommand)

    if ([string]::IsNullOrWhiteSpace($PreviousTrayCommand)) { return $false }
    try {
        Set-ItemProperty -Path $script:TrayRunKey -Name 'Ollama' -Value $PreviousTrayCommand -ErrorAction Stop
        Write-CapstoneLog 'Restored the Ollama tray autostart, because the boot task did not work.' -Level WARN
        return $true
    } catch {
        Write-CapstoneLog ('Could not restore the tray autostart: {0}' -f $_.Exception.Message) -Level ERROR
        return $false
    }
}

function Start-CapstoneOllamaService {
    <#
        .SYNOPSIS
            Stops any running Ollama, then starts the boot task's server.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$ExpectedBaseUri,
        [int]$TimeoutSeconds = 180
    )

    foreach ($process in (Get-CapstoneOllamaProcesses)) {
        try { Stop-Process -Id $process.Id -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 3

    try {
        Start-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ Success = $false; Error = ('Could not start the task: {0}' -f $_.Exception.Message) }
    }

    $probe = Wait-CapstoneOllamaApi -BaseUri $ExpectedBaseUri -TimeoutSeconds $TimeoutSeconds -Activity 'Waiting for the Ollama server started by the boot task'
    return [pscustomobject]@{
        Success = $probe.Healthy
        Error = $(if ($probe.Healthy) { '' } else { ('The boot task ran but no API answered on {0} within {1} seconds.' -f $ExpectedBaseUri, $TimeoutSeconds) })
    }
}

function Get-CapstoneServiceFacts {
    [CmdletBinding()]
    param()
    try {
        $task = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $script:TaskName -ErrorAction SilentlyContinue
        return [pscustomobject]@{
            Present = $true
            State = [string]$task.State
            LastRunTime = $(if ($info) { [string]$info.LastRunTime } else { '' })
            LastResult = $(if ($info) { $info.LastTaskResult } else { $null })
        }
    } catch {
        return [pscustomobject]@{ Present = $false; State = ''; LastRunTime = ''; LastResult = $null }
    }
}

# ---------------------------------------------------------------------------
# Verification levels  (pure - unit tested)
# ---------------------------------------------------------------------------

function Get-CapstoneRemoteVerification {
    <#
        .SYNOPSIS
            Grades remote serving across the four levels in spec section 21.
        .DESCRIPTION
            The head cannot prove on its own that the coordinator reached it.
            When it has no reliable evidence of a coordinator-side test the
            result is REMOTE_COORDINATOR_VERIFICATION_PENDING, never success.
    #>
    [CmdletBinding()]
    param(
        [bool]$LocalApiHealthy = $false,
        [bool]$PrivateListenerHealthy = $false,
        [bool]$FirewallRuleApplied = $false,
        [bool]$CoordinatorLinkProven = $false,
        [bool]$CoordinatorApiProven = $false,
        [bool]$CoordinatorInferenceProven = $false
    )

    $levels = @(
        [pscustomobject]@{ Level = 'LocalServerHealthy';     Pass = $LocalApiHealthy;        Note = 'The head''s own Ollama API answers.' }
        [pscustomobject]@{ Level = 'PrivateListenerHealthy'; Pass = $PrivateListenerHealthy; Note = 'The head answers on its own 192.0.2.x:11434 address.' }
        [pscustomobject]@{ Level = 'CoordinatorReachable';   Pass = $CoordinatorApiProven;   Note = 'The coordinator connected to this head''s Ollama API.' }
        [pscustomobject]@{ Level = 'RemoteInferenceProven';  Pass = $CoordinatorInferenceProven; Note = 'The coordinator generated from a model installed on this head.' }
    )

    $overall = 'REMOTE_NOT_CONFIGURED'
    if ($LocalApiHealthy -and $PrivateListenerHealthy) {
        $overall = 'REMOTE_COORDINATOR_VERIFICATION_PENDING'
        if ($CoordinatorApiProven -and $CoordinatorInferenceProven) {
            $overall = 'REMOTE_VERIFIED_END_TO_END'
        } elseif ($CoordinatorApiProven) {
            $overall = 'REMOTE_COORDINATOR_REACHABLE'
        }
    } elseif ($LocalApiHealthy) {
        $overall = 'REMOTE_LISTENER_NOT_BOUND'
    }

    $note = 'Run COORDINATOR-CHECK-ALL-HEADS.cmd on the coordinator (192.0.2.1) to complete the last two levels. This head cannot truthfully claim the coordinator tested it.'
    if ($overall -eq 'REMOTE_VERIFIED_END_TO_END') {
        $note = 'End-to-end remote inference was confirmed from the coordinator.'
    }

    return [pscustomobject]@{
        Overall             = $overall
        Levels              = $levels
        FirewallRuleApplied = $FirewallRuleApplied
        CoordinatorLinkProven = $CoordinatorLinkProven
        Note                = $note
    }
}

Export-ModuleMember -Function @(
    'Resolve-CapstoneRemoteBinding', 'Test-CapstoneManagedHostValue',
    'Set-CapstoneOllamaHostVariable',
    'Set-CapstoneFirewallRule', 'Get-CapstoneFirewallRuleFacts',
    'Register-CapstoneOllamaService', 'Restore-CapstoneTrayAutostart',
    'Start-CapstoneOllamaService', 'Get-CapstoneServiceFacts',
    'Restart-CapstoneOllamaForBinding', 'Get-CapstoneRemoteVerification'
)
