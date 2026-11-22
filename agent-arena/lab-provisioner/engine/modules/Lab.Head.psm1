<#
    Lab.Head - inference-head configuration: address, time, power, the Ollama server at boot,
    restart and reboot proof, and the undo of each change.

    Reused unchanged from provisioner 1.0.3 (engine\head-engine): adapter selection, the
    address-conflict probes, the network backup, the atomic netsh apply and its polled
    verification, the Ollama installer with its signature check, the API probes.

    Replaced here, because the assessment found them wrong:
      * the boot task (1.0.3 ran "ollama serve" as SYSTEM with no OLLAMA_MODELS, so after a
        reboot the server could look in the SYSTEM profile and serve no models). The task now
        runs a small wrapper that sets OLLAMA_HOST and an explicit OLLAMA_MODELS, chosen from
        where the models actually are, never moving or deleting any;
      * the OLLAMA_HOST record (1.0.3 overwrote the "before" record on every run). Each run
        keeps its own backup;
      * Ollama is restarted only on an approved maintenance action and never while a model is
        loaded unless the operator says so (1.0.3 stopped every Ollama process on every run);
      * time and power (absent from 1.0.3; the D: FIX had them without backup) now have
        backup and undo.
#>

# ---------------------------------------------------------------------------
# Engine paths
# ---------------------------------------------------------------------------

function Get-LabEnginePaths {
    param([Parameter(Mandatory)]$Run)
    $paths = Get-CapstonePaths -Root (Join-Path $Run.Root 'head-engine')
    Initialize-CapstonePaths -Paths $paths | Out-Null
    return $paths
}

function ConvertTo-LabMask {
    param([int]$PrefixLength = 24)
    $bits = ('1' * $PrefixLength).PadRight(32, '0')
    return (0..3 | ForEach-Object { [Convert]::ToInt32($bits.Substring($_ * 8, 8), 2) }) -join '.'
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

function Get-LabAdapterChoice {
    param([Parameter(Mandatory)][string]$TargetIp, [string]$PreferMac = '')
    $adapters = @(Get-CapstoneAdapterInventory)
    return (Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp $TargetIp -PreferMacAddress $PreferMac)
}

function Set-LabHeadNetwork {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$TargetIp, [string]$PreferMac = '')
    $choice = Get-LabAdapterChoice -TargetIp $TargetIp -PreferMac $PreferMac
    if ($choice.Status -eq 'AlreadyConfigured') { return (New-LabOutcome 'UNCHANGED' $choice.Reason) }
    if ($choice.Status -ne 'Selected') { throw ('no adapter can be configured: ' + $choice.Reason) }
    $paths = Get-LabEnginePaths -Run $Run
    $backup = New-CapstoneNetworkBackup -Paths $paths -Adapter $choice.Adapter -TargetIp $TargetIp -NodeNumber (Get-LabHostNumber $TargetIp)
    Copy-Item -LiteralPath $backup.Path -Destination (Join-Path $Run.Backups (Split-Path -Leaf $backup.Path)) -Force
    $apply = Set-CapstonePrivateAddress -InterfaceIndex $choice.Adapter.InterfaceIndex -TargetIp $TargetIp
    Register-LabChange -Kind 'network' -Description ('{0} set to {1}/24 (was {2})' -f $choice.Adapter.Name, $TargetIp, (@($choice.Adapter.IPv4Addresses | ForEach-Object { $_.IPAddress }) -join ', ')) -Backup $backup.Path -Undo ([pscustomobject]@{ type = 'network'; backup = $backup.Path }) | Out-Null
    if (-not $apply.Success) { throw ('the address did not verify on {0}: {1}' -f $choice.Adapter.Name, (($apply.Verification | ConvertTo-Json -Depth 4 -Compress))) }
    return (New-LabOutcome 'DONE' ('{0} holds {1}/24, verified ({2})' -f $choice.Adapter.Name, $TargetIp, $choice.Reason) $choice.Adapter.Name)
}

function Undo-LabHeadNetwork {
    param([Parameter(Mandatory)][string]$BackupFile)
    $record = Get-Content -LiteralPath $BackupFile -Raw | ConvertFrom-Json
    $plan = Resolve-CapstoneRollbackPlan -Backup $record
    $idx = $plan.InterfaceIndex
    if ($plan.Action -eq 'RestoreDhcp') {
        $null = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'set', 'address', ('name={0}' -f $idx), 'source=dhcp')
        $null = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'set', 'dnsservers', ('name={0}' -f $idx), 'source=dhcp')
        return ('adapter {0} returned to DHCP' -f $plan.InterfaceAlias)
    }
    $first = $plan.RestoreAddresses[0]
    $gw = 'none'; if ($plan.RestoreGateway) { $gw = $plan.RestoreGateway }
    $r = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'set', 'address', ('name={0}' -f $idx), 'source=static', ('address={0}' -f $first.ip), ('mask={0}' -f (ConvertTo-LabMask ([int]$first.prefix))), ('gateway={0}' -f $gw), 'store=persistent')
    if ($r.ExitCode -ne 0) { throw ('netsh could not restore the address: ' + $r.Output) }
    $dns = @($plan.RestoreDns)
    if ($dns.Count) {
        $null = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'set', 'dnsservers', ('name={0}' -f $idx), 'source=static', ('address={0}' -f $dns[0]), 'register=primary', 'validate=no')
        for ($i = 1; $i -lt $dns.Count; $i++) { $null = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'add', 'dnsservers', ('name={0}' -f $idx), ('address={0}' -f $dns[$i]), ('index={0}' -f ($i + 1)), 'validate=no') }
    }
    return ('adapter {0} restored to {1}/{2}' -f $plan.InterfaceAlias, $first.ip, $first.prefix)
}

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

$script:W32Key = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Parameters'

function Get-LabTimeState {
    $p = $null; try { $p = Get-ItemProperty -Path $script:W32Key -ErrorAction Stop } catch { }
    $svc = Get-Service w32time -ErrorAction SilentlyContinue
    $start = ''; try { $start = [string](Get-CimInstance Win32_Service -Filter "Name='w32time'" -ErrorAction Stop).StartMode } catch { }
    return [pscustomobject][ordered]@{ TimeZone = (Get-TimeZone).Id; NtpServer = $(if ($p) { [string]$p.NtpServer } else { '' }); Type = $(if ($p) { [string]$p.Type } else { '' })
                                       Service = $(if ($svc) { [string]$svc.Status } else { 'missing' }); StartMode = $start }
}

function Test-LabTimeCompliant {
    param([Parameter(Mandatory)]$State, [Parameter(Mandatory)][string]$Hub, [Parameter(Mandatory)][string]$Zone)
    return ($State.TimeZone -eq $Zone -and $State.Type -eq 'NTP' -and ([string]$State.NtpServer) -match ('^' + [regex]::Escape($Hub) + '(,0x\w+)?$') -and $State.StartMode -eq 'Auto')
}

function Set-LabTimeSync {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Hub, [Parameter(Mandatory)][string]$Zone)
    $before = Get-LabTimeState
    if (Test-LabTimeCompliant -State $before -Hub $Hub -Zone $Zone) { return (New-LabOutcome 'UNCHANGED' ("$Zone, Windows Time syncing from $Hub")) }
    $file = Join-Path $Run.Backups 'time-before.json'; Save-LabJson -InputObject $before -Path $file
    if ($before.TimeZone -ne $Zone) { Set-TimeZone -Id $Zone -ErrorAction Stop }
    Set-Service w32time -StartupType Automatic -ErrorAction Stop
    Start-Service w32time -ErrorAction SilentlyContinue
    $out = & w32tm.exe /config ("/manualpeerlist:{0},0x8" -f $Hub) /syncfromflags:manual /reliable:no /update 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('w32tm /config failed: ' + ($out -join ' ')) }
    Restart-Service w32time -ErrorAction Stop
    & w32tm.exe /resync /nowait 2>&1 | Out-Null
    Register-LabChange -Kind 'time' -Description ("time zone $Zone (was $($before.TimeZone)); Windows Time from $Hub (was '$($before.NtpServer)' $($before.Type), start $($before.StartMode))") -Backup $file -Undo ([pscustomobject]@{ type = 'time'; before = $before }) | Out-Null
    return (New-LabOutcome 'DONE' ("$Zone; Windows Time syncs from $Hub (retries until the hub answers)"))
}

function Undo-LabTimeSync {
    param([Parameter(Mandatory)]$Before)
    if ($Before.TimeZone) { Set-TimeZone -Id $Before.TimeZone -ErrorAction Stop }
    if ($Before.NtpServer) { Set-ItemProperty -Path $script:W32Key -Name NtpServer -Value $Before.NtpServer -ErrorAction Stop }
    if ($Before.Type) { Set-ItemProperty -Path $script:W32Key -Name Type -Value $Before.Type -ErrorAction Stop }
    $mode = @{ Auto = 'Automatic'; Manual = 'Manual'; Disabled = 'Disabled' }[[string]$Before.StartMode]
    if ($mode) { Set-Service w32time -StartupType $mode -ErrorAction SilentlyContinue }
    & w32tm.exe /config /update 2>&1 | Out-Null
    if ($Before.Service -eq 'Running') { Restart-Service w32time -ErrorAction SilentlyContinue } elseif ($Before.Service -eq 'Stopped') { Stop-Service w32time -ErrorAction SilentlyContinue }
    return ('time zone {0}, Windows Time source "{1}" restored' -f $Before.TimeZone, $Before.NtpServer)
}

function Get-LabTimeOffset {
    <#  Offset to the hub in seconds, measured with one NTP sample; $null when the hub does not answer.  #>
    param([Parameter(Mandatory)][string]$Hub)
    $out = & w32tm.exe /stripchart ("/computer:{0}" -f $Hub) /samples:1 /dataonly 2>&1 | Out-String
    if ($out -match ',\s*([+-]?\d+(\.\d+)?)s') { return [double]$Matches[1] }
    return $null
}

# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------

function Get-LabPowerIndex {
    param([Parameter(Mandatory)][string]$Sub, [Parameter(Mandatory)][string]$Setting)
    $text = & powercfg.exe /q SCHEME_CURRENT $Sub $Setting 2>$null | Out-String
    if ($text -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') { return [Convert]::ToInt32($Matches[1], 16) }
    return $null
}

function Get-LabPowerState {
    param([string]$Adapter = '')
    $nic = $null; $eee = @()
    if ($Adapter) {
        try { $pm = Get-NetAdapterPowerManagement -Name $Adapter -ErrorAction Stop; $nic = [string]$pm.AllowComputerToTurnOffDevice } catch { $nic = 'unavailable' }
        $eee = @(Get-NetAdapterAdvancedProperty -Name $Adapter -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Energy.Efficient|Green Ethernet' } | ForEach-Object { [pscustomobject]@{ DisplayName = $_.DisplayName; DisplayValue = $_.DisplayValue } })
    }
    return [pscustomobject][ordered]@{ StandbyAcSeconds = (Get-LabPowerIndex 'SUB_SLEEP' 'STANDBYIDLE'); HibernateAcSeconds = (Get-LabPowerIndex 'SUB_SLEEP' 'HIBERNATEIDLE')
                                       MonitorAcSeconds = (Get-LabPowerIndex 'SUB_VIDEO' 'VIDEOIDLE'); LidAc = (Get-LabPowerIndex 'SUB_BUTTONS' 'LIDACTION')
                                       Adapter = $Adapter; AdapterPowerOff = $nic; Eee = $eee }
}

function Test-LabPowerCompliant {
    <#  -StayOn (the hub): also screen on and the lid does nothing, as SET-UP-THIS-COMPUTER sets them.  #>
    param([Parameter(Mandatory)]$State, [switch]$StayOn)
    $eeeOn = @($State.Eee | Where-Object { $_.DisplayValue -notmatch '^(Off|Disabled)$' }).Count
    $base = ($State.StandbyAcSeconds -eq 0 -and ($State.HibernateAcSeconds -eq 0 -or $null -eq $State.HibernateAcSeconds) -and ($State.AdapterPowerOff -in 'Disabled', 'Unsupported', 'unavailable', '', $null) -and $eeeOn -eq 0)
    if (-not $StayOn) { return $base }
    return ($base -and $State.MonitorAcSeconds -eq 0 -and ($State.LidAc -eq 0 -or $null -eq $State.LidAc))
}

function Set-LabPowerState {
    param([Parameter(Mandatory)]$Run, [string]$Adapter = '', [switch]$StayOn)
    $before = Get-LabPowerState -Adapter $Adapter
    if (Test-LabPowerCompliant -State $before -StayOn:$StayOn) { return (New-LabOutcome 'UNCHANGED' ('no sleep or hibernate on AC power; Ethernet power saving off' + $(if ($StayOn) { '; screen on; lid does nothing' } else { '' }))) }
    $file = Join-Path $Run.Backups 'power-before.json'; Save-LabJson -InputObject $before -Path $file
    & powercfg.exe /change standby-timeout-ac 0 | Out-Null
    & powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
    $notes = @()
    if ($StayOn) {
        & powercfg.exe /change monitor-timeout-ac 0 | Out-Null
        & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
        & powercfg.exe /setactive SCHEME_CURRENT | Out-Null
        $notes += ('screen stays on (was {0}s); lid does nothing (was {1})' -f $before.MonitorAcSeconds, $before.LidAc)
    }
    if ($Adapter -and $before.AdapterPowerOff -eq 'Enabled') { try { Disable-NetAdapterPowerManagement -Name $Adapter -NoRestart -ErrorAction Stop; $notes += "'$Adapter' may no longer be turned off to save power" } catch { $notes += ("could not change power management on '$Adapter': " + $_.Exception.Message) } }
    foreach ($p in @($before.Eee | Where-Object { $_.DisplayValue -notmatch '^(Off|Disabled)$' })) {
        try { Set-NetAdapterAdvancedProperty -Name $Adapter -DisplayName $p.DisplayName -DisplayValue 'Off' -NoRestart -ErrorAction Stop; $notes += "$($p.DisplayName) off" }
        catch { try { Set-NetAdapterAdvancedProperty -Name $Adapter -DisplayName $p.DisplayName -DisplayValue 'Disabled' -NoRestart -ErrorAction Stop; $notes += "$($p.DisplayName) disabled" } catch { $notes += "$($p.DisplayName) could not be changed" } }
    }
    Register-LabChange -Kind 'power' -Description ('no sleep/hibernate on AC (were {0}s/{1}s); {2}' -f $before.StandbyAcSeconds, $before.HibernateAcSeconds, ($notes -join '; ')) -Backup $file -Undo ([pscustomobject]@{ type = 'power'; before = $before }) | Out-Null
    return (New-LabOutcome 'DONE' ('no sleep or hibernate on AC power; ' + ($notes -join '; ')))
}

function Undo-LabPowerState {
    param([Parameter(Mandatory)]$Before)
    if ($null -ne $Before.StandbyAcSeconds) { & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE ([int]$Before.StandbyAcSeconds) | Out-Null }
    if ($null -ne $Before.HibernateAcSeconds) { & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE ([int]$Before.HibernateAcSeconds) | Out-Null }
    if ($Before.PSObject.Properties.Name -contains 'MonitorAcSeconds' -and $null -ne $Before.MonitorAcSeconds) { & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_VIDEO VIDEOIDLE ([int]$Before.MonitorAcSeconds) | Out-Null }
    if ($Before.PSObject.Properties.Name -contains 'LidAc' -and $null -ne $Before.LidAc) { & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION ([int]$Before.LidAc) | Out-Null }
    & powercfg.exe /setactive SCHEME_CURRENT | Out-Null
    if ($Before.Adapter -and $Before.AdapterPowerOff -eq 'Enabled') { Enable-NetAdapterPowerManagement -Name $Before.Adapter -NoRestart -ErrorAction SilentlyContinue }
    foreach ($p in @($Before.Eee)) { Set-NetAdapterAdvancedProperty -Name $Before.Adapter -DisplayName $p.DisplayName -DisplayValue $p.DisplayValue -NoRestart -ErrorAction SilentlyContinue }
    return 'power settings restored'
}

# ---------------------------------------------------------------------------
# Ollama: where the models are (pure decision + read-only discovery)
# ---------------------------------------------------------------------------

function ConvertTo-LabManifestModelName {
    <#  Pure - unit tested. A manifest path below <models>\manifests -> the model name Ollama lists:
        registry.ollama.ai\library\gemma3\4b -> gemma3:4b; registry.ollama.ai\user\m\t -> user/m:t;
        hf.co\unsloth\SmolLM3-3B-GGUF\Q4_K_M -> hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M.  #>
    param([Parameter(Mandatory)][string]$RelativePath)
    $p = @($RelativePath -split '[\\/]' | Where-Object { $_ })
    if ($p.Count -ne 4) { return $null }
    $name = $(if ($p[0] -ieq 'registry.ollama.ai' -and $p[1] -ieq 'library') { $p[2] } elseif ($p[0] -ieq 'registry.ollama.ai') { '{0}/{1}' -f $p[1], $p[2] } else { '{0}/{1}/{2}' -f $p[0], $p[1], $p[2] })
    return ('{0}:{1}' -f $name, $p[3])
}

function Get-LabServingModelDirectory {
    <#  Pure - unit tested. The folder the running boot server reads now, when nothing names it: a task that runs
        ollama serve as SYSTEM without OLLAMA_MODELS uses the SYSTEM profile's folder. '' when not that case.  #>
    param($Task = $null, [AllowEmptyString()][string]$MachineValue = '', [AllowEmptyCollection()][array]$Candidates = @())
    if ($MachineValue -or -not $Task -or -not $Task.Present -or [string]$Task.User -notmatch 'SYSTEM' -or [string]$Task.Action -match 'OLLAMA_MODELS|ollama-serve\.cmd') { return '' }
    $sys = @($Candidates | Where-Object { $_.Source -eq 'SYSTEM profile' }) | Select-Object -First 1
    return $(if ($sys) { [string]$sys.Path } else { '' })
}

function Test-LabServedOnDisk {
    <#  Pure - unit tested. Fails only when a folder holds a model the server does not serve. A second folder with
        the same models (a spare copy, as on heads set up by provisioner 1.0.3) is reported, not failed.  #>
    param([AllowEmptyCollection()][string[]]$Served = @(), [AllowEmptyCollection()][array]$Candidates = @(), [AllowEmptyString()][string]$ServerPath = '')
    $have = @{}; foreach ($n in $Served) { $have[$n.ToLowerInvariant()] = $true }
    $missing = @(); $spare = @()
    foreach ($c in @($Candidates | Where-Object { $_.Manifests -gt 0 -and (-not $ServerPath -or $_.Path.TrimEnd('\') -ine $ServerPath.TrimEnd('\')) })) {
        $names = @($c.Names)
        $notServed = @($names | Where-Object { -not $have.ContainsKey($_.ToLowerInvariant()) })
        if (-not $names.Count -or $notServed.Count) { $missing += ('{0} ({1})' -f $c.Path, $(if ($names.Count) { 'not served: ' + ($notServed -join ', ') } else { "$($c.Manifests) manifests" })) }
        else { $spare += $c.Path }
    }
    $detail = $(if ($missing.Count) { 'models NOT served: ' + ($missing -join '; ') } elseif ($spare.Count) { 'a spare copy of the same models (not used; can be deleted later to free space): ' + ($spare -join ', ') } else { '' })
    return [pscustomobject]@{ Ok = ($missing.Count -eq 0); Detail = $detail; Spare = $spare }
}

function Get-LabModelDirectoryCandidates {
    param([string]$NewDefault = 'C:\ProgramData\Ollama\models')
    $paths = @()
    foreach ($scope in 'Machine', 'User') { $v = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', $scope); if ($v) { $paths += [pscustomobject]@{ Path = $v; Source = "OLLAMA_MODELS ($scope)" } } }
    foreach ($u in @(Get-ChildItem (Join-Path $env:SystemDrive 'Users') -Directory -ErrorAction SilentlyContinue)) { $paths += [pscustomobject]@{ Path = (Join-Path $u.FullName '.ollama\models'); Source = "user profile $($u.Name)" } }
    $paths += [pscustomobject]@{ Path = (Join-Path $env:WINDIR 'System32\config\systemprofile\.ollama\models'); Source = 'SYSTEM profile' }
    $paths += [pscustomobject]@{ Path = $NewDefault; Source = 'lab default' }
    $seen = @{}; $out = @()
    foreach ($c in $paths) {
        $key = $c.Path.TrimEnd('\').ToLowerInvariant(); if ($seen[$key]) { continue }; $seen[$key] = $true
        $count = 0; $status = 'Observed'; $names = @()
        try {
            if (Test-Path -LiteralPath $c.Path) {
                $m = Join-Path $c.Path 'manifests'
                if (Test-Path -LiteralPath $m) {
                    $files = @(Get-ChildItem -LiteralPath $m -Recurse -File -ErrorAction Stop); $count = $files.Count
                    $names = @($files | ForEach-Object { ConvertTo-LabManifestModelName -RelativePath $_.FullName.Substring($m.TrimEnd('\').Length + 1) } | Where-Object { $_ })
                }
            }
            else { $status = 'Missing' }
        } catch { $status = 'Inaccessible' }
        $out += [pscustomobject][ordered]@{ Path = $c.Path; Source = $c.Source; Manifests = $count; Status = $status; Names = $names }
    }
    return $out
}

function Resolve-LabModelDirectory {
    <#  Chooses OLLAMA_MODELS for the boot server. Pure - unit tested.
        Keep an explicit machine setting; adopt the one directory that holds models; ask when
        several do; a fresh head gets the lab default. Models are never moved or deleted.  #>
    param([AllowEmptyString()][string]$MachineValue = '', [AllowEmptyCollection()][array]$Candidates = @(), [string]$NewDefault = 'C:\ProgramData\Ollama\models',
          [AllowEmptyString()][string]$ServingPath = '', [AllowEmptyCollection()][string[]]$Served = @())
    $withModels = @($Candidates | Where-Object { $_.Manifests -gt 0 })
    # Several folders, but the running server already serves every model from one of them (head 6, 2026-11-06:
    # 12 in the SYSTEM profile, the same 12 in LabOperator1's): keep that one, so nothing the heads serve changes.
    if (-not $MachineValue -and $withModels.Count -gt 1 -and $ServingPath -and @($Served).Count) {
        $srv = @($withModels | Where-Object { $_.Path.TrimEnd('\') -ieq $ServingPath.TrimEnd('\') }) | Select-Object -First 1
        $inIt = @{}; if ($srv) { foreach ($n in @($srv.Names)) { $inIt[$n.ToLowerInvariant()] = $true } }
        if ($srv -and -not @($Served | Where-Object { -not $inIt.ContainsKey($_.ToLowerInvariant()) }).Count) {
            $others = @($withModels | Where-Object { $_ -ne $srv } | ForEach-Object { '{0} ({1})' -f $_.Path, $_.Manifests })
            return [pscustomobject]@{ Decision = 'Adopt'; Path = $srv.Path; Reason = ('the running Ollama serves all {0} of its models from {1}; kept, so nothing served changes' -f @($Served).Count, $srv.Path)
                Note = ('also in ' + ($others -join ', ') + ': not used, left in place'); Choices = @() }
        }
    }
    if ($MachineValue) {
        $elsewhere = @($withModels | Where-Object { $_.Path.TrimEnd('\') -ine $MachineValue.TrimEnd('\') })
        $note = ''; if ($elsewhere.Count) { $note = ('models also exist in {0}; the server will not serve them' -f (($elsewhere | ForEach-Object { "$($_.Path) ($($_.Manifests))" }) -join ', ')) }
        return [pscustomobject]@{ Decision = 'Keep'; Path = $MachineValue; Reason = 'OLLAMA_MODELS is already set for the machine'; Note = $note; Choices = @() }
    }
    if ($withModels.Count -eq 0) { return [pscustomobject]@{ Decision = 'New'; Path = $NewDefault; Reason = 'no models on this machine yet'; Note = ''; Choices = @() } }
    if ($withModels.Count -eq 1) { return [pscustomobject]@{ Decision = 'Adopt'; Path = $withModels[0].Path; Reason = ('the models ({0} manifests) are in {1}' -f $withModels[0].Manifests, $withModels[0].Source); Note = ''; Choices = @() } }
    return [pscustomobject]@{ Decision = 'Ambiguous'; Path = ''; Reason = ('models exist in {0} places; the operator must choose one' -f $withModels.Count); Note = ''; Choices = $withModels }
}

function Get-LabOllamaTask {
    param([string]$TaskName = 'Capstone Ollama Server')
    try {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $i = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        return [pscustomobject][ordered]@{ Present = $true; State = [string]$t.State; User = [string]$t.Principal.UserId
                                           Action = (@($t.Actions | ForEach-Object { ('{0} {1}' -f $_.Execute, $_.Arguments).Trim() }) -join ' | ')
                                           LastRun = $(if ($i) { $i.LastRunTime } else { $null }); LastResult = $(if ($i) { $i.LastTaskResult } else { $null }) }
    } catch { return [pscustomobject]@{ Present = $false; State = ''; User = ''; Action = ''; LastRun = $null; LastResult = $null } }
}

function New-LabOllamaWrapperText {
    <#  Pure. 1.6.14: with -Settings (the managed keys of an active HEAD-OLLAMA record) and -LogFile, the same wrapper
        with those keys before the serve line and the server's output appended to the log (Lab.OllamaRuntime).  #>
    param([Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$Bind, [Parameter(Mandatory)][string]$ModelDirectory,
          [System.Collections.IDictionary]$Settings = $null, [string]$LogFile = '')
    $base = (@(
        '@echo off',
        'rem Written by the Agent Arena Lab Provisioner. The boot task runs this as SYSTEM so the',
        'rem server always binds the same address and serves the same model directory as the',
        'rem interactive install (provisioner 1.0.3 passed neither).',
        ('set "OLLAMA_HOST={0}"' -f $Bind),
        ('set "OLLAMA_MODELS={0}"' -f $ModelDirectory),
        ('"{0}" serve' -f $Executable)
    ) -join "`r`n") + "`r`n"
    if (-not $Settings -and -not $LogFile) { return $base }
    return (Set-LabWrapperContent -Text $base -Settings $(if ($Settings) { $Settings } else { @{} }) -LogFile $LogFile)
}

function Set-LabOllamaService {
    <#  Boot task + wrapper + machine-scope OLLAMA_HOST/OLLAMA_MODELS. Does not restart anything.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$Bind, [Parameter(Mandatory)][string]$ModelDirectory,
          [string]$TaskName = 'Capstone Ollama Server')
    $wrapperDir = Join-Path $Run.Root 'ollama'
    $wrapper = Join-Path $wrapperDir 'ollama-serve.cmd'
    # 1.6.14: a head HEAD-OLLAMA qualified keeps its pinned runtime and settings when it is provisioned again.
    $active = Read-LabOllamaSettings -Path (Join-Path $wrapperDir 'ollama-settings.json')
    if ($active) { $Executable = [string]$active.executable
                   $text = New-LabOllamaWrapperText -Executable $Executable -Bind $Bind -ModelDirectory $ModelDirectory -Settings (ConvertTo-LabSettingsMap $active.env) -LogFile ([string]$active.log_file) }
    else { $text = New-LabOllamaWrapperText -Executable $Executable -Bind $Bind -ModelDirectory $ModelDirectory }
    $task = Get-LabOllamaTask -TaskName $TaskName
    $envBefore = [pscustomobject]@{ OLLAMA_HOST = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine'); OLLAMA_MODELS = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine') }
    $current = ''; if (Test-Path -LiteralPath $wrapper) { $current = [IO.File]::ReadAllText($wrapper) }
    $taskOk = $task.Present -and $task.User -match 'SYSTEM' -and $task.Action -like ('*' + $wrapper + '*')
    if ($current -eq $text -and $taskOk -and $envBefore.OLLAMA_HOST -eq $Bind -and $envBefore.OLLAMA_MODELS -eq $ModelDirectory) {
        return (New-LabOutcome 'UNCHANGED' ("boot task runs $wrapper (OLLAMA_MODELS=$ModelDirectory)"))
    }
    if ($envBefore.OLLAMA_HOST -and -not (Test-CapstoneManagedHostValueLab $envBefore.OLLAMA_HOST)) { throw ("OLLAMA_HOST is set for the machine to '{0}', which this provisioner did not set; review it first" -f $envBefore.OLLAMA_HOST) }
    $userHost = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User')
    if ($userHost) { Add-LabWarning ("A user-scope OLLAMA_HOST ('$userHost') exists for $env:USERNAME. It does not affect the boot server, but CLI commands of this user follow it.") }
    $backupXml = ''
    if ($task.Present) { $backupXml = Join-Path $Run.Backups 'ollama-task-before.xml'; Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $backupXml -Encoding Unicode }
    $envFile = Join-Path $Run.Backups 'ollama-env-before.json'; Save-LabJson -InputObject $envBefore -Path $envFile
    if (-not (Test-Path -LiteralPath $ModelDirectory)) { New-Item -ItemType Directory -Path $ModelDirectory -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $wrapperDir)) { New-Item -ItemType Directory -Path $wrapperDir -Force | Out-Null }
    # SYSTEM runs this file at boot: only SYSTEM and Administrators may change it.
    & icacls.exe $wrapperDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not restrict the permissions of $wrapperDir" }
    $wrapperBefore = $null; if ($current) { $wrapperBefore = $current }
    [IO.File]::WriteAllText($wrapper, $text, (New-Object Text.ASCIIEncoding))
    [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $Bind, 'Machine')
    [Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $ModelDirectory, 'Machine')
    $env:OLLAMA_HOST = $Bind; $env:OLLAMA_MODELS = $ModelDirectory
    $tray = $null
    try { $tray = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Ollama' -ErrorAction Stop).Ollama } catch { }
    if ($tray) { Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Ollama' -ErrorAction Stop }
    $action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\cmd.exe') -Argument ('/c "{0}"' -f $wrapper)
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop `
        -Description 'Agent Arena Lab Provisioner: runs the Ollama server at system startup as SYSTEM, with an explicit binding and model directory.' | Out-Null
    Register-LabChange -Kind 'ollama-service' -Description ("boot task '$TaskName' runs $wrapper; OLLAMA_HOST=$Bind and OLLAMA_MODELS=$ModelDirectory for the machine (were '$($envBefore.OLLAMA_HOST)', '$($envBefore.OLLAMA_MODELS)')" + $(if ($tray) { '; Ollama tray autostart removed' } else { '' })) `
        -Backup $backupXml -Undo ([pscustomobject]@{ type = 'ollama-service'; task = $TaskName; task_xml = $backupXml; env = $envBefore; wrapper = $wrapper; wrapper_before = $wrapperBefore; tray = $tray }) | Out-Null
    return (New-LabOutcome 'DONE' ("boot task runs $wrapper with OLLAMA_MODELS=$ModelDirectory; takes effect at the next server start"))
}

function Test-CapstoneManagedHostValueLab {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    $n = $Value.Trim().ToLowerInvariant() -replace '^https?://', ''
    return [bool]($n -match '^(127\.0\.0\.1|localhost|0\.0\.0\.0|192\.0\.2\.\d{1,3})(:\d+)?$')
}

function Undo-LabOllamaService {
    param([Parameter(Mandatory)]$Undo)
    if ($Undo.task_xml -and (Test-Path -LiteralPath $Undo.task_xml)) { Register-ScheduledTask -TaskName $Undo.task -Xml (Get-Content -LiteralPath $Undo.task_xml -Raw) -Force -ErrorAction Stop | Out-Null }
    else { Unregister-ScheduledTask -TaskName $Undo.task -Confirm:$false -ErrorAction SilentlyContinue }
    [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $(if ($Undo.env.OLLAMA_HOST) { $Undo.env.OLLAMA_HOST } else { $null }), 'Machine')
    [Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $(if ($Undo.env.OLLAMA_MODELS) { $Undo.env.OLLAMA_MODELS } else { $null }), 'Machine')
    if ($Undo.wrapper_before) { [IO.File]::WriteAllText($Undo.wrapper, $Undo.wrapper_before, (New-Object Text.ASCIIEncoding)) } elseif (Test-Path -LiteralPath $Undo.wrapper) { Remove-Item -LiteralPath $Undo.wrapper -Force }
    if ($Undo.tray) { Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Ollama' -Value $Undo.tray }
    return ("boot task '{0}' and the machine OLLAMA_HOST/OLLAMA_MODELS restored (takes effect at the next server start)" -f $Undo.task)
}

# ---------------------------------------------------------------------------
# Ollama: restart (disruptive, approved), model visibility, reboot proof
# ---------------------------------------------------------------------------

function Get-LabModelSet {
    param([Parameter(Mandatory)][string]$BaseUri)
    $r = Get-CapstoneInstalledModels -BaseUri $BaseUri
    return [pscustomobject]@{ Available = $r.Available; Error = $r.Error; Models = @($r.Models | ForEach-Object { [pscustomobject]@{ name = $_.Name; digest = $_.Digest; size = $_.Size } } | Sort-Object name) }
}

function Compare-LabModelSet {
    param([Parameter(Mandatory)]$Before, [Parameter(Mandatory)]$After)
    $b = @{}; foreach ($m in @($Before.Models)) { $b[$m.name] = $m.digest }
    $a = @{}; foreach ($m in @($After.Models)) { $a[$m.name] = $m.digest }
    $missing = @($b.Keys | Where-Object { -not $a.ContainsKey($_) } | Sort-Object)
    $added = @($a.Keys | Where-Object { -not $b.ContainsKey($_) } | Sort-Object)
    $changed = @($b.Keys | Where-Object { $a.ContainsKey($_) -and $a[$_] -ne $b[$_] } | Sort-Object)
    return [pscustomobject]@{ Same = ($After.Available -and $missing.Count -eq 0 -and $changed.Count -eq 0); Missing = $missing; Added = $added; Changed = $changed; BeforeCount = $b.Count; AfterCount = $a.Count }
}

function Restart-LabOllamaServer {
    <#  Stops this installation's Ollama processes and starts the boot task. Refuses while a
        model is loaded unless -AllowLoaded (the operator approved interrupting it).  #>
    param([Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$CheckBase, [string]$TaskName = 'Capstone Ollama Server', [switch]$AllowLoaded, [int]$TimeoutSeconds = 180)
    $loaded = Get-CapstoneRunningModels -BaseUri 'http://127.0.0.1:11434'
    if ($loaded.Available -and $loaded.Count -gt 0 -and -not $AllowLoaded) { return (New-LabOutcome 'DECLINED' ('{0} model(s) loaded ({1}); not restarted' -f $loaded.Count, ($loaded.Names -join ', '))) }
    $dir = Split-Path -Parent $Executable
    # 1.6.18: the boot task's cmd.exe ends before Ollama, so it cannot read on in a rewritten wrapper (Lab.OllamaRuntime).
    if (-not (Get-Command Stop-LabOllamaBootHost -ErrorAction SilentlyContinue)) { Import-Module (Join-Path $PSScriptRoot 'Lab.OllamaRuntime.psm1') -Global -DisableNameChecking }
    $stopped = @(Stop-LabOllamaBootHost -TaskName $TaskName -Wrapper (Get-LabOllamaPaths).Wrapper)
    foreach ($p in @(Get-CapstoneOllamaProcesses)) {
        $mine = $false; try { $mine = ($p.Path -and ([IO.Path]::GetFullPath((Split-Path -Parent $p.Path)) -ieq [IO.Path]::GetFullPath($dir))) } catch { }
        if ($mine) { try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; $stopped += ('{0} ({1})' -f $p.Name, $p.Id) } catch { } }
        else { Write-LabLog ("  left $($p.Name) ($($p.Id)) alone: not from $dir") -Level INFO }
    }
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $probe = Wait-CapstoneOllamaApi -BaseUri $CheckBase -TimeoutSeconds $TimeoutSeconds -Activity 'Waiting for the boot task''s Ollama server'
    if (-not $probe.Healthy) { throw ('the boot task started but nothing answered on {0} within {1} s' -f $CheckBase, $TimeoutSeconds) }
    return (New-LabOutcome 'DONE' ('stopped {0}; the boot task''s server answers on {1} (Ollama {2})' -f $(if ($stopped.Count) { $stopped -join ', ' } else { 'nothing' }), $CheckBase, $probe.Version))
}

function Save-LabRebootCheckpoint {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$ModelSet, [Parameter(Mandatory)][string]$CheckBase, [string]$TaskName = 'Capstone Ollama Server')
    $file = Join-Path $Run.StateDir 'reboot-check.json'
    Save-LabJson -InputObject ([pscustomobject]@{ saved_at = (Get-Date).ToString('o'); run = $Run.Id; boot_before = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
                                                  check_base = $CheckBase; task = $TaskName; models = @($ModelSet.Models) }) -Path $file
    return $file
}

function Test-LabRebootCheckpoint {
    <#  After a reboot: did the boot task bring the server up by itself, serving the same models?  #>
    param([Parameter(Mandatory)][string]$StateDir)
    $file = Join-Path $StateDir 'reboot-check.json'
    $cp = Read-LabJson $file
    if (-not $cp) { return [pscustomobject]@{ Status = 'PENDING'; Detail = 'no reboot checkpoint was saved on this machine' } }
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    if ($boot -le [datetime]$cp.saved_at) { return [pscustomobject]@{ Status = 'PENDING'; Detail = ('not rebooted since the checkpoint of {0}' -f $cp.saved_at) } }
    $task = Get-LabOllamaTask -TaskName $cp.task
    $after = Get-LabModelSet -BaseUri $cp.check_base
    $cmp = Compare-LabModelSet -Before ([pscustomobject]@{ Models = @($cp.models) }) -After $after
    $ranAtBoot = ($task.Present -and $task.LastRun -and ([datetime]$task.LastRun) -ge $boot.AddMinutes(-1))
    $ok = $after.Available -and $cmp.Same -and $ranAtBoot
    $detail = ('rebooted {0}; boot task ran {1}; {2} of {3} models served on {4}{5}' -f $boot.ToString('s'), $task.LastRun, $cmp.AfterCount, $cmp.BeforeCount, $cp.check_base, $(if ($cmp.Missing.Count) { '; MISSING ' + ($cmp.Missing -join ', ') } else { '' }))
    return [pscustomobject]@{ Status = $(if ($ok) { 'PASS' } else { 'FAIL' }); Detail = $detail }
}

# ---------------------------------------------------------------------------
# Undo dispatcher: one record format for every change a run makes
# ---------------------------------------------------------------------------

function Undo-LabRun {
    param([Parameter(Mandatory)][string]$ChangesFile, [scriptblock]$Confirm = $null)
    $record = Read-LabJson $ChangesFile
    if (-not $record) { throw "no change record at $ChangesFile" }
    $changes = @($record.changes)
    $results = @()
    for ($i = $changes.Count - 1; $i -ge 0; $i--) {
        $c = $changes[$i]
        if ($c.undone -or -not $c.undo) { continue }
        if ($Confirm -and -not (& $Confirm $c)) { $results += ('kept: ' + $c.description); continue }
        try {
            switch ($c.undo.type) {
                'firewall' { $msg = Undo-LabFirewallChange -Undo $c.undo }
                'network' { $msg = Undo-LabHeadNetwork -BackupFile $c.undo.backup }
                'time' { $msg = Undo-LabTimeSync -Before $c.undo.before }
                'power' { $msg = Undo-LabPowerState -Before $c.undo.before }
                'ollama-service' { $msg = Undo-LabOllamaService -Undo $c.undo }
                'network-profile' { $msg = Undo-LabNetworkProfile -Undo $c.undo }
                'time-server' { $msg = Undo-LabTimeServer -Before $c.undo.before }
                'registry' { $msg = Undo-LabRegistryValue -Undo $c.undo }
                'file-restore' { $msg = Undo-LabFileChange -Undo $c.undo }
                'user-right' { $msg = Undo-LabUserRight -Undo $c.undo }
                'local-account' { $msg = Undo-LabLocalAccount -Undo $c.undo }
                'arena-migration' { $msg = Undo-LabArenaMigration -Undo $c.undo }
                'coder' { $msg = Undo-LabCoderChange -Undo $c.undo }
                'manual' { $msg = $null }
                default { throw ('no undo for change type ' + $c.undo.type) }
            }
            if ($c.undo.type -eq 'manual') { $results += ('manual step, not undone automatically: ' + $c.undo.instructions) }
            else { $c.undone = $true; $results += ('undone: ' + $msg) }
        } catch { $results += ('FAILED to undo "{0}": {1}' -f $c.description, $_.Exception.Message); break }
    }
    Save-LabJson -InputObject $record -Path $ChangesFile
    return $results
}

Export-ModuleMember -Function @(
    'Get-LabEnginePaths', 'ConvertTo-LabMask', 'Get-LabAdapterChoice', 'Set-LabHeadNetwork', 'Undo-LabHeadNetwork',
    'Get-LabTimeState', 'Test-LabTimeCompliant', 'Set-LabTimeSync', 'Undo-LabTimeSync', 'Get-LabTimeOffset',
    'Get-LabPowerIndex', 'Get-LabPowerState', 'Test-LabPowerCompliant', 'Set-LabPowerState', 'Undo-LabPowerState',
    'ConvertTo-LabManifestModelName', 'Get-LabServingModelDirectory', 'Test-LabServedOnDisk', 'Get-LabModelDirectoryCandidates', 'Resolve-LabModelDirectory', 'Get-LabOllamaTask', 'New-LabOllamaWrapperText', 'Set-LabOllamaService',
    'Test-CapstoneManagedHostValueLab', 'Undo-LabOllamaService', 'Get-LabModelSet', 'Compare-LabModelSet', 'Restart-LabOllamaServer',
    'Save-LabRebootCheckpoint', 'Test-LabRebootCheckpoint', 'Undo-LabRun'
)
