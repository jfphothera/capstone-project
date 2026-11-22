<#
    Lab.Firewall - who can reach this head's Ollama port, and the one reviewed way to change it.

    Ollama has no authentication; Windows Defender Firewall is the only access control on
    port 11434. Provisioner 1.0.3 created one rule admitting 192.0.2.1 and never looked at
    any other rule. This module:

      * reads EVERY enabled inbound rule from the active policy store (local and Group Policy),
        with its port, address, program and service filters, and the firewall profiles;
      * works out, for each source address of interest, whether a connection to Ollama would
        be admitted, and by which rules (an analysis of the rules, labelled as such - a real
        connection from that address is the field proof);
      * backs up the rule set (JSON plus a full `netsh advfirewall export`) before any change;
      * converges on one managed rule admitting only the approved clients, disables the
        provisioner's legacy rule, and offers - one by one, with approval - to disable other
        Ollama-specific rules that admit anyone else. Unrelated or ambiguous rules (for example
        a broad "allow everything" rule or a Group Policy rule) are reported, never changed;
      * records every change with its undo, so Undo-LabFirewallChange restores the exact
        earlier state.
#>

$script:Backend = $null

function Get-LabDefaultFirewallBackend {
    return @{
        Snapshot = { Get-LabFirewallSnapshotLive }
        NewRule  = { param($p) New-NetFirewallRule @p -ErrorAction Stop | Out-Null }
        SetRule  = { param($name, $p) $q = ConvertTo-LabSetRuleParameters $p; Set-NetFirewallRule -Name $name @q -ErrorAction Stop | Out-Null }
        Enable   = { param($name) Enable-NetFirewallRule -Name $name -ErrorAction Stop | Out-Null }
        Disable  = { param($name) Disable-NetFirewallRule -Name $name -ErrorAction Stop | Out-Null }
        Remove   = { param($name) Remove-NetFirewallRule -Name $name -ErrorAction Stop | Out-Null }
        Export   = { param($path) $out = & netsh.exe advfirewall export "$path" 2>&1; if ($LASTEXITCODE -ne 0) { throw ('netsh export failed: ' + ($out -join ' ')) } }
    }
}

function ConvertTo-LabSetRuleParameters {
    <#  Pure - unit tested. Set-NetFirewallRule -Name cannot take -DisplayName (Windows: "Parameter set
        cannot be resolved"); renaming a rule is -NewDisplayName. Found by a -WhatIf rehearsal
        (2026-11-06): every update and undo of an existing head's Ollama rule would have failed.  #>
    param([Parameter(Mandatory)][hashtable]$Parameters)
    $q = @{}; foreach ($k in $Parameters.Keys) { if ($k -eq 'DisplayName') { $q['NewDisplayName'] = $Parameters[$k] } else { $q[$k] = $Parameters[$k] } }
    return $q
}

function Set-LabFirewallBackend { param([hashtable]$Backend) $script:Backend = $Backend }
function Get-LabFirewallBackend { if (-not $script:Backend) { $script:Backend = Get-LabDefaultFirewallBackend }; return $script:Backend }

# ---------------------------------------------------------------------------
# Snapshot (read-only)
# ---------------------------------------------------------------------------

function ConvertTo-LabProfileList {
    param([AllowNull()]$Value)
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text) -or $text -match 'Any') { return @('Domain', 'Private', 'Public') }
    return @($text -split '\s*,\s*' | Where-Object { $_ })
}

function Get-LabRulePackage {
    <#  The app package (AppContainer) a rule is bound to: the rule's PackageFamilyName (rules Windows adds
        for Store apps, such as "<family>-In-Allow-ServerCapability") or the application filter's package
        SID. '' for a rule that applies to desktop programs.  #>
    param($Rule, $ApplicationFilter = $null)
    $pfn = ''; try { if ($Rule.PSObject.Properties.Name -contains 'PackageFamilyName') { $pfn = [string]$Rule.PackageFamilyName } } catch { }
    if ($pfn) { return $pfn }
    $pkg = ''; try { if ($ApplicationFilter -and $ApplicationFilter.Package) { $pkg = [string]$ApplicationFilter.Package } } catch { }
    if ($pkg -and $pkg -ne 'Any') { return $pkg }
    return ''
}

function Test-LabRulePackaged {
    <#  Pure. A rule bound to an app package applies only to that package's AppContainer processes. Ollama
        and the Inference Gateway (python.exe as a Windows service) are desktop programs, never packaged.  #>
    param($Rule)
    return [bool]($Rule -and $Rule.PSObject.Properties.Name -contains 'Package' -and $Rule.Package -and [string]$Rule.Package -ne 'Any')
}

function Get-LabFirewallSnapshotLive {
    $store = 'ActiveStore'
    $rules = @(Get-NetFirewallRule -PolicyStore $store -Direction Inbound -ErrorAction Stop)
    $index = @{}
    foreach ($f in @(Get-NetFirewallPortFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['p:' + $f.InstanceID] = $f }
    foreach ($f in @(Get-NetFirewallAddressFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['a:' + $f.InstanceID] = $f }
    foreach ($f in @(Get-NetFirewallApplicationFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['x:' + $f.InstanceID] = $f }
    foreach ($f in @(Get-NetFirewallServiceFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['s:' + $f.InstanceID] = $f }
    $normal = @()
    foreach ($r in $rules) {
        $id = [string]$r.InstanceID
        $p = $index['p:' + $id]; $a = $index['a:' + $id]; $x = $index['x:' + $id]; $s = $index['s:' + $id]
        $normal += [pscustomobject][ordered]@{
            Name = [string]$r.Name; DisplayName = [string]$r.DisplayName; Group = [string]$r.Group
            Enabled = ([string]$r.Enabled -eq 'True'); Direction = [string]$r.Direction; Action = [string]$r.Action
            Profiles = @(ConvertTo-LabProfileList $r.Profile)
            Protocol = $(if ($p) { [string]$p.Protocol } else { 'Any' })
            LocalPorts = @($(if ($p) { @($p.LocalPort) | ForEach-Object { [string]$_ } } else { 'Any' }))
            RemoteAddresses = @($(if ($a) { @($a.RemoteAddress) | ForEach-Object { [string]$_ } } else { 'Any' }))
            LocalAddresses = @($(if ($a) { @($a.LocalAddress) | ForEach-Object { [string]$_ } } else { 'Any' }))
            Program = $(if ($x -and $x.Program) { [string]$x.Program } else { 'Any' })
            Service = $(if ($s -and $s.Service) { [string]$s.Service } else { 'Any' })
            Package = (Get-LabRulePackage -Rule $r -ApplicationFilter $x)
            PolicySource = [string]$r.PolicyStoreSourceType
            Description = [string]$r.Description
        }
    }
    $profiles = @(Get-NetFirewallProfile -PolicyStore $store -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject][ordered]@{ Name = [string]$_.Name; Enabled = ([string]$_.Enabled -eq 'True'); DefaultInboundAction = [string]$_.DefaultInboundAction
                                    AllowLocalFirewallRules = [string]$_.AllowLocalFirewallRules } })
    $connections = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject][ordered]@{ InterfaceAlias = [string]$_.InterfaceAlias; InterfaceIndex = [int]$_.InterfaceIndex; NetworkCategory = [string]$_.NetworkCategory } })
    return [pscustomobject][ordered]@{ TakenAt = (Get-Date).ToString('o'); Computer = $env:COMPUTERNAME; Rules = $normal; Profiles = $profiles; Connections = $connections }
}

function Get-LabFirewallSnapshot { $b = Get-LabFirewallBackend; return (& $b.Snapshot) }

# ---------------------------------------------------------------------------
# Matching (pure - unit tested)
# ---------------------------------------------------------------------------

function ConvertTo-LabIpNumber {
    param([Parameter(Mandatory)][string]$Address)
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($Address.Trim(), [ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork') { return $null }
    $b = $ip.GetAddressBytes()
    return ([uint32]$b[0] * 16777216 + [uint32]$b[1] * 65536 + [uint32]$b[2] * 256 + [uint32]$b[3])
}

function Test-LabAddressSpec {
    <#  Does one firewall address entry cover this IPv4 source?  Returns 'yes', 'no' or 'uncertain'
        (keywords such as Intranet or DNS depend on Windows' own view of the network).  #>
    param([Parameter(Mandatory)][string]$Spec, [Parameter(Mandatory)][string]$Source, [string]$LocalSubnet = '192.0.2.0/24')
    $s = $Spec.Trim()
    if ($s -eq '' -or $s -eq 'Any' -or $s -eq '*') { return 'yes' }
    if ($s -eq 'LocalSubnet' -or $s -eq 'LocalSubnet4') { return (Test-LabAddressSpec -Spec $LocalSubnet -Source $Source) }
    if ($s -match '^(Intranet|Internet|IntranetRemoteAccess|DNS|DHCP|WINS|DefaultGateway|PlayToDevice|LocalSubnet6|CaptivePortal)$') { return 'uncertain' }
    if ($s -match ':') { return 'no' }      # IPv6 entries never cover an IPv4 source
    $src = ConvertTo-LabIpNumber $Source
    if ($null -eq $src) { return 'no' }
    if ($s -match '^([\d\.]+)\s*-\s*([\d\.]+)$') {
        $lo = ConvertTo-LabIpNumber $Matches[1]; $hi = ConvertTo-LabIpNumber $Matches[2]
        if ($null -eq $lo -or $null -eq $hi) { return 'uncertain' }
        if ($src -ge $lo -and $src -le $hi) { return 'yes' } else { return 'no' }
    }
    if ($s -match '^([\d\.]+)/([\d\.]+)$') {
        $net = ConvertTo-LabIpNumber $Matches[1]; $m = $Matches[2]
        if ($null -eq $net) { return 'uncertain' }
        if ($m -match '^\d{1,2}$') { $bits = [int]$m; if ($bits -eq 0) { return 'yes' }; $mask = [uint32]([math]::Pow(2, 32) - [math]::Pow(2, 32 - $bits)) }
        else { $mask = ConvertTo-LabIpNumber $m; if ($null -eq $mask) { return 'uncertain' } }
        if (($src -band $mask) -eq ($net -band $mask)) { return 'yes' } else { return 'no' }
    }
    $one = ConvertTo-LabIpNumber $s
    if ($null -eq $one) { return 'uncertain' }
    if ($one -eq $src) { return 'yes' } else { return 'no' }
}

function Test-LabAddressList {
    param([AllowEmptyCollection()][array]$Specs, [Parameter(Mandatory)][string]$Source, [string]$LocalSubnet = '192.0.2.0/24')
    $results = @(@($Specs) | ForEach-Object { Test-LabAddressSpec -Spec ([string]$_) -Source $Source -LocalSubnet $LocalSubnet })
    if ($results.Count -eq 0) { return 'yes' }
    if ($results -contains 'yes') { return 'yes' }
    if ($results -contains 'uncertain') { return 'uncertain' }
    return 'no'
}

function Test-LabPortSpec {
    param([AllowEmptyCollection()][array]$Ports, [int]$Port = 11434)
    foreach ($p in @($Ports)) {
        $t = ([string]$p).Trim()
        if ($t -eq '' -or $t -eq 'Any') { return $true }
        if ($t -match '^\d+$' -and [int]$t -eq $Port) { return $true }
        if ($t -match '^(\d+)\s*-\s*(\d+)$' -and [int]$Matches[1] -le $Port -and [int]$Matches[2] -ge $Port) { return $true }
    }
    return $false
}

function Test-LabOllamaProgram {
    param([string]$Program)
    if ([string]::IsNullOrWhiteSpace($Program) -or $Program -eq 'Any') { return $false }
    return ([IO.Path]::GetFileName([Environment]::ExpandEnvironmentVariables($Program)) -match '^(?i)ollama( app)?\.exe$')
}

function Test-LabProgramMatch {
    <#  Would this rule's program condition apply to the Ollama server process?  #>
    param([string]$Program, [AllowEmptyCollection()][array]$ServerPaths = @())
    if ([string]::IsNullOrWhiteSpace($Program) -or $Program -eq 'Any') { return 'yes' }
    $expanded = [Environment]::ExpandEnvironmentVariables($Program)
    foreach ($p in @($ServerPaths)) { if ($p -and ([Environment]::ExpandEnvironmentVariables([string]$p)) -ieq $expanded) { return 'yes' } }
    if ([IO.Path]::GetFileName($expanded) -ieq 'ollama.exe') { return 'uncertain' }   # another ollama.exe path
    return 'no'
}

function Get-LabRuleClass {
    param([Parameter(Mandatory)]$Rule, $Config)
    $managed = 'AgentArenaLab-Ollama-11434'; $legacy = 'Capstone Ollama Head'
    if ($Config -and $Config.ollama) { $managed = [string]$Config.ollama.managed_rule_name; $legacy = [string]$Config.ollama.legacy_rule_prefix }
    if ($Rule.Name -eq $managed) { return 'MANAGED' }
    if ($Rule.DisplayName -like ($legacy + '*')) { return 'LEGACY_PROVISIONER' }
    if ($Rule.PolicySource -eq 'GroupPolicy') { return 'GROUP_POLICY' }
    if ($Rule.Action -eq 'Block') { return 'BLOCK' }
    if (Test-LabRulePackaged $Rule) { return 'APP_PACKAGE' }
    if (Test-LabOllamaProgram $Rule.Program) { return 'OLLAMA_PROGRAM' }
    # Only a rule that names the Ollama port itself is Ollama-specific. Port ranges and "Any"
    # belong to other applications and are reported for review, never changed.
    $port = '11434'; if ($Config -and $Config.ollama) { $port = [string]$Config.ollama.port }
    if (@($Rule.LocalPorts | Where-Object { ([string]$_).Trim() -eq $port }).Count -gt 0) { return 'PORT_11434' }
    return 'BROAD'
}

function Test-LabRuleCovers {
    <#  'yes' / 'no' / 'uncertain': this rule applies to an Ollama connection from Source.  #>
    param([Parameter(Mandatory)]$Rule, [Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$HeadIp,
          [Parameter(Mandatory)][string]$ActiveProfile, [AllowEmptyCollection()][array]$ServerPaths = @(), [int]$Port = 11434, [string]$LocalSubnet = '192.0.2.0/24')
    if (-not $Rule.Enabled -or $Rule.Direction -ne 'Inbound') { return 'no' }
    if (Test-LabRulePackaged $Rule) { return 'no' }      # an app-package rule never admits a desktop program such as Ollama
    if (@($Rule.Profiles) -notcontains $ActiveProfile) { return 'no' }
    if (@('TCP', 'Any', '6') -notcontains [string]$Rule.Protocol) { return 'no' }
    if (-not (Test-LabPortSpec -Ports $Rule.LocalPorts -Port $Port)) { return 'no' }
    if ($Rule.Service -and $Rule.Service -ne 'Any') { return 'no' }
    $local = Test-LabAddressList -Specs $Rule.LocalAddresses -Source $HeadIp -LocalSubnet $LocalSubnet
    $remote = Test-LabAddressList -Specs $Rule.RemoteAddresses -Source $Source -LocalSubnet $LocalSubnet
    $program = Test-LabProgramMatch -Program $Rule.Program -ServerPaths $ServerPaths
    $all = @($local, $remote, $program)
    if ($all -contains 'no') { return 'no' }
    if ($all -contains 'uncertain') { return 'uncertain' }
    return 'yes'
}

function Get-LabActiveProfile {
    param([Parameter(Mandatory)]$Snapshot, [string]$InterfaceAlias = '')
    $c = @($Snapshot.Connections | Where-Object { -not $InterfaceAlias -or $_.InterfaceAlias -eq $InterfaceAlias })
    if ($c.Count -eq 0) { return 'Public' }      # an unidentified network is treated as Public by Windows
    switch ([string]$c[0].NetworkCategory) { 'DomainAuthenticated' { return 'Domain' } 'Private' { return 'Private' } default { return 'Public' } }
}

function Get-LabOllamaExposure {
    <#  For each source: ADMITTED, DENIED or UNCERTAIN, with the rules that decide it.
        This is an analysis of the rule set (Windows: a matching block rule wins over allow
        rules; otherwise any matching allow rule admits; otherwise the profile default).  #>
    param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)][string]$HeadIp, [Parameter(Mandatory)][string[]]$Sources,
          [string]$InterfaceAlias = '', [AllowEmptyCollection()][array]$ServerPaths = @(), [int]$Port = 11434, $Config = $null)
    $profileName = Get-LabActiveProfile -Snapshot $Snapshot -InterfaceAlias $InterfaceAlias
    $fwProfile = @($Snapshot.Profiles | Where-Object { $_.Name -eq $profileName }) | Select-Object -First 1
    $relevant = @()
    foreach ($rule in @($Snapshot.Rules)) {
        if (-not $rule.Enabled -or $rule.Direction -ne 'Inbound') { continue }
        $any = 'no'
        foreach ($src in $Sources) { $v = Test-LabRuleCovers -Rule $rule -Source $src -HeadIp $HeadIp -ActiveProfile $profileName -ServerPaths $ServerPaths -Port $Port; if ($v -eq 'yes') { $any = 'yes'; break }; if ($v -eq 'uncertain') { $any = 'uncertain' } }
        if ($any -ne 'no' -or (Test-LabOllamaProgram $rule.Program)) {
            $relevant += [pscustomobject][ordered]@{ Name = $rule.Name; DisplayName = $rule.DisplayName; Class = (Get-LabRuleClass -Rule $rule -Config $Config)
                                                     Action = $rule.Action; Profiles = ($rule.Profiles -join ','); Program = $rule.Program
                                                     LocalPorts = ($rule.LocalPorts -join ','); RemoteAddresses = ($rule.RemoteAddresses -join ','); PolicySource = $rule.PolicySource }
        }
    }
    $results = @()
    foreach ($src in $Sources) {
        $allow = @(); $allowMaybe = @(); $block = @(); $blockMaybe = @()
        foreach ($rule in @($Snapshot.Rules)) {
            $v = Test-LabRuleCovers -Rule $rule -Source $src -HeadIp $HeadIp -ActiveProfile $profileName -ServerPaths $ServerPaths -Port $Port
            if ($v -eq 'no') { continue }
            if ($rule.Action -eq 'Block') { if ($v -eq 'yes') { $block += $rule.DisplayName } else { $blockMaybe += $rule.DisplayName } }
            else { if ($v -eq 'yes') { $allow += $rule.DisplayName } else { $allowMaybe += $rule.DisplayName } }
        }
        $verdict = 'DENIED'; $why = ''
        if ($fwProfile -and -not $fwProfile.Enabled) { $verdict = 'ADMITTED'; $why = "the $profileName firewall profile is turned off" }
        elseif ($block.Count) { $verdict = 'DENIED'; $why = 'blocked by ' + ($block -join '; ') }
        elseif ($allow.Count) { $verdict = 'ADMITTED'; $why = 'allowed by ' + ($allow -join '; ') }
        elseif ($allowMaybe.Count -or $blockMaybe.Count) { $verdict = 'UNCERTAIN'; $why = 'depends on rules Windows evaluates by network keyword or program path: ' + (@($allowMaybe + $blockMaybe) -join '; ') }
        elseif ($fwProfile -and $fwProfile.DefaultInboundAction -eq 'Allow') { $verdict = 'ADMITTED'; $why = "the $profileName profile allows inbound connections by default" }
        else { $why = 'no allow rule matches; the profile blocks inbound connections by default' }
        $results += [pscustomobject][ordered]@{ Source = $src; Verdict = $verdict; Reason = $why; AllowedBy = $allow; BlockedBy = $block }
    }
    $policyNote = ''
    if ($fwProfile -and [string]$fwProfile.AllowLocalFirewallRules -eq 'False') { $policyNote = 'Group Policy ignores local firewall rules on this profile: a local rule change has no effect.' }
    return [pscustomobject][ordered]@{ HeadIp = $HeadIp; ActiveProfile = $profileName; ProfileEnabled = $(if ($fwProfile) { $fwProfile.Enabled } else { $null })
                                       DefaultInbound = $(if ($fwProfile) { $fwProfile.DefaultInboundAction } else { '' }); PolicyNote = $policyNote
                                       Sources = $results; RelevantRules = $relevant; Analysis = 'rule analysis (inferred); a real connection from each address is the field proof' }
}

# ---------------------------------------------------------------------------
# Plan (pure - unit tested)
# ---------------------------------------------------------------------------

function New-LabFirewallPlan {
    param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)][string]$HeadIp, [Parameter(Mandatory)][string[]]$Clients, [Parameter(Mandatory)]$Config,
          [string]$InterfaceAlias = '', [AllowEmptyCollection()][array]$ServerPaths = @())
    $port = [int]$Config.ollama.port
    $managedName = [string]$Config.ollama.managed_rule_name
    $denied = @($Config.ollama.denied_probe_sources)
    $dev = [string]$Config.dev_workstation
    if ($dev -and $Clients -notcontains $dev) { $denied += $dev }    # gateway-only: the dev exception must now be refused
    $actions = @(); $review = @(); $blockers = @()
    $display = ('Agent Arena Lab - Ollama {0}:{1} from {2}' -f $HeadIp, $port, ($Clients -join ', '))
    $want = [ordered]@{ DisplayName = $display; LocalAddress = $HeadIp; RemoteAddress = @($Clients); LocalPort = [string]$port; Protocol = 'TCP'; Profiles = @('Domain', 'Private', 'Public') }

    $existing = @($Snapshot.Rules | Where-Object { $_.Name -eq $managedName }) | Select-Object -First 1
    if (-not $existing) { $actions += [pscustomobject]@{ Action = 'Create'; Name = $managedName; Detail = ('create "{0}"' -f $display); Approval = 'change'; Want = [pscustomobject]$want } }
    else {
        $sameRemote = ((@($existing.RemoteAddresses) | Sort-Object) -join ',') -eq ((@($Clients) | Sort-Object) -join ',')
        $sameLocal = ((@($existing.LocalAddresses)) -join ',') -eq $HeadIp
        $samePort = ((@($existing.LocalPorts)) -join ',') -eq [string]$port
        $sameProfiles = (@($existing.Profiles).Count -eq 3)
        if (-not ($sameRemote -and $sameLocal -and $samePort -and $sameProfiles -and $existing.Enabled -and $existing.Action -eq 'Allow' -and $existing.Protocol -eq 'TCP')) {
            $actions += [pscustomobject]@{ Action = 'Update'; Name = $managedName; Detail = ('update "{0}" (now {1} from {2}, enabled={3})' -f $display, (@($existing.LocalPorts) -join ','), (@($existing.RemoteAddresses) -join ','), $existing.Enabled); Approval = 'change'; Want = [pscustomobject]$want; Before = $existing }
        }
    }
    $profileName = Get-LabActiveProfile -Snapshot $Snapshot -InterfaceAlias $InterfaceAlias
    foreach ($rule in @($Snapshot.Rules)) {
        if ($rule.Name -eq $managedName -or -not $rule.Enabled -or $rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow') { continue }
        $admitsOther = @(); $uncertain = @()
        foreach ($src in $denied) {
            $v = Test-LabRuleCovers -Rule $rule -Source $src -HeadIp $HeadIp -ActiveProfile $profileName -ServerPaths $ServerPaths -Port $port
            if ($v -eq 'yes') { $admitsOther += $src } elseif ($v -eq 'uncertain') { $uncertain += $src }
        }
        $class = Get-LabRuleClass -Rule $rule -Config $Config
        $coversAnything = $false
        foreach ($src in @($Clients + $denied)) { if ((Test-LabRuleCovers -Rule $rule -Source $src -HeadIp $HeadIp -ActiveProfile $profileName -ServerPaths $ServerPaths -Port $port) -ne 'no') { $coversAnything = $true; break } }
        if ($class -eq 'LEGACY_PROVISIONER') {
            $actions += [pscustomobject]@{ Action = 'Disable'; Name = $rule.Name; Detail = ('disable the provisioner 1.0.3 rule "{0}" (admits {1}); the managed rule replaces it' -f $rule.DisplayName, (@($rule.RemoteAddresses) -join ',')); Approval = 'change'; Before = $rule }
            continue
        }
        if (-not $coversAnything) { continue }
        if ($class -in 'OLLAMA_PROGRAM', 'PORT_11434' -and $admitsOther.Count) {
            $actions += [pscustomobject]@{ Action = 'Disable'; Name = $rule.Name; Detail = ('disable "{0}" ({1}): it admits {2} to Ollama' -f $rule.DisplayName, $class, ($admitsOther -join ', ')); Approval = 'per-rule'; Before = $rule }
        } elseif ($class -eq 'GROUP_POLICY' -and ($admitsOther.Count -or $uncertain.Count)) {
            $blockers += ('Group Policy rule "{0}" admits {1} to port {2}; it cannot be changed here. Ask the policy owner.' -f $rule.DisplayName, (@($admitsOther + $uncertain) -join ', '), $port)
        } elseif ($admitsOther.Count -or $uncertain.Count) {
            $review += ('"{0}" ({1}, program {2}, ports {3}, remote {4}) may admit {5}. Not changed: review it.' -f $rule.DisplayName, $class, $rule.Program, (@($rule.LocalPorts) -join ','), (@($rule.RemoteAddresses) -join ','), (@($admitsOther + $uncertain) -join ', '))
        }
    }
    $fwProfile = @($Snapshot.Profiles | Where-Object { $_.Name -eq $profileName }) | Select-Object -First 1
    if ($fwProfile -and -not $fwProfile.Enabled) { $blockers += ("The $profileName firewall profile is OFF: every address can reach Ollama. Turning it on affects every application on this machine and needs a separate decision.") }
    elseif ($fwProfile -and $fwProfile.DefaultInboundAction -eq 'Allow') { $blockers += ("The $profileName profile allows inbound connections by default: the restriction cannot hold. Needs a separate decision.") }
    if ($fwProfile -and [string]$fwProfile.AllowLocalFirewallRules -eq 'False') { $blockers += 'Group Policy ignores local firewall rules here: the managed rule would have no effect.' }
    return [pscustomobject][ordered]@{ HeadIp = $HeadIp; Clients = @($Clients); Denied = $denied; ActiveProfile = $profileName; Actions = $actions; Review = $review; Blockers = $blockers
                                       Unchanged = ($actions.Count -eq 0) }
}

# ---------------------------------------------------------------------------
# Backup, apply, undo
# ---------------------------------------------------------------------------

function Backup-LabFirewall {
    param([Parameter(Mandatory)][string]$Folder, $Snapshot = $null)
    if (-not (Test-Path -LiteralPath $Folder)) { New-Item -ItemType Directory -Path $Folder -Force | Out-Null }
    if (-not $Snapshot) { $Snapshot = Get-LabFirewallSnapshot }
    $json = Join-Path $Folder 'firewall-before.json'
    Save-LabJson -InputObject $Snapshot -Path $json
    $wfw = Join-Path $Folder 'firewall-before.wfw'
    $b = Get-LabFirewallBackend
    & $b.Export $wfw
    if (-not (Test-Path -LiteralPath $json)) { throw 'firewall backup was not written' }
    return [pscustomobject]@{ Json = $json; Policy = $wfw; RuleCount = @($Snapshot.Rules).Count }
}

function Invoke-LabFirewallPlan {
    <#  Applies the approved actions, in order, recording each one's undo. Stops at the first error.  #>
    param([Parameter(Mandatory)]$Plan, [Parameter(Mandatory)][string]$BackupFile, [scriptblock]$ApprovePerRule = $null)
    $b = Get-LabFirewallBackend
    $applied = @(); $declined = @()
    foreach ($a in @($Plan.Actions)) {
        if ($a.Approval -eq 'per-rule') {
            $ok = $false
            if ($ApprovePerRule) { $ok = [bool](& $ApprovePerRule $a) }
            if (-not $ok) { $declined += $a.Detail; Write-LabLog ('  left as it is: ' + $a.Detail) -Level WARN; continue }
        }
        switch ($a.Action) {
            'Create' {
                $w = $a.Want
                & $b.NewRule @{ Name = $a.Name; DisplayName = $w.DisplayName; Group = 'Agent Arena Lab'; Direction = 'Inbound'; Action = 'Allow'; Protocol = 'TCP'
                                LocalPort = $w.LocalPort; LocalAddress = $w.LocalAddress; RemoteAddress = $w.RemoteAddress; Profile = 'Any'; Enabled = 'True'
                                Description = 'Agent Arena Lab Provisioner: only the approved clients (Inference Gateway on the hub, and the documented development exception during cutover) may reach this head''s Ollama API.' }
                $applied += Register-LabChange -Kind 'firewall' -Description $a.Detail -Backup $BackupFile -Undo ([pscustomobject]@{ type = 'firewall'; action = 'remove'; name = $a.Name })
            }
            'Update' {
                $w = $a.Want; $before = $a.Before
                & $b.SetRule $a.Name @{ DisplayName = $w.DisplayName; LocalPort = $w.LocalPort; LocalAddress = $w.LocalAddress; RemoteAddress = $w.RemoteAddress; Protocol = 'TCP'; Profile = 'Any'; Enabled = 'True'; Action = 'Allow' }
                $applied += Register-LabChange -Kind 'firewall' -Description $a.Detail -Backup $BackupFile -Undo ([pscustomobject]@{ type = 'firewall'; action = 'restore'; name = $a.Name
                    before = [pscustomobject]@{ DisplayName = $before.DisplayName; LocalPort = @($before.LocalPorts); LocalAddress = @($before.LocalAddresses); RemoteAddress = @($before.RemoteAddresses); Protocol = $before.Protocol; Enabled = $before.Enabled; Action = $before.Action; Profiles = @($before.Profiles) } })
            }
            'Disable' {
                & $b.Disable $a.Name
                $applied += Register-LabChange -Kind 'firewall' -Description $a.Detail -Backup $BackupFile -Undo ([pscustomobject]@{ type = 'firewall'; action = 'enable'; name = $a.Name })
            }
        }
    }
    return [pscustomobject]@{ Applied = $applied; Declined = $declined }
}

function Undo-LabFirewallChange {
    param([Parameter(Mandatory)]$Undo)
    $b = Get-LabFirewallBackend
    switch ($Undo.action) {
        'remove' { & $b.Remove $Undo.name; return ('removed rule ' + $Undo.name) }
        'enable' { & $b.Enable $Undo.name; return ('re-enabled rule ' + $Undo.name) }
        'disable' { & $b.Disable $Undo.name; return ('disabled rule ' + $Undo.name) }
        'restore-remote' {
            # Predefined rules (Remote Desktop): only the scope and the enabled state were changed.
            & $b.SetRule $Undo.name @{ RemoteAddress = @($Undo.remote); Enabled = $(if ($Undo.enabled) { 'True' } else { 'False' }) }
            return ('restored the scope of rule ' + $Undo.name)
        }
        'restore' {
            $p = $Undo.before
            $profile = 'Any'; if (@($p.Profiles).Count -lt 3) { $profile = (@($p.Profiles) -join ',') }
            & $b.SetRule $Undo.name @{ DisplayName = $p.DisplayName; LocalPort = @($p.LocalPort); LocalAddress = @($p.LocalAddress); RemoteAddress = @($p.RemoteAddress); Protocol = $p.Protocol; Profile = $profile; Enabled = $(if ($p.Enabled) { 'True' } else { 'False' }); Action = $p.Action }
            return ('restored rule ' + $Undo.name)
        }
    }
    throw ('unknown firewall undo action ' + $Undo.action)
}

# ---------------------------------------------------------------------------
# Outbound (read-only): can this host - the Gateway's host - open TCP 11434 to the heads?
# Windows allows outbound by default; a profile set to block outbound, or an outbound block rule
# (local or Group Policy), would stop the Gateway even with every head's firewall correct.
# ---------------------------------------------------------------------------

function Get-LabOutboundSnapshotLive {
    $store = 'ActiveStore'
    $rules = @(Get-NetFirewallRule -PolicyStore $store -Direction Outbound -Enabled True -ErrorAction Stop)
    $index = @{}
    foreach ($f in @(Get-NetFirewallPortFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['p:' + $f.InstanceID] = $f }
    foreach ($f in @(Get-NetFirewallAddressFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['a:' + $f.InstanceID] = $f }
    foreach ($f in @(Get-NetFirewallApplicationFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['x:' + $f.InstanceID] = $f }
    foreach ($f in @(Get-NetFirewallServiceFilter -PolicyStore $store -All -ErrorAction SilentlyContinue)) { $index['s:' + $f.InstanceID] = $f }
    $normal = @(foreach ($r in $rules) {
        $id = [string]$r.InstanceID; $p = $index['p:' + $id]; $a = $index['a:' + $id]; $x = $index['x:' + $id]; $s = $index['s:' + $id]
        [pscustomobject][ordered]@{ Name = [string]$r.Name; DisplayName = [string]$r.DisplayName; Enabled = $true; Action = [string]$r.Action; Profiles = @(ConvertTo-LabProfileList $r.Profile)
            Protocol = $(if ($p) { [string]$p.Protocol } else { 'Any' }); RemotePorts = @($(if ($p) { @($p.RemotePort) | ForEach-Object { [string]$_ } } else { 'Any' }))
            RemoteAddresses = @($(if ($a) { @($a.RemoteAddress) | ForEach-Object { [string]$_ } } else { 'Any' })); Program = $(if ($x -and $x.Program) { [string]$x.Program } else { 'Any' })
            Service = $(if ($s -and $s.Service) { [string]$s.Service } else { 'Any' }); Package = (Get-LabRulePackage -Rule $r -ApplicationFilter $x); PolicySource = [string]$r.PolicyStoreSourceType }
    })
    $profiles = @(Get-NetFirewallProfile -PolicyStore $store -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ Name = [string]$_.Name; Enabled = ([string]$_.Enabled -eq 'True'); DefaultOutboundAction = [string]$_.DefaultOutboundAction } })
    $connections = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ InterfaceAlias = [string]$_.InterfaceAlias; NetworkCategory = [string]$_.NetworkCategory } })
    return [pscustomobject]@{ Rules = $normal; Profiles = $profiles; Connections = $connections }
}

function Test-LabOutboundRuleApplies {
    <#  Pure - unit tested. 'yes' / 'no' / 'uncertain': does this outbound rule's application condition
        cover the Gateway's own process (its runtime python.exe, running as its Windows service)?
        A rule bound to an app package, another service or another program does not; with the runtime
        unknown, a program-specific rule stays uncertain. Another python.exe stays uncertain too.  #>
    param([Parameter(Mandatory)]$Rule, [AllowEmptyCollection()][string[]]$Programs = @(), [string]$ServiceName = '')
    if (Test-LabRulePackaged $Rule) { return 'no' }
    $svc = ''; if ($Rule.PSObject.Properties.Name -contains 'Service') { $svc = [string]$Rule.Service }
    if ($svc -and $svc -ne 'Any' -and $svc -ne '*' -and -not ($ServiceName -and $svc -ieq $ServiceName)) { return 'no' }
    $prog = [string]$Rule.Program
    if (-not $prog -or $prog -eq 'Any') { return 'yes' }
    $known = @($Programs | Where-Object { $_ })
    if (-not $known.Count) { return 'uncertain' }
    $e = [Environment]::ExpandEnvironmentVariables($prog)
    foreach ($p in $known) { if ([Environment]::ExpandEnvironmentVariables([string]$p) -ieq $e) { return 'yes' } }
    if ([IO.Path]::GetFileName($e) -match '^(?i)pythonw?\.exe$') { return 'uncertain' }
    return 'no'
}

function Get-LabOutboundVerdict {
    <#  Per head: ALLOWED, BLOCKED or UNCERTAIN for TCP <port> from this host (rule analysis). Pure.
        Windows evaluates outbound as: a matching block rule wins; otherwise a matching allow rule
        allows; otherwise the profile default. So with outbound allowed by default only block rules
        matter, and with it blocked by default only allow rules can open the path. -Programs names the
        Gateway's runtime (config runtime.python.path) and -ServiceName its service: a rule for another
        program, service or app package does not apply to the Gateway. In 1.3.0 every program-specific
        ALLOW rule (spoolsv, Teams, ...) was counted as a possible block, so DESKTOP-REDACT10 read UNCERTAIN for
        every head while the real connections succeeded.  #>
    param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)][string[]]$Heads, [int]$Port = 11434, [string]$InterfaceAlias = '',
          [AllowEmptyCollection()][string[]]$Programs = @(), [string]$ServiceName = '')
    $profileName = Get-LabActiveProfile -Snapshot $Snapshot -InterfaceAlias $InterfaceAlias
    $fw = @($Snapshot.Profiles | Where-Object { $_.Name -eq $profileName }) | Select-Object -First 1
    $defaultBlock = ($fw -and $fw.Enabled -and $fw.DefaultOutboundAction -eq 'Block')
    $out = @()
    foreach ($h in $Heads) {
        $block = @(); $blockMaybe = @(); $allow = @(); $allowMaybe = @()
        foreach ($r in @($Snapshot.Rules)) {
            if ($r.PSObject.Properties.Name -contains 'Enabled' -and -not $r.Enabled) { continue }
            if (@($r.Profiles) -notcontains $profileName -or @('TCP', 'Any', '6') -notcontains [string]$r.Protocol -or -not (Test-LabPortSpec -Ports $r.RemotePorts -Port $Port)) { continue }
            $addr = Test-LabAddressList -Specs $r.RemoteAddresses -Source $h
            if ($addr -eq 'no') { continue }
            $app = Test-LabOutboundRuleApplies -Rule $r -Programs $Programs -ServiceName $ServiceName
            if ($app -eq 'no') { continue }
            $sure = ($addr -eq 'yes' -and $app -eq 'yes')
            if ($r.Action -eq 'Block') { if ($sure) { $block += $r.DisplayName } else { $blockMaybe += $r.DisplayName } }
            elseif ($sure) { $allow += $r.DisplayName } else { $allowMaybe += $r.DisplayName }
        }
        $verdict = 'ALLOWED'; $why = "no outbound block rule applies to the Gateway's process; the $profileName profile allows outbound by default"
        if ($block.Count) { $verdict = 'BLOCKED'; $why = 'blocked by ' + ($block -join '; ') }
        elseif ($defaultBlock -and $allow.Count) { $why = "the $profileName profile blocks outbound by default; allowed by " + ($allow -join '; ') }
        elseif ($defaultBlock) { $verdict = $(if ($allowMaybe.Count) { 'UNCERTAIN' } else { 'BLOCKED' }); $why = "the $profileName profile blocks outbound by default" + $(if ($allowMaybe.Count) { '; only rules whose program or address Windows decides may allow it: ' + ($allowMaybe -join '; ') } else { ' and no rule allows TCP ' + $Port + ' for the Gateway' }) }
        elseif ($blockMaybe.Count) { $verdict = 'UNCERTAIN'; $why = 'block rules that may apply to the Gateway''s process (by program path or network keyword): ' + ($blockMaybe -join '; ') }
        $out += [pscustomobject][ordered]@{ Head = $h; Verdict = $verdict; Reason = $why }
    }
    return [pscustomobject]@{ ActiveProfile = $profileName; DefaultOutbound = $(if ($fw) { $fw.DefaultOutboundAction } else { '' }); Programs = @($Programs); ServiceName = $ServiceName; Heads = $out; Analysis = 'rule analysis (inferred); the real connection test is the proof' }
}

Export-ModuleMember -Function @(
    'Get-LabOutboundSnapshotLive', 'Get-LabOutboundVerdict', 'Test-LabOutboundRuleApplies', 'Get-LabRulePackage', 'Test-LabRulePackaged',
    'Get-LabDefaultFirewallBackend', 'ConvertTo-LabSetRuleParameters', 'Set-LabFirewallBackend', 'Get-LabFirewallBackend', 'Get-LabFirewallSnapshot', 'Get-LabFirewallSnapshotLive',
    'ConvertTo-LabProfileList', 'ConvertTo-LabIpNumber', 'Test-LabAddressSpec', 'Test-LabAddressList', 'Test-LabPortSpec', 'Test-LabOllamaProgram',
    'Test-LabProgramMatch', 'Get-LabRuleClass', 'Test-LabRuleCovers', 'Get-LabActiveProfile', 'Get-LabOllamaExposure',
    'New-LabFirewallPlan', 'Backup-LabFirewall', 'Invoke-LabFirewallPlan', 'Undo-LabFirewallChange'
)
