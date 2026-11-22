<#
    Lab.Plan - Stage B. A machine-specific plan built from the inspection, before any change.

    Each step says what kind of change it is (read, change, privileged, disruptive) and whether
    and how it can be undone. Nothing runs until the operator approves the plan; disruptive
    steps (address change, Ollama restart) and per-rule firewall changes ask again.

    Existing machines are never silently rewritten: a new-head role refuses a machine that is
    already a head, the hub, an application laptop or the GPU workstation, and sends the
    operator to the maintenance path instead.
#>

function Get-LabAddressVerdict {
    <#  Is this address acceptable for this role on this machine?  Pure.  #>
    param([Parameter(Mandatory)][string]$Address, [Parameter(Mandatory)][string]$Role, [Parameter(Mandatory)]$Config, [AllowEmptyCollection()][string[]]$LocalLabAddresses = @())
    $reasons = @(); $warnings = @(); $n = Get-LabHostNumber $Address
    if (-not (Test-LabAddressInSubnet -Address $Address -Prefix $Config.prefix)) { return [pscustomobject]@{ Allowed = $false; Reasons = @("$Address is not on $($Config.subnet)"); Warnings = @(); HeldHere = $false } }
    $held = ($LocalLabAddresses -contains $Address)
    $protected = @($Config.protected_addresses | Where-Object { $_.address -eq $Address }) | Select-Object -First 1
    switch -Regex ($Role) {
        '^(cpu-head|gpu-head)$' {
            $lo = [int]$Config.ranges.head[0]; $hi = [int]$Config.ranges.head[1]
            if ($n -lt $lo -or $n -gt $hi) { $reasons += ("a head address must be .{0}-.{1}" -f $lo, $hi) }
            if ($protected) { $reasons += ("$Address is protected: " + $protected.reason) }
        }
        '^hub$' { if ($Address -ne $Config.hub) { $reasons += ('the hub must be ' + $Config.hub) } }
        '^(capstone|coder)$' {
            $want = [string]$Config.roles.$Role.address
            $lo = [int]$Config.ranges.app[0]; $hi = [int]$Config.ranges.app[1]
            if ($n -lt $lo -or $n -gt $hi) { $reasons += ("an application laptop address must be .{0}-.{1}" -f $lo, $hi) }
            elseif ($Address -ne $want) { $warnings += ("the planned $Role address is $want") }
            if ($protected -and $Address -ne $want) { $reasons += ("$Address is protected: " + $protected.reason) }
        }
    }
    return [pscustomobject]@{ Allowed = ($reasons.Count -eq 0); Reasons = $reasons; Warnings = $warnings; HeldHere = $held }
}

function Test-LabAddressConflict {
    <#  Read-only probes (ICMP, TCP 11434, ARP) from the 1.0.3 engine. Absence of evidence is not proof.  #>
    param([Parameter(Mandatory)][string]$Address, [bool]$HeldHere = $false)
    if ($HeldHere) { return [pscustomobject]@{ Verdict = 'Clear'; Reason = 'this machine already holds the address' } }
    try { $r = Test-CapstoneAddressInUse -IPAddress $Address -HeldLocally $false; return [pscustomobject]@{ Verdict = [string]$r.Verdict; Reason = [string]$r.Reason } }
    catch { return [pscustomobject]@{ Verdict = 'Unknown'; Reason = $_.Exception.Message } }
}

function Get-LabHardwareVerdict {
    <#  Pure.  #>
    param([Parameter(Mandatory)][string]$Role, $Hardware, $Os, [int64]$ModelBytes = 0, [int64]$FreeBytes = -1)
    $notes = @(); $blocking = @()
    if ($Os) {
        if ([string]$Os.Arch -notmatch '64') { $blocking += 'a 64-bit Windows is required' }
        if ([string]$Os.Caption -notmatch 'Windows 1[01]') { $blocking += ('Windows 10 or 11 is required (found {0})' -f $Os.Caption) }
    }
    if ($Hardware) {
        $ram = [double]$Hardware.TotalMemoryGB; $vram = [int]$Hardware.NvidiaVramMB
        if ($Role -eq 'gpu-head') {
            if ($vram -lt 8000) { $blocking += ('a GPU head needs an NVIDIA GPU with at least 8 GB VRAM (found {0} MB); use the CPU head role instead' -f $vram) }
            else { $notes += ('NVIDIA GPU with {0} MB VRAM' -f $vram) }
        }
        if ($Role -in 'cpu-head', 'gpu-head') {
            if ($ram -lt 7) { $blocking += ('{0} GB RAM is below the smallest profile (CPU8 needs 7 GB)' -f $ram) } else { $notes += ('{0} GB RAM' -f $ram) }
        }
    }
    if ($ModelBytes -gt 0 -and $FreeBytes -ge 0) {
        $need = $ModelBytes + 10737418240
        if ($FreeBytes -lt $need) { $notes += ('{0} free on the model volume; the selected models need about {1} with the 10 GB reserve: some will not be downloaded' -f (Format-CapstoneBytes -Bytes $FreeBytes), (Format-CapstoneBytes -Bytes $need)) }
    }
    return [pscustomobject]@{ Suitable = ($blocking.Count -eq 0); Blocking = $blocking; Notes = $notes }
}

function Get-LabServingArgs {
    <#  Pure. Which folder the running boot server reads now and the models it serves (from the inspection), for
        Resolve-LabModelDirectory.  #>
    param($Ollama = $null)
    if (-not $Ollama) { return [pscustomobject]@{ ServingPath = ''; Served = [string[]]@() } }
    $machine = ''; if ($Ollama.Env) { $machine = [string]$Ollama.Env.MachineModels }
    $path = Get-LabServingModelDirectory -Task $Ollama.Task -MachineValue $machine -Candidates @($Ollama.ModelDirectories)
    $served = [string[]]@(); if ($Ollama.Models -and $Ollama.Models.Models) { $served = [string[]]@(@($Ollama.Models.Models) | ForEach-Object { [string]$_.name }) }
    return [pscustomobject]@{ ServingPath = $path; Served = $served }
}

function New-LabPlanStep {
    param([Parameter(Mandatory)][string]$Id, [Parameter(Mandatory)][string]$Stage, [Parameter(Mandatory)][string]$Title, [ValidateSet('read', 'change', 'privileged', 'disruptive')][string]$Kind = 'privileged',
          [string]$Detail = '', [string]$Rollback = '', [string]$Preserves = '', [bool]$Approval = $true, [string]$State = 'planned')
    return [pscustomobject][ordered]@{ id = $Id; stage = $Stage; title = $Title; kind = $Kind; detail = $Detail; rollback = $Rollback; preserves = $Preserves; approval = $Approval; state = $State }
}

function New-LabGatewayPlanSteps {
    <#  Pure. The guided Gateway 0.3.0 installation, in the manifest's "order", each step naming whose
        command it runs. F: steps prepare the machine; every other step is the Gateway's own command.  #>
    param([Parameter(Mandatory)]$Config)
    $s = @()
    $s += New-LabPlanStep 'g-intake' 'C' 'Validate the Gateway release: archive SHA-256, SHA256SUMS, deployment manifest against the lab' -Kind 'read' -Approval $false -Detail 'F: read-only'
    $s += New-LabPlanStep 'g-runtime' 'C' 'Machine-wide Python 3.12.10 and PowerShell 7.4.22 (official installers on the provisioner stick, publisher hash and signature checked)' -Detail 'machine prerequisite (manifest runtime)' -Rollback 'manual uninstall, only after the Gateway is uninstalled'
    $s += New-LabPlanStep 'g-account' 'C' ("Local standard account $($Config.gateway.service_account): Users only, Log on as a service, interactive and Remote Desktop logon denied") -Detail 'F: machine prerequisite; password typed by the operator, never stored' -Rollback 'rights removed and the account deleted (only if this run created it)'
    $s += New-LabPlanStep 'g-arena-accounts' 'C' 'Every Arena account is a standard account, not in Administrators or Backup Operators (transitively), not the Gateway account' -Kind 'read' -Approval $false -Detail 'F: check; the Gateway''s plan and ACL script decide'
    $s += New-LabPlanStep 'g-settings' 'C' 'deployment-settings.json from the Gateway template (arena_accounts filled in; every account as <computer>\name)' -Kind 'change' -Rollback 'earlier file restored'
    $s += New-LabPlanStep 'g-plan' 'C' 'Gateway: plan (read-only checks; on a first install only tls and ledger may be missing)' -Kind 'read' -Approval $false
    $s += New-LabPlanStep 'g-certificate' 'C' 'Gateway: certificate (lab CA and server certificate); then move ca.key to offline media; the public CA is copied to the provisioner stick' -Detail 'Gateway command'
    $s += New-LabPlanStep 'g-install' 'C' 'Gateway: install --password-stdin --new-ledger (or --restore-backup for a migration)' -Kind 'disruptive' -Detail 'Gateway command; creates the InferenceGateway service and its folders and ACLs' -Rollback 'Gateway uninstall (data is never deleted)'
    $s += New-LabPlanStep 'g-inventory' 'C' 'Export the verified head inventory (agent-arena.verified-head-inventory/1); with no reachable head the run stops safely here and resumes on the next run' -Kind 'read' -Approval $false -Detail 'F: real connections from the hub'
    $s += New-LabPlanStep 'g-heads' 'C' 'Gateway: heads --inventory ... --policy <approved> --output policy-review-<time>.json; operator reviews it' -Kind 'change' -Detail 'Gateway command; nothing enrolled yet; a new review file each run (the Gateway never overwrites one)'
    $s += New-LabPlanStep 'g-credentials' 'C' 'Gateway: credentials issue for capstone and capstone-research (before bootstrap; an application that already holds one is kept unless ROTATE is chosen); files carried privately, never read by F:' -Detail 'Gateway command; coder when open item O5 is decided'
    $s += New-LabPlanStep 'g-bootstrap' 'C' 'Gateway: bootstrap --policy <review> (client policies and the reviewed heads; refused for a declared client without a credential)' -Detail 'Gateway command'
    $s += New-LabPlanStep 'g-qualify' 'C' 'Gateway: qualify each head and model (real inference, recorded evidence)' -Kind 'disruptive' -Detail 'Gateway command; uses head capacity'
    $s += New-LabPlanStep 'g-accept' 'C' 'Tell the Arena operator the ledger_id; Gateway: publisher accept-ledger' -Detail 'Gateway command'
    $s += New-LabPlanStep 'g-acl' 'C' 'Gateway: Set-ObservationPublicationAcl.ps1 with every Arena account (PlanOnly first; must return ok)' -Detail 'Gateway script under PowerShell 7'
    $s += New-LabPlanStep 'g-verify' 'C' 'Gateway: verify (must return ok)' -Kind 'read' -Approval $false
    $s += New-LabPlanStep 'g-listener' 'C' ('Inbound rule InferenceGateway-HTTPS-{0}: TCP {0} on {1} from {2} and {3} only, Private profile' -f $Config.gateway.port, $Config.hub, $Config.roles.capstone.address, $Config.roles.coder.address) -Detail 'F: exactly manifest listener.firewall_rule, after verify' -Rollback 'rule removed'
    $s += New-LabPlanStep 'g-cutover' 'C' ('Optional, separately approved: temporary rule for {0} during an approved Capstone migration (open item O2)' -f $Config.dev_workstation) -Kind 'change' -Rollback 'rule removed'
    $s += New-LabPlanStep 'g-health' 'E' 'Gateway /health and /ready over TLS with the Gateway CA; Arena observation checked separately' -Kind 'read' -Approval $false
    return $s
}

function New-LabPlan {
    <#  Builds the plan for a role from the inspection. $Choices carries operator answers:
        Address, ClientMode, Profiles, ModelsAdd, ModelsExclude, Actions (maintenance).  #>
    param([Parameter(Mandatory)][string]$Role, [Parameter(Mandatory)]$Inspection, [Parameter(Mandatory)]$Config, [hashtable]$Choices = @{}, $Catalog = $null)
    $i = $Inspection
    $labAddrs = @(); if ($i.network) { $labAddrs = @($i.network.Lab | ForEach-Object { $_.Address }) }
    $plan = [ordered]@{ role = $Role; computer = $i.computer; detected = $i.detected; address = ''; address_verdict = $null; conflict = $null; hardware = $null
                        profile = $null; models = @(); model_bytes = 0; model_directory = ''; client_mode = ''; clients = @(); denied = @(); steps = @(); external = @(); approvals = @()
                        blocked = @(); warnings = @(); backups = @(); preserves = @() }
    $hw = $i.hardware; $os = $i.os
    $isExistingHead = ($i.detected.Role -in 'head', 'gpu-workstation') -and ($labAddrs.Count -gt 0)
    $mode = [string]$Choices.ClientMode; if (-not $mode) { $mode = [string]$Config.ollama.default_client_mode }
    $plan.client_mode = $mode; $plan.clients = @($Config.ollama.client_modes.$mode); $plan.denied = @($Config.ollama.denied_probe_sources)

    switch ($Role) {
        { $_ -in 'cpu-head', 'gpu-head' } {
            if ($i.detected.Role -ne 'new') {
                $plan.blocked += ('This machine is already {0} ({1}). A new-head install would rewrite it; use "Existing machine" instead.' -f $i.detected.Role, $i.detected.Reason)
            }
            $addr = [string]$Choices.Address
            if (-not $addr) { $plan.blocked += 'no head address was chosen' }
            else {
                $plan.address = $addr
                $plan.address_verdict = Get-LabAddressVerdict -Address $addr -Role $Role -Config $Config -LocalLabAddresses $labAddrs
                if (-not $plan.address_verdict.Allowed) { $plan.blocked += $plan.address_verdict.Reasons }
                $plan.conflict = Test-LabAddressConflict -Address $addr -HeldHere $plan.address_verdict.HeldHere
                if ($plan.conflict.Verdict -eq 'Conflict') { $plan.blocked += ('{0} is in use: {1}' -f $addr, $plan.conflict.Reason) }
            }
            $proposal = Get-LabProfileProposal -RamGB $(if ($hw) { [double]$hw.TotalMemoryGB } else { 0 }) -VramMB $(if ($hw) { [int]$hw.NvidiaVramMB } else { 0 })
            $profiles = @($Choices.Profiles); if (-not $profiles.Count -or -not $profiles[0]) { $profiles = @($proposal.Profile) }
            $plan.profile = [pscustomobject]@{ Proposed = $proposal.Profile; Reason = $proposal.Reason; Chosen = $profiles }
            if ($Catalog) {
                $sel = Select-LabModels -Catalog $Catalog -Profiles $profiles -Add @($Choices.ModelsAdd | Where-Object { $_ }) -Exclude @($Choices.ModelsExclude | Where-Object { $_ })
                $plan.models = @($sel.Models | ForEach-Object { $_.tag }); $plan.model_bytes = $sel.TotalBytes
                foreach ($r in $sel.Refused) { $plan.warnings += $r }
            }
            $free = -1; if ($hw) { $c = @($hw.Volumes | Where-Object { $_.Drive -eq 'C:' }) | Select-Object -First 1; if ($c) { $free = [int64]$c.FreeBytes } }
            $plan.hardware = Get-LabHardwareVerdict -Role $Role -Hardware $hw -Os $os -ModelBytes $plan.model_bytes -FreeBytes $free
            if (-not $plan.hardware.Suitable) { $plan.blocked += $plan.hardware.Blocking }
            $held = $false; if ($plan.address_verdict) { $held = $plan.address_verdict.HeldHere }
            $ollama = $i.ollama
            $dirs = @(); if ($ollama) { $dirs = @($ollama.ModelDirectories) }
            $machineModels = ''; if ($ollama -and $ollama.Env) { $machineModels = [string]$ollama.Env.MachineModels }
            $md = Resolve-LabModelDirectory -MachineValue $machineModels -Candidates $dirs -NewDefault $Config.ollama.new_model_directory -ServingPath (Get-LabServingArgs -Ollama $ollama).ServingPath -Served (Get-LabServingArgs -Ollama $ollama).Served
            $plan.model_directory = $md.Path
            if ($md.Decision -eq 'Ambiguous') { $plan.blocked += ('models exist in several folders; choose one first: ' + (($md.Choices | ForEach-Object { '{0} ({1})' -f $_.Path, $_.Manifests }) -join ', ')) }
            $plan.steps += New-LabPlanStep 'network' 'C' $(if ($held) { "Keep $($plan.address)/24 (already set)" } else { "Set the lab Ethernet adapter to $($plan.address)/24, no gateway" }) -Kind $(if ($held) { 'read' } else { 'disruptive' }) -Detail 'provisioner 1.0.3 adapter choice, conflict probes, backup and atomic netsh apply, polled verification' -Rollback 'network backup JSON; undo restores DHCP or the earlier static address' -Approval (-not $held)
            $plan.steps += New-LabPlanStep 'time' 'C' ("Time zone $($Config.time_zone); Windows Time from the hub $($Config.hub)") -Detail 'Arena refuses clocks more than 120 s off' -Rollback 'earlier time zone and Windows Time source restored'
            $plan.steps += New-LabPlanStep 'power' 'C' 'No sleep or hibernate on AC power; Ethernet power saving and EEE off' -Rollback 'earlier power values restored'
            $plan.steps += New-LabPlanStep 'ollama-install' 'C' $(if ($ollama -and $ollama.Executable) { "Keep Ollama $($ollama.Version) (installed)" } else { 'Install Ollama (official signed installer, or packages\ollama\OllamaSetup.exe on the USB)' }) -Kind $(if ($ollama -and $ollama.Executable) { 'read' } else { 'privileged' }) -Detail 'publisher signature checked before running; completion by measured state' -Rollback 'uninstall Ollama from Apps (models are kept)' -Approval (-not ($ollama -and $ollama.Executable))
            $plan.steps += New-LabPlanStep 'ollama-service' 'C' ("Boot task '$($Config.ollama.task_name)' as SYSTEM with OLLAMA_HOST=$($Config.ollama.bind) and OLLAMA_MODELS=$($md.Path)") -Detail ('{0}: {1}. Models are never moved or deleted.' -f $md.Decision, $md.Reason) -Rollback 'earlier task XML and machine variables restored' -Preserves 'installed models'
            $plan.steps += New-LabPlanStep 'ollama-start' 'C' 'Start the boot task''s server and check the models are visible' -Kind 'disruptive' -Detail 'stops this installation''s other Ollama processes; refused while a model is loaded' -Rollback 'n/a (no configuration change)'
            $plan.steps += New-LabPlanStep 'firewall' 'C' ("Ollama 11434 reachable only from $($plan.clients -join ', ')") -Detail 'audit of every inbound rule, backup (JSON and netsh export), one managed rule, the 1.0.3 rule disabled, other Ollama rules only with per-rule approval' -Rollback 'each rule change undone exactly from the record'
            $plan.steps += New-LabPlanStep 'arena-claim' 'D' "Claim $($plan.address) in Agent Arena (one-time claim code from the admin console, Machines > Addresses)" -Kind 'change' -Detail 'POST /v2/claim from this address; registers the head and its display' -Rollback 'release the address in the admin console'
            $plan.steps += New-LabPlanStep 'viewer' 'D' 'Install or update the Agent Arena head viewer (a new display needs its one-time display code; an enrolled one updates with no code)' -Kind 'change' -Detail 'the AGENT-ARENA-<version> toolset on the stick, run as the signed-in user' -Rollback 'Agent Arena uninstall (keeps or removes the credential, as chosen)'
            $plan.steps += New-LabPlanStep 'models' 'M' ('Models (separate, optional): {0} model(s), about {1}' -f $plan.models.Count, (Format-CapstoneBytes -Bytes $plan.model_bytes)) -Kind 'change' -Detail ('profile {0}; pulled through this head''s Ollama; each verified by a real generate or embed request' -f ($profiles -join '+')) -Rollback 'ollama rm <tag> (never automatic)'
            $plan.external += 'Arena claim code and display enrollment code (admin console on the hub)'
            $plan.external += 'internet access for the Ollama installer and the models, unless supplied on the USB'
            $plan.preserves += 'existing models (never deleted or moved)'
            $plan.backups += 'network, time, power, Ollama task/variables, firewall (JSON + netsh export) in the run folder'
        }
        'existing' {
            $role = $i.detected.Role
            $plan.address = $(if ($labAddrs.Count) { $labAddrs[0] } else { '' })
            if ($role -eq 'gpu-workstation') { $plan.warnings += 'This is the GPU workstation (192.0.2.2), set up by hand. Every change needs its own approval naming it; verify-only is the default.' }
            foreach ($a in @($Choices.Actions)) {
                switch ($a) {
                    'verify' { $plan.steps += New-LabPlanStep 'verify' 'E' 'Verify only (read-only)' -Kind 'read' -Approval $false }
                    'firewall' { $plan.steps += New-LabPlanStep 'firewall' 'C' ("Ollama 11434 reachable only from $($plan.clients -join ', ') ($mode)") -Rollback 'exact undo from the record' }
                    'time-power' { $plan.steps += New-LabPlanStep 'time' 'C' ("Time zone $($Config.time_zone); Windows Time from $($Config.hub)") -Rollback 'earlier values restored'; $plan.steps += New-LabPlanStep 'power' 'C' 'No sleep on AC; Ethernet power saving off' -Rollback 'earlier values restored' }
                    'ollama-service' {
                        $ollama = $i.ollama; $dirs = @(); if ($ollama) { $dirs = @($ollama.ModelDirectories) }
                        $md = Resolve-LabModelDirectory -MachineValue $(if ($ollama -and $ollama.Env) { [string]$ollama.Env.MachineModels } else { '' }) -Candidates $dirs -NewDefault $Config.ollama.new_model_directory -ServingPath (Get-LabServingArgs -Ollama $ollama).ServingPath -Served (Get-LabServingArgs -Ollama $ollama).Served
                        if ($md.Decision -eq 'Ambiguous' -and $Choices.ModelDirectory) { $md = [pscustomobject]@{ Decision = 'Chosen'; Path = [string]$Choices.ModelDirectory; Reason = 'chosen by the operator' } }
                        $plan.model_directory = $md.Path
                        if ($md.Decision -eq 'Ambiguous') { $plan.blocked += ('models exist in several folders; choose one: ' + (($md.Choices | ForEach-Object { '{0} ({1})' -f $_.Path, $_.Manifests }) -join ', ')) }
                        $plan.steps += New-LabPlanStep 'ollama-service' 'C' ("Boot task with OLLAMA_MODELS=$($md.Path)") -Detail ('{0}: {1}' -f $md.Decision, $md.Reason) -Rollback 'earlier task XML and variables restored' -Preserves 'installed models'
                        $plan.steps += New-LabPlanStep 'ollama-start' 'C' 'Restart Ollama through the boot task and compare the models before and after' -Kind 'disruptive' -Detail 'refused while a model is loaded unless approved' -Rollback 'n/a'
                    }
                    'reboot-check' { $plan.steps += New-LabPlanStep 'reboot-check' 'E' 'Post-reboot verification against the saved checkpoint' -Kind 'read' -Approval $false }
                    'models' { $plan.steps += New-LabPlanStep 'models' 'M' 'Install or update models from the approved catalog' -Kind 'change' -Rollback 'ollama rm <tag> (never automatic)' }
                    'viewer' { $plan.steps += New-LabPlanStep 'viewer' 'D' 'Repair or update the Agent Arena head viewer (keeps the credential)' -Kind 'change' -Rollback 'Agent Arena uninstall' }
                    'rollback' { $plan.steps += New-LabPlanStep 'rollback' 'C' ('Undo the recorded changes of run ' + [string]$Choices.RollbackRun) -Kind 'privileged' -Rollback 'n/a' }
                    'business-service' {
                        $svc = Get-LabBusinessServiceSpec -Config $Config -HeadIp $plan.address
                        if (-not $svc) { $plan.blocked += ('no Capstone business service belongs on ' + $plan.address); break }
                        $plan.steps += New-LabPlanStep 'business-service' 'C' ('{0} ({1}) on this head for the Capstone laptop: Visual C++ runtime if missing, its own installer, a token carried to the laptop on this stick, its LOCAL SERVICE startup task, TCP {2} from {3} only' -f $svc.Product, $svc.Label, $svc.Port, $svc.Client) -Kind 'change' `
                            -Detail ('release {0} (archive SHA-256 checked); program {1}, data {2}; each part asked separately' -f $svc.Approved.version, $svc.InstallRoot, $svc.DataRoot) -Rollback ("Unregister-ScheduledTask '{0}'; remove the firewall rule {1}; delete {2}" -f $svc.TaskName, $svc.RuleName, $svc.InstallRoot) -Preserves 'Ollama, its models and the Arena viewer'
                    }
                }
            }
            if ($role -in 'head', 'gpu-workstation') { $plan.preserves += 'installed models, local configuration and the Arena enrollment' }
        }
        'hub' {
            $v = Get-LabAddressVerdict -Address $Config.hub -Role 'hub' -Config $Config -LocalLabAddresses $labAddrs
            $plan.address = $Config.hub; $plan.address_verdict = $v
            if (-not $v.HeldHere) { $plan.warnings += ('this machine does not hold {0} yet; the hub-network step sets it (it asks again before changing the address, and records its undo)' -f $Config.hub) }
            $hub = $null; if ($i.arena) { $hub = $i.arena.Hub }
            $installed = [bool]($hub -and $hub.ProgramInstalled)
            if (-not $installed) { $plan.warnings += ('the Arena hub is not installed here: a fresh install creates a NEW service identity. The fleet''s identity {0} lives in the current hub''s data; this USB carries no moved data.' -f $Config.arena.service_id) }
            $hs = $Config.hub_setup
            $hubMode = [string]$Choices.HubMode; if (-not $hubMode) { $hubMode = 'arena' }
            # A key of the plan dictionary, not Add-Member: a NoteProperty on the dictionary is dropped when
            # it becomes the plan object below, and every hub choice then ran as Verify (field run 1.3.0).
            $plan['hub_mode'] = $hubMode
            if ($hubMode -eq 'gateway') {
                $plan.steps = @(New-LabGatewayPlanSteps -Config $Config)
                $plan.external += 'the svc-inference-gw password (typed twice: account creation, then install), never stored'
                $plan.external += 'an approved Gateway policy (from the release''s config\policy.template.json: approved models per application)'
                $plan.external += 'offline media for the Gateway CA private key; a second USB stick (not the provisioner stick) for each application token file'
                $plan.preserves += 'Arena data and identity; the Gateway''s ledger, config, tls, ca, observation, logs and backups (preserved by every Gateway verb)'
                $acct = $null; if ($i.PSObject.Properties.Name -contains 'hubgw' -and $i.hubgw) { $acct = $i.hubgw.Accounts }
                # 1.6.15: a Gateway folder that only grants Arena access is repaired in the run (approved there), not blocked.
                if ($acct -and $acct.State -eq 'BLOCKED' -and (Test-LabGatewayFolderGrantsOnly -Blockers @($acct.Blockers))) { $plan.external += ('a Gateway folder grants an Arena account access (' + ($acct.Blockers -join '; ') + '): the run offers to remove that access (type REMOVE-ACCESS)') }
                elseif ($acct -and $acct.State -eq 'BLOCKED') { $plan.blocked += ('Gateway installation needs every Arena account to be a standard account: ' + ($acct.Blockers -join '; ') + '. Run Administration hub > Arena standard-account migration first (separately approved).') }
                break
            }
            if ($hubMode -eq 'migrate') {
                $target = [string]$Choices.MigrationTarget
                $mp = Get-LabArenaMigrationPlan -Config $Config -Target $target -GatewayAccount ([string]$Config.gateway.service_account)
                $plan['migration'] = $mp
                if (-not $mp.Ok) { $plan.blocked += $mp.Blockers }
                $plan.steps += New-LabPlanStep 'migrate-export' 'C' ("Stop Arena of $(if ($mp.Source) { $mp.Source.Account } else { '?' }), export its data for $target, mark the old copy moved") -Kind 'disruptive' -Detail ($mp.Steps -join ' | ') -Rollback ($mp.Rollback -join '; ') -Preserves 'Arena service identity, registrations, enrollments, history'
                $plan.warnings += $mp.Warnings
                break
            }
            if ($hubMode -eq 'verify') { $plan.steps += New-LabPlanStep 'verify' 'E' 'Verify the hub, Arena and the Gateway (read-only)' -Kind 'read' -Approval $false; break }
            if ($hubMode -eq 'models') {
                $plan.steps += New-LabPlanStep 'g-models-console' 'C' 'Install or update the Gateway Models screen (C:\Program Files\AgentArenaLab\GatewayModels) and a "Gateway Models" shortcut on this administrator''s desktop' -Kind 'change' `
                    -Detail 'every file checked against this USB; standard accounts cannot change it (it runs as administrator); its settings folder is SYSTEM and Administrators only; then its self-check reads the Gateway once (read-only)' `
                    -Rollback 'delete the folder and the shortcut (the settings folder is kept)' -Preserves 'the Gateway, its ledger, credentials and every model choice'
                $plan.steps += New-LabPlanStep 'g-fleet' 'C' 'Set up the admin hub''s Fleet window: C:\ProgramData\AgentArenaLab\fleet (catalog, published state, Commit requests and status) and the model library folder' -Kind 'change' `
                    -Detail 'the Arena account may write only its Commit requests; the library is administrators only; the Fleet Commit helper (installed with the screen) publishes the Gateway''s state once (read-only)' `
                    -Rollback 'delete C:\ProgramData\AgentArenaLab\fleet (the model library is kept)' -Preserves 'the Gateway, its ledger, credentials and every model choice'
                $plan.steps += New-LabPlanStep 'g-hide-account' 'C' ("Hide the Gateway's service account $($Config.gateway.service_account) from the sign-in screen (it keeps running the Gateway)") -Kind 'change' -Rollback 'delete its value under Winlogon\SpecialAccounts\UserList'
                $plan.preserves += 'the Gateway and Arena are not changed by this choice'
                break
            }
            $plan.steps += New-LabPlanStep 'hub-network' 'C' $(if ($v.HeldHere) { "Keep $($Config.hub)/24 (already set)" } else { "Set the lab Ethernet adapter to $($Config.hub)/24, no gateway" }) -Kind $(if ($v.HeldHere) { 'read' } else { 'disruptive' }) -Detail 'adapter choice, conflict probes, backup and verified apply (as for heads)' -Rollback 'network backup JSON; undo restores DHCP or the earlier static address' -Approval (-not $v.HeldHere)
            $plan.steps += New-LabPlanStep 'hub-profile' 'C' ("Lab adapter network profile: $($hs.network_category)") -Rollback 'earlier profile restored'
            $plan.steps += New-LabPlanStep 'hub-power' 'C' 'Stay awake: no sleep or hibernate on AC, screen on, lid does nothing, Ethernet power saving and EEE off' -Rollback 'earlier power values restored'
            $plan.steps += New-LabPlanStep 'hub-time' 'C' ("Time zone $($Config.time_zone); this computer serves time to the lab (syncs from $($hs.time_upstream) when it has internet)") -Rollback 'earlier time service settings restored'
            $plan.steps += New-LabPlanStep 'hub-firewall' 'C' ("Inbound from $($Config.subnet): time (UDP 123) and ping; Remote Desktop from $($hs.remote_desktop_from) only") -Detail 'rules that already exist are left alone' -Rollback 'added rules removed; Remote Desktop scope and setting restored'
            $plan.steps += New-LabPlanStep 'hub-python' 'C' 'Python 3.12+ for the Arena installer (winget for this user, if missing)' -Kind 'change' -Rollback 'winget uninstall Python.Python.3.12'
            $plan.steps += New-LabPlanStep 'hub-install' 'C' $(if ($installed) { 'Update the Arena event service and admin window in place (data kept)' } else { 'Install the Arena event service and admin window (fresh data)' }) -Kind 'change' -Detail 'the existing AGENT-ARENA-HUB installer, unchanged: checks first, keeps the service identity and data' -Rollback 'UNINSTALL-ADMIN-HUB.cmd (keeps data); data backups in the service folder' -Preserves 'Arena data, registrations, credentials and history'
            $plan.steps += New-LabPlanStep 'arena-gateway-config' 'C' ('Arena reads the Gateway observation file ({0}, gateway_id {1})' -f $Config.gateway.observation_file, $Config.gateway.gateway_id) -Kind 'change' -Detail 'only the "gateway" block of aes.config.json; every other setting is proven unchanged' -Rollback 'aes.config.json restored from the backup'
            # Gateway 0.3.0 is carried on this USB (packages\gateway) and installed by its own guided choice,
            # never as a side effect of this one.
            $gwDetail = ('Inference Gateway {0} is on this USB (packages\gateway); it is installed by the separate, separately approved choice Administration hub > 2. Nothing of the Gateway is installed by this choice.' -f $Config.gateway.expected_release)
            $acct = $null; if ($i.PSObject.Properties.Name -contains 'hubgw' -and $i.hubgw) { $acct = $i.hubgw.Accounts }
            if ($acct -and $acct.State -eq 'BLOCKED') { $gwDetail += ' Before that choice can run: ' + ($acct.Blockers -join '; ') + ' (Administration hub > 3 moves Arena to a standard account).'; $plan.warnings += ('Gateway prerequisite not met yet: ' + ($acct.Blockers -join '; ') + '. Administration hub > 3 moves Arena to an existing standard account (separately approved); this choice changes no account.') }
            elseif ($acct -and $acct.State -eq 'UNKNOWN') { $gwDetail += ' Account check incomplete: ' + ($acct.Unknown -join '; ') }
            $plan.steps += New-LabPlanStep 'gateway' 'C' ('Inference Gateway {0}: installed by Administration hub > 2 (separate choice)' -f $Config.gateway.expected_release) -Kind 'read' -Approval $false -State 'pending' -Detail $gwDetail
            $plan.preserves += 'Arena data and history; Gateway storage, credentials and lifecycle stay separate'
        }
        { $_ -in 'capstone', 'coder' } {
            $want = [string]$Config.roles.$Role.address
            $addr = [string]$Choices.Address; if (-not $addr) { $addr = $want }
            $plan.address = $addr
            $plan.address_verdict = Get-LabAddressVerdict -Address $addr -Role $Role -Config $Config -LocalLabAddresses $labAddrs
            if (-not $plan.address_verdict.Allowed) { $plan.blocked += $plan.address_verdict.Reasons }
            $plan.warnings += $plan.address_verdict.Warnings
            # The provisioner sets the laptop's lab address itself, as it does for heads and the hub: the operator
            # no longer types it into Windows Settings (whose fields differ between Windows builds; 2026-11-07).
            $heldApp = [bool]$plan.address_verdict.HeldHere
            if (-not $heldApp) {
                $plan.conflict = Test-LabAddressConflict -Address $addr
                # As for heads: a device answering on the address stops the run; nothing takes over another device's address.
                if ($plan.conflict.Verdict -eq 'Conflict') { $plan.blocked += ('{0} is in use: {1}' -f $addr, $plan.conflict.Reason) }
            }
            $plan.steps += New-LabPlanStep 'network' 'C' $(if ($heldApp) { "Keep $addr/24 (already set)" } else { "Set the lab Ethernet adapter to $addr/24, no gateway (the lab has no router)" }) -Kind $(if ($heldApp) { 'read' } else { 'disruptive' }) -Detail 'the same adapter choice, conflict probe, backup and verified apply as for heads; Wi-Fi is not touched' -Rollback 'network backup JSON; undo restores DHCP or the earlier static address' -Approval (-not $heldApp)
            if ($Role -eq 'capstone') {
            $plan.steps += New-LabPlanStep 'time' 'C' ("Time zone $($Config.time_zone); Windows Time from the hub $($Config.hub)") -Detail 'as on heads: Arena refuses clocks more than 120 s off' -Rollback 'earlier time zone and Windows Time source restored'
            $plan.steps += New-LabPlanStep 'appcontrol' 'C' 'Smart App Control off, if it is on or in evaluation (asked separately: TURN-OFF)' -Kind 'read' -Approval $false -Detail 'it can block unsigned parts of Capstone''s Python packages and of the Ticketing adapter, as it blocked the viewer on head 10' -Rollback 'Windows Security > App & browser control > Smart App Control'
            } else {
                $plan.steps += New-LabPlanStep 'clock' 'C' "Measure the clock against the hub $($Config.hub) (nothing changed: Coder's laptop keeps its own time source)" -Kind 'read' -Approval $false -Detail 'Arena refuses events more than 120 s off; an offset is reported for the operator to correct'
            }
            if ($Role -eq 'capstone') {
                $c = $i.capstone
                $pkg = $null; if ($Choices.CapstonePackage) { $pkg = [string]$Choices.CapstonePackage }
                $pkgVer = ''; if ($pkg -and (Split-Path -Leaf $pkg) -match '^Capstone-(.+)-windows-x64\.zip$') { $pkgVer = $Matches[1] }
                $installTitle = $(if ($c -and $c.Installed -and (-not $pkgVer -or $c.Version -eq $pkgVer)) { "Keep Capstone $($c.Version) (installed)" } elseif ($c -and $c.Installed) { "Update Capstone $($c.Version) to $pkgVer (its own Update-Capstone.ps1; restores the previous release and both databases if its health check fails)" } elseif ($pkg) { "Install Capstone from $(Split-Path -Leaf $pkg) (its own Install-Capstone.ps1)" } else { 'Install Capstone: PENDING, no Capstone release package on this USB' })
                $plan.steps += New-LabPlanStep 'vcredist' 'C' 'Microsoft Visual C++ 2015-2022 runtime (x64), if missing: Capstone''s torch needs it' -Kind 'read' -Approval $false -Detail 'asked separately (APPROVE) only when its DLLs are missing; Microsoft''s signed installer from the stick' -Rollback 'Settings > Apps > uninstall (other programs may need it)'
                $plan.steps += New-LabPlanStep 'capstone-install' 'C' $installTitle -Kind $(if ($c -and $c.Installed -and (-not $pkgVer -or $c.Version -eq $pkgVer)) { 'read' } else { 'privileged' }) -Detail 'archive SHA-256 checked against the approved release, then every file against its own manifest' -Rollback 'Capstone''s Rollback-Capstone.ps1 -Version <previous> (its project)' -Preserves 'ProgramData\Capstone (operations.db, ai_configuration.db, Testing Lab history, Candidate A, QMS, experiments)' -State $(if (($c -and $c.Installed) -or $pkg) { 'planned' } else { 'pending' })
                $plan.steps += New-LabPlanStep 'gateway-client' 'C' 'Gateway client folder C:\ProgramData\Capstone\gateway (SYSTEM/Administrators full, LOCAL SERVICE read) and the Gateway CA from the provisioner stick' -Kind 'change' -Detail 'paths and ACL from the Gateway manifest; Capstone supplies its own two client bootstraps; the capstone and capstone-research token files are carried from the hub by the operator and only checked (presence, ACL), never read' -Rollback 'files restored or removed'
                if (@($Config.PSObject.Properties.Name) -contains 'business_services') {
                    $plan.steps += New-LabPlanStep 'business-clients' 'C' 'QMS (head 3) and ERP (head 4): take the tokens those heads put on the provisioner stick into C:\ProgramData\Capstone\services, delete them from the stick, and call each service with its token' -Kind 'change' -Detail 'asked separately for each token; a service Capstone cannot reach with its token stays in-process, as before' -Rollback 'delete the files in C:\ProgramData\Capstone\services; Configure-Capstone.ps1 without the service then keeps it in-process'
                }
                $plan.steps += New-LabPlanStep 'capstone-configure' 'C' 'Configure Capstone as a Gateway client with two identities (its own Configure-Capstone.ps1 -OperationalGatewayBootstrap -ResearchGatewayBootstrap; gateway mode), and -QmsServiceConfig / -ErpServiceConfig for each service that accepted its token' -Detail 'Capstone''s own bootstrap templates, validated against the Gateway manifest; needs both token files, the CA and the developer token file; refused by Capstone if any is missing' -Rollback 'appsettings.json is rewritten by Capstone; earlier copy kept in the run folder'
                $plan.steps += New-LabPlanStep 'capstone-task' 'C' 'Register Capstone''s startup task as LOCAL SERVICE (its own Register-CapstoneTask.ps1)' -Detail 'Capstone refuses while either identity''s endpoint, token or trust material is incomplete' -Rollback 'Unregister-ScheduledTask "Capstone Application"'
                $plan.steps += New-LabPlanStep 'capstone-shortcut' 'C' '"Capstone" and "Capstone Testing Lab" icons on every user''s desktop that open them in the browser (Capstone has no window of its own; skipped where Capstone''s installer made its own)' -Kind 'change' -Approval $false -Rollback 'delete C:\Users\Public\Desktop\Capstone.url and Capstone Testing Lab.url'
                $plan.steps += New-LabPlanStep 'capstone-lab-heads' 'C' 'Capstone 6.0.0-rc.5 Research Lab > Heads: write C:\ProgramData\Capstone\config\lab-heads.json (heads 2-10 with names and hardware, QMS on head 3, ERP on head 4; Capstone reads it, the Gateway decides what each head serves)' -Kind 'change' -Approval $false -Rollback 'delete C:\ProgramData\Capstone\config\lab-heads.json'
                $plan.steps += New-LabPlanStep 'arena-claim' 'D' "Claim $addr in Agent Arena for capstone/main (one-time claim code)" -Kind 'change' -Detail 'POST /v2/claim from this address' -Rollback 'release in the admin console'
                $plan.steps += New-LabPlanStep 'ticketing' 'D' 'Install the Arena Ticketing adapter (existing AGENT-ARENA-TICKETING installer, one-time app code)' -Kind 'change' -Detail ('reads Capstone''s database read-only at ' + $Config.capstone.operations_database) -Rollback 'remove %LOCALAPPDATA%\Programs\AgentArenaAdapter and its sign-in entry'
                $plan.steps += New-LabPlanStep 'capstone-viewer' 'D' 'Agent Arena viewer for this user, showing Capstone''s Ticketing Operations room (for the second monitor; asked separately: APPROVE, then the display''s one-time code)' -Kind 'change' -Detail 'the display is registered on the hub first: Machines > Add a machine..., this address, Ticketing coordinator' -Rollback 'Start menu > Agent Arena > Agent Arena Setup (repair or uninstall) > Uninstall'
                $plan.external += 'capstone.token and capstone-research.token, issued separately on the hub (Gateway credentials step), carried on a second stick; the Gateway CA comes from the provisioner stick''s gateway-ca folder (whatever drive letter it has)'
                $plan.external += 'Capstone developer token file (Capstone owner)'
                $plan.external += 'Arena claim code and app code (admin console on the hub)'
                $plan.preserves += 'Capstone application data, Testing Lab, QMS and experiment records'
            } else {
                $cl = Get-LabCoderLayout -Config $Config
                $plan.steps += New-LabPlanStep 'coder-separation' 'C' 'Check that Ethernet and Wi-Fi stay apart: no IP forwarding, no Internet Connection Sharing, no bridge, no gateway or DNS on the lab adapter' -Kind 'read' -Approval $false -Detail 'a finding stops nothing and changes nothing; it is reported'
                $plan.steps += New-LabPlanStep 'coder-account' 'C' 'Choose the local account that runs Coder (its SID gets read access to Coder''s Gateway files)' -Kind 'read' -Approval $false -Detail 'never assumed: the operator picks it from this laptop''s enabled local accounts; not the built-in Administrator'
                $plan.steps += New-LabPlanStep 'coder-rdp' 'C' 'Remote Desktop with Network Level Authentication, only from the control workstation''s Wi-Fi address and only on Wi-Fi; the Coder account may sign in remotely' -Kind 'change' -Detail ("firewall rule {0}; an enabled built-in Remote Desktop rule is turned off only if you type DISABLE; console sign-in untouched; Windows Home cannot host Remote Desktop" -f $cl.RdpRule) -Rollback 'rule removed, Remote Desktop and NLA registry values restored, group membership removed'
                $plan.steps += New-LabPlanStep 'coder-python' 'C' ('Python {0} machine-wide from the stick, if missing (for TEST-CODER-GATEWAY; Coder itself runs on Python 3.12)' -f $Config.runtime.python.version) -Kind 'privileged' -Detail 'the publisher''s hash and signature are checked first' -Rollback 'Settings > Apps > Python > Uninstall'
                $plan.steps += New-LabPlanStep 'coder-gateway-files' 'C' ("Coder's Gateway files in {0}: SYSTEM and Administrators full, the Coder account read; the lab CA from the stick (SHA-256 pinned); {1} exactly as Coder's template" -f $cl.Folder, (Split-Path -Leaf $cl.ClientConfig)) -Kind 'change' -Detail ("{0}, service {1}, application {2}, token_file only (no environment token)" -f $cl.Url, $cl.ServiceId, $cl.AppId) -Rollback 'files restored from the run''s backups or removed'
                $plan.steps += New-LabPlanStep 'coder-token' 'C' ("Coder's Gateway token from a second stick (<drive>\{0}\coder.token) into {1}; copied, never read; the transfer copy deleted only if you type DELETE" -f $cl.TransferFolder, $cl.Token) -Kind 'change' -Detail 'issued on the hub by Coder Onboarding (shortcut, as LabOperator1)' -Rollback 'the token file removed'
                $plan.steps += New-LabPlanStep 'coder-install' 'C' 'Coder application: PENDING (Coder project)' -Kind 'read' -Approval $false -State 'pending' -Detail 'Coder''s release, its Gateway adapter, Arena publisher and code runner are the Coder project''s; nothing is installed or invented here'
                $plan.steps += New-LabPlanStep 'arena-claim' 'D' "Claim $addr in Agent Arena for coder/main (one-time claim code from the admin hub: Machines > Addresses)" -Kind 'change' -Detail 'POST /v2/claim from this address; no app code yet (Coder has no publisher to redeem it)' -Rollback 'release in the admin console'
                $plan.external += 'coder.token, issued on the hub by Coder Onboarding and carried on a second stick'
                $plan.external += 'Arena: application coder and the reservation of this address (admin hub, ArenaSvcUser''s screen)'
                $plan.external += 'Coder release, its Gateway adapter, Arena publisher and code runner (Coder project)'
                $plan.preserves += 'this laptop''s clock, time source, Smart App Control and other security settings; Wi-Fi settings'
            }
        }
    }
    foreach ($s in @($plan.steps | Where-Object { $_.approval -and $_.state -eq 'planned' })) { $plan.approvals += ('{0} ({1})' -f $s.title, $s.kind) }
    return [pscustomobject]$plan
}

function Show-LabPlan {
    param([Parameter(Mandatory)]$Plan)
    Write-Host ('  Role: {0}    Machine: {1}    Appears to be: {2}' -f $Plan.role, $Plan.computer, $Plan.detected.Role) -ForegroundColor Cyan
    if ($Plan.address) { Write-Host ('  Address: {0}  {1}' -f $Plan.address, $(if ($Plan.conflict) { '(conflict check: {0} - {1})' -f $Plan.conflict.Verdict, $Plan.conflict.Reason } else { '' })) }
    if ($Plan.hardware) { Write-Host ('  Hardware: {0}' -f $(if ($Plan.hardware.Suitable) { 'suitable - ' + ($Plan.hardware.Notes -join '; ') } else { 'NOT suitable - ' + ($Plan.hardware.Blocking -join '; ') })) }
    if ($Plan.profile) { Write-Host ('  Model profile: proposed {0} ({1}); chosen {2}; {3} model(s)' -f $Plan.profile.Proposed, $Plan.profile.Reason, ($Plan.profile.Chosen -join '+'), @($Plan.models).Count) }
    if (@($Plan.clients).Count -and $Plan.role -in 'cpu-head', 'gpu-head', 'existing') { Write-Host ('  Ollama access ({0}): {1}; denied: {2}' -f $Plan.client_mode, ($Plan.clients -join ', '), ($Plan.denied -join ', ')) }
    Write-Host '  Steps:' -ForegroundColor Cyan
    $n = 0
    foreach ($s in @($Plan.steps)) { $n++; $c = @{ read = 'Gray'; change = 'White'; privileged = 'Yellow'; disruptive = 'Red' }[$s.kind]; if ($s.state -eq 'pending') { $c = 'DarkYellow' }
        Write-Host ('   {0,2}. [{1}] {2}{3}' -f $n, $s.kind, $s.title, $(if ($s.state -eq 'pending') { '  (PENDING)' } else { '' })) -ForegroundColor $c
        if ($s.detail) { Write-Host ('       {0}' -f $s.detail) -ForegroundColor DarkGray }
        if ($s.rollback) { Write-Host ('       rollback: {0}' -f $s.rollback) -ForegroundColor DarkGray } }
    foreach ($e in @($Plan.external)) { Write-Host ('  Needs from others: ' + $e) -ForegroundColor DarkYellow }
    foreach ($p in @($Plan.preserves)) { Write-Host ('  Preserved: ' + $p) -ForegroundColor Green }
    foreach ($w in @($Plan.warnings)) { Write-Host ('  Warning: ' + $w) -ForegroundColor Yellow }
    foreach ($b in @($Plan.blocked)) { Write-Host ('  BLOCKED: ' + $b) -ForegroundColor Red }
}

Export-ModuleMember -Function @('Get-LabServingArgs', 'New-LabGatewayPlanSteps', 'Get-LabAddressVerdict', 'Test-LabAddressConflict', 'Get-LabHardwareVerdict', 'New-LabPlanStep', 'New-LabPlan', 'Show-LabPlan')
