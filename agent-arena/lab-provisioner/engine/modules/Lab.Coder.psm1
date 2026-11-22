<#
    Coder application laptop (192.0.2.202): its identity, Remote Desktop from the control workstation over Wi-Fi only,
    Ethernet and Wi-Fi kept apart, Coder's own Gateway client files and their permissions. Windows PowerShell 5.1.

    Nothing here changes the clock, Smart App Control or any other security setting: Coder's laptop keeps its own
    time source and its own protections (operator's decision, 2026-11-12). The Gateway token is copied as bytes and
    never read, shown or logged.
#>

$script:TsKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
$script:RdpTcpKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
$script:WellKnown = @{ 'S-1-5-18' = 'SYSTEM'; 'S-1-5-32-544' = 'Administrators' }
$script:Broad = @{ 'S-1-1-0' = 'Everyone'; 'S-1-5-11' = 'Authenticated Users'; 'S-1-5-32-545' = 'Users'; 'S-1-5-4' = 'INTERACTIVE'; 'S-1-5-32-555' = 'Remote Desktop Users'; 'S-1-3-0' = 'CREATOR OWNER' }

# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------

function Get-LabCoderLayout {
    <#  Coder's Gateway client files and the values its configuration must carry. Pure.  #>
    param([Parameter(Mandatory)]$Config)
    $c = $Config.coder
    return [pscustomobject][ordered]@{
        Folder = [string]$c.gateway_dir; ClientConfig = [string]$c.client_config; Token = [string]$c.token_file; Ca = [string]$c.ca_file
        Url = ('https://{0}:{1}' -f $Config.hub, $Config.gateway.port); ServiceId = [string]$c.service_id; Contract = [string]$c.contract
        AppId = [string]$Config.roles.coder.app_id; CaSha256 = ([string]$Config.gateway.ca_sha256).ToLowerInvariant()
        TransferFolder = [string]$c.token_transfer_folder; RdpRule = [string]$c.rdp_rule; RdpRuleDisplay = [string]$c.rdp_rule_display }
}

function ConvertTo-LabJsonString {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    $sb = New-Object Text.StringBuilder
    foreach ($ch in $Value.ToCharArray()) {
        switch ($ch) {
            '"' { [void]$sb.Append('\"') } '\' { [void]$sb.Append('\\') }
            default { if ([int]$ch -lt 32) { [void]$sb.AppendFormat('\u{0:x4}', [int]$ch) } else { [void]$sb.Append($ch) } }
        }
    }
    return '"' + $sb.ToString() + '"'
}

function New-LabCoderClientConfigText {
    <#  Exactly Coder's config/gateway-client.template.json for these values: the seven file-based fields, LF line
        endings, two-space indent, no byte-order mark, a final newline. Coder's own loader refuses any other field
        (token_environment included). Pure.  #>
    param([Parameter(Mandatory)]$Layout)
    $lines = @(
        '{'
        '  "configuration_version": 1,'
        ('  "contract": {0},' -f (ConvertTo-LabJsonString $Layout.Contract))
        ('  "service_id": {0},' -f (ConvertTo-LabJsonString $Layout.ServiceId))
        ('  "url": {0},' -f (ConvertTo-LabJsonString $Layout.Url))
        ('  "application_id": {0},' -f (ConvertTo-LabJsonString $Layout.AppId))
        ('  "token_file": {0},' -f (ConvertTo-LabJsonString $Layout.Token))
        ('  "ca_file": {0}' -f (ConvertTo-LabJsonString $Layout.Ca))
        '}'
    )
    return (($lines -join "`n") + "`n")
}

function Test-LabCoderRdpSource {
    <#  The control workstation's Wi-Fi address may reach Remote Desktop only if it is a plain IPv4 address in this
        laptop's Wi-Fi subnet, not on the lab subnet and not this laptop itself. Pure.  #>
    param([string]$Source, [string]$WifiAddress, [int]$WifiPrefix, [Parameter(Mandatory)][string]$LabPrefix)
    $ip = $null
    if (-not [Net.IPAddress]::TryParse([string]$Source, [ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork' -or $Source -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { return [pscustomobject]@{ Ok = $false; Reason = "'$Source' is not an IPv4 address" } }
    if ($Source.StartsWith($LabPrefix + '.')) { return [pscustomobject]@{ Ok = $false; Reason = "$Source is on the lab cable ($LabPrefix.0/24): Remote Desktop comes over Wi-Fi only" } }
    if (-not $WifiAddress) { return [pscustomobject]@{ Ok = $false; Reason = 'this laptop has no Wi-Fi address: connect its Wi-Fi first' } }
    if ($Source -eq $WifiAddress) { return [pscustomobject]@{ Ok = $false; Reason = "$Source is this laptop's own Wi-Fi address; type the control workstation's" } }
    if ($WifiPrefix -lt 8 -or $WifiPrefix -gt 30) { return [pscustomobject]@{ Ok = $false; Reason = "the Wi-Fi network /$WifiPrefix is not a normal local network" } }
    # Plain arithmetic: Windows PowerShell 5.1 reads 0xFFFFFFFF as -1, so no hexadecimal masks here.
    $toNum = { param($a) $b = ([Net.IPAddress]::Parse($a)).GetAddressBytes(); [double]$b[0] * 16777216 + [double]$b[1] * 65536 + [double]$b[2] * 256 + [double]$b[3] }
    $block = [math]::Pow(2, 32 - $WifiPrefix)
    if ([math]::Floor((& $toNum $Source) / $block) -ne [math]::Floor((& $toNum $WifiAddress) / $block)) { return [pscustomobject]@{ Ok = $false; Reason = "$Source is not on this laptop's Wi-Fi network ($WifiAddress/$WifiPrefix)" } }
    return [pscustomobject]@{ Ok = $true; Reason = "$Source is on this laptop's Wi-Fi network ($WifiAddress/$WifiPrefix)" }
}

function Get-LabCoderSeparationFindings {
    <#  Ethernet (lab) and Wi-Fi stay apart: no forwarding on any interface, no Internet Connection Sharing, no
        network bridge, no default gateway and no DNS server on the lab adapter. Pure.  #>
    param([object[]]$Forwarding = @(), [object[]]$Adapters = @(), [object[]]$Sharing = @(), $LabAdapter = $null)
    $f = @()
    foreach ($i in @($Forwarding)) { if ([string]$i.Forwarding -eq 'Enabled') { $f += ('IP forwarding is on for {0} ({1})' -f $i.InterfaceAlias, $i.AddressFamily) } }
    foreach ($a in @($Adapters)) { if ([string]$a.Description -match '(?i)MAC Bridge|Network Bridge') { $f += ('a network bridge exists: {0}' -f $a.Name) } }
    foreach ($s in @($Sharing)) { if ($s.Enabled) { $f += ('Internet Connection Sharing is on for {0}' -f $s.Name) } }
    if ($LabAdapter) {
        if (@($LabAdapter.DefaultGateways | Where-Object { $_ }).Count) { $f += ('the lab adapter {0} has a default gateway ({1}): the lab has no router and the Gateway service is not one' -f $LabAdapter.Name, (@($LabAdapter.DefaultGateways) -join ', ')) }
        if (@($LabAdapter.DnsServers | Where-Object { $_ }).Count) { $f += ('the lab adapter {0} has DNS servers ({1}): the lab has none' -f $LabAdapter.Name, (@($LabAdapter.DnsServers) -join ', ')) }
    }
    return $f
}

function Test-LabCoderAclEntries {
    <#  Coder's Gateway folder or file: inheritance off; only SYSTEM and Administrators may change it; the runtime account
        reads it (and cannot change it); nobody else has any access. Pure.  #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Entries, [bool]$Protected, [Parameter(Mandatory)][string]$RuntimeSid, [switch]$AllowInherited)
    $f = @()
    # A file inside Coder's protected folder inherits exactly the folder's three grants; the folder itself must not inherit.
    if (-not $Protected -and -not $AllowInherited) { $f += 'inheritance is on (permissions come from the parent folder)' }
    $write = 'FullControl|Modify|Write|Delete|ChangePermissions|TakeOwnership|AppendData|CreateFiles'
    $runtimeReads = $false
    foreach ($e in @($Entries)) {
        $sid = [string]$e.Sid; $rights = [string]$e.Rights
        if ([string]$e.Type -ne 'Allow') { continue }
        if ($script:WellKnown.ContainsKey($sid)) { continue }
        if ($sid -eq $RuntimeSid) {
            if ($rights -match $write) { $f += ("the runtime account may change it ($rights)") }
            if ($rights -match 'Read') { $runtimeReads = $true }
            continue
        }
        $name = $(if ($script:Broad.ContainsKey($sid)) { $script:Broad[$sid] } else { $sid })
        $f += ("{0} has access ({1})" -f $name, $rights)
    }
    if (-not $runtimeReads) { $f += 'the runtime account cannot read it' }
    return $f
}

function Find-LabCoderTokenSource {
    <#  Coder's token on a transfer stick: <drive>\<TransferFolder>\coder.token, on a removable drive that is not the
        provisioner stick. Pure given the drive list.  #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Drives, [string]$ProvisionerRoot = '', [Parameter(Mandatory)][string]$TransferFolder, [string]$FileName = 'coder.token', [scriptblock]$Exists = { param($p) Test-Path -LiteralPath $p -PathType Leaf })
    $skip = ''; if ($ProvisionerRoot) { $skip = ([IO.Path]::GetPathRoot($ProvisionerRoot)).ToUpperInvariant() }
    $found = @()
    foreach ($d in @($Drives)) {
        $root = ([string]$d.Root).ToUpperInvariant(); if (-not $root.EndsWith('\')) { $root += '\' }
        if ($skip -and $root -eq $skip) { continue }
        # [IO.Path]::Combine, not Join-Path: Windows PowerShell 5.1's Join-Path throws for a drive letter that is not
        # present (a stick that was pulled), which stopped the search instead of skipping that drive.
        $p = [IO.Path]::Combine($root, $TransferFolder, $FileName)
        if (& $Exists $p) { $found += $p }
    }
    return $found
}

function Get-LabCoderRuntimeVerdict {
    <#  Which local account runs Coder. Pure.  #>
    param([Parameter(Mandatory)]$Account, [string]$Membership = 'unknown')
    $problems = @(); $notes = @()
    if (-not $Account.Enabled) { $problems += "$($Account.Name) is disabled" }
    if ([string]$Account.Sid -match '-500$') { $problems += 'the built-in Administrator account does not run applications' }
    if ([string]$Account.Sid -notmatch '^S-1-5-21-') { $problems += "$($Account.Name) is not a local account of this computer" }
    if ($Membership -eq 'privileged') { $notes += "$($Account.Name) is an administrator: Coder then runs with administrator rights when elevated; a standard account is safer" }
    elseif ($Membership -eq 'unknown') { $notes += "whether $($Account.Name) is an administrator could not be read" }
    return [pscustomobject]@{ Ok = ($problems.Count -eq 0); Problems = $problems; Notes = $notes }
}

# ---------------------------------------------------------------------------
# What this laptop is (read-only)
# ---------------------------------------------------------------------------

function Get-LabCoderFacts {
    param($Config, [switch]$Quiet)
    if ($Quiet) { function Add-LabFact { param($a, $b, $c) } }
    $users = @(); try { $users = @(Get-LocalUser -ErrorAction Stop | ForEach-Object { [pscustomobject][ordered]@{ Name = $_.Name; Sid = $_.SID.Value; Enabled = [bool]$_.Enabled; Microsoft = ([string]$_.PrincipalSource -eq 'MicrosoftAccount') } }) } catch { }
    $priv = @{}
    if ($users.Count) { try { foreach ($m in @(Get-LabPrivilegedMembership -Sids @($users | ForEach-Object Sid))) { $priv[$m.Sid] = $m.State } } catch { } }
    foreach ($u in $users) { $u | Add-Member -NotePropertyName Membership -NotePropertyValue $(if ($priv.ContainsKey($u.Sid)) { $priv[$u.Sid] } else { 'unknown' }) }
    $ed = ''; try { $ed = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop).EditionID } catch { }
    $deny = $null; $nla = $null
    try { $deny = (Get-ItemProperty -Path $script:TsKey -Name fDenyTSConnections -ErrorAction Stop).fDenyTSConnections } catch { }
    try { $nla = (Get-ItemProperty -Path $script:RdpTcpKey -Name UserAuthentication -ErrorAction Stop).UserAuthentication } catch { }
    $builtin = @(); try { $builtin = @(Get-NetFirewallRule -Group '@FirewallAPI.dll,-28752' -ErrorAction Stop | ForEach-Object { [pscustomobject]@{ Name = $_.Name; Display = $_.DisplayName; Enabled = ([string]$_.Enabled -eq 'True'); Profile = [string]$_.Profile } }) } catch { }
    $own = $null; $lay = $null; if ($Config) { $lay = Get-LabCoderLayout -Config $Config; try { $r = Get-NetFirewallRule -Name $lay.RdpRule -ErrorAction Stop; $af = $r | Get-NetFirewallAddressFilter; $if = $r | Get-NetFirewallInterfaceTypeFilter; $own = [pscustomobject]@{ Enabled = ([string]$r.Enabled -eq 'True'); Remote = @($af.RemoteAddress); InterfaceType = [string]$if.InterfaceType } } catch { } }
    $fwd = @(); try { $fwd = @(Get-NetIPInterface -ErrorAction Stop | Where-Object { $_.InterfaceAlias -notmatch '^Loopback' } | ForEach-Object { [pscustomobject]@{ InterfaceAlias = $_.InterfaceAlias; AddressFamily = [string]$_.AddressFamily; Forwarding = [string]$_.Forwarding } }) } catch { }
    $share = @()
    try {
        $h = New-Object -ComObject HNetCfg.HNetShare
        foreach ($c in @($h.EnumEveryConnection)) { $cfg = $h.INetSharingConfigurationForINetConnection.Invoke($c); $p = $h.NetConnectionProps.Invoke($c); $share += [pscustomobject]@{ Name = [string]$p.Name; Enabled = [bool]$cfg.SharingEnabled } }
    } catch { }
    $adapters = @(); try { $adapters = @(Get-CapstoneAdapterInventory) } catch { }
    $wifi = @(); try { $wifi = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object { $_.NdisPhysicalMedium -eq 9 -or $_.PhysicalMediaType -match '802\.11|Native 802' } | ForEach-Object { $ix = $_.ifIndex; $ip = @(Get-NetIPAddress -InterfaceIndex $ix -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' }) | Select-Object -First 1; [pscustomobject]@{ Name = $_.Name; Status = [string]$_.Status; Mac = [string]$_.MacAddress; Address = $(if ($ip) { $ip.IPAddress } else { '' }); Prefix = $(if ($ip) { [int]$ip.PrefixLength } else { 0 }) } }) } catch { }
    $ssid = ''; try { $w = & netsh.exe wlan show interfaces 2>$null; $m = @($w | Select-String -Pattern '^\s*SSID\s*:\s*(.+)$') | Select-Object -First 1; if ($m) { $ssid = $m.Matches[0].Groups[1].Value.Trim() } } catch { }
    $f = [pscustomobject][ordered]@{
        Accounts = $users; Edition = $ed; RdpHostSupported = ($ed -and $ed -notmatch '^(Core|Home)')
        Rdp = [pscustomobject]@{ Enabled = ($deny -eq 0); Nla = ($nla -eq 1); BuiltinRules = $builtin; OwnRule = $own }
        Forwarding = $fwd; Sharing = $share; Adapters = $adapters; Wifi = $wifi; WifiSsid = $ssid }
    Add-LabFact coder 'local accounts' $(if ($users.Count) { @($users | ForEach-Object { '{0} ({1}{2}{3})' -f $_.Name, $_.Membership, $(if ($_.Enabled) { '' } else { ', disabled' }), $(if ($_.Microsoft) { ', Microsoft account' } else { '' }) }) } else { 'not readable' })
    Add-LabFact coder 'Remote Desktop host' ('edition {0}: {1}; Remote Desktop {2}; NLA {3}' -f $ed, $(if ($f.RdpHostSupported) { 'can host' } else { 'CANNOT host Remote Desktop (Home)' }), $(if ($f.Rdp.Enabled) { 'on' } else { 'off' }), $(if ($f.Rdp.Nla) { 'required' } else { 'not required' }))
    Add-LabFact coder 'Remote Desktop firewall' ('built-in rules enabled: {0}; Coder rule: {1}' -f (@($builtin | Where-Object Enabled | ForEach-Object Display) -join ', '), $(if ($own) { 'from ' + ($own.Remote -join ',') + ' on ' + $own.InterfaceType } else { 'none' }))
    Add-LabFact coder 'Wi-Fi' $(if ($wifi.Count) { @($wifi | ForEach-Object { '{0}: {1}, {2}/{3}{4}' -f $_.Name, $_.Status, $_.Address, $_.Prefix, $(if ($ssid) { ", SSID $ssid" } else { '' }) }) } else { 'no Wi-Fi adapter' })
    Add-LabFact coder 'separation' $(if ((Get-LabCoderSeparationFindings -Forwarding $fwd -Adapters $adapters -Sharing $share).Count) { (Get-LabCoderSeparationFindings -Forwarding $fwd -Adapters $adapters -Sharing $share) } else { 'no forwarding, sharing or bridge' })
    return $f
}

# ---------------------------------------------------------------------------
# Changes (each recorded with its undo)
# ---------------------------------------------------------------------------

function Read-LabCoderRuntimeAccount {
    <#  The operator names the local account that runs Coder (never assumed). Returns Name, Sid, Membership.  #>
    param([Parameter(Mandatory)]$Facts)
    $list = @($Facts.Accounts | Where-Object { $_.Enabled -and $_.Sid -match '^S-1-5-21-' -and $_.Sid -notmatch '-(500|501|503|504)$' })
    if (-not $list.Count) { throw 'no enabled local account to run Coder: create one (Settings > Accounts > Other users) and run this again' }
    Write-Host ''
    Write-Host '  Which account runs Coder on this laptop? Coder''s Gateway token is readable by that account only (and administrators).'
    for ($i = 0; $i -lt $list.Count; $i++) { Write-Host ('   {0}. {1}  ({2}{3})' -f ($i + 1), $list[$i].Name, $list[$i].Membership, $(if ($list[$i].Microsoft) { ', Microsoft account' } else { '' })) }
    $n = Read-LabInput -Prompt 'Type its number (Enter to stop)'
    if ($n -notmatch '^\d+$' -or [int]$n -lt 1 -or [int]$n -gt $list.Count) { return $null }
    $a = $list[[int]$n - 1]
    $v = Get-LabCoderRuntimeVerdict -Account $a -Membership $a.Membership
    if (-not $v.Ok) { throw ($v.Problems -join '; ') }
    foreach ($note in $v.Notes) { Add-LabWarning $note }
    return $a
}

function Undo-LabCoderChange {
    param([Parameter(Mandatory)]$Undo)
    switch ([string]$Undo.action) {
        'remove-firewall-rule' { Remove-NetFirewallRule -Name $Undo.name -ErrorAction SilentlyContinue; return ('firewall rule {0} removed' -f $Undo.name) }
        'remove-group-member' { Remove-LocalGroupMember -SID $Undo.group -Member $Undo.member -ErrorAction SilentlyContinue; return ('{0} taken out of {1}' -f $Undo.member, $Undo.group) }
        'enable-firewall-rule' { Enable-NetFirewallRule -Name $Undo.name -ErrorAction SilentlyContinue; return ('firewall rule {0} enabled again' -f $Undo.name) }
        'remove-file' { if (Test-Path -LiteralPath $Undo.path) { Remove-Item -LiteralPath $Undo.path -Force }; return ('{0} removed' -f $Undo.path) }
        default { throw ('no Coder undo for ' + $Undo.action) }
    }
}

function Set-LabCoderRegistry {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][int]$Value)
    $before = $null; try { $before = (Get-ItemProperty -Path $Path -Name $Name -ErrorAction Stop).$Name } catch { }
    if ($before -eq $Value) { return $false }
    Set-ItemProperty -Path $Path -Name $Name -Value $Value -Type DWord -ErrorAction Stop
    Register-LabChange -Kind 'registry' -Description ('{0}\{1} = {2} (was {3})' -f $Path, $Name, $Value, $(if ($null -eq $before) { 'not set' } else { $before })) -Undo ([pscustomobject]@{ type = 'registry'; path = $Path; name = $Name; value = $before }) | Out-Null
    return $true
}

function Set-LabCoderRdp {
    <#  Remote Desktop with Network Level Authentication, reachable only from the control workstation's Wi-Fi address,
        only on wireless interfaces. The built-in Remote Desktop rules stay as they are unless the operator turns an
        enabled one off (it would admit any address). Console sign-in is not touched.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Facts, [Parameter(Mandatory)]$Layout, [Parameter(Mandatory)]$Runtime, [Parameter(Mandatory)][string]$LabPrefix)
    if (-not $Facts.RdpHostSupported) { throw ("Windows {0} cannot host Remote Desktop (Home edition): upgrade this laptop to Pro, or manage it at its own console" -f $Facts.Edition) }
    $w = @($Facts.Wifi | Where-Object { $_.Address }) | Select-Object -First 1
    if (-not $w) { throw 'Wi-Fi is not connected: connect this laptop to the same Wi-Fi network as the control workstation (taskbar > Wi-Fi), then run this again' }
    Write-Host ''
    Write-Host ('  This laptop''s Wi-Fi: {0} on {1}{2}.' -f $w.Address, $w.Name, $(if ($Facts.WifiSsid) { " ($($Facts.WifiSsid))" } else { '' }))
    Write-Host '  On the control workstation (DEVPC-REDACTED) the address is under Settings > Network & internet > Wi-Fi > its network > IPv4 address.'
    $src = Read-LabInput -Prompt 'Type the control workstation''s Wi-Fi IPv4 address (Enter to leave Remote Desktop for later)'
    if (-not $src) { return (New-LabOutcome 'PENDING' 'no control workstation address given') }
    $chk = Test-LabCoderRdpSource -Source $src -WifiAddress $w.Address -WifiPrefix $w.Prefix -LabPrefix $LabPrefix
    if (-not $chk.Ok) { throw $chk.Reason }
    if (-not (Request-LabApproval -Prompt ("Turn on Remote Desktop with Network Level Authentication, reachable only from {0} over Wi-Fi (rule '{1}', TCP 3389), and let {2} sign in remotely? The lab cable never carries it; the console sign-in stays as it is." -f $src, $Layout.RdpRuleDisplay, $Runtime.Name) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
    $notes = @()
    if (Set-LabCoderRegistry -Path $script:RdpTcpKey -Name 'UserAuthentication' -Value 1) { $notes += 'NLA required' }
    $existing = $null; try { $existing = Get-NetFirewallRule -Name $Layout.RdpRule -ErrorAction Stop } catch { }
    if ($existing) { Remove-NetFirewallRule -Name $Layout.RdpRule -ErrorAction Stop }
    New-NetFirewallRule -Name $Layout.RdpRule -DisplayName $Layout.RdpRuleDisplay -Group 'Agent Arena Lab' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3389 -RemoteAddress $src -InterfaceType Wireless -Profile Any -Description 'Coder laptop: Remote Desktop from the control workstation over Wi-Fi only (Agent Arena Lab provisioner)' -ErrorAction Stop | Out-Null
    Register-LabChange -Kind 'firewall' -Description ("rule {0}: TCP 3389 from {1}, wireless only" -f $Layout.RdpRule, $src) -Undo ([pscustomobject]@{ type = 'coder'; action = 'remove-firewall-rule'; name = $Layout.RdpRule }) | Out-Null
    $notes += "TCP 3389 from $src on Wi-Fi only"
    foreach ($b in @($Facts.Rdp.BuiltinRules | Where-Object Enabled)) {
        if (Request-LabApproval -Prompt ("The built-in rule '{0}' is on and admits Remote Desktop from any address ({1}). Turn it off so only the rule above applies?" -f $b.Display, $b.Profile) -Word 'DISABLE' -Kind 'change') {
            Disable-NetFirewallRule -Name $b.Name -ErrorAction Stop
            Register-LabChange -Kind 'firewall' -Description ("built-in rule {0} turned off" -f $b.Display) -Undo ([pscustomobject]@{ type = 'coder'; action = 'enable-firewall-rule'; name = $b.Name }) | Out-Null
            $notes += "built-in '$($b.Display)' off"
        } else { Add-LabWarning ("the built-in rule '{0}' stays on: Remote Desktop answers any address it allows" -f $b.Display) }
    }
    if ($Runtime.Membership -ne 'privileged') {
        $in = @(); try { $in = @(Get-LocalGroupMember -SID 'S-1-5-32-555' -ErrorAction Stop | ForEach-Object { $_.SID.Value }) } catch { }
        $added = $false
        if ($in -notcontains $Runtime.Sid) {
            # Get-LocalGroupMember throws on an orphaned member, so "already a member" can still come back here.
            try { Add-LocalGroupMember -SID 'S-1-5-32-555' -Member $Runtime.Sid -ErrorAction Stop; $added = $true }
            catch { if ($_.Exception.GetType().Name -ne 'MemberExistsException' -and [string]$_.Exception.Message -notmatch '(?i)already a member') { throw } }
        }
        if ($added) {
            Register-LabChange -Kind 'account' -Description ("{0} added to Remote Desktop Users" -f $Runtime.Name) -Undo ([pscustomobject]@{ type = 'coder'; action = 'remove-group-member'; group = 'S-1-5-32-555'; member = $Runtime.Sid }) | Out-Null
            $notes += "$($Runtime.Name) may sign in remotely"
        }
    }
    # Last: Remote Desktop itself, once only the scoped rule can admit it.
    if (Set-LabCoderRegistry -Path $script:TsKey -Name 'fDenyTSConnections' -Value 0) { $notes += 'Remote Desktop on' }
    try { Start-Service TermService -ErrorAction Stop } catch { Add-LabWarning ('Remote Desktop service did not start: ' + $_.Exception.Message) }
    return (New-LabOutcome 'DONE' ($notes -join '; ') ([pscustomobject]@{ Source = $src; Wifi = $w.Address }))
}

function Get-LabCoderAclState {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$RuntimeSid)
    if (-not (Test-Path -LiteralPath $Path)) { return [pscustomobject]@{ Present = $false; Findings = @("missing: $Path"); Entries = @() } }
    $acl = Get-Acl -LiteralPath $Path
    $isFile = -not (Get-Item -LiteralPath $Path).PSIsContainer
    $entries = @($acl.Access | ForEach-Object { $sid = $null; try { $sid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $sid = [string]$_.IdentityReference }; [pscustomobject]@{ Sid = $sid; Rights = [string]$_.FileSystemRights; Type = [string]$_.AccessControlType } })
    $owner = $null; try { $owner = ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value } catch { $owner = [string]$acl.Owner }
    $f = @(Test-LabCoderAclEntries -Entries $entries -Protected $acl.AreAccessRulesProtected -RuntimeSid $RuntimeSid -AllowInherited:$isFile)
    # The owner may always change permissions: never the runtime account. An administrator who created it is fine.
    $me = ''; try { $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value } catch { }
    if ($owner -eq $RuntimeSid) { $f += 'owned by the runtime account (it could change the permissions)' }
    elseif ($owner -notin 'S-1-5-18', 'S-1-5-32-544', $me) { $f += ("owned by {0}, not by Administrators or SYSTEM" -f $acl.Owner) }
    return [pscustomobject]@{ Present = $true; Findings = $f; Entries = $entries }
}

function Install-LabCoderGatewayFiles {
    <#  C:\ProgramData\Coder\gateway with SYSTEM and Administrators full and the runtime account read only; the public
        Gateway CA from the provisioner stick (SHA-256 checked against the stick's record and against the lab's pinned
        value, which the hub's own install report of 2026-11-06 carries); Coder's client configuration.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Layout, [Parameter(Mandatory)]$Runtime, [Parameter(Mandatory)][string]$UsbRoot)
    $usbCa = Join-Path $UsbRoot 'gateway-ca\inference-gateway-ca.pem'
    if (-not (Test-Path -LiteralPath $usbCa)) { throw "no Gateway CA on the provisioner stick ($usbCa)" }
    $h = (Get-FileHash -LiteralPath $usbCa -Algorithm SHA256).Hash.ToLowerInvariant()
    $rec = ''; $recFile = $usbCa + '.sha256'; if (Test-Path -LiteralPath $recFile) { $rec = (([string](Get-Content -LiteralPath $recFile -Raw)).Trim() -split '\s+')[0].ToLowerInvariant() }
    if ($h -ne $Layout.CaSha256) { throw ("the stick's Gateway CA ({0}) is not the lab's CA ({1}): refused" -f $h.Substring(0, 12), $Layout.CaSha256.Substring(0, 12)) }
    if ($rec -and $rec -ne $h) { throw 'the stick''s Gateway CA does not match its own .sha256 record: refused' }
    $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $usbCa
    if ($cert.HasPrivateKey) { throw 'the CA file carries a private key: refused' }
    $dir = $Layout.Folder
    $parent = Split-Path -Parent $dir
    foreach ($p in @($parent, $dir)) {
        if (Test-Path -LiteralPath $p) {
            $o = (Get-Acl -LiteralPath $p).Owner; $os = $o; try { $os = ([Security.Principal.NTAccount]$o).Translate([Security.Principal.SecurityIdentifier]).Value } catch { }
            if ($os -notin 'S-1-5-18', 'S-1-5-32-544') { throw ("{0} already exists and belongs to {1}, not to Administrators: its owner could open its permissions again. Delete it as administrator, then run this again" -f $p, $o) }
        }
    }
    $created = -not (Test-Path -LiteralPath $dir)
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Set-LabFolderAcl -Path $dir -Grants @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', ('*{0}:(OI)(CI)RX' -f $Runtime.Sid))
    if ($created) { Register-LabChange -Kind 'folder' -Description ("{0} created: SYSTEM and Administrators full, {1} read" -f $dir, $Runtime.Name) -Undo ([pscustomobject]@{ type = 'manual'; instructions = "delete $dir (as administrator) once Coder no longer uses it" }) | Out-Null }
    $notes = @()
    $caOk = (Test-Path -LiteralPath $Layout.Ca) -and ((Get-FileHash -LiteralPath $Layout.Ca -Algorithm SHA256).Hash.ToLowerInvariant() -eq $h)
    if (-not $caOk) {
        $had = Test-Path -LiteralPath $Layout.Ca
        $bk = ''; if ($had) { $bk = Join-Path $Run.Backups 'coder-inference-gateway-ca.pem'; Copy-Item -LiteralPath $Layout.Ca -Destination $bk -Force }
        Copy-Item -LiteralPath $usbCa -Destination $Layout.Ca -Force
        Register-LabChange -Kind 'file' -Description ("Gateway CA {0} (SHA-256 {1}) in {2}" -f $cert.Subject, $h.Substring(0, 12), $Layout.Ca) -Undo ([pscustomobject]@{ type = 'file-restore'; path = $Layout.Ca; backup = $bk }) | Out-Null
        $notes += 'CA installed'
    } else { $notes += 'CA already the lab''s' }
    $text = New-LabCoderClientConfigText -Layout $Layout
    $now = $null; if (Test-Path -LiteralPath $Layout.ClientConfig) { $now = [IO.File]::ReadAllText($Layout.ClientConfig) }
    if ($now -ne $text) {
        $bk = ''
        if ($null -ne $now) {
            if (-not (Request-LabApproval -Prompt ("{0} exists with other content. Replace it with Coder's lab configuration (the earlier file is kept in the run's backups)?" -f $Layout.ClientConfig) -Word 'REPLACE' -Kind 'change')) { throw 'the existing client configuration was kept; nothing else changed' }
            $bk = Join-Path $Run.Backups 'coder-gateway-client.json'; Copy-Item -LiteralPath $Layout.ClientConfig -Destination $bk -Force
        }
        [IO.File]::WriteAllText($Layout.ClientConfig, $text, (New-Object Text.UTF8Encoding $false))
        Register-LabChange -Kind 'file' -Description ("Coder's Gateway client configuration {0} ({1}, {2}, {3})" -f $Layout.ClientConfig, $Layout.Url, $Layout.ServiceId, $Layout.AppId) -Undo ([pscustomobject]@{ type = 'file-restore'; path = $Layout.ClientConfig; backup = $bk }) | Out-Null
        $notes += 'configuration written'
    } else { $notes += 'configuration already exact' }
    Set-LabFolderAcl -Path $dir -Grants @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', ('*{0}:(OI)(CI)RX' -f $Runtime.Sid))
    return (New-LabOutcome 'DONE' ($notes -join '; '))
}

function Import-LabCoderToken {
    <#  Coder's token from the transfer stick into C:\ProgramData\Coder\gateway, copied as bytes (never read or shown),
        compared byte for byte, then the folder's permissions applied to it. The transfer copy is deleted only if the
        operator types DELETE.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Layout, [Parameter(Mandatory)]$Runtime, [string]$UsbRoot = '')
    $present = Test-Path -LiteralPath $Layout.Token
    $drives = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ Root = $_.DeviceID + '\' } })
    $found = @(Find-LabCoderTokenSource -Drives $drives -ProvisionerRoot $UsbRoot -TransferFolder $Layout.TransferFolder)
    if (-not $found.Count) {
        if ($present) { return (New-LabOutcome 'UNCHANGED' ("{0} is in place ({1} bytes); no newer copy on a transfer stick" -f $Layout.Token, (Get-Item -LiteralPath $Layout.Token).Length)) }
        Add-LabPending -Area 'coder' -Text ("Coder's Gateway token: on the hub, Coder Onboarding > 2 writes it to C:\ProgramData\AgentArenaLab\gateway\outbox\{0}. Copy that folder to the root of a SECOND stick (not the provisioner stick), plug it in here and run this again" -f $Layout.TransferFolder)
        return (New-LabOutcome 'PENDING' 'no token on a transfer stick')
    }
    if ($found.Count -gt 1) { throw ('more than one transfer stick holds a Coder token: ' + ($found -join ', ') + '. Leave only one plugged in') }
    $src = $found[0]; $same = $false; $msg = ''
    if ((Get-Item -LiteralPath $src).Length -lt 16) { throw "$src is too small to be a Gateway token" }
    if ($present) {
        $same = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $Layout.Token -Algorithm SHA256).Hash
        if (-not $same -and -not (Request-LabApproval -Prompt ("{0} already holds a token and {1} is a different one (after a rotation on the hub?). Replace it?" -f $Layout.Token, $src) -Word 'REPLACE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'the token in place was kept') }
        if ($same) { $msg = 'already in place (same token)' }
    }
    if (-not $present -or -not $same) {
        Copy-Item -LiteralPath $src -Destination $Layout.Token -Force
        if ((Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $Layout.Token -Algorithm SHA256).Hash) { throw 'the copied token differs from the transfer copy' }
        Set-LabFolderAcl -Path $Layout.Folder -Grants @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', ('*{0}:(OI)(CI)RX' -f $Runtime.Sid))
        Register-LabChange -Kind 'secret-file' -Description ("Coder's Gateway token copied to {0} (read by {1} and administrators; contents never read)" -f $Layout.Token, $Runtime.Name) -Undo ([pscustomobject]@{ type = 'coder'; action = 'remove-file'; path = $Layout.Token }) | Out-Null
        $msg = 'copied, identical to the transfer copy'
    }
    $acl = Get-LabCoderAclState -Path $Layout.Token -RuntimeSid $Runtime.Sid
    if ($acl.Findings.Count) { throw ('the token''s permissions are not right: ' + ($acl.Findings -join '; ')) }
    if (Request-LabApproval -Prompt ("Delete the transfer copy {0} now (recommended: the token then exists only here and, until you delete it there, on the hub)?" -f $src) -Word 'DELETE' -Kind 'change') {
        Remove-Item -LiteralPath $src -Force
        $msg += '; transfer copy deleted'
    } else { Add-LabPending -Area 'coder' -Text "delete the transfer copy of Coder's token: $src" }
    return (New-LabOutcome 'DONE' $msg)
}

function Get-LabCoderFilesVerdict {
    <#  Verify: files present, exact configuration, the lab's CA, permissions. Reads no token contents.  #>
    param([Parameter(Mandatory)]$Layout, [Parameter(Mandatory)][string]$RuntimeSid)
    $out = @()
    $cfgOk = (Test-Path -LiteralPath $Layout.ClientConfig) -and ([IO.File]::ReadAllText($Layout.ClientConfig) -ceq (New-LabCoderClientConfigText -Layout $Layout))
    $out += [pscustomobject]@{ Check = "Coder's client configuration is exactly the lab's ($($Layout.Url), $($Layout.ServiceId), $($Layout.AppId), token_file only)"; Ok = $cfgOk; Detail = $Layout.ClientConfig }
    $caOk = (Test-Path -LiteralPath $Layout.Ca) -and ((Get-FileHash -LiteralPath $Layout.Ca -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Layout.CaSha256)
    $out += [pscustomobject]@{ Check = 'the Gateway CA is the lab''s (SHA-256 pinned)'; Ok = $caOk; Detail = $Layout.Ca }
    $tk = Test-Path -LiteralPath $Layout.Token
    $out += [pscustomobject]@{ Check = "Coder's token is in place (contents not read)"; Ok = $tk; Detail = $(if ($tk) { '{0} ({1} bytes)' -f $Layout.Token, (Get-Item -LiteralPath $Layout.Token).Length } else { 'missing: ' + $Layout.Token }) }
    foreach ($p in @($Layout.Folder, $Layout.ClientConfig, $Layout.Ca, $Layout.Token)) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $a = Get-LabCoderAclState -Path $p -RuntimeSid $RuntimeSid
        $out += [pscustomobject]@{ Check = "permissions of $p (SYSTEM and Administrators full, the runtime account read, nobody else)"; Ok = ($a.Findings.Count -eq 0); Detail = $(if ($a.Findings.Count) { $a.Findings -join '; ' } else { 'as required' }) }
    }
    $env1 = @('CAPSTONE_INFRASTRUCTURE_TOKEN', 'CAPSTONE_INFERENCE_TOKEN', 'APPLICATION_INFERENCE_TOKEN', 'CODER_INFERENCE_TOKEN') | Where-Object { [Environment]::GetEnvironmentVariable($_, 'Machine') }
    $out += [pscustomobject]@{ Check = 'no Gateway token in a machine environment variable'; Ok = (-not @($env1).Count); Detail = $(if (@($env1).Count) { ($env1 -join ', ') + ' set (values not read)' } else { 'none' }) }
    return $out
}

function Get-LabCoderClockOffset {
    <#  Offset from the hub's clock, measured (w32tm /stripchart, read-only). Nothing is reconfigured.  #>
    param([Parameter(Mandatory)][string]$Hub)
    $o = & w32tm.exe /stripchart /computer:$Hub /samples:3 /dataonly 2>&1
    $vals = @($o | ForEach-Object { if ([string]$_ -match ',\s*([+-]?\d+(\.\d+)?)s\s*$') { [double]$Matches[1] } })
    if (-not $vals.Count) { return [pscustomobject]@{ Ok = $false; Seconds = $null; Detail = ('the hub''s time service did not answer: ' + ((@($o) | Select-Object -Last 1) -join ' ')) } }
    $s = ($vals | Measure-Object -Average).Average
    return [pscustomobject]@{ Ok = ([math]::Abs($s) -lt 60); Seconds = [math]::Round($s, 3); Detail = ('{0:+0.000;-0.000} s from the hub (Arena refuses more than 120 s)' -f $s) }
}

# ---------------------------------------------------------------------------
# The Coder laptop's Configure and Verify (called by Start-LabProvisioner's application laptop flow)
# ---------------------------------------------------------------------------

function Get-LabCoderLabAdapter {
    param([Parameter(Mandatory)][string]$Address)
    return (@(Get-CapstoneAdapterInventory) | Where-Object { @($_.IPv4Addresses | Where-Object { $_.IPAddress -eq $Address }).Count } | Select-Object -First 1)
}

function Get-LabCoderSeparationLive {
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string]$Address)
    $f = Get-LabCoderFacts -Config $Config -Quiet
    return @(Get-LabCoderSeparationFindings -Forwarding $f.Forwarding -Adapters $f.Adapters -Sharing $f.Sharing -LabAdapter (Get-LabCoderLabAdapter -Address $Address))
}

function Invoke-LabCoderConfigure {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Config, $Inspection, [Parameter(Mandatory)][string]$Address, [Parameter(Mandatory)][string]$UsbRoot, [Parameter(Mandatory)][string]$Packages)
    $layout = Get-LabCoderLayout -Config $Config
    $st = @{ Runtime = $null; Facts = $null; Layout = $layout }
    if ($Inspection -and $Inspection.PSObject.Properties.Name -contains 'coder' -and $Inspection.coder) { $st.Facts = $Inspection.coder } else { $st.Facts = Get-LabCoderFacts -Config $Config -Quiet }
    Invoke-LabStep -Id 'coder-separation' -Title 'Ethernet and Wi-Fi kept apart (no forwarding, sharing or bridge; no gateway or DNS on the lab cable)' -ContinueOnFailure -Action {
        $f = Get-LabCoderSeparationLive -Config $Config -Address $Address
        if ($f.Count) { foreach ($x in $f) { Add-LabWarning ('Ethernet and Wi-Fi: ' + $x) }; throw ('not apart: ' + ($f -join '; ') + ' (nothing changed; correct it in Windows, then run this again)') }
        New-LabOutcome 'UNCHANGED' 'no forwarding, sharing or bridge; the lab adapter has no gateway and no DNS'
    } | Out-Null
    Invoke-LabStep -Id 'coder-account' -Title 'The account that runs Coder' -ContinueOnFailure -Action {
        $a = Read-LabCoderRuntimeAccount -Facts $st.Facts
        if (-not $a) { throw 'no account chosen: Coder''s Gateway files are made readable for the account that runs Coder' }
        $st.Runtime = $a
        Save-LabJson -InputObject ([pscustomobject][ordered]@{ name = $a.Name; sid = $a.Sid; membership = $a.Membership; chosen_at = (Get-Date).ToString('o') }) -Path (Join-Path $Run.Folder 'coder-runtime-account.json')
        New-LabOutcome 'DONE' ('{0} ({1}), SID {2}' -f $a.Name, $a.Membership, $a.Sid)
    } | Out-Null
    if (-not $st.Runtime) { return $st }
    Invoke-LabStep -Id 'coder-rdp' -Title 'Remote Desktop from the control workstation, over Wi-Fi only' -ContinueOnFailure -Action { Set-LabCoderRdp -Run $Run -Facts $st.Facts -Layout $layout -Runtime $st.Runtime -LabPrefix ([string]$Config.prefix) } | Out-Null
    Invoke-LabStep -Id 'coder-python' -Title ('Python {0} machine-wide (for TEST-CODER-GATEWAY)' -f $Config.runtime.python.version) -ContinueOnFailure -Action {
        $rs = Get-LabRuntimeState -Config $Config
        if ($rs.Python.Ok) { return (New-LabOutcome 'UNCHANGED' ('Python {0} at {1}' -f $rs.Python.Version, $rs.Python.Path)) }
        if (-not (Request-LabApproval -Prompt ('Install Python {0} machine-wide from {1} (the publisher''s hash and signature are checked first)? TEST-CODER-GATEWAY runs on it.' -f $Config.runtime.python.version, (Get-LabUsbLabel $UsbRoot)) -Word 'APPROVE' -Kind 'privileged')) { return (New-LabOutcome 'DECLINED' 'Python declined: TEST-CODER-GATEWAY cannot run') }
        Install-LabRuntime -Run $Run -Config $Config -Kind 'python' -PackageDir (Join-Path $Packages 'runtime')
    } | Out-Null
    Invoke-LabStep -Id 'coder-gateway-files' -Title 'Coder''s Gateway client files (configuration, lab CA, permissions)' -Action { Install-LabCoderGatewayFiles -Run $Run -Layout $layout -Runtime $st.Runtime -UsbRoot $UsbRoot } | Out-Null
    Invoke-LabStep -Id 'coder-token' -Title 'Coder''s Gateway token from the transfer stick' -ContinueOnFailure -Action { Import-LabCoderToken -Run $Run -Layout $layout -Runtime $st.Runtime -UsbRoot $UsbRoot } | Out-Null
    Invoke-LabStep -Id 'coder-install' -Title 'Coder application (Coder project)' -ContinueOnFailure -Action {
        Add-LabPending -Area 'coder' -Text 'Coder itself: its release, Gateway adapter, Arena publisher and code runner come from the Coder project; this laptop is ready for them (address, Remote Desktop, Gateway files). Nothing is installed or invented here.'
        New-LabOutcome 'PENDING' 'no Coder release on this stick'
    } | Out-Null
    return $st
}

function Add-LabCoderVerification {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string]$Address, $State)
    $layout = Get-LabCoderLayout -Config $Config
    $sid = ''
    if ($State -and $State.Runtime) { $sid = $State.Runtime.Sid }
    else { $saved = Get-ChildItem -LiteralPath (Split-Path -Parent $Run.Folder) -Filter 'coder-runtime-account.json' -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($saved) { $sid = [string](Read-LabJson $saved.FullName).sid } }
    $sep = @(Get-LabCoderSeparationLive -Config $Config -Address $Address)
    Add-LabVerification -Check 'Ethernet and Wi-Fi apart (no forwarding, sharing or bridge; no gateway or DNS on the lab adapter)' -Status $(if ($sep.Count) { 'FAIL' } else { 'PASS' }) -Detail $(if ($sep.Count) { $sep -join '; ' } else { 'apart' }) | Out-Null
    $f = Get-LabCoderFacts -Config $Config -Quiet
    $own = $f.Rdp.OwnRule; $broad = @($f.Rdp.BuiltinRules | Where-Object Enabled)
    $listen = @(Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue).Count -gt 0
    $rdpOk = $f.Rdp.Enabled -and $f.Rdp.Nla -and $own -and $own.Enabled -and $own.InterfaceType -eq 'Wireless' -and @($own.Remote).Count -eq 1 -and $listen -and -not $broad.Count
    Add-LabVerification -Check 'Remote Desktop: NLA, only from the control workstation over Wi-Fi, listening' -Status $(if ($rdpOk) { 'PASS' } elseif ($f.Rdp.Enabled -or $own) { 'FAIL' } else { 'PENDING' }) -Detail ('on {0}; NLA {1}; rule {2}; built-in rules on: {3}; listening {4}' -f $f.Rdp.Enabled, $f.Rdp.Nla, $(if ($own) { ($own.Remote -join ',') + ' on ' + $own.InterfaceType } else { 'none' }), $(if ($broad.Count) { ($broad | ForEach-Object Display) -join ', ' } else { 'none' }), $listen) | Out-Null
    Add-LabVerification -Check 'Remote Desktop works from the control workstation (sign in as the Coder account, then close it)' -Status PENDING -Scope external -Detail 'the operator, from DEVPC-REDACTED: mstsc to this laptop''s Wi-Fi address; keep this console signed in until it works' | Out-Null
    if ($sid) { foreach ($v in @(Get-LabCoderFilesVerdict -Layout $layout -RuntimeSid $sid)) { Add-LabVerification -Check $v.Check -Status $(if ($v.Ok) { 'PASS' } else { 'FAIL' }) -Detail $v.Detail | Out-Null } }
    else { Add-LabVerification -Check 'Coder''s Gateway files and permissions' -Status PENDING -Detail 'no Coder account chosen yet' | Out-Null }
    $h = Test-LabGatewayHealth -BaseUrl $layout.Url -CaFile $layout.Ca -ExpectedServiceId $layout.ServiceId
    Add-LabVerification -Check ('TLS to the Gateway with Coder''s CA file; service identity {0}; /health and /ready (public checks only)' -f $layout.ServiceId) -Status $(if ($h.Ok) { 'PASS' } elseif (Test-Path -LiteralPath $layout.Ca) { 'FAIL' } else { 'PENDING' }) -Scope lan -Detail $h.Detail | Out-Null
    $o = Get-LabCoderClockOffset -Hub $Config.hub
    Add-LabVerification -Check 'clock within a minute of the hub (measured)' -Status $(if ($o.Ok) { 'PASS' } elseif ($null -eq $o.Seconds) { 'PENDING' } else { 'FAIL' }) -Scope lan -Required $false -Detail $o.Detail | Out-Null
    Add-LabVerification -Check 'Coder''s own Gateway login: models, policy, availability and one tiny inference (TEST-CODER-GATEWAY, signed in as the Coder account, not as administrator)' -Status PENDING -Scope external -Detail 'double-click TEST-CODER-GATEWAY.cmd on the stick as the Coder account; its receipt goes to reports\<computer>\coder-gateway-test' | Out-Null
    Add-LabVerification -Check 'Coder application integration (Gateway adapter, Arena publisher, code runner)' -Status PENDING -Scope external -Detail 'Coder project: not built yet; infrastructure readiness only' | Out-Null
}

Export-ModuleMember -Function *-LabCoder*, ConvertTo-LabJsonString, Get-LabCoderLayout, New-LabCoderClientConfigText, Find-LabCoderTokenSource, Undo-LabCoderChange
