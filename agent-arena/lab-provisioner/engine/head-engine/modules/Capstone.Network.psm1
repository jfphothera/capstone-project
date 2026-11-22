<#
    Capstone.Network
    Private-LAN Ethernet configuration for a Capstone head.

    Design notes that exist because earlier versions failed:

      * The decision about WHICH adapter to touch is a pure function
        (Select-CapstoneEthernetAdapter). It is unit tested against synthetic
        adapter inventories, including the real trap on the coordinator: an
        OpenVPN TAP adapter that reports MediaType 802.3.

      * The address is applied with ONE atomic `netsh interface ipv4 set address
        source=static` call. Earlier versions disabled DHCP, then tried to
        remove the lease address that disabling DHCP had already withdrawn, and
        died on "No matching MSFT_NetIPAddress objects found" (defect D-02).
        There is no remove-then-add window here at all.

      * netsh is addressed by INTERFACE INDEX, not alias. `netsh ... name=` takes
        "Interface name or index", so an adapter alias containing spaces or
        non-ASCII characters cannot break argument quoting.

      * netsh exit codes and messages are NOT trusted. Measured on Windows 11
        26200, netsh returns the same text ("The filename, directory name, or
        volume label syntax is incorrect.") and exit 1 for an unknown interface
        AND for a genuine parameter error, and its own `show config` reports
        "DHCP enabled: Yes" for an interface that Get-NetIPInterface correctly
        reports as Dhcp=Disabled. Success is therefore established only by
        re-reading state through the NetTCPIP cmdlets.
#>

$script:PrivatePrefix = '192.0.2'
$script:CoordinatorIp = '192.0.2.1'

# ---------------------------------------------------------------------------
# Read-only collection
# ---------------------------------------------------------------------------

function Get-CapstoneAdapterInventory {
    <#
        .SYNOPSIS
            Collects every adapter with the facts the selection rules need.
        .DESCRIPTION
            Returns plain objects, so the same shape can be produced by tests.
    #>
    [CmdletBinding()]
    param()

    $inventory = @()
    $adapters = @()
    try { $adapters = @(Get-NetAdapter -ErrorAction Stop) } catch {
        Write-CapstoneLog ("Could not enumerate network adapters: {0}" -f $_.Exception.Message) -Level ERROR
        return @()
    }

    foreach ($adapter in $adapters) {
        $ipv4 = @()
        $dns = @()
        $gateways = @()
        $dhcp = 'Unknown'

        try {
            $ipv4 = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                ForEach-Object { [pscustomobject]@{ IPAddress = $_.IPAddress; PrefixLength = $_.PrefixLength; PrefixOrigin = [string]$_.PrefixOrigin; SuffixOrigin = [string]$_.SuffixOrigin } })
        } catch { }

        try {
            $interface = Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
            if ($interface) { $dhcp = [string]$interface.Dhcp }
        } catch { }

        try {
            $dnsEntry = Get-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
            if ($dnsEntry) { $dns = @($dnsEntry.ServerAddresses) }
        } catch { }

        try {
            $gateways = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } |
                ForEach-Object { $_.NextHop })
        } catch { }

        $inventory += [pscustomobject]@{
            Name                 = [string]$adapter.Name
            InterfaceIndex       = [int]$adapter.ifIndex
            Description          = [string]$adapter.InterfaceDescription
            MacAddress           = [string]$adapter.MacAddress
            Status               = [string]$adapter.Status
            LinkSpeed            = [string]$adapter.LinkSpeed
            HardwareInterface    = [bool]$adapter.HardwareInterface
            Virtual              = [bool]$adapter.Virtual
            NdisPhysicalMedium   = [int]$adapter.NdisPhysicalMedium
            MediaType            = [string]$adapter.MediaType
            Dhcp                 = $dhcp
            IPv4Addresses        = $ipv4
            DnsServers           = $dns
            DefaultGateways      = $gateways
        }
    }
    return $inventory
}

# ---------------------------------------------------------------------------
# Adapter selection  (pure - unit tested)
# ---------------------------------------------------------------------------

function Test-CapstonePhysicalEthernet {
    <#
        .SYNOPSIS
            True only for a real, wired, non-virtual NIC.
        .DESCRIPTION
            NdisPhysicalMedium 14 is 802.3; 9 is Native 802.11. Checking
            MediaType alone is not enough: the OpenVPN TAP-Windows6 adapter
            reports MediaType 802.3 while being Virtual with
            HardwareInterface = $false.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Adapter)

    if (-not $Adapter.HardwareInterface) { return $false }
    if ($Adapter.Virtual) { return $false }
    if ($Adapter.NdisPhysicalMedium -ne 14) { return $false }
    if ($Adapter.Name -match '(?i)wi-?fi|wireless|wlan') { return $false }
    if ($Adapter.Description -match '(?i)wi-?fi|wireless|802\.11|bluetooth|virtual|tap-windows|wintun|hyper-v|loopback') { return $false }
    return $true
}

function Select-CapstoneEthernetAdapter {
    <#
        .SYNOPSIS
            Decides which adapter may be reconfigured, or refuses to decide.
        .DESCRIPTION
            Returns a decision object; it never changes anything. Statuses:

              AlreadyConfigured - an adapter already holds the target address.
              Selected          - exactly one safe candidate.
              Ambiguous         - several equally plausible candidates. Stop.
              NoCandidate       - no physical Ethernet is connected. Stop.
              SoleInternetPath  - the only wired NIC is also the only route to
                                  the internet. Taking it static with no gateway
                                  would cut off the model downloads, so stop.

            A default gateway on the Ethernet port is NOT by itself a reason to
            refuse (defect D-01): on a fresh machine that is just an ordinary
            DHCP lease. It only matters when no other internet path exists.
        .PARAMETER PreferMacAddress
            Optional operator pin from the config file, for a head with two
            wired NICs where automatic selection would be ambiguous.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][array]$Adapters,
        [Parameter(Mandatory)][string]$TargetIp,
        [string]$PreferMacAddress = '',
        [string]$PreferName = ''
    )

    $decision = [ordered]@{
        Status      = 'NoCandidate'
        Adapter     = $null
        Reason      = ''
        Candidates  = @()
        AllEthernet = @()
    }

    # An adapter that already owns the target address wins outright, whatever
    # else is true. Never overwrite a working private-LAN configuration.
    $owner = @($Adapters | Where-Object {
        $null -ne $_.IPv4Addresses -and
        @($_.IPv4Addresses | Where-Object { $_.IPAddress -eq $TargetIp -and $_.PrefixLength -eq 24 }).Count -gt 0
    })
    if ($owner.Count -ge 1) {
        $decision.Status = 'AlreadyConfigured'
        $decision.Adapter = $owner[0]
        $decision.Reason = ('{0} (index {1}) already holds {2}/24.' -f $owner[0].Name, $owner[0].InterfaceIndex, $TargetIp)
        $decision.Candidates = $owner
        return [pscustomobject]$decision
    }

    $ethernet = @($Adapters | Where-Object { Test-CapstonePhysicalEthernet -Adapter $_ })
    $decision.AllEthernet = $ethernet

    $connected = @($ethernet | Where-Object { $_.Status -eq 'Up' })
    if ($connected.Count -eq 0) {
        $decision.Reason = 'No connected physical Ethernet adapter was found. Check the cable to the isolated Capstone switch, then re-run.'
        return [pscustomobject]$decision
    }

    # An explicit operator pin short-circuits the heuristics.
    if (-not [string]::IsNullOrWhiteSpace($PreferMacAddress)) {
        $wanted = ($PreferMacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
        $pinned = @($connected | Where-Object { ($_.MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() -eq $wanted })
        if ($pinned.Count -eq 1) {
            $decision.Status = 'Selected'
            $decision.Adapter = $pinned[0]
            $decision.Reason = ('Adapter pinned by MAC address {0} in the configuration file.' -f $PreferMacAddress)
            $decision.Candidates = $pinned
            return [pscustomobject]$decision
        }
        $decision.Status = 'NoCandidate'
        $decision.Reason = ('The configuration file pins MAC address {0}, but no connected Ethernet adapter has it. Nothing was changed.' -f $PreferMacAddress)
        $decision.Candidates = $connected
        return [pscustomobject]$decision
    }

    if (-not [string]::IsNullOrWhiteSpace($PreferName)) {
        $pinned = @($connected | Where-Object { $_.Name -eq $PreferName })
        if ($pinned.Count -eq 1) {
            $decision.Status = 'Selected'
            $decision.Adapter = $pinned[0]
            $decision.Reason = ('Adapter pinned by name "{0}" in the configuration file.' -f $PreferName)
            $decision.Candidates = $pinned
            return [pscustomobject]$decision
        }
    }

    $decision.Candidates = $connected

    # Is there an internet path that does NOT depend on one of these wired NICs?
    $candidateIndexes = @($connected | ForEach-Object { $_.InterfaceIndex })
    $otherInternet = @($Adapters | Where-Object {
        $_.Status -eq 'Up' -and
        @($_.DefaultGateways).Count -gt 0 -and
        ($candidateIndexes -notcontains $_.InterfaceIndex)
    })

    if ($connected.Count -eq 1) {
        $only = $connected[0]
        if (@($only.DefaultGateways).Count -gt 0 -and $otherInternet.Count -eq 0) {
            $decision.Status = 'SoleInternetPath'
            $decision.Adapter = $only
            $decision.Reason = ('{0} (index {1}) carries the only default gateway on this machine. Making it a gateway-less private-LAN port would remove the internet access needed to download models. Connect Wi-Fi, or add a second Ethernet port, then re-run.' -f $only.Name, $only.InterfaceIndex)
            return [pscustomobject]$decision
        }
        $decision.Status = 'Selected'
        $decision.Adapter = $only
        $gatewayNote = 'no default gateway'
        if (@($only.DefaultGateways).Count -gt 0) {
            $gatewayNote = ('an ordinary DHCP default gateway ({0}), which another connected adapter also provides' -f (@($only.DefaultGateways) -join ', '))
        }
        $decision.Reason = ('{0} (index {1}, {2}) is the only connected physical Ethernet adapter and has {3}.' -f $only.Name, $only.InterfaceIndex, $only.Description, $gatewayNote)
        return [pscustomobject]$decision
    }

    # More than one wired NIC. Prefer one already on the private subnet, then
    # one with no default gateway. Anything still tied is genuinely ambiguous.
    $onPrivate = @($connected | Where-Object {
        @($_.IPv4Addresses | Where-Object { $_.IPAddress -like ($script:PrivatePrefix + '.*') }).Count -gt 0
    })
    if ($onPrivate.Count -eq 1) {
        $decision.Status = 'Selected'
        $decision.Adapter = $onPrivate[0]
        $decision.Reason = ('{0} (index {1}) is the only wired adapter already on the {2}.0/24 private LAN.' -f $onPrivate[0].Name, $onPrivate[0].InterfaceIndex, $script:PrivatePrefix)
        return [pscustomobject]$decision
    }

    $noGateway = @($connected | Where-Object { @($_.DefaultGateways).Count -eq 0 })
    if ($noGateway.Count -eq 1) {
        $decision.Status = 'Selected'
        $decision.Adapter = $noGateway[0]
        $decision.Reason = ('{0} (index {1}) is the only wired adapter without a default gateway, so it is the one not carrying internet traffic.' -f $noGateway[0].Name, $noGateway[0].InterfaceIndex)
        return [pscustomobject]$decision
    }

    $decision.Status = 'Ambiguous'
    $names = @($connected | ForEach-Object { '{0} (index {1}, {2})' -f $_.Name, $_.InterfaceIndex, $_.Description })
    $decision.Reason = ('{0} connected Ethernet adapters could each plausibly be the Capstone switch port, and nothing distinguishes them: {1}. Nothing was changed. Set "ethernet_mac" in the configuration file to the adapter wired to the switch, then re-run.' -f $connected.Count, ($names -join '; '))
    return [pscustomobject]$decision
}

# ---------------------------------------------------------------------------
# Address conflict evidence  (pure - unit tested)
# ---------------------------------------------------------------------------

function Get-CapstoneConflictVerdict {
    <#
        .SYNOPSIS
            Turns probe observations into Conflict / Clear / Unknown.
        .DESCRIPTION
            Absence of an ICMP reply is NOT evidence that an address is free,
            because Windows Firewall blocks ICMP by default. That was measured
            on this very LAN: 192.0.2.2 answers TCP 11434 while dropping ping.
            So a silent probe yields "Clear" only when the ARP table is also
            empty for that address.
    #>
    [CmdletBinding()]
    param(
        [bool]$IcmpReplied = $false,
        [bool]$TcpAnswered = $false,
        [string]$NeighbourMac = '',
        [bool]$HeldLocally = $false
    )

    if ($HeldLocally) {
        return [pscustomobject]@{ Verdict = 'Clear'; Reason = 'This machine already holds the address.' }
    }
    if ($IcmpReplied) {
        return [pscustomobject]@{ Verdict = 'Conflict'; Reason = 'Another device replied to ICMP at this address.' }
    }
    if ($TcpAnswered) {
        return [pscustomobject]@{ Verdict = 'Conflict'; Reason = 'Another device accepted a TCP connection at this address.' }
    }
    $mac = ($NeighbourMac -replace '[^0-9A-Fa-f]', '')
    if ($mac.Length -ge 12 -and $mac -ne '000000000000' -and $mac -ne 'FFFFFFFFFFFF') {
        return [pscustomobject]@{ Verdict = 'Conflict'; Reason = ('The ARP table maps this address to hardware address {0}.' -f $NeighbourMac) }
    }
    return [pscustomobject]@{ Verdict = 'Clear'; Reason = 'No ICMP reply, no TCP response and no ARP entry. Note that this is absence of evidence, not proof the address is unused.' }
}

function Test-CapstoneAddressInUse {
    <#
        .SYNOPSIS
            Probes an address three independent ways before claiming it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$IPAddress,
        [int]$TcpPort = 11434,
        [bool]$HeldLocally = $false
    )

    if ($HeldLocally) {
        return Get-CapstoneConflictVerdict -HeldLocally $true
    }

    $icmp = $false
    try { $icmp = [bool](Test-Connection -ComputerName $IPAddress -Count 1 -Quiet -ErrorAction SilentlyContinue) } catch { }

    $tcp = $false
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($IPAddress, $TcpPort, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(1500, $false)) {
            try { $client.EndConnect($async); $tcp = $client.Connected } catch { $tcp = $false }
        }
        $client.Close()
    } catch { $tcp = $false }

    $mac = ''
    try {
        $neighbour = Get-NetNeighbor -IPAddress $IPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.State -in @('Reachable', 'Stale', 'Delay', 'Probe', 'Permanent') } |
            Select-Object -First 1
        if ($neighbour) { $mac = [string]$neighbour.LinkLayerAddress }
    } catch { }

    return Get-CapstoneConflictVerdict -IcmpReplied $icmp -TcpAnswered $tcp -NeighbourMac $mac -HeldLocally $false
}

# ---------------------------------------------------------------------------
# Backup and rollback
# ---------------------------------------------------------------------------

function New-CapstoneNetworkBackup {
    <#
        .SYNOPSIS
            Captures everything rollback needs, before anything is changed.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [Parameter(Mandatory)]$Adapter,
        [Parameter(Mandatory)][string]$TargetIp,
        [int]$NodeNumber = 0
    )

    $record = [ordered]@{
        schema_version    = 2
        captured_at       = (Get-Date).ToString('o')
        computer          = $env:COMPUTERNAME
        node_number       = $NodeNumber
        target_ip         = $TargetIp
        interface_index   = $Adapter.InterfaceIndex
        interface_alias   = $Adapter.Name
        interface_desc    = $Adapter.Description
        mac_address       = $Adapter.MacAddress
        dhcp_state        = $Adapter.Dhcp
        ipv4_addresses    = @($Adapter.IPv4Addresses | ForEach-Object { [ordered]@{ ip = $_.IPAddress; prefix = $_.PrefixLength; prefix_origin = $_.PrefixOrigin } })
        dns_servers       = @($Adapter.DnsServers)
        default_gateways  = @($Adapter.DefaultGateways)
        applied           = $false
    }

    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $file = Join-Path $Paths.Backups ('network-backup-{0}-{1}.json' -f $Adapter.InterfaceIndex, $stamp)
    Write-CapstoneJson -InputObject ([pscustomobject]$record) -Path $file
    Write-CapstoneJson -InputObject ([pscustomobject]$record) -Path (Join-Path $Paths.Backups 'network-backup-LATEST.json')

    return [pscustomobject]@{ Path = $file; Record = [pscustomobject]$record }
}

function Resolve-CapstoneRollbackPlan {
    <#
        .SYNOPSIS
            Works out how to undo a change, from the backup record alone.
        .DESCRIPTION
            Defect D-08 was restoring an expired DHCP lease address as though it
            had been a static configuration. An adapter that was on DHCP is
            returned to DHCP; only a genuinely static adapter gets its literal
            addresses back.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Backup)

    $plan = [ordered]@{
        Action           = 'None'
        InterfaceIndex   = [int]$Backup.interface_index
        InterfaceAlias   = [string]$Backup.interface_alias
        RestoreAddresses = @()
        RestoreGateway   = ''
        RestoreDns       = @()
        Reason           = ''
    }

    $wasDhcp = ([string]$Backup.dhcp_state -eq 'Enabled')

    if ($wasDhcp) {
        $plan.Action = 'RestoreDhcp'
        $plan.Reason = 'The adapter was on DHCP before provisioning, so DHCP is re-enabled and the lease is requested again. Its previous lease address is deliberately not re-applied as a static address.'
        $plan.RestoreDns = @()
        return [pscustomobject]$plan
    }

    $addresses = @($Backup.ipv4_addresses | Where-Object { $_ -and $_.ip -and ($_.ip -notlike '169.254.*') })
    if ($addresses.Count -eq 0) {
        $plan.Action = 'RestoreDhcp'
        $plan.Reason = 'The adapter had no usable static IPv4 address recorded, so DHCP is restored as the safe default.'
        return [pscustomobject]$plan
    }

    $plan.Action = 'RestoreStatic'
    $plan.RestoreAddresses = $addresses
    $gateways = @($Backup.default_gateways | Where-Object { $_ })
    if ($gateways.Count -gt 0) { $plan.RestoreGateway = [string]$gateways[0] }
    $plan.RestoreDns = @($Backup.dns_servers | Where-Object { $_ })
    $plan.Reason = ('The adapter had a static configuration, so its previous address, gateway and DNS servers are restored exactly.')
    return [pscustomobject]$plan
}

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

function Invoke-CapstoneNetsh {
    <#
        .SYNOPSIS
            Runs netsh with an argument array and captures output and exit code.
        .DESCRIPTION
            Arguments are passed as discrete array elements so nothing is
            re-parsed by a shell. The result is informational only; callers
            must verify the resulting state independently.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = ''
    $exit = -1

    # The engine runs with $ErrorActionPreference = 'Stop'. Under Stop, stderr
    # from a native command can be promoted to a terminating error, which would
    # abort the network stage instead of returning a result the caller can
    # inspect and fall back from. That is defect D-09's failure mode, so the
    # preference is relaxed for the duration of this call only.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $captured = & netsh.exe @Arguments 2>&1
        $exit = $LASTEXITCODE
        $output = (@($captured | ForEach-Object { [string]$_ }) -join [Environment]::NewLine)
    } catch {
        $output = $_.Exception.Message
        $exit = -1
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{
        Arguments = ($Arguments -join ' ')
        ExitCode  = $exit
        Output    = $output.Trim()
    }
}

function Set-CapstonePrivateAddress {
    <#
        .SYNOPSIS
            Applies the static private-LAN address atomically, then verifies it.
        .OUTPUTS
            An object with Success, Changed, Verification and Attempts.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$InterfaceIndex,
        [Parameter(Mandatory)][string]$TargetIp,
        [int]$PrefixLength = 24,
        [string]$Mask = '255.255.255.0'
    )

    $attempts = @()

    # One atomic operation: static source, address, mask and explicitly no
    # gateway. Nothing is removed first, so there is no window in which the
    # adapter has no address and no lease to lose.
    $setAddress = Invoke-CapstoneNetsh -Arguments @(
        'interface', 'ipv4', 'set', 'address',
        ('name={0}' -f $InterfaceIndex),
        'source=static',
        ('address={0}' -f $TargetIp),
        ('mask={0}' -f $Mask),
        'gateway=none',
        'store=persistent'
    )
    $attempts += $setAddress
    Write-CapstoneLog ("netsh set address exit {0}" -f $setAddress.ExitCode) -Level DEBUG

    # Clear IPv4 DNS on the private port. source=static address=none leaves the
    # adapter with no resolver at all, which is what an isolated LAN wants.
    # `source=dhcp` would instead leave it waiting on a DHCP server that this
    # subnet does not have.
    $setDns = Invoke-CapstoneNetsh -Arguments @(
        'interface', 'ipv4', 'set', 'dnsservers',
        ('name={0}' -f $InterfaceIndex),
        'source=static',
        'address=none',
        'register=none',
        'validate=no'
    )
    $attempts += $setDns
    Write-CapstoneLog ("netsh set dnsservers exit {0}" -f $setDns.ExitCode) -Level DEBUG

    # Windows does not finish applying the configuration synchronously. The
    # address appears immediately, but the on-link subnet route is created a
    # moment later. Measured on node 6 (2026-10-30): two seconds after a netsh
    # apply that otherwise passed every check, OnLinkRoute was still False -
    # and the machine then reached the coordinator eight seconds afterwards,
    # proving the route had simply not landed yet.
    #
    # A premature read made a correct configuration look failed, which also
    # skipped remote serving. So the verification is polled until it passes
    # rather than taken once.
    $verification = Wait-CapstoneNetworkState -InterfaceIndex $InterfaceIndex -TargetIp $TargetIp -PrefixLength $PrefixLength -TimeoutSeconds 45

    if (-not $verification.Pass -and $setAddress.ExitCode -ne 0) {
        # netsh refused and the state did not change. Fall back to the NetTCPIP
        # cmdlets, which give structured errors. Order matters: withdraw the old
        # addresses FIRST, tolerate their absence, and only then disable DHCP.
        Write-CapstoneLog 'netsh did not apply the address; falling back to the NetTCPIP cmdlets.' -Level WARN
        try {
            Get-NetIPAddress -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -ne $TargetIp } |
                Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue

            Get-NetRoute -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } |
                Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue

            Set-NetIPInterface -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue

            $present = @(Get-NetIPAddress -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -eq $TargetIp })
            if ($present.Count -eq 0) {
                New-NetIPAddress -InterfaceIndex $InterfaceIndex -IPAddress $TargetIp -PrefixLength $PrefixLength -AddressFamily IPv4 -Type Unicast -ErrorAction Stop | Out-Null
            }
            $attempts += [pscustomobject]@{ Arguments = 'NetTCPIP fallback'; ExitCode = 0; Output = 'Applied via New-NetIPAddress.' }
        } catch {
            $attempts += [pscustomobject]@{ Arguments = 'NetTCPIP fallback'; ExitCode = 1; Output = $_.Exception.Message }
        }
        $verification = Wait-CapstoneNetworkState -InterfaceIndex $InterfaceIndex -TargetIp $TargetIp -PrefixLength $PrefixLength -TimeoutSeconds 45
    }

    return [pscustomobject]@{
        Success      = $verification.Pass
        Verification = $verification
        Attempts     = $attempts
    }
}

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

function Get-CapstoneNetworkFacts {
    <#
        .SYNOPSIS
            Re-reads the authoritative post-change state of one adapter.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$InterfaceIndex,
        [Parameter(Mandatory)][string]$TargetIp,
        [int]$PrefixLength = 24
    )

    $addresses = @()
    $dhcp = 'Unknown'
    $dns = @()
    $gateways = @()
    $onLink = $false
    $alias = ''

    try {
        $addresses = @(Get-NetIPAddress -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            ForEach-Object { [pscustomobject]@{ IPAddress = $_.IPAddress; PrefixLength = [int]$_.PrefixLength } })
    } catch { }
    try {
        $interface = Get-NetIPInterface -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
        if ($interface) { $dhcp = [string]$interface.Dhcp; $alias = [string]$interface.InterfaceAlias }
    } catch { }
    try {
        $dnsEntry = Get-DnsClientServerAddress -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
        if ($dnsEntry) { $dns = @($dnsEntry.ServerAddresses) }
    } catch { }
    try {
        $gateways = @(Get-NetRoute -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } | ForEach-Object { $_.NextHop })
        $subnet = ('{0}.0/{1}' -f $script:PrivatePrefix, $PrefixLength)
        $onLink = @(Get-NetRoute -InterfaceIndex $InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.DestinationPrefix -eq $subnet }).Count -gt 0
    } catch { }

    return [pscustomobject]@{
        InterfaceIndex  = $InterfaceIndex
        InterfaceAlias  = $alias
        Addresses       = $addresses
        Dhcp            = $dhcp
        DnsServers      = $dns
        DefaultGateways = $gateways
        OnLinkRoute     = $onLink
        TargetIp        = $TargetIp
        PrefixLength    = $PrefixLength
    }
}

function Get-CapstoneNetworkVerdict {
    <#
        .SYNOPSIS
            Scores observed adapter facts against the required end state.
        .DESCRIPTION
            Pure, so the acceptance rules themselves are unit tested. Note that
            the DHCP check reads Get-NetIPInterface, never netsh's own
            "DHCP enabled" line, which was measured to disagree with it.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Facts)

    $checks = @()

    $hasAddress = @($Facts.Addresses | Where-Object { $_.IPAddress -eq $Facts.TargetIp -and $_.PrefixLength -eq $Facts.PrefixLength }).Count -eq 1
    $checks += [pscustomobject]@{
        Name = 'Address'; Pass = $hasAddress
        Expected = ('{0}/{1} present exactly once' -f $Facts.TargetIp, $Facts.PrefixLength)
        Actual = (@($Facts.Addresses | ForEach-Object { '{0}/{1}' -f $_.IPAddress, $_.PrefixLength }) -join ', ')
    }

    $dhcpOff = ([string]$Facts.Dhcp -eq 'Disabled')
    $checks += [pscustomobject]@{
        Name = 'DhcpDisabled'; Pass = $dhcpOff
        Expected = 'Get-NetIPInterface reports Dhcp = Disabled'
        Actual = [string]$Facts.Dhcp
    }

    $noGateway = (@($Facts.DefaultGateways | Where-Object { $_ -and $_ -ne '0.0.0.0' }).Count -eq 0)
    $checks += [pscustomobject]@{
        Name = 'NoDefaultGateway'; Pass = $noGateway
        Expected = 'no IPv4 default gateway on the private port'
        Actual = (@($Facts.DefaultGateways) -join ', ')
    }

    $noDns = (@($Facts.DnsServers | Where-Object { $_ }).Count -eq 0)
    $checks += [pscustomobject]@{
        Name = 'NoIpv4Dns'; Pass = $noDns
        Expected = 'no IPv4 DNS server on the private port'
        Actual = (@($Facts.DnsServers) -join ', ')
    }

    $checks += [pscustomobject]@{
        Name = 'OnLinkRoute'; Pass = [bool]$Facts.OnLinkRoute
        Expected = ('an on-link route for {0}.0/{1}' -f $script:PrivatePrefix, $Facts.PrefixLength)
        Actual = ([string][bool]$Facts.OnLinkRoute)
    }

    $failed = @($checks | Where-Object { -not $_.Pass })
    return [pscustomobject]@{
        Pass    = ($failed.Count -eq 0)
        Checks  = $checks
        Failed  = @($failed | ForEach-Object { $_.Name })
        Summary = (@($checks | ForEach-Object { '{0}={1}' -f $_.Name, $(if ($_.Pass) { 'pass' } else { 'FAIL' }) }) -join ' ')
    }
}

function Test-CapstoneNetworkState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$InterfaceIndex,
        [Parameter(Mandatory)][string]$TargetIp,
        [int]$PrefixLength = 24
    )
    $facts = Get-CapstoneNetworkFacts -InterfaceIndex $InterfaceIndex -TargetIp $TargetIp -PrefixLength $PrefixLength
    $verdict = Get-CapstoneNetworkVerdict -Facts $facts
    Add-Member -InputObject $verdict -MemberType NoteProperty -Name Facts -Value $facts -Force
    return $verdict
}

function Wait-CapstoneNetworkState {
    <#
        .SYNOPSIS
            Polls the adapter state until it passes, or the budget expires.
        .DESCRIPTION
            Windows applies IPv4 configuration asynchronously. Reading the
            result once, immediately, can report a correct configuration as
            failed - most often on the on-link subnet route, which lands a few
            seconds after the address itself.

            The last verdict is returned either way, so a genuine failure is
            still reported accurately; this only removes the false negative
            caused by looking too early.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$InterfaceIndex,
        [Parameter(Mandatory)][string]$TargetIp,
        [int]$PrefixLength = 24,
        [int]$TimeoutSeconds = 45,
        [int]$IntervalSeconds = 3
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $verdict = $null
    $announced = $false

    while ($true) {
        $verdict = Test-CapstoneNetworkState -InterfaceIndex $InterfaceIndex -TargetIp $TargetIp -PrefixLength $PrefixLength
        if ($verdict.Pass) { return $verdict }
        if ((Get-Date) -ge $deadline) { break }

        if (-not $announced) {
            Write-CapstoneLog ('Waiting for Windows to finish applying the configuration (outstanding: {0}) ...' -f ($verdict.Failed -join ', ')) -Level INFO
            $announced = $true
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
    return $verdict
}

function Test-CapstoneCoordinatorReachable {
    <#
        .SYNOPSIS
            Checks the coordinator two ways and never treats ping as decisive.
    #>
    [CmdletBinding()]
    param(
        [string]$CoordinatorIp = $script:CoordinatorIp,
        [int]$TcpPort = 11434
    )

    $icmp = $false
    try { $icmp = [bool](Test-Connection -ComputerName $CoordinatorIp -Count 2 -Quiet -ErrorAction SilentlyContinue) } catch { }

    $tcp = $false
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($CoordinatorIp, $TcpPort, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(2000, $false)) {
            try { $client.EndConnect($async); $tcp = $client.Connected } catch { $tcp = $false }
        }
        $client.Close()
    } catch { }

    $arp = ''
    try {
        $neighbour = Get-NetNeighbor -IPAddress $CoordinatorIp -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.State -in @('Reachable', 'Stale', 'Delay', 'Probe', 'Permanent') } | Select-Object -First 1
        if ($neighbour) { $arp = [string]$neighbour.LinkLayerAddress }
    } catch { }

    $linkProven = ($icmp -or $tcp -or -not [string]::IsNullOrWhiteSpace($arp))
    $note = 'No response of any kind from the coordinator. The private IPv4 configuration may still be correct; check the switch and cabling.'
    if ($linkProven) {
        $note = ('Layer-2/3 contact with {0} established (icmp={1} tcp{2}={3} arp={4}).' -f $CoordinatorIp, $icmp, $TcpPort, $tcp, $(if ($arp) { $arp } else { 'none' }))
    }

    return [pscustomobject]@{
        CoordinatorIp = $CoordinatorIp
        IcmpReplied   = $icmp
        TcpAnswered   = $tcp
        ArpEntry      = $arp
        LinkProven    = $linkProven
        Note          = $note
    }
}

Export-ModuleMember -Function @(
    'Get-CapstoneAdapterInventory', 'Test-CapstonePhysicalEthernet', 'Select-CapstoneEthernetAdapter',
    'Get-CapstoneConflictVerdict', 'Test-CapstoneAddressInUse',
    'New-CapstoneNetworkBackup', 'Resolve-CapstoneRollbackPlan',
    'Invoke-CapstoneNetsh', 'Set-CapstonePrivateAddress',
    'Get-CapstoneNetworkFacts', 'Get-CapstoneNetworkVerdict', 'Test-CapstoneNetworkState',
    'Wait-CapstoneNetworkState', 'Test-CapstoneCoordinatorReachable'
)
