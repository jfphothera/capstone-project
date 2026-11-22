<#
    Unit tests for the engine's decision logic.

    Every function tested here is pure: it takes already-measured state and
    returns a decision. That is deliberate. The risky parts of provisioning -
    choosing an adapter, deciding whether to retry, deciding whether to call a
    run successful - are therefore testable without touching a real adapter, a
    real Ollama install or a real network, and they are exercised against the
    exact scenarios that broke the earlier versions.

    The scenario list follows spec section 27B.
#>
[CmdletBinding()]
param(
    [string]$PackageRoot = '',
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Continue'

$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($PackageRoot)) { $PackageRoot = Split-Path -Parent $testRoot }

Import-Module (Join-Path $testRoot 'TestHarness.psm1') -Force -DisableNameChecking
foreach ($moduleName in @('Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Remote', 'Capstone.Report')) {
    Import-Module (Join-Path $PackageRoot ('engine\modules\{0}.psm1' -f $moduleName)) -Force -DisableNameChecking -Global
}
Reset-TestRun

Write-Host ''
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host '  CAPSTONE HEAD PROVISIONER - UNIT TESTS' -ForegroundColor Cyan
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host ('  Host: PowerShell {0}' -f $PSVersionTable.PSVersion)

# ---------------------------------------------------------------------------
# Fixtures: synthetic adapter inventories in the shape Get-CapstoneAdapterInventory returns
# ---------------------------------------------------------------------------

function New-Adapter {
    param(
        [string]$Name, [int]$Index, [string]$Description = 'Test NIC',
        [string]$Status = 'Up', [bool]$Hardware = $true, [bool]$Virtual = $false,
        [int]$Medium = 14, [string]$Dhcp = 'Enabled',
        [array]$Addresses = @(), [array]$Dns = @(), [array]$Gateways = @(),
        [string]$Mac = '00-11-22-33-44-55'
    )
    return [pscustomobject]@{
        Name = $Name; InterfaceIndex = $Index; Description = $Description
        MacAddress = $Mac; Status = $Status; LinkSpeed = '1 Gbps'
        HardwareInterface = $Hardware; Virtual = $Virtual
        NdisPhysicalMedium = $Medium; MediaType = '802.3'; Dhcp = $Dhcp
        IPv4Addresses = $Addresses; DnsServers = $Dns; DefaultGateways = $Gateways
    }
}
function New-Address {
    param([string]$Ip, [int]$Prefix = 24, [string]$Origin = 'Dhcp')
    return [pscustomobject]@{ IPAddress = $Ip; PrefixLength = $Prefix; PrefixOrigin = $Origin; SuffixOrigin = $Origin }
}

# The real coordinator inventory measured on 2026-10-29: a genuine Intel NIC,
# an Intel Wi-Fi radio, and an OpenVPN TAP adapter that reports MediaType 802.3
# and would be selected by a naive MediaType-only filter.
$realWorldAdapters = @(
    New-Adapter -Name 'Ethernet' -Index 15 -Description 'Intel(R) Ethernet Connection (18) I219-V' -Medium 14 -Dhcp 'Disabled' -Addresses @(New-Address -Ip '192.0.2.1' -Origin 'Manual') -Mac '02-00-00-00-00-01'
    New-Adapter -Name 'Wi-Fi 2' -Index 17 -Description 'Intel(R) Wi-Fi 6 AX201 160MHz' -Medium 9 -Addresses @(New-Address -Ip '198.51.100.149') -Gateways @('198.51.100.1') -Dns @('198.51.100.1') -Mac '02-00-00-00-00-02'
    New-Adapter -Name 'OpenVPN TAP-Windows6' -Index 14 -Description 'TAP-Windows Adapter V9' -Hardware $false -Virtual $true -Medium 0 -Addresses @(New-Address -Ip '203.0.113.2')
    New-Adapter -Name 'OpenVPN Wintun' -Index 19 -Description 'Wintun Userspace Tunnel' -Status 'Disconnected' -Hardware $false -Virtual $true -Medium 0
)

# ---------------------------------------------------------------------------
Start-TestGroup 'Adapter identification'
# ---------------------------------------------------------------------------

Test-Case 'a real Intel Ethernet NIC is recognised as physical Ethernet' {
    Assert-True (Test-CapstonePhysicalEthernet -Adapter $realWorldAdapters[0]) 'the I219-V should qualify'
}
Test-Case 'a Wi-Fi radio is never treated as Ethernet' {
    Assert-False (Test-CapstonePhysicalEthernet -Adapter $realWorldAdapters[1]) 'NdisPhysicalMedium 9 is Native 802.11'
}
Test-Case 'an OpenVPN TAP adapter reporting MediaType 802.3 is rejected' {
    Assert-False (Test-CapstonePhysicalEthernet -Adapter $realWorldAdapters[2]) 'a MediaType-only filter would wrongly accept this'
}
Test-Case 'a Wintun tunnel is rejected' {
    Assert-False (Test-CapstonePhysicalEthernet -Adapter $realWorldAdapters[3]) 'virtual tunnels are not provisioning targets'
}
Test-Case 'a kernel debugger pseudo-adapter on medium 14 is rejected' {
    $debugger = New-Adapter -Name 'Ethernet (Kernel Debugger)' -Index 21 -Hardware $false -Virtual $true -Medium 14
    Assert-False (Test-CapstonePhysicalEthernet -Adapter $debugger) 'HardwareInterface is false'
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Adapter selection'
# ---------------------------------------------------------------------------

Test-Case 'a machine already holding the target address reports AlreadyConfigured' {
    $decision = Select-CapstoneEthernetAdapter -Adapters $realWorldAdapters -TargetIp '192.0.2.1'
    Assert-Equal 'AlreadyConfigured' $decision.Status
    Assert-Equal 15 $decision.Adapter.InterfaceIndex
}

Test-Case 'DHCP-to-static: an ordinary DHCP gateway on Ethernet is NOT a reason to refuse (defect D-01)' {
    # The exact pre-provisioning state reported for node 3: Ethernet on DHCP
    # with 198.51.100.63, gateway 198.51.100.1, plus Wi-Fi carrying internet.
    $adapters = @(
        New-Adapter -Name 'Ethernet' -Index 6 -Description 'Intel Ethernet Controller I226-V' -Addresses @(New-Address -Ip '198.51.100.63') -Gateways @('198.51.100.1') -Dns @('198.51.100.1')
        New-Adapter -Name 'Wi-Fi' -Index 17 -Medium 9 -Addresses @(New-Address -Ip '198.51.100.149') -Gateways @('198.51.100.1')
    )
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.3'
    Assert-Equal 'Selected' $decision.Status 'the earlier version aborted here'
    Assert-Equal 6 $decision.Adapter.InterfaceIndex
}

Test-Case 'missing Wi-Fi internet: a sole gateway-bearing NIC is refused, not taken' {
    $adapters = @(
        New-Adapter -Name 'Ethernet' -Index 6 -Addresses @(New-Address -Ip '198.51.100.63') -Gateways @('198.51.100.1')
    )
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.3'
    Assert-Equal 'SoleInternetPath' $decision.Status
    Assert-Match 'only default gateway' $decision.Reason
}

Test-Case 'a disconnected Ethernet port yields NoCandidate rather than a guess' {
    $adapters = @(New-Adapter -Name 'Ethernet' -Index 6 -Status 'Disconnected')
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.4'
    Assert-Equal 'NoCandidate' $decision.Status
}

Test-Case 'two indistinguishable Ethernet ports stop the run as Ambiguous' {
    $adapters = @(
        New-Adapter -Name 'Ethernet' -Index 6 -Mac 'AA-AA-AA-AA-AA-01'
        New-Adapter -Name 'Ethernet 2' -Index 7 -Mac 'AA-AA-AA-AA-AA-02'
        New-Adapter -Name 'Wi-Fi' -Index 17 -Medium 9 -Gateways @('198.51.100.1')
    )
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.5'
    Assert-Equal 'Ambiguous' $decision.Status
    Assert-Match 'ethernet_mac' $decision.Reason 'the operator must be told how to resolve it'
}

Test-Case 'a MAC pin resolves an otherwise ambiguous choice' {
    $adapters = @(
        New-Adapter -Name 'Ethernet' -Index 6 -Mac 'AA-AA-AA-AA-AA-01'
        New-Adapter -Name 'Ethernet 2' -Index 7 -Mac 'AA-AA-AA-AA-AA-02'
    )
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.5' -PreferMacAddress 'aaaaaaaaaa02'
    Assert-Equal 'Selected' $decision.Status
    Assert-Equal 7 $decision.Adapter.InterfaceIndex
}

Test-Case 'a MAC pin that matches nothing refuses rather than falling back' {
    $adapters = @(New-Adapter -Name 'Ethernet' -Index 6 -Mac 'AA-AA-AA-AA-AA-01')
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.5' -PreferMacAddress 'DE-AD-BE-EF-00-00'
    Assert-Equal 'NoCandidate' $decision.Status
    Assert-Match 'Nothing was changed' $decision.Reason
}

Test-Case 'with two wired ports, the gateway-free one is chosen' {
    $adapters = @(
        New-Adapter -Name 'Ethernet' -Index 6 -Addresses @(New-Address -Ip '198.51.100.63') -Gateways @('198.51.100.1')
        New-Adapter -Name 'Ethernet 2' -Index 7 -Addresses @(New-Address -Ip '169.254.3.4' -Origin 'WellKnown')
    )
    $decision = Select-CapstoneEthernetAdapter -Adapters $adapters -TargetIp '192.0.2.6'
    Assert-Equal 'Selected' $decision.Status
    Assert-Equal 7 $decision.Adapter.InterfaceIndex
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Address conflict evidence'
# ---------------------------------------------------------------------------

Test-Case 'a silent address with no ARP entry is treated as clear, with a caveat' {
    $verdict = Get-CapstoneConflictVerdict -IcmpReplied $false -TcpAnswered $false -NeighbourMac ''
    Assert-Equal 'Clear' $verdict.Verdict
    Assert-Match 'not proof' $verdict.Reason 'absence of evidence must be stated as such'
}
Test-Case 'a host that drops ping but answers TCP is still a conflict' {
    # This is the measured behaviour of the GPU workstation at 192.0.2.2.
    $verdict = Get-CapstoneConflictVerdict -IcmpReplied $false -TcpAnswered $true
    Assert-Equal 'Conflict' $verdict.Verdict
}
Test-Case 'an ARP entry alone is enough to refuse the address' {
    $verdict = Get-CapstoneConflictVerdict -IcmpReplied $false -TcpAnswered $false -NeighbourMac '00-1A-2B-3C-4D-5E'
    Assert-Equal 'Conflict' $verdict.Verdict
}
Test-Case 'an all-zero ARP entry is not evidence of a conflict' {
    $verdict = Get-CapstoneConflictVerdict -NeighbourMac '00-00-00-00-00-00'
    Assert-Equal 'Clear' $verdict.Verdict
}
Test-Case 'an address this machine already owns is never a conflict' {
    $verdict = Get-CapstoneConflictVerdict -IcmpReplied $true -HeldLocally $true
    Assert-Equal 'Clear' $verdict.Verdict
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Network acceptance rules'
# ---------------------------------------------------------------------------

function New-Facts {
    param([array]$Addresses, [string]$Dhcp = 'Disabled', [array]$Dns = @(), [array]$Gateways = @(), [bool]$OnLink = $true)
    return [pscustomobject]@{
        InterfaceIndex = 6; InterfaceAlias = 'Ethernet'; Addresses = $Addresses
        Dhcp = $Dhcp; DnsServers = $Dns; DefaultGateways = $Gateways
        OnLinkRoute = $OnLink; TargetIp = '192.0.2.3'; PrefixLength = 24
    }
}

Test-Case 'a fully correct private port passes every check' {
    $verdict = Get-CapstoneNetworkVerdict -Facts (New-Facts -Addresses @([pscustomobject]@{ IPAddress = '192.0.2.3'; PrefixLength = 24 }))
    Assert-True $verdict.Pass $verdict.Summary
}
Test-Case 'DHCP left enabled fails, even with the right address present' {
    # netsh reports "DHCP enabled: Yes" for the coordinator while
    # Get-NetIPInterface reports Disabled. Only the cmdlet is trusted.
    $verdict = Get-CapstoneNetworkVerdict -Facts (New-Facts -Addresses @([pscustomobject]@{ IPAddress = '192.0.2.3'; PrefixLength = 24 }) -Dhcp 'Enabled')
    Assert-False $verdict.Pass
    Assert-True ($verdict.Failed -contains 'DhcpDisabled') 'DhcpDisabled should be the failing check'
}
Test-Case 'a default gateway on the private port fails' {
    $verdict = Get-CapstoneNetworkVerdict -Facts (New-Facts -Addresses @([pscustomobject]@{ IPAddress = '192.0.2.3'; PrefixLength = 24 }) -Gateways @('192.0.2.254'))
    Assert-False $verdict.Pass
    Assert-True ($verdict.Failed -contains 'NoDefaultGateway')
}
Test-Case 'an IPv4 DNS server on the private port fails' {
    $verdict = Get-CapstoneNetworkVerdict -Facts (New-Facts -Addresses @([pscustomobject]@{ IPAddress = '192.0.2.3'; PrefixLength = 24 }) -Dns @('198.51.100.1'))
    Assert-False $verdict.Pass
    Assert-True ($verdict.Failed -contains 'NoIpv4Dns')
}
Test-Case 'a 0.0.0.0 next hop is not counted as a real gateway' {
    $verdict = Get-CapstoneNetworkVerdict -Facts (New-Facts -Addresses @([pscustomobject]@{ IPAddress = '192.0.2.3'; PrefixLength = 24 }) -Gateways @('0.0.0.0'))
    Assert-True $verdict.Pass 'an on-link 0.0.0.0 next hop is normal'
}
Test-Case 'the wrong prefix length fails' {
    $verdict = Get-CapstoneNetworkVerdict -Facts (New-Facts -Addresses @([pscustomobject]@{ IPAddress = '192.0.2.3'; PrefixLength = 16 }))
    Assert-False $verdict.Pass
    Assert-True ($verdict.Failed -contains 'Address')
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Rollback planning'
# ---------------------------------------------------------------------------

Test-Case 'an adapter that was on DHCP goes back to DHCP, not to its old lease (defect D-08)' {
    $backup = [pscustomobject]@{
        interface_index = 6; interface_alias = 'Ethernet'; dhcp_state = 'Enabled'
        ipv4_addresses = @([pscustomobject]@{ ip = '198.51.100.63'; prefix = 24 })
        dns_servers = @('198.51.100.1'); default_gateways = @('198.51.100.1')
    }
    $plan = Resolve-CapstoneRollbackPlan -Backup $backup
    Assert-Equal 'RestoreDhcp' $plan.Action
    Assert-Equal 0 @($plan.RestoreAddresses).Count 'an expired lease must not be re-applied as a static address'
}

Test-Case 'a genuinely static adapter has its exact settings restored' {
    $backup = [pscustomobject]@{
        interface_index = 6; interface_alias = 'Ethernet'; dhcp_state = 'Disabled'
        ipv4_addresses = @([pscustomobject]@{ ip = '10.50.0.9'; prefix = 24 })
        dns_servers = @('10.50.0.1'); default_gateways = @('10.50.0.1')
    }
    $plan = Resolve-CapstoneRollbackPlan -Backup $backup
    Assert-Equal 'RestoreStatic' $plan.Action
    Assert-Equal '10.50.0.9' @($plan.RestoreAddresses)[0].ip
    Assert-Equal '10.50.0.1' $plan.RestoreGateway
}

Test-Case 'an APIPA-only adapter falls back to DHCP rather than restoring 169.254.x' {
    $backup = [pscustomobject]@{
        interface_index = 6; interface_alias = 'Ethernet'; dhcp_state = 'Disabled'
        ipv4_addresses = @([pscustomobject]@{ ip = '169.254.9.9'; prefix = 16 })
        dns_servers = @(); default_gateways = @()
    }
    $plan = Resolve-CapstoneRollbackPlan -Backup $backup
    Assert-Equal 'RestoreDhcp' $plan.Action
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Ollama server strategy'
# ---------------------------------------------------------------------------

Test-Case 'a healthy loopback server is reused, never replaced (defect D-06)' {
    $strategy = Resolve-CapstoneServerStrategy -LocalApiHealthy $true -ExecutablePresent $true
    Assert-Equal 'ReuseExisting' $strategy.Action
}
Test-Case 'a healthy private-LAN server is reused' {
    $strategy = Resolve-CapstoneServerStrategy -LocalApiHealthy $false -PrivateApiHealthy $true -ExecutablePresent $true
    Assert-Equal 'ReusePrivate' $strategy.Action
}
Test-Case 'no executable means install first' {
    $strategy = Resolve-CapstoneServerStrategy -ExecutablePresent $false
    Assert-Equal 'InstallFirst' $strategy.Action
}
Test-Case 'a non-Ollama process on port 11434 stops the run instead of killing it' {
    $listeners = @([pscustomobject]@{ LocalAddress = '0.0.0.0'; Port = 11434; ProcessId = 4242; ProcessName = 'python' })
    $strategy = Resolve-CapstoneServerStrategy -Listeners $listeners -ExecutablePresent $true
    Assert-Equal 'StopWithPortConflict' $strategy.Action
    Assert-Match 'python' $strategy.Reason
}
Test-Case 'an Ollama process already listening is waited for, not duplicated' {
    $listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; Port = 11434; ProcessId = 10; ProcessName = 'ollama' })
    $strategy = Resolve-CapstoneServerStrategy -Listeners $listeners -ExecutablePresent $true
    Assert-Equal 'WaitForExisting' $strategy.Action
}
Test-Case 'a running tray app with no listener yet is waited for' {
    $strategy = Resolve-CapstoneServerStrategy -TrayAppRunning $true -ExecutablePresent $true
    Assert-Equal 'WaitForExisting' $strategy.Action
}
Test-Case 'a clean machine with Ollama present starts the supported Windows app' {
    $strategy = Resolve-CapstoneServerStrategy -ExecutablePresent $true
    Assert-Equal 'StartTrayApp' $strategy.Action
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Model name handling'
# ---------------------------------------------------------------------------

Test-Case 'an untagged reference is canonicalised to :latest' {
    Assert-Equal 'nomic-embed-text:latest' (Resolve-CapstoneModelKey -Reference 'nomic-embed-text')
}
Test-Case 'an explicit tag is preserved' {
    Assert-Equal 'qwen3:0.6b' (Resolve-CapstoneModelKey -Reference 'qwen3:0.6b')
}
Test-Case 'comparison is case-insensitive' {
    Assert-Equal 'gemma3:4b' (Resolve-CapstoneModelKey -Reference 'Gemma3:4B')
}
Test-Case 'a model requested untagged is found when the server reports it as :latest' {
    # Comparing raw strings here marked an installed model as missing, which
    # then failed the whole profile.
    Assert-True (Test-CapstoneModelPresent -Tag 'all-minilm' -InstalledNames @('all-minilm:latest'))
}
Test-Case 'a model requested with :latest is found when reported untagged' {
    Assert-True (Test-CapstoneModelPresent -Tag 'all-minilm:latest' -InstalledNames @('all-minilm'))
}
Test-Case 'a similarly named model is not mistaken for the requested one' {
    Assert-False (Test-CapstoneModelPresent -Tag 'qwen3:4b' -InstalledNames @('qwen3:14b', 'qwen3:0.6b'))
}
Test-Case 'an empty inventory reports nothing present without erroring' {
    Assert-False (Test-CapstoneModelPresent -Tag 'qwen3:4b' -InstalledNames @())
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Download planning'
# ---------------------------------------------------------------------------

$sampleModels = @(
    [pscustomobject]@{ tag = 'qwen3:0.6b'; kind = 'generate'; bytes = 522640096L; family = 'qwen3'; role = 'routing' }
    [pscustomobject]@{ tag = 'gemma3:1b';  kind = 'generate'; bytes = 815319791L; family = 'gemma3'; role = 'routing' }
    [pscustomobject]@{ tag = 'all-minilm'; kind = 'embedding'; bytes = 46269440L; family = 'all-minilm'; role = 'embedding' }
)

Test-Case 'already-present models are not queued for download' {
    $plan = Get-CapstoneModelPlan -CatalogModels $sampleModels -InstalledNames @('qwen3:0.6b', 'all-minilm:latest') -FreeBytes (200GB)
    Assert-Equal 2 $plan.AlreadyInstalled
    Assert-Equal 1 $plan.PendingCount
    Assert-Equal 815319791 $plan.ToDownloadBytes 'only the missing model counts toward the download size'
}

Test-Case 'a partially populated inventory resumes rather than restarting (7 of 18 case)' {
    $eighteen = @(1..18 | ForEach-Object { [pscustomobject]@{ tag = ('model-{0}:1b' -f $_); kind = 'generate'; bytes = 1GB } })
    $installed = @(1..7 | ForEach-Object { ('model-{0}:1b' -f $_) })
    $plan = Get-CapstoneModelPlan -CatalogModels $eighteen -InstalledNames $installed -FreeBytes (200GB)
    Assert-Equal 7 $plan.AlreadyInstalled
    Assert-Equal 11 $plan.PendingCount
    Assert-Equal 18 $plan.TotalCount
}

Test-Case 'low disk space marks pending items InsufficientDisk instead of starting' {
    $plan = Get-CapstoneModelPlan -CatalogModels $sampleModels -InstalledNames @() -FreeBytes (2GB)
    Assert-False $plan.DiskSufficient
    Assert-Equal 3 @($plan.Items | Where-Object { $_.Status -eq 'InsufficientDisk' }).Count
    Assert-Match 'No existing model is deleted' $plan.DiskNote
}

Test-Case 'the disk reserve is honoured even when the raw total would fit' {
    # 1.4 GB of models would fit in 5 GB, but not with a 10 GB reserve.
    $plan = Get-CapstoneModelPlan -CatalogModels $sampleModels -InstalledNames @() -FreeBytes (5GB) -ReserveBytes (10GB)
    Assert-False $plan.DiskSufficient
}

Test-Case 'unmeasured free space does not block downloads but is reported honestly' {
    $plan = Get-CapstoneModelPlan -CatalogModels $sampleModels -InstalledNames @() -FreeBytes -1
    Assert-True $plan.DiskSufficient
    Assert-Match 'not measured' $plan.DiskNote
}

Test-Case 'an embedding model keeps its kind through planning, so it is verified correctly' {
    $plan = Get-CapstoneModelPlan -CatalogModels $sampleModels -InstalledNames @() -FreeBytes (200GB)
    $embedding = @($plan.Items | Where-Object { $_.Tag -eq 'all-minilm' })[0]
    Assert-Equal 'embedding' $embedding.Kind 'a generation prompt must never be sent to this model'
}

Test-Case 'queue summary counts installed, failed and verified separately' {
    $items = @(
        [pscustomobject]@{ Tag = 'a:1b'; Status = 'Verified';         InferenceOk = $true }
        [pscustomobject]@{ Tag = 'b:1b'; Status = 'AlreadyInstalled'; InferenceOk = $true }
        [pscustomobject]@{ Tag = 'c:1b'; Status = 'Failed';           InferenceOk = $false }
        [pscustomobject]@{ Tag = 'd:1b'; Status = 'InsufficientDisk'; InferenceOk = $false }
    )
    $summary = Get-CapstoneQueueSummary -Items $items
    Assert-Equal 2 $summary.Installed
    Assert-Equal 2 $summary.Failed
    Assert-Equal 2 $summary.InferenceOk
    Assert-True ($summary.FailedTags -contains 'c:1b')
    Assert-True ($summary.FailedTags -contains 'd:1b')
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Generation response interpretation'
# ---------------------------------------------------------------------------

Test-Case 'an ordinary answer is a pass' {
    $verdict = Test-CapstoneGenerationResponse -ResponseText 'READY' -Done $true -DoneReason 'stop' -EvalCount 3
    Assert-True $verdict.Success
    Assert-Equal 'response' $verdict.Kind
    Assert-Match 'READY' $verdict.Note
}

Test-Case 'a reasoning model that spent its budget thinking is a pass, not a failure' {
    # The exact response measured from qwen3:4b on the GPU workstation:
    # empty response, populated thinking, done=true, done_reason=length.
    # Treating this as a failure would fail all nine Qwen3 tags in the catalog.
    $verdict = Test-CapstoneGenerationResponse `
        -ResponseText '' `
        -ThinkingText 'Hmm, the user just asked me to reply with the single word "READY".' `
        -Done $true -DoneReason 'length' -EvalCount 16
    Assert-True $verdict.Success 'a model that generated reasoning tokens demonstrably loaded and ran'
    Assert-Equal 'thinking' $verdict.Kind
    Assert-Match 'thinks before answering' $verdict.Note 'the operator should understand why there is no final answer'
}

Test-Case 'an incomplete generation is a failure' {
    $verdict = Test-CapstoneGenerationResponse -ResponseText 'part' -Done $false -EvalCount 2
    Assert-False $verdict.Success
    Assert-Match 'did not report a completed generation' $verdict.Note
}

Test-Case 'a completed generation with no tokens at all is a failure' {
    $verdict = Test-CapstoneGenerationResponse -ResponseText '' -ThinkingText '' -Done $true -DoneReason 'stop' -EvalCount 0
    Assert-False $verdict.Success
    Assert-Match 'no tokens at all' $verdict.Note
}

Test-Case 'whitespace-only output is not accepted as a reply' {
    $verdict = Test-CapstoneGenerationResponse -ResponseText "   `n" -ThinkingText '' -Done $true -EvalCount 0
    Assert-False $verdict.Success
}

Test-Case 'a model that emitted tokens but returned no text is still a pass' {
    $verdict = Test-CapstoneGenerationResponse -ResponseText '' -ThinkingText '' -Done $true -DoneReason 'length' -EvalCount 12
    Assert-True $verdict.Success 'tokens were generated, so the model loaded and ran'
}

Test-Case 'a long reply is truncated in the note rather than dumped whole' {
    $long = 'A' * 500
    $verdict = Test-CapstoneGenerationResponse -ResponseText $long -Done $true -EvalCount 500
    Assert-True $verdict.Success
    Assert-True ($verdict.Note.Length -lt 200) ('the note was {0} characters' -f $verdict.Note.Length)
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Failure classification and retry'
# ---------------------------------------------------------------------------

Test-Case 'an invalid model tag is permanent and is never retried' {
    $category = Get-CapstoneFailureCategory -Message 'pull model manifest: file does not exist' -HttpStatus 404
    Assert-Equal 'MODEL_TAG_INVALID' $category.Category
    Assert-False $category.Retryable
    Assert-Match 'no substitute model' $category.Recovery
}
Test-Case 'a forcibly closed transport is transient and retried (defect D-08)' {
    $category = Get-CapstoneFailureCategory -Message 'Unable to read data from the transport connection: An existing connection was forcibly closed by the remote host.'
    Assert-Equal 'MODEL_DOWNLOAD_INTERRUPTED' $category.Category
    Assert-True $category.Retryable
}
Test-Case 'HTTP 500 from the registry is transient but bounded (defect D-07)' {
    $category = Get-CapstoneFailureCategory -Message 'internal server error' -HttpStatus 500
    Assert-Equal 'MODEL_PULL_SERVER_ERROR' $category.Category
    Assert-True $category.Retryable
    Assert-Match 'rather than reinterpreted' $category.Recovery
}
Test-Case 'a DNS failure is reported as a network problem, not a bad tag' {
    $category = Get-CapstoneFailureCategory -Message 'No such host is known'
    Assert-Equal 'NO_INTERNET_CONNECTION' $category.Category
    Assert-True $category.Retryable
}
Test-Case 'a full disk stops immediately rather than retrying' {
    $category = Get-CapstoneFailureCategory -Message 'write blob: no space left on device'
    Assert-Equal 'DISK_SPACE_INADEQUATE' $category.Category
    Assert-False $category.Retryable
}
Test-Case 'a refused connection is a missing server, not a model fault' {
    $category = Get-CapstoneFailureCategory -Message 'No connection could be made because the target machine actively refused it'
    Assert-Equal 'LOCAL_API_UNAVAILABLE' $category.Category
    Assert-Match 'second server' $category.Recovery
}
Test-Case 'an unsigned installer is a permanent, named failure' {
    $category = Get-CapstoneFailureCategory -Message 'The file is not digitally signed'
    Assert-Equal 'INSTALLER_SIGNATURE_FAILURE' $category.Category
    Assert-False $category.Retryable
}
Test-Case 'an unrecognised error is never silently classed as retryable' {
    $category = Get-CapstoneFailureCategory -Message 'something nobody predicted'
    Assert-Equal 'UNEXPECTED_EXCEPTION' $category.Category
    Assert-False $category.Retryable
}
Test-Case 'retry backoff grows but is capped' {
    Assert-Equal 10 (Get-CapstoneRetryDelaySeconds -Attempt 1)
    Assert-Equal 20 (Get-CapstoneRetryDelaySeconds -Attempt 2)
    Assert-Equal 40 (Get-CapstoneRetryDelaySeconds -Attempt 3)
    Assert-Equal 300 (Get-CapstoneRetryDelaySeconds -Attempt 20) 'must never grow without bound'
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Lifecycle state machine'
# ---------------------------------------------------------------------------

Test-Case 'no executable means NOT_INSTALLED' {
    Assert-Equal 'NOT_INSTALLED' (Resolve-CapstoneMachineState -ExecutablePresent $false)
}
Test-Case 'installed but silent API means SERVER_STARTING, not failed' {
    Assert-Equal 'SERVER_STARTING' (Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $false)
}
Test-Case 'an empty model inventory on a healthy server is not a failed install (defect D-05)' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true -ModelsRequested 0 -ModelsInstalled 0 -RemoteRequested $false
    Assert-Equal 'VERIFIED' $state 'zero requested models is a legitimate fully-verified outcome'
}
Test-Case 'a partially populated inventory is PARTIAL' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true -ModelsRequested 18 -ModelsInstalled 7 -ModelsFailed 1 -InferenceVerified $true
    Assert-Equal 'PARTIAL' $state
}
Test-Case 'downloaded models that fail inference are not VERIFIED' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $false -RemoteRequested $false
    Assert-Equal 'PARTIAL' $state 'presence in the inventory is not proof a model loads'
}
Test-Case 'a complete head awaiting coordinator proof is REMOTE_READY, not VERIFIED' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true `
        -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $true -CoordinatorVerified $false
    Assert-Equal 'REMOTE_READY' $state
}
Test-Case 'a fully proven head is VERIFIED' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true `
        -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $true -CoordinatorVerified $true
    Assert-Equal 'VERIFIED' $state
}
Test-Case 'a configured listener that will not bind is PARTIAL' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true `
        -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $false
    Assert-Equal 'PARTIAL' $state
}
Test-Case 'a blocking condition overrides every other signal' {
    $state = Resolve-CapstoneMachineState -ExecutablePresent $true -LocalApiHealthy $true -BlockingActionRequired $true
    Assert-Equal 'FAILED_REQUIRES_ACTION' $state
}
Test-Case 'every state name in the specification is declared' {
    $names = Get-CapstoneStateNames
    foreach ($required in @('NOT_INSTALLED', 'INSTALLING', 'INSTALLED', 'SERVER_STARTING', 'SERVER_READY',
                            'MODELS_DOWNLOADING', 'MODELS_VERIFIED', 'REMOTE_CONFIGURING', 'REMOTE_READY',
                            'VERIFIED', 'PARTIAL', 'FAILED_RECOVERABLE', 'FAILED_REQUIRES_ACTION')) {
        if ($names -notcontains $required) { throw ('state {0} is missing' -f $required) }
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Remote serving'
# ---------------------------------------------------------------------------

Test-Case 'PrivateLan mode binds all interfaces and keeps loopback usable' {
    $binding = Resolve-CapstoneRemoteBinding -PrivateIp '192.0.2.3' -BindMode 'PrivateLan'
    Assert-Equal '0.0.0.0:11434' $binding.Value
    Assert-True $binding.LoopbackWorks
}
Test-Case 'PrivateOnly mode binds the private address and gives up loopback' {
    $binding = Resolve-CapstoneRemoteBinding -PrivateIp '192.0.2.3' -BindMode 'PrivateOnly'
    Assert-Equal '192.0.2.3:11434' $binding.Value
    Assert-False $binding.LoopbackWorks
}
Test-Case 'an unset OLLAMA_HOST is safe to manage' {
    Assert-True (Test-CapstoneManagedHostValue -Value '')
}
Test-Case 'a value this tool previously set is safe to replace' {
    Assert-True (Test-CapstoneManagedHostValue -Value '192.0.2.4:11434')
    Assert-True (Test-CapstoneManagedHostValue -Value '127.0.0.1:11434')
    Assert-True (Test-CapstoneManagedHostValue -Value '0.0.0.0:11434')
}
Test-Case 'an operator''s own custom endpoint is never silently overwritten' {
    Assert-False (Test-CapstoneManagedHostValue -Value 'ollama.internal.example.com:11434')
    Assert-False (Test-CapstoneManagedHostValue -Value '192.168.7.7:11434')
}

Test-Case 'a head with a live private listener still reports coordinator proof as pending' {
    $verification = Get-CapstoneRemoteVerification -LocalApiHealthy $true -PrivateListenerHealthy $true -FirewallRuleApplied $true
    Assert-Equal 'REMOTE_COORDINATOR_VERIFICATION_PENDING' $verification.Overall
    Assert-Match 'cannot truthfully claim' $verification.Note
}
Test-Case 'coordinator reachability alone does not equal proven remote inference' {
    $verification = Get-CapstoneRemoteVerification -LocalApiHealthy $true -PrivateListenerHealthy $true -CoordinatorApiProven $true
    Assert-Equal 'REMOTE_COORDINATOR_REACHABLE' $verification.Overall
}
Test-Case 'all four levels proven gives end-to-end verification' {
    $verification = Get-CapstoneRemoteVerification -LocalApiHealthy $true -PrivateListenerHealthy $true -CoordinatorApiProven $true -CoordinatorInferenceProven $true
    Assert-Equal 'REMOTE_VERIFIED_END_TO_END' $verification.Overall
}
Test-Case 'a healthy local server with no private listener is reported as not bound' {
    $verification = Get-CapstoneRemoteVerification -LocalApiHealthy $true -PrivateListenerHealthy $false
    Assert-Equal 'REMOTE_LISTENER_NOT_BOUND' $verification.Overall
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Overall outcome reporting'
# ---------------------------------------------------------------------------

Test-Case 'everything proven reports FULL_SUCCESS and Phase 4 readiness' {
    $outcome = Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 10 -ModelsInstalled 10 -ModelsFailed 0 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $true -CoordinatorVerified $true
    Assert-Equal 'FULL_SUCCESS' $outcome.Outcome
    Assert-True $outcome.ReadyForPhase4
}

Test-Case 'one failed tag out of eighteen is PARTIAL_SUCCESS, never success (defect D-11)' {
    $outcome = Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 18 -ModelsInstalled 17 -ModelsFailed 1 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $true -CoordinatorVerified $true `
        -FailedTags @('granite4:3b')
    Assert-Equal 'PARTIAL_SUCCESS' $outcome.Outcome
    Assert-False $outcome.ReadyForPhase4
    Assert-Match 'granite4:3b' ($outcome.Blockers -join ' ') 'the exact failing tag must be named'
}

Test-Case 'zero installed models with a healthy server is FAILED, not success (defect D-11)' {
    $outcome = Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 18 -ModelsInstalled 0 -ModelsFailed 18 -InferenceVerified $false
    Assert-Equal 'FAILED' $outcome.Outcome
}

Test-Case 'pending coordinator verification prevents FULL_SUCCESS but is not a blocker' {
    $outcome = Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $true -CoordinatorVerified $false
    Assert-Equal 'PARTIAL_SUCCESS' $outcome.Outcome
    Assert-Equal 0 $outcome.Blockers.Count 'nothing is broken; verification is simply incomplete'
    Assert-Match 'Coordinator verification' ($outcome.Notes -join ' ')
}

Test-Case 'a remote-serving problem does not erase successful model work (defect D-06)' {
    $outcome = Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 18 -ModelsInstalled 18 -ModelsFailed 0 -InferenceVerified $true `
        -RemoteConfigured $true -PrivateListenerHealthy $false
    Assert-Equal 'PARTIAL_SUCCESS' $outcome.Outcome
    Assert-Equal 0 $outcome.Blockers.Count 'the models are fine; only the listener is not'
}

Test-Case 'a run that skipped remote serving can still be FULL_SUCCESS' {
    $outcome = Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $true -RemoteRequested $false
    Assert-Equal 'FULL_SUCCESS' $outcome.Outcome
}

Test-Case 'an unverified network blocks success even when models are complete' {
    $outcome = Get-CapstoneOverallOutcome -NetworkRequested $true -NetworkVerified $false `
        -OllamaInstalled $true -LocalApiHealthy $true `
        -ModelsRequested 3 -ModelsInstalled 3 -InferenceVerified $true
    Assert-Equal 'PARTIAL_SUCCESS' $outcome.Outcome
    Assert-Match 'private-LAN address' ($outcome.Blockers -join ' ')
}

Test-Case 'yes/no formatting is unambiguous in the operator report' {
    Assert-Equal 'YES' (Format-CapstoneYesNo $true)
    Assert-Equal 'NO' (Format-CapstoneYesNo $false)
    Assert-Equal 'unknown' (Format-CapstoneYesNo $null)
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Shipped catalog'
# ---------------------------------------------------------------------------

$catalog = Get-CapstoneCatalog -Path (Join-Path $PackageRoot 'engine\catalog\model-catalog.json')

Test-Case 'the CPU16 profile offers the full multi-family comparison set' {
    $models = @(Get-CapstoneProfileModels -Catalog $catalog -ProfileName 'CPU16')
    Assert-Equal 18 $models.Count 'the eighteen requested CPU16 generation tags'
    $families = @($models | ForEach-Object { $_.family } | Select-Object -Unique)
    Assert-Equal 7 $families.Count 'qwen3, qwen3.5, qwen2.5, gemma3, llama3.2, granite4 and smollm2 are seven families across the eighteen tags'
}

Test-Case 'embeddings are included only when explicitly requested' {
    $without = @(Get-CapstoneProfileModels -Catalog $catalog -ProfileName 'CPU8')
    $with = @(Get-CapstoneProfileModels -Catalog $catalog -ProfileName 'CPU8' -IncludeEmbeddings)
    Assert-True ($with.Count -gt $without.Count) 'the embedding list must add entries'
    Assert-Equal 0 @($without | Where-Object { $_.kind -eq 'embedding' }).Count
}

Test-Case 'an unknown profile name fails loudly instead of defaulting' {
    $threw = $false
    try { [void](Get-CapstoneProfileModels -Catalog $catalog -ProfileName 'CPU64') } catch { $threw = $true }
    Assert-True $threw 'an unknown profile must not silently fall back to another one'
}

Test-Case 'every shipped tag carries a registry-measured size' {
    foreach ($model in @($catalog.models)) {
        if ([int64]$model.bytes -le 0) { throw ('{0} has no measured size' -f $model.tag) }
    }
}

Test-Case 'profile sizes are plausible against the documented hardware' {
    $cpu8 = ($catalog.profiles.CPU8.total_bytes / 1GB)
    $cpu32 = ($catalog.profiles.CPU32.total_bytes / 1GB)
    Assert-True ($cpu8 -lt $cpu32) 'CPU32 must be the larger download'
    Assert-True ($cpu32 -lt 200) 'a profile larger than 200 GB would indicate a catalog error'
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Byte formatting'
# ---------------------------------------------------------------------------

Test-Case 'sizes render in sensible units' {
    Assert-Match 'GB' (Format-CapstoneBytes -Bytes 8589934592)
    Assert-Match 'MB' (Format-CapstoneBytes -Bytes 52428800)
    Assert-Match 'KB' (Format-CapstoneBytes -Bytes 4096)
}
Test-Case 'an unmeasured size is reported as unknown, not as zero' {
    Assert-Equal 'unknown' (Format-CapstoneBytes -Bytes -1)
}

# ---------------------------------------------------------------------------

$summary = Write-TestSummary -Title 'UNIT TEST SUMMARY'

if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    $directory = Split-Path -Parent $ResultPath
    if ($directory -and -not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $payload = ([pscustomobject]@{
        suite = 'unit-tests'
        generated_at = (Get-Date).ToString('o')
        powershell_version = $PSVersionTable.PSVersion.ToString()
        total = $summary.Total; passed = $summary.Passed; failed = $summary.Failed
        results = $summary.Results
    }) | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($ResultPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ('  Results written to {0}' -f $ResultPath) -ForegroundColor Cyan
}

if ($summary.Failed -gt 0) { exit 1 }
exit 0
