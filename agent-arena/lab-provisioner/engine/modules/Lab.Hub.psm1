<#
    Lab.Hub - the administration host (192.0.2.254): Arena-owned machine prerequisites for running
    Agent Arena and, independently, the Inference Gateway.

    A  Reversible network profile, time server, firewall prerequisites and Remote Desktop scope.
       Same settings as the field-verified SET-UP-THIS-COMPUTER, but every change goes through the
       run's change journal with its backup and undo. (Address and power: Lab.Head, reused.)
    C  Arena's own Gateway observation configuration (aes.config.json "gateway" block), with backup
       and undo, and a state that tells apart: configured / file present / access verified / fresh /
       publisher running. Live integration is never reported until the Gateway's publisher is there.
    D  A sanitized inventory of the heads this host has actually verified (Arena node ID, endpoint,
       Ollama version, models). It is an input for the Gateway operator, not a Gateway bootstrap,
       and nothing is enrolled anywhere.
    E  Read-only: which Windows account runs Arena, whether it is an Administrator, and whether it
       can reach the Gateway's private folders - the rules the Gateway's own ACL script enforces.
       Accounts are never created, removed, demoted or switched.

    Nothing here touches the Gateway's scripts, ledger, state or keys.
#>

$script:AdminsSid = 'S-1-5-32-544'
$script:BroadSids = @('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11', 'S-1-5-4', 'S-1-5-32-546')   # Everyone, Users, Authenticated Users, Interactive, Guests

# ---------------------------------------------------------------------------
# A. Network profile, time server, firewall prerequisites, Remote Desktop
# ---------------------------------------------------------------------------

function Set-LabNetworkProfile {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Alias, [string]$Category = 'Private')
    $p = Get-NetConnectionProfile -InterfaceAlias $Alias -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { return (New-LabOutcome 'PENDING' "'$Alias' has no network profile yet (Windows is still identifying it); run the hub role again") }
    if ([string]$p.NetworkCategory -eq $Category) { return (New-LabOutcome 'UNCHANGED' "'$Alias' network profile: $Category") }
    $before = [string]$p.NetworkCategory
    Set-NetConnectionProfile -InterfaceAlias $Alias -NetworkCategory $Category -ErrorAction Stop
    Register-LabChange -Kind 'network-profile' -Description "'$Alias' network profile $Category (was $before)" -Undo ([pscustomobject]@{ type = 'network-profile'; alias = $Alias; before = $before }) | Out-Null
    return (New-LabOutcome 'DONE' "'$Alias' network profile: $Category (was $before)")
}

# The Inference Gateway's 8443 rules (its own manifest) apply only on a Private network. The hub's lab cable has no
# router, so Windows calls it "Unidentified network" and set it back to Public at the restart of 2026-11-11 06:43: the
# Capstone laptop's connections then timed out while Arena (profile Any) kept answering. Read-only check.
$script:KeepPrivateTask = 'Agent Arena Lab - keep the lab network Private'
function Test-LabHubLabNetwork {
    param([Parameter(Mandatory)][string]$HubAddress)
    $ip = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $HubAddress }) | Select-Object -First 1
    # No lab cable here: other hub checks report the cable; this one only judges the network type it can see.
    if (-not $ip) { return [pscustomobject]@{ Status = 'PENDING'; Detail = "no adapter holds $HubAddress (lab cable out?): the network type cannot be checked" } }
    $cat = [string](Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1).NetworkCategory
    $task = [bool](Get-ScheduledTask -TaskName $script:KeepPrivateTask -ErrorAction SilentlyContinue)
    $fix = 'fix: this stick > engine > hub-tools > lab-network > SET-HUB-LAB-NETWORK-PRIVATE.cmd (as LabOperator1)'
    $detail = "'{0}' {1}: {2}; task '{3}' {4}" -f $ip.InterfaceAlias, $HubAddress, $(if ($cat) { $cat } else { 'no profile yet' }), $script:KeepPrivateTask, $(if ($task) { 'present' } else { 'missing' })
    if ($cat -ne 'Private') { return [pscustomobject]@{ Status = 'FAIL'; Detail = "$detail. The Gateway's rule for the Capstone laptop does not apply; $fix" } }
    if (-not $task) { return [pscustomobject]@{ Status = 'PENDING'; Detail = "$detail. Private now, but a restart can make it Public again; $fix" } }
    return [pscustomobject]@{ Status = 'PASS'; Detail = $detail }
}

function Undo-LabNetworkProfile {
    param([Parameter(Mandatory)]$Undo)
    if ($Undo.before -in 'Public', 'Private') { Set-NetConnectionProfile -InterfaceAlias $Undo.alias -NetworkCategory $Undo.before -ErrorAction Stop }
    return ("'{0}' network profile back to {1}" -f $Undo.alias, $Undo.before)
}

$script:W32 = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time'
function Get-LabRegValue { param([string]$Path, [string]$Name) try { return (Get-ItemProperty -Path $Path -Name $Name -ErrorAction Stop).$Name } catch { return $null } }

function Get-LabTimeServerState {
    $start = ''; try { $start = [string](Get-CimInstance Win32_Service -Filter "Name='w32time'" -ErrorAction Stop).StartMode } catch { }
    $svc = Get-Service w32time -ErrorAction SilentlyContinue
    return [pscustomobject][ordered]@{ TimeZone = (Get-TimeZone).Id; NtpServer = (Get-LabRegValue "$script:W32\Parameters" 'NtpServer'); Type = (Get-LabRegValue "$script:W32\Parameters" 'Type')
        AnnounceFlags = (Get-LabRegValue "$script:W32\Config" 'AnnounceFlags'); ServerEnabled = (Get-LabRegValue "$script:W32\TimeProviders\NtpServer" 'Enabled')
        StartMode = $start; Service = $(if ($svc) { [string]$svc.Status } else { 'missing' }) }
}

function Test-LabTimeServerCompliant {
    <#  Pure. The hub serves time (AnnounceFlags 5, NTP server provider on) and syncs from the upstream when it has internet.  #>
    param([Parameter(Mandatory)]$State, [Parameter(Mandatory)][string]$Zone, [Parameter(Mandatory)][string]$Upstream)
    return ($State.TimeZone -eq $Zone -and ([string]$State.NtpServer) -eq ($Upstream + ',0x8') -and $State.Type -eq 'NTP' -and [int]$State.AnnounceFlags -eq 5 -and [int]$State.ServerEnabled -eq 1 -and $State.StartMode -eq 'Auto')
}

function Set-LabTimeServer {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Zone, [string]$Upstream = 'time.windows.com')
    $before = Get-LabTimeServerState
    if (Test-LabTimeServerCompliant -State $before -Zone $Zone -Upstream $Upstream) { return (New-LabOutcome 'UNCHANGED' "$Zone; this computer serves time to the lab") }
    $file = Join-Path $Run.Backups 'time-server-before.json'; Save-LabJson -InputObject $before -Path $file
    if ($before.TimeZone -ne $Zone) { Set-TimeZone -Id $Zone -ErrorAction Stop }
    Set-Service w32time -StartupType Automatic -ErrorAction Stop; Start-Service w32time -ErrorAction SilentlyContinue
    $out = & w32tm.exe /config ("/manualpeerlist:{0},0x8" -f $Upstream) /syncfromflags:manual /reliable:yes /update 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('w32tm /config failed: ' + ($out -join ' ')) }
    Set-ItemProperty "$script:W32\TimeProviders\NtpServer" -Name Enabled -Value 1 -ErrorAction Stop
    Set-ItemProperty "$script:W32\Config" -Name AnnounceFlags -Value 5 -ErrorAction Stop
    Restart-Service w32time -ErrorAction Stop
    Register-LabChange -Kind 'time-server' -Description ("time zone $Zone (was $($before.TimeZone)); serves time to the lab, syncs from $Upstream (was '$($before.NtpServer)', server provider $($before.ServerEnabled), announce $($before.AnnounceFlags))") -Backup $file -Undo ([pscustomobject]@{ type = 'time-server'; before = $before }) | Out-Null
    return (New-LabOutcome 'DONE' "$Zone; this computer serves time to the lab")
}

function Undo-LabTimeServer {
    param([Parameter(Mandatory)]$Before)
    if ($Before.TimeZone) { Set-TimeZone -Id $Before.TimeZone -ErrorAction Stop }
    foreach ($v in @(@("$script:W32\Parameters", 'NtpServer', $Before.NtpServer), @("$script:W32\Parameters", 'Type', $Before.Type), @("$script:W32\Config", 'AnnounceFlags', $Before.AnnounceFlags), @("$script:W32\TimeProviders\NtpServer", 'Enabled', $Before.ServerEnabled))) {
        if ($null -ne $v[2]) { Set-ItemProperty -Path $v[0] -Name $v[1] -Value $v[2] -ErrorAction Stop }
    }
    $mode = @{ Auto = 'Automatic'; Manual = 'Manual'; Disabled = 'Disabled' }[[string]$Before.StartMode]
    if ($mode) { Set-Service w32time -StartupType $mode -ErrorAction SilentlyContinue }
    & w32tm.exe /config /update 2>&1 | Out-Null
    if ($Before.Service -eq 'Running') { Restart-Service w32time -ErrorAction SilentlyContinue } elseif ($Before.Service -eq 'Stopped') { Stop-Service w32time -ErrorAction SilentlyContinue }
    return ('time zone {0} and time service settings restored' -f $Before.TimeZone)
}

function Set-LabHubFirewallPrerequisites {
    <#  The two inbound rules SET-UP-THIS-COMPUTER adds (time UDP 123 and ping, from the lab only).
        A rule already present under its display name is left alone.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Lan)
    $b = Get-LabFirewallBackend
    $wanted = @(
        @{ Name = 'AgentArenaLab-TimeServer'; DisplayName = 'Arena time server'; Protocol = 'UDP'; LocalPort = '123'; RemoteAddress = @($Lan) },
        @{ Name = 'AgentArenaLab-Ping'; DisplayName = 'Arena ping'; Protocol = 'ICMPv4'; IcmpType = '8'; RemoteAddress = @($Lan) })
    $done = @()
    foreach ($w in $wanted) {
        if (@(Get-NetFirewallRule -DisplayName $w.DisplayName -ErrorAction SilentlyContinue).Count) { $done += "'$($w.DisplayName)' already present"; continue }
        $p = @{ Name = $w.Name; DisplayName = $w.DisplayName; Group = 'Agent Arena Lab'; Direction = 'Inbound'; Action = 'Allow'; Protocol = $w.Protocol; RemoteAddress = $w.RemoteAddress; Profile = 'Any'; Enabled = 'True' }
        if ($w.LocalPort) { $p.LocalPort = $w.LocalPort }; if ($w.IcmpType) { $p.IcmpType = $w.IcmpType }
        & $b.NewRule $p
        Register-LabChange -Kind 'firewall' -Description ("added '{0}' ({1} from {2})" -f $w.DisplayName, $w.Protocol, $Lan) -Undo ([pscustomobject]@{ type = 'firewall'; action = 'remove'; name = $w.Name }) | Out-Null
        $done += "'$($w.DisplayName)' added"
    }
    return (New-LabOutcome $(if (@($done | Where-Object { $_ -like '*added' }).Count) { 'DONE' } else { 'UNCHANGED' }) ($done -join '; '))
}

function Set-LabRemoteDesktopScope {
    <#  Remote Desktop on, from the development workstation only (as SET-UP-THIS-COMPUTER).  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$From)
    $key = 'HKLM:\System\CurrentControlSet\Control\Terminal Server'
    $rules = @(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue)
    if (-not $rules.Count) { return (New-LabOutcome 'SKIPPED' 'no Remote Desktop rules on this edition of Windows') }
    $deny = Get-LabRegValue $key 'fDenyTSConnections'
    $notes = @(); $b = Get-LabFirewallBackend
    if ($deny -ne 0) {
        Set-ItemProperty -Path $key -Name fDenyTSConnections -Value 0 -ErrorAction Stop
        Register-LabChange -Kind 'registry' -Description "Remote Desktop host enabled (fDenyTSConnections was $deny)" -Undo ([pscustomobject]@{ type = 'registry'; path = $key; name = 'fDenyTSConnections'; value = $deny }) | Out-Null
        $notes += 'Remote Desktop enabled'
    }
    foreach ($r in $rules) {
        $remote = @((Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $r).RemoteAddress | ForEach-Object { [string]$_ })
        $enabled = ([string]$r.Enabled -eq 'True')
        if ($enabled -and ($remote -join ',') -eq $From) { continue }
        & $b.SetRule $r.Name @{ RemoteAddress = @($From); Enabled = 'True' }
        Register-LabChange -Kind 'firewall' -Description ("'{0}': from {1} only (was {2}, enabled {3})" -f $r.DisplayName, $From, ($remote -join ','), $enabled) -Undo ([pscustomobject]@{ type = 'firewall'; action = 'restore-remote'; name = $r.Name; remote = $remote; enabled = $enabled }) | Out-Null
        $notes += $r.DisplayName
    }
    if (-not $notes.Count) { return (New-LabOutcome 'UNCHANGED' "Remote Desktop on, from $From only") }
    return (New-LabOutcome 'DONE' ("Remote Desktop from $From only: " + ($notes -join '; ')))
}

function Undo-LabRegistryValue {
    param([Parameter(Mandatory)]$Undo)
    if ($null -eq $Undo.value) { Remove-ItemProperty -Path $Undo.path -Name $Undo.name -ErrorAction SilentlyContinue } else { Set-ItemProperty -Path $Undo.path -Name $Undo.name -Value $Undo.value -ErrorAction Stop }
    return ('{0}\{1} restored' -f $Undo.path, $Undo.name)
}

function Undo-LabFileChange {
    param([Parameter(Mandatory)]$Undo)
    if ($Undo.backup -and (Test-Path -LiteralPath $Undo.backup)) { Copy-Item -LiteralPath $Undo.backup -Destination $Undo.path -Force; return ('{0} restored from {1}' -f $Undo.path, $Undo.backup) }
    if (-not $Undo.backup -and (Test-Path -LiteralPath $Undo.path)) { Remove-Item -LiteralPath $Undo.path -Force; return ('{0} removed (it did not exist before)' -f $Undo.path) }
    throw ('no backup to restore {0}' -f $Undo.path)
}

# ---------------------------------------------------------------------------
# E. Arena's running identity and its access to the Gateway's private folders (read-only)
# ---------------------------------------------------------------------------

function ConvertTo-LabResolvableAccount {
    <#  Pure - unit tested. ".\name" is understood by the service manager and by logon, but Windows'
        name lookup (NTAccount.Translate, LsaLookupNames) cannot resolve it: it becomes
        <computer>\name. Every other form is returned unchanged.  #>
    param([string]$Account, [string]$Computer = $env:COMPUTERNAME)
    if ($Account -match '^\.\\(.+)$') { return ('{0}\{1}' -f $Computer, $Matches[1]) }
    return $Account
}
function Resolve-LabSid { param([string]$Account) try { return (New-Object Security.Principal.NTAccount((ConvertTo-LabResolvableAccount $Account))).Translate([Security.Principal.SecurityIdentifier]).Value } catch { return '' } }
function Resolve-LabAccount { param([string]$Sid) try { return (New-Object Security.Principal.SecurityIdentifier($Sid)).Translate([Security.Principal.NTAccount]).Value } catch { return $Sid } }

function Get-LabAdministratorSids {
    <#  Direct members of the local Administrators group, as SIDs. Get-LocalGroupMember fails on some
        domain-joined machines (an unresolvable member), so the WinNT provider is the fallback.  #>
    try { return @(Get-LocalGroupMember -SID $script:AdminsSid -ErrorAction Stop | ForEach-Object { [string]$_.SID.Value }) } catch { }
    try {
        $name = ((New-Object Security.Principal.SecurityIdentifier($script:AdminsSid)).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1]
        $group = [ADSI]("WinNT://{0}/{1},group" -f $env:COMPUTERNAME, $name)
        $sids = @()
        foreach ($m in @($group.psbase.Invoke('Members'))) {
            $bytes = $m.GetType().InvokeMember('objectSid', 'GetProperty', $null, $m, $null)
            if ($bytes) { $sids += (New-Object Security.Principal.SecurityIdentifier([byte[]]$bytes, 0)).Value }
        }
        return $sids
    } catch { return @('inaccessible') }
}

function Get-LabArenaInstallRecord {
    <#  One account's Arena hub install: its configuration, and whether its data is marked moved
        (moved-to-hub.json next to the database). Arena's own service refuses to start from marked
        data (arena_service\__main__.py), so a marked install is a rollback copy, not a running Arena.  #>
    param([Parameter(Mandatory)][string]$Sid, [Parameter(Mandatory)][string]$Profile, [string]$Account = '')
    $cfgPath = Get-LabArenaConfigPath -ProfilePath $Profile
    $cfg = $null; try { $cfg = Read-LabJson $cfgPath } catch { }
    $moved = $false; $why = ''
    if ($cfg -and $cfg.PSObject.Properties.Name -contains 'db_path' -and $cfg.db_path) {
        $marker = Join-Path (Split-Path -Parent ([string]$cfg.db_path)) 'moved-to-hub.json'
        if (Test-Path -LiteralPath $marker) { $moved = $true; try { $why = [string](Read-LabJson $marker).reason } catch { } }
    }
    return [pscustomobject][ordered]@{ Sid = $Sid; Account = $(if ($Account) { $Account } else { Resolve-LabAccount $Sid }); Profile = $Profile; ConfigPath = $cfgPath
                                       ConfigPresent = [bool]$cfg; Moved = $moved; MovedReason = $why }
}

function Select-LabActiveArenaInstall {
    <#  Pure - unit tested. The install that is this hub's Arena: not marked moved, preferring the account
        that runs Arena now. $null when every install is a moved copy or there is none.  #>
    param([AllowEmptyCollection()][array]$Installed = @(), [AllowEmptyCollection()][string[]]$RunningSids = @())
    $live = @(@($Installed) | Where-Object { $_ -and -not $_.Moved })
    $running = @($live | Where-Object { $RunningSids -contains $_.Sid })
    if ($running.Count) { return $running[0] }
    if ($live.Count) { return $live[0] }
    return $null
}

function Get-LabArenaAccountSids {
    <#  Pure - unit tested. The accounts the Gateway's account rule applies to: every account running an
        Arena process now, and every account holding a live (not moved) Arena install. A moved copy is
        left out only while it does not run: a running process always counts.  #>
    param([Parameter(Mandatory)]$Identity)
    return @(@(@($Identity.Running) | ForEach-Object { $_.Sid }) + @(@($Identity.Installed) | Where-Object { -not $_.Moved } | ForEach-Object { $_.Sid }) | Where-Object { $_ } | Select-Object -Unique)
}

function Get-LabArenaIdentity {
    <#  Which accounts have the Arena hub installed (and whether each copy is live or moved), and which
        accounts run it now.  #>
    $installed = @()
    foreach ($k in @(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList' -ErrorAction SilentlyContinue)) {
        $path = [string](Get-ItemProperty -Path $k.PSPath -Name ProfileImagePath -ErrorAction SilentlyContinue).ProfileImagePath
        if (-not $path) { continue }
        $hub = Join-Path $path 'AppData\Local\Programs\AgentArenaHub\app'
        $exists = $false; try { $exists = Test-Path -LiteralPath $hub } catch { }
        if ($exists) { $installed += Get-LabArenaInstallRecord -Sid $k.PSChildName -Profile $path }
    }
    $running = @()
    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { ($_.Name -eq 'AgentArena.exe' -and $_.CommandLine -match '--admin') -or ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'arena_service') })) {
        $sid = ''; try { $sid = [string](Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid -ErrorAction Stop).Sid } catch { }
        $what = $(if ($p.Name -eq 'AgentArena.exe') { 'admin window' } elseif ($p.CommandLine -match 'admin_console') { 'admin console' } else { 'event service' })
        $running += [pscustomobject]@{ What = $what; Pid = $p.ProcessId; Sid = $sid; Account = $(if ($sid) { Resolve-LabAccount $sid } else { 'unknown' }) }
    }
    $admins = @(Get-LabAdministratorSids); $adminsStatus = 'observed'
    if ($admins.Count -eq 1 -and $admins[0] -eq 'inaccessible') { $admins = @(); $adminsStatus = 'inaccessible' }
    return [pscustomobject][ordered]@{ Installed = $installed; Running = $running; AdministratorsDirect = $admins; AdministratorsStatus = $adminsStatus
                                       Current = [pscustomobject]@{ Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; Account = "$env:USERDOMAIN\$env:USERNAME" } }
}

function Get-LabAclAllowSids {
    <#  SIDs holding an allow ACE on a folder or anything inside it (read-only).  #>
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $sids = @{}
    try {
        foreach ($item in @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction SilentlyContinue)) {
            foreach ($rule in (Get-Acl -LiteralPath $item.FullName).Access) {
                if ($rule.AccessControlType -ne 'Allow') { continue }
                try { $s = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $s = [string]$rule.IdentityReference }
                $sids[$s] = $true
            }
        }
    } catch { return 'inaccessible' }
    return ,@($sids.Keys)    # keep an empty list a list: $null means the folder does not exist
}

function Get-LabGatewayAccessState {
    <#  The Gateway folders' ACL facts relevant to Arena, and the Gateway service (read-only; the
        Gateway's own scripts are never run here). Gateway 0.3.0 publishes from inside its service:
        a separate publisher task is absent by design.  #>
    param([Parameter(Mandatory)]$Config)
    $g = $Config.gateway
    $folders = [ordered]@{ ledger = [string]$g.ledger_directory; state = [string]$g.state_directory; logs = [string]$g.log_directory; publication = (Split-Path -Parent ([string]$g.observation_file)) }
    $out = [ordered]@{}
    foreach ($k in $folders.Keys) { $out[$k] = [pscustomobject]@{ Path = $folders[$k]; AllowSids = (Get-LabAclAllowSids -Path $folders[$k]) } }
    $svc = Get-LabGatewayService -Name ([string]$g.service_name)
    $svcSid = ''; if ($svc.Account) { $svcSid = Resolve-LabSid $svc.Account }
    if (-not $svcSid) { $svcSid = Resolve-LabSid ('.\' + [string]$g.service_account) }
    $task = $null; try { $t = Get-ScheduledTask -TaskName $g.publisher_task -ErrorAction Stop; $task = [pscustomobject]@{ Present = $true; State = [string]$t.State } } catch { $task = [pscustomobject]@{ Present = $false; State = '' } }
    return [pscustomobject]@{ Folders = [pscustomobject]$out; Service = $svc; GatewaySid = $svcSid; AlternativeTask = $task }
}

function Get-LabGatewayAccountVerdict {
    <#  Pure - unit tested. The rules of the Gateway 0.3.0 contract, applied to what was observed:
        every Arena account a standard account - not a TRANSITIVE member of Administrators or Backup
        Operators - and different from the Gateway service account; no Arena or broad allow ACE on
        ledger, state or logs. Membership that cannot be read blocks nothing but is never "OK".
        The Gateway's own plan/ACL JSON stays authoritative; this is F:'s early warning.  #>
    param([AllowEmptyCollection()][string[]]$ArenaSids = @(), [AllowEmptyCollection()][array]$Membership = @(), [string]$GatewaySid = '', $Folders = $null, [string]$MembershipStatus = 'observed')
    $blockers = @(); $unknown = @(); $notes = @()
    if (-not $ArenaSids.Count) { $unknown += 'no account runs or has installed the Arena hub here, so the Arena account is not known' }
    if ($MembershipStatus -ne 'observed') { $unknown += 'local group membership could not be read' }
    foreach ($s in $ArenaSids) {
        $m = @($Membership | Where-Object { $_.Sid -eq $s }) | Select-Object -First 1
        if (-not $m) { $unknown += ('membership of {0} was not checked' -f (Resolve-LabAccount $s)) }
        elseif ($m.State -eq 'privileged') { $blockers += ('the Arena account {0} is a member of {1} (directly or through a nested group): the Gateway refuses this, since no ACL protects the ledger from it' -f (Resolve-LabAccount $s), ($m.Groups -join ' and ')) }
        elseif ($m.State -eq 'unknown') { $unknown += ('membership of {0} in {1} could not be expanded' -f (Resolve-LabAccount $s), ($m.Unreadable -join ', ')) }
        if ($GatewaySid -and $s -eq $GatewaySid) { $blockers += ('Arena and the Gateway run as the same account {0}: they must differ' -f (Resolve-LabAccount $s)) }
    }
    if (-not $GatewaySid) { $notes += 'the Gateway service account does not exist yet' }
    if ($Folders) {
        foreach ($k in 'ledger', 'state', 'logs') {
            $f = $Folders.$k
            if ($null -eq $f -or $null -eq $f.AllowSids) { $notes += "$k folder not present yet"; continue }
            if ($f.AllowSids -is [string]) { $unknown += "$k folder ACL could not be read"; continue }
            $bad = @($f.AllowSids | Where-Object { $ArenaSids -contains $_ -or $script:BroadSids -contains $_ })
            if ($bad.Count) { $blockers += ('{0} folder {1} grants {2}: Arena and broad groups must hold no access' -f $k, $f.Path, (($bad | ForEach-Object { Resolve-LabAccount $_ }) -join ', ')) }
        }
    }
    $state = $(if ($blockers.Count) { 'BLOCKED' } elseif ($unknown.Count) { 'UNKNOWN' } else { 'OK' })
    return [pscustomobject]@{ State = $state; Blockers = $blockers; Unknown = $unknown; Notes = $notes }
}

# ---------------------------------------------------------------------------
# C. Arena's Gateway observation configuration and its states
# ---------------------------------------------------------------------------

function Get-LabArenaConfigPath {
    <#  Arena installs per user: its configuration lives in the Arena account's own profile, which is
        not the profile of the administrator running F:.  #>
    param([string]$ProfilePath = '')
    if ($ProfilePath) { return (Join-Path $ProfilePath 'AppData\Local\AgentArenaService\aes.config.json') }
    return (Join-Path $env:LOCALAPPDATA 'AgentArenaService\aes.config.json')
}

function Get-LabArenaConfigPaths {
    <#  The configuration of every LIVE Arena install. A copy whose data is marked moved (the rollback copy
        an account migration leaves behind) does not start, so it is not configured and not counted.  #>
    param($Identity = $null)
    if (-not $Identity) { $Identity = Get-LabArenaIdentity }
    $paths = @(@($Identity.Installed) | Where-Object { -not $_.Moved } | ForEach-Object { [pscustomobject]@{ Account = $_.Account; Sid = $_.Sid; Path = (Get-LabArenaConfigPath -ProfilePath $_.Profile) } } | Where-Object { Test-Path -LiteralPath $_.Path })
    if (-not $paths.Count -and -not @($Identity.Installed).Count -and (Test-Path -LiteralPath (Get-LabArenaConfigPath))) { $paths = @([pscustomobject]@{ Account = $Identity.Current.Account; Sid = $Identity.Current.Sid; Path = (Get-LabArenaConfigPath) }) }
    return $paths
}

function Get-LabWantedGatewayBlock {
    param([Parameter(Mandatory)]$Config, $Existing = $null)
    $heads = [pscustomobject]@{}
    if ($Existing -and $Existing.PSObject.Properties.Name -contains 'resource_heads' -and $Existing.resource_heads) { $heads = $Existing.resource_heads }   # keep an operator's mapping
    return [pscustomobject][ordered]@{ observation_file = [string]$Config.gateway.observation_file; gateway_id = [string]$Config.gateway.gateway_id; stale_seconds = [int]$Config.gateway.stale_seconds; resource_heads = $heads }
}

function Test-LabGatewayBlockMatches {
    param($Block, [Parameter(Mandatory)]$Wanted)
    if (-not $Block) { return $false }
    return ([string]$Block.observation_file -eq $Wanted.observation_file -and [string]$Block.gateway_id -eq $Wanted.gateway_id -and [int]$Block.stale_seconds -eq $Wanted.stale_seconds)
}

function Merge-LabGatewayConfig {
    <#  Pure - unit tested. Returns the new config JSON text with only the "gateway" block replaced,
        and proves every other setting is unchanged.  #>
    param([Parameter(Mandatory)][string]$Json, [Parameter(Mandatory)]$Config)
    $doc = $Json | ConvertFrom-Json
    $existing = $null; if ($doc.PSObject.Properties.Name -contains 'gateway') { $existing = $doc.gateway }
    $wanted = Get-LabWantedGatewayBlock -Config $Config -Existing $existing
    $others = @($doc.PSObject.Properties | Where-Object { $_.Name -ne 'gateway' } | ForEach-Object { $_.Name })
    $before = @{}; foreach ($n in $others) { $before[$n] = ($doc.$n | ConvertTo-Json -Depth 32 -Compress) }
    if ($doc.PSObject.Properties.Name -contains 'gateway') { $doc.gateway = $wanted } else { $doc | Add-Member -NotePropertyName gateway -NotePropertyValue $wanted }
    $text = $doc | ConvertTo-Json -Depth 32
    $check = $text | ConvertFrom-Json
    foreach ($n in $others) { if ((($check.$n) | ConvertTo-Json -Depth 32 -Compress) -ne $before[$n]) { throw "merging the gateway block would change the setting '$n'; nothing was written" } }
    return $text
}

function Set-LabArenaGatewayConfig {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Config, [string]$Path = '')
    if (-not $Path) { $Path = Get-LabArenaConfigPath }
    if (-not (Test-Path -LiteralPath $Path)) { return (New-LabOutcome 'PENDING' "no Arena configuration at $Path (install the hub first)") }
    $json = [IO.File]::ReadAllText($Path)
    $doc = $json | ConvertFrom-Json
    $existing = $null; if ($doc.PSObject.Properties.Name -contains 'gateway') { $existing = $doc.gateway }
    if (Test-LabGatewayBlockMatches -Block $existing -Wanted (Get-LabWantedGatewayBlock -Config $Config -Existing $existing)) { return (New-LabOutcome 'UNCHANGED' 'Arena already reads the Gateway observation file') }
    $text = Merge-LabGatewayConfig -Json $json -Config $Config
    $backup = Join-Path $Run.Backups 'aes.config.json.before'
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding $false))     # no BOM: the Python service reads plain UTF-8
    Register-LabChange -Kind 'arena-config' -Description ("Arena reads the Gateway observation file {0} (gateway_id {1}, stale after {2} s)" -f $Config.gateway.observation_file, $Config.gateway.gateway_id, $Config.gateway.stale_seconds) -Backup $backup -Undo ([pscustomobject]@{ type = 'file-restore'; path = $Path; backup = $backup }) | Out-Null
    Add-LabPending -Area 'arena' -Text 'the admin console reads its configuration at start: restart it (admin window tray > Restart service, or sign out and in) for the Gateway line to appear'
    return (New-LabOutcome 'DONE' 'Gateway observation block written to aes.config.json (backup kept)')
}

function Get-LabObservationState {
    <#  Pure - unit tested. The facts, kept apart, and the one conclusion allowed from them.
        Gateway 0.3.0 publishes from inside its Windows service, and only from an accepted ledger:
        a fresh file therefore also proves the service runs and the ledger was accepted.  #>
    param([bool]$Configured, [bool]$FilePresent, [string]$Access = 'unknown', [bool]$Fresh = $false, [string]$Contract = '', [string]$ExpectedContract = 'shared.inference.observation/1',
          [bool]$ServiceRunning = $false, [string]$LedgerAccepted = 'unknown')
    $live = $Configured -and $FilePresent -and $Access -eq 'verified' -and $Fresh -and $Contract -eq $ExpectedContract -and $ServiceRunning
    $summary = $(if ($live) { 'LIVE: Arena reads fresh observations from the InferenceGateway service' }
                 elseif (-not $FilePresent -and -not $ServiceRunning) { 'PENDING GATEWAY INSTALLATION: nothing is published yet' + $(if ($Configured) { ' (Arena is configured and waiting)' } else { ' (Arena is not configured yet)' }) }
                 elseif (-not $FilePresent -and $LedgerAccepted -like 'not accepted*') { 'NOT PUBLISHED: the service runs but the ledger has not been accepted (publisher accept-ledger)' }
                 elseif (-not $FilePresent) { 'NOT PUBLISHED YET: the service runs; the publication file does not exist (ledger acceptance ' + $LedgerAccepted + ')' }
                 elseif (-not $Configured) { 'NOT CONFIGURED: the Gateway publishes, Arena is not reading it' }
                 elseif ($Access -ne 'verified') { 'ACCESS NOT VERIFIED: ' + $Access }
                 elseif (-not $ServiceRunning) { 'SERVICE NOT RUNNING: the InferenceGateway service is stopped; the file is left over' }
                 elseif (-not $Fresh) { 'STALE: older than the stale limit (ledger not accepted, replaced, or the publisher blocked; see the Gateway status command)' }
                 elseif ($Contract -ne $ExpectedContract) { 'WRONG CONTRACT: ' + $Contract }
                 else { 'NOT LIVE' })
    return [pscustomobject][ordered]@{ configured = $Configured; file_present = $FilePresent; access = $Access; fresh = $Fresh; contract = $Contract; service_running = $ServiceRunning
                                       ledger_accepted = $LedgerAccepted; live = $live; summary = $summary }
}

function Get-LabArenaObservationAccess {
    <#  ACL analysis: can the Arena account read the publication, and is it kept out of ledger/state/logs?  #>
    param([AllowEmptyCollection()][string[]]$ArenaSids, $Folders)
    if (-not $ArenaSids.Count) { return 'Arena account unknown' }
    $pub = $Folders.publication
    if ($null -eq $pub -or $null -eq $pub.AllowSids) { return 'publication folder not present' }
    if ($pub.AllowSids -is [string]) { return 'publication ACL could not be read' }
    $reads = @($ArenaSids | Where-Object { $pub.AllowSids -contains $_ }).Count -gt 0
    foreach ($k in 'ledger', 'state', 'logs') { $f = $Folders.$k; if ($f -and $null -ne $f.AllowSids -and -not ($f.AllowSids -is [string]) -and @($f.AllowSids | Where-Object { $ArenaSids -contains $_ }).Count) { return "the Arena account has access to the Gateway $k folder" } }
    if (-not $reads) { return 'no read grant for the Arena account on the publication folder' }
    return 'verified'
}

function Test-LabGatewayFolderGrantsOnly {
    <#  Pure. True when every blocker is a Gateway folder that grants Arena or a broad group access (1.6.15):
        that one is repairable by removing the grant; any other blocker still needs the migration.  #>
    param([AllowEmptyCollection()][string[]]$Blockers = @())
    return ($Blockers.Count -gt 0 -and @($Blockers | Where-Object { $_ -notmatch 'folder .+ grants .+: Arena and broad groups must hold no access' }).Count -eq 0)
}

function Repair-LabGatewayFolderGrants {
    <#  1.6.15: removes the allow entries for Arena accounts and broad groups from the Gateway's ledger, state and logs
        folders (and the files in them). Only those grants: the Gateway's own account, SYSTEM and Administrators keep
        theirs. Nothing is deleted. Run only after the operator approved it.  #>
    param([Parameter(Mandatory)]$Readiness)
    $done = @()
    foreach ($k in 'ledger', 'state', 'logs') {
        $f = $Readiness.Access.Folders.$k
        if ($null -eq $f -or $null -eq $f.AllowSids -or $f.AllowSids -is [string]) { continue }
        foreach ($s in @($f.AllowSids | Where-Object { $Readiness.ArenaSids -contains $_ -or $script:BroadSids -contains $_ })) {
            $out = (& icacls.exe $f.Path /remove:g ('*' + $s) /T /C 2>&1 | Out-String).Trim()
            $who = Resolve-LabAccount $s
            $done += ('{0}: removed the access of {1} (icacls exit {2})' -f $f.Path, $who, $LASTEXITCODE)
            Register-LabChange -Kind 'security' -Description ("Gateway {0} folder: removed the access of {1}" -f $k, $who) `
                -Undo ([pscustomobject]@{ type = 'manual'; instructions = ('icacls "{0}" /grant "*{1}:(OI)(CI)RX" (the Gateway refuses to run with it)' -f $f.Path, $s) }) | Out-Null
        }
    }
    return $done
}

function Get-LabHubGatewayReadiness {
    <#  Everything C and E observe, in one object for Inspect, Verify and the report.
        -LedgerAccepted comes from the Gateway's own status command when it is installed.  #>
    param([Parameter(Mandatory)]$Config, [string]$LedgerAccepted = 'unknown', [scriptblock]$MembershipLookup = $null)
    $id = Get-LabArenaIdentity
    $arenaSids = @(Get-LabArenaAccountSids -Identity $id)
    $acc = Get-LabGatewayAccessState -Config $Config
    $membership = @(); $mstatus = 'observed'
    try { if ($MembershipLookup) { $membership = @(Get-LabPrivilegedMembership -Sids $arenaSids -Lookup $MembershipLookup) } else { $membership = @(Get-LabPrivilegedMembership -Sids $arenaSids) } } catch { $mstatus = 'inaccessible' }
    $verdict = Get-LabGatewayAccountVerdict -ArenaSids $arenaSids -Membership $membership -GatewaySid $acc.GatewaySid -Folders $acc.Folders -MembershipStatus $mstatus
    foreach ($m in @($id.Installed | Where-Object { $_.Moved -and $arenaSids -notcontains $_.Sid })) { $verdict.Notes += ('{0} holds a moved copy of Arena that does not start ({1}); rolling that migration back makes {0} an Arena account again' -f $m.Account, $(if ($m.MovedReason) { $m.MovedReason } else { 'moved-to-hub.json' })) }
    $configs = @(Get-LabArenaConfigPaths -Identity $id)
    $configuredAll = ($configs.Count -gt 0)
    foreach ($c in $configs) { $cfg = Read-LabJson $c.Path; $block = $null; if ($cfg -and $cfg.PSObject.Properties.Name -contains 'gateway') { $block = $cfg.gateway }; if (-not (Test-LabGatewayBlockMatches -Block $block -Wanted (Get-LabWantedGatewayBlock -Config $Config -Existing $block))) { $configuredAll = $false } }
    $file = [string]$Config.gateway.observation_file
    $present = Test-Path -LiteralPath $file
    $fresh = $false; $contract = ''
    if ($present) {
        $fresh = (((Get-Date) - (Get-Item -LiteralPath $file).LastWriteTime).TotalSeconds -le [int]$Config.gateway.stale_seconds)
        try { $contract = [string]((Get-Content -LiteralPath $file -Raw -ErrorAction Stop | ConvertFrom-Json).contract) } catch { $contract = 'unreadable' }
    }
    $obs = Get-LabObservationState -Configured $configuredAll -FilePresent $present -Access (Get-LabArenaObservationAccess -ArenaSids $arenaSids -Folders $acc.Folders) -Fresh $fresh `
        -Contract $contract -ExpectedContract $Config.gateway.observation_contract -ServiceRunning ($acc.Service.Present -and $acc.Service.State -eq 'Running') -LedgerAccepted $LedgerAccepted
    return [pscustomobject]@{ Identity = $id; ArenaSids = $arenaSids; Membership = $membership; Access = $acc; Accounts = $verdict; Observation = $obs; Configs = $configs }
}

# ---------------------------------------------------------------------------
# D. Verified head inventory (an input for the Gateway operator; nothing is enrolled)
# ---------------------------------------------------------------------------

function Get-LabHeadProbe {
    <#  Real connections from this host to each head's Ollama port: TCP, /api/version, /api/tags.  #>
    param([Parameter(Mandatory)][string[]]$Heads, [int]$Port = 11434)
    $out = @()
    foreach ($h in $Heads) {
        $open = Test-LabTcp -HostName $h -Port $Port -TimeoutMs 2000
        $v = $null; $tags = $null
        if ($open) { $v = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/version' -f $h, $Port) -TimeoutSec 5; if ($v) { $tags = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/tags' -f $h, $Port) -TimeoutSec 10 } }
        $out += [pscustomobject][ordered]@{ Head = $h; Open = $open; Version = $(if ($v) { [string]$v.version } else { '' })
            Models = @($(if ($tags) { @($tags.models | ForEach-Object { [pscustomobject]@{ name = [string]$_.name; digest = [string]$_.digest } }) }))
            ModelsRead = [bool]$tags; CheckedAt = (Get-Date).ToString('o') }
    }
    return $out
}

function ConvertTo-LabArenaNodeId { param([Parameter(Mandatory)][string]$Address) return ('head-' + $Address.Replace('.', '-')) }   # arena_service.admin.head_identity

function Get-LabHeadCapacity {
    <#  Pure - unit tested. Gateway 0.4.0's per-head capacity (FAILURES-AND-PLACEMENT.md section 4), from the lab head list:
        slots = the head's Ollama parallel requests (no head sets OLLAMA_NUM_PARALLEL: config gateway.head_capacity.slots);
        memory_gb = RAM minus the reserve for Windows and the Agent Arena viewer, or the GPU's VRAM on a GPU head (so its
        models stay on the card). Decided by the lab operator on 2026-11-12 (open item O10). $null for an unlisted head.  #>
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string]$Address, [string]$SlotsFile = 'C:\ProgramData\AgentArenaLab\gateway\head-slots.json')
    $h = @($Config.head_inventory | Where-Object { [string]$_.address -eq $Address }) | Select-Object -First 1
    if (-not $h) { return $null }
    $rule = $Config.gateway.head_capacity
    $slots = $(if ($rule -and $rule.slots) { [int]$rule.slots } else { 1 })
    # The operator's choice from SET-PARALLEL-PER-HEAD (hub, 2026-11-12) wins, so a later hub run never resets it to 1.
    if ($SlotsFile -and (Test-Path -LiteralPath $SlotsFile)) {
        try { $chosen = (Get-Content -LiteralPath $SlotsFile -Raw | ConvertFrom-Json).slots; $rid = 'head-' + ($Address -replace '\.', '-')
              if ($chosen -and $chosen.PSObject.Properties.Name -contains $rid -and [int]$chosen.$rid -ge 1) { $slots = [int]$chosen.$rid } } catch { }
    }
    $reserve = $(if ($rule -and $null -ne $rule.memory_reserve_gb) { [double]$rule.memory_reserve_gb } else { 4 })
    # [double]1.0 below: Max(1, x) would pick the int overload and drop the .4 of 11.4 or 27.4.
    $vram = $null; if ([string]$h.gpu -match '(\d+(?:\.\d+)?)\s*GB') { $vram = [double]$Matches[1] }
    $mem = $(if ($vram) { $vram } else { [math]::Round([math]::Max([double]1.0, [double]$h.ram_gb - $reserve), 1) })
    # 1.6.19: the machine-class memory APPLY-MODEL-CAPACITY gave the Gateway (head-slots.json memory_gb) wins too.
    if ($SlotsFile -and (Test-Path -LiteralPath $SlotsFile)) {
        try { $mm = (Get-Content -LiteralPath $SlotsFile -Raw | ConvertFrom-Json).memory_gb; $rid = 'head-' + ($Address -replace '\.', '-')
              if ($mm -and $mm.PSObject.Properties.Name -contains $rid -and [double]$mm.$rid -gt 0) { $mem = [double]$mm.$rid } } catch { }
    }
    return [pscustomobject][ordered]@{ slots = $slots; memory_gb = $mem }
}

function New-LabHeadInventory {
    <#  Pure - unit tested. Only heads that answered as Ollama from this host are listed; every other
        known address is listed as excluded with its reason. Provisional format until the Gateway
        confirms its bootstrap input.  #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][array]$Probes, [Parameter(Mandatory)]$Config, [string]$From = '', [string]$Computer = '')
    $port = [int]$Config.ollama.port
    $heads = @(); $excluded = @()
    foreach ($p in $Probes) {
        $n = Get-LabHostNumber $p.Head
        $protected = @($Config.protected_addresses | Where-Object { $_.address -eq $p.Head }) | Select-Object -First 1
        if ($n -lt [int]$Config.ranges.head[0] -or $n -gt [int]$Config.ranges.head[1] -or $p.Head -eq $Config.hub) { $excluded += [pscustomobject]@{ address = $p.Head; reason = 'not a head address' }; continue }
        if (-not $p.Open) { $excluded += [pscustomobject]@{ address = $p.Head; reason = 'port 11434 did not accept a connection from this host' }; continue }
        if (-not $p.Version) { $excluded += [pscustomobject]@{ address = $p.Head; reason = 'port open but Ollama did not answer /api/version' }; continue }
        $heads += [pscustomobject][ordered]@{
            resource_id = (ConvertTo-LabArenaNodeId $p.Head); arena_node_id = (ConvertTo-LabArenaNodeId $p.Head); address = $p.Head
            endpoint = ('http://{0}:{1}' -f $p.Head, $port); note = $(if ($protected) { 'protected address: ' + $protected.reason } else { '' })
            verified = [pscustomobject][ordered]@{ ollama_version = $p.Version; models_read = $p.ModelsRead; models = @($p.Models); checked_at = $p.CheckedAt; from_address = $From; from_computer = $Computer }
            capacity = (Get-LabHeadCapacity -Config $Config -Address $p.Head)
        }
    }
    return [pscustomobject][ordered]@{
        schema = 'agent-arena.verified-head-inventory/1'; status = 'accepted as is by Inference Gateway 0.3.0 (manifest head_inventory_input)'
        purpose = 'input for the Gateway operator''s bootstrap "resources"; nothing has been enrolled in the Gateway'
        generated_at = (Get-Date).ToString('o'); generated_on = $Computer; checked_from = $From
        identity_rule = 'resource_id = Arena head node ID head-192-0-2-N (ARENA_INTEGRATION.md section 4)'
        heads = $heads; excluded = $excluded
    }
}

function Export-LabHeadInventory {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Inventory)
    $name = 'verified-head-inventory-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $safe = Protect-LabObject $Inventory
    $a = Join-Path $Run.Folder $name; $b = Join-Path $Run.ReportsDir $name
    Save-LabJson -InputObject $safe -Path $a; Copy-Item -LiteralPath $a -Destination $b -Force
    return $b
}

# ---------------------------------------------------------------------------
# Arena standard-account migration (Gateway open item O3). Prepared and tested; it runs only on the
# operator's separate approval. It reuses the field-verified "moved data" path of the hub installer.
# ---------------------------------------------------------------------------

function Get-LabArenaMigrationPlan {
    <#  Read-only. Which Arena install moves where, and whether the target qualifies.  #>
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string]$Target, $Identity = $null, $Membership = $null, [string]$GatewayAccount = '')
    if (-not $Identity) { $Identity = Get-LabArenaIdentity }
    $blockers = @(); $warnings = @()
    # Only a live install can be the source: a copy marked moved is what an earlier export left behind.
    $installs = @(@($Identity.Installed) | Where-Object { -not $_.Moved })
    $moved = @(@($Identity.Installed) | Where-Object { $_.Moved })
    if ($installs.Count -eq 0 -and $moved.Count) { $blockers += ('Arena of {0} was already exported by an earlier migration (its data is marked moved: {1}): continue with step 2 as the target account, or roll that run back first' -f (($moved | ForEach-Object { $_.Account }) -join ', '), (($moved | ForEach-Object { $_.MovedReason }) -join '; ')) }
    elseif ($installs.Count -ne 1) { $blockers += ('expected exactly one live Arena hub install on this computer, found {0}' -f $installs.Count) }
    $source = $(if ($installs.Count -eq 1) { $installs[0] } else { $null })
    $t = Get-LabLocalAccount -Name $Target
    if ($t.PSObject.Properties.Name -contains 'NotLocal' -and $t.NotLocal) { $blockers += "$Target is not a local account of this computer ($env:COMPUTERNAME): Arena moves only to a local standard account" }
    elseif (-not $t.Exists) { $blockers += "the target account $Target does not exist: create it first (a separate, approved step; F: never creates Arena accounts silently)" }
    else {
        if (-not $Membership) { $Membership = @(Get-LabPrivilegedMembership -Sids @($t.Sid)) }
        $m = @($Membership | Where-Object { $_.Sid -eq $t.Sid }) | Select-Object -First 1
        if (-not $m -or $m.State -ne 'standard') { $blockers += ('{0} is not verified as a standard account ({1})' -f $Target, $(if ($m) { $m.State + ' ' + ($m.Groups -join ',') } else { 'not checked' })) }
        if ($source -and $t.Sid -eq $source.Sid) { $blockers += "$Target already runs Arena" }
        if ($GatewayAccount -and ($Target -ieq $GatewayAccount -or ($Target -split '\\')[-1] -ieq ($GatewayAccount -split '\\')[-1])) { $blockers += 'the Arena account cannot be the Gateway service account' }
        if (-not $t.Enabled) { $blockers += "$Target is disabled" }
    }
    $cfg = $null; if ($source) { $cfg = Get-LabArenaConfigPath -ProfilePath $source.Profile; if (-not (Test-Path -LiteralPath $cfg)) { $blockers += "no Arena configuration at $cfg" } }
    $warnings += 'events that arrive between the export and the new account''s first start are not carried over: do it in a quiet period'
    $steps = @(
        "1. (now, as the maintenance administrator) stop the Arena hub of $(if ($source) { $source.Account } else { '?' }) and export its data with the service identity; the old copy is marked moved and will refuse to start",
        "2. sign out; sign in as $Target; if 'py -3 --version' fails, run INSTALL-PYTHON-FOR-THIS-USER.cmd from the export folder first; then run the exported INSTALL-ADMIN-HUB.cmd (unchanged installer, moved data: same service identity, registrations and enrollments; start at sign-in and shortcut for $Target)",
        "3. sign back in as the maintenance administrator; run START-LAB-PROVISIONER > Administration hub > verify: Arena must run as $Target and answer as $($Config.arena.service_id)",
        "4. re-apply Arena's Gateway configuration for $Target (hub role step), then pass $Target as the Arena account to the Gateway's ACL script",
        "5. when satisfied, as $(if ($source) { $source.Account } else { 'the old account' }): run UNINSTALL-ADMIN-HUB.cmd (keeps data) to remove its start entry; the marked data stays as the rollback copy")
    return [pscustomobject]@{ Ok = ($blockers.Count -eq 0); Blockers = $blockers; Warnings = $warnings; Source = $source; SourceConfig = $cfg; Target = $t; Steps = $steps
                              Rollback = @('stop the Arena processes of the new account (F: does it)', 'remove the moved marker on the old data (hub_migrate rollback)', 'sign in as the old account: its admin window starts the old service with its data as at the export', 'as the new account, UNINSTALL-ADMIN-HUB.cmd') }
}

function Stop-LabArenaProcesses {
    <#  Stops one account's Arena hub processes (window, console, event service), nothing else.  #>
    param([Parameter(Mandatory)][string]$Sid)
    $n = 0
    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { ($_.Name -eq 'AgentArena.exe' -and $_.CommandLine -match '--admin') -or ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'arena_service') })) {
        $owner = ''; try { $owner = [string](Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid -ErrorAction Stop).Sid } catch { }
        if ($owner -eq $Sid) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $n++ } catch { } }
    }
    Start-Sleep -Milliseconds 800
    return $n
}

function Get-LabVenvBasePython {
    <#  Pure over a pyvenv.cfg text: the base interpreter a venv was made from ("home = <folder>").  #>
    param([AllowEmptyString()][string]$Text)
    foreach ($line in @($Text -split "`r?`n")) { if ($line -match '^\s*home\s*=\s*(.+?)\s*$') { return (Join-Path $Matches[1] 'python.exe') } }
    return ''
}

function Get-LabArenaMigrationPython {
    <#  A Python 3.12+ to run Arena's own migration helper. On the hub the Arena account's Python is often
        not on the administrator's PATH (DESKTOP-REDACT10, 1.3.0: "not found on PATH" while Arena ran), so the
        interpreter Arena itself runs on is the first choice after the machine-wide Gateway runtime.  #>
    param($Source = $null, [string]$MachineWide = 'C:\Program Files\Python312\python.exe')
    $cands = New-Object System.Collections.Generic.List[string]
    if ($MachineWide) { $cands.Add($MachineWide) }
    if ($Source -and $Source.Sid) {
        foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'arena_service' -and $_.ExecutablePath })) {
            $owner = ''; try { $owner = [string](Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid -ErrorAction Stop).Sid } catch { }
            if ($owner -eq $Source.Sid) { $cands.Add(([string]$p.ExecutablePath -replace '(?i)pythonw\.exe$', 'python.exe')) }
        }
    }
    if ($Source -and $Source.Profile) {
        $venv = Join-Path $Source.Profile 'AppData\Local\Programs\AgentArenaHub\app\.venv'
        $cfg = Join-Path $venv 'pyvenv.cfg'
        if (Test-Path -LiteralPath $cfg) { $b = Get-LabVenvBasePython -Text ([IO.File]::ReadAllText($cfg)); if ($b) { $cands.Add($b) } }
        $cands.Add((Join-Path $venv 'Scripts\python.exe'))
    }
    foreach ($c in $cands) {
        if (-not $c -or -not (Test-Path -LiteralPath $c)) { continue }
        try { $ok = & $c -E -s -c 'import sys; print(int(sys.version_info >= (3, 12)))' 2>$null; if ($LASTEXITCODE -eq 0 -and [string]$ok -eq '1') { return $c } } catch { }
    }
    try { $v = & py -3 -c 'import sys; print(sys.executable)' 2>$null; if ($LASTEXITCODE -eq 0 -and $v) { return [string]$v } } catch { }
    try { $v = & python -c 'import sys; print(sys.executable)' 2>$null; if ($LASTEXITCODE -eq 0 -and $v) { return [string]$v } } catch { }
    return ''
}

function Invoke-LabArenaMigrationExport {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Plan, [Parameter(Mandatory)][string]$HubFolder, [string]$PythonInstaller = '')
    if (-not $Plan -or -not $Plan.Ok) { throw ('migration refused: ' + $(if ($Plan) { $Plan.Blockers -join '; ' } else { 'no migration plan' })) }
    # Resolved before Arena is stopped: the running service names its own interpreter.
    $py = Get-LabArenaMigrationPython -Source $Plan.Source; if (-not $py) { throw 'no Python 3.12 found to run the migration helper (machine-wide, Arena''s own, or on PATH)' }
    $stopped = Stop-LabArenaProcesses -Sid $Plan.Source.Sid
    $dest = Join-Path $Run.Root ('arena-migration\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    # The copy holds the service's credentials: SYSTEM, Administrators and the target account only.
    & icacls.exe $dest /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*{0}:(OI)(CI)M' -f $Plan.Target.Sid) /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not restrict $dest" }
    $r = Invoke-LabGatewayCommand -FileName $py -Arguments @('-E', '-s', '-m', 'arena_service.hub_migrate', 'export', '--config', $Plan.SourceConfig, '--source', $HubFolder, '--dest', $dest, '--account', $Plan.Target.Name) -Label 'arena-migrate-export' -WorkingDirectory (Join-Path $HubFolder 'app') -Run $Run
    if (-not $r.Ok) { Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue; throw ('the migration helper refused (' + $r.Meaning + '): ' + $(if ($r.Json) { $r.Json.error } else { $r.Stderr })) }
    # The Arena installer needs Python 3.12+ on the new account's PATH. The Gateway's machine-wide Python
    # is deliberately not on PATH (PrependPath=0, no launcher), so the account gets its own per-user copy
    # from the same verified official installer.
    if ($PythonInstaller -and (Test-Path -LiteralPath $PythonInstaller)) {
        Copy-Item -LiteralPath $PythonInstaller -Destination $dest -Force
        $leaf = [IO.Path]::GetFileName($PythonInstaller)
        [IO.File]::WriteAllText((Join-Path $dest 'INSTALL-PYTHON-FOR-THIS-USER.cmd'), ("@echo off`r`nrem Python 3.12 for this Windows user only (the Arena installer needs it on PATH). Run as the Arena account, not as administrator.`r`n`"%~dp0{0}`" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0`r`necho.`r`necho Open a NEW window and check: py -3 --version`r`npause`r`n" -f $leaf), (New-Object Text.ASCIIEncoding))
    }
    Register-LabChange -Kind 'arena-migration' -Description ("Arena data of {0} exported for {1} (service {2}, {3} displays); the old copy is marked moved" -f $Plan.Source.Account, $Plan.Target.Name, $r.Json.service_id, $r.Json.active_displays) `
        -Undo ([pscustomobject]@{ type = 'arena-migration'; config = $Plan.SourceConfig; dest = $dest; hub = $HubFolder; target_sid = $Plan.Target.Sid; python = $py }) | Out-Null
    return (New-LabOutcome 'DONE' ('stopped {0} process(es); exported to {1}. Next: sign in as {2} and run {3}' -f $stopped, $r.Json.folder, $Plan.Target.Name, (Join-Path $r.Json.folder 'INSTALL-ADMIN-HUB.cmd')) $r.Json)
}

function Undo-LabArenaMigration {
    param([Parameter(Mandatory)]$Undo)
    $n = Stop-LabArenaProcesses -Sid $Undo.target_sid
    $r = Invoke-LabGatewayCommand -FileName $Undo.python -Arguments @('-E', '-s', '-m', 'arena_service.hub_migrate', 'rollback', '--config', $Undo.config) -Label 'arena-migrate-rollback' -WorkingDirectory (Join-Path $Undo.hub 'app')
    if (-not $r.Ok) { throw ('rollback refused: ' + $(if ($r.Json) { $r.Json.error } else { $r.Stderr })) }
    if (Test-Path -LiteralPath $Undo.dest) { Remove-Item -LiteralPath $Undo.dest -Recurse -Force -ErrorAction SilentlyContinue }
    return ("stopped {0} Arena process(es) of the new account; the old Arena may start again (sign in as its account). As the new account, run UNINSTALL-ADMIN-HUB.cmd" -f $n)
}

Export-ModuleMember -Function @('Test-LabGatewayFolderGrantsOnly', 'Repair-LabGatewayFolderGrants',
    'Get-LabArenaMigrationPlan', 'Stop-LabArenaProcesses', 'Get-LabVenvBasePython', 'Get-LabArenaMigrationPython', 'Invoke-LabArenaMigrationExport', 'Undo-LabArenaMigration',
    'Get-LabArenaInstallRecord', 'Select-LabActiveArenaInstall', 'Get-LabArenaAccountSids',
    'Set-LabNetworkProfile', 'Undo-LabNetworkProfile', 'Test-LabHubLabNetwork', 'Get-LabHeadCapacity', 'Get-LabTimeServerState', 'Test-LabTimeServerCompliant', 'Set-LabTimeServer', 'Undo-LabTimeServer',
    'Set-LabHubFirewallPrerequisites', 'Set-LabRemoteDesktopScope', 'Undo-LabRegistryValue', 'Undo-LabFileChange',
    'ConvertTo-LabResolvableAccount', 'Resolve-LabSid', 'Resolve-LabAccount', 'Get-LabAdministratorSids', 'Get-LabArenaIdentity', 'Get-LabAclAllowSids', 'Get-LabGatewayAccessState', 'Get-LabGatewayAccountVerdict',
    'Get-LabArenaConfigPath', 'Get-LabArenaConfigPaths', 'Get-LabWantedGatewayBlock', 'Test-LabGatewayBlockMatches', 'Merge-LabGatewayConfig', 'Set-LabArenaGatewayConfig',
    'Get-LabObservationState', 'Get-LabArenaObservationAccess', 'Get-LabHubGatewayReadiness',
    'Get-LabHeadProbe', 'ConvertTo-LabArenaNodeId', 'New-LabHeadInventory', 'Export-LabHeadInventory'
)
