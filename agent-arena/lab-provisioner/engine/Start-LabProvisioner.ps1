<#
    Agent Arena Lab Provisioner - the one operator entry point (START-LAB-PROVISIONER.cmd).

    Choose the machine's role; every role runs the same visible stages:
        A Inspect (read-only)  B Plan (approval)  C Configure  D Register  E Verify  F Report

    Options for a dry run or for scripted use:
        -Role cpu-head|gpu-head|hub|capstone|coder|existing|inspect
        -HubMode arena|gateway|migrate|verify|models   (hub) and -MigrationTarget <account> (migrate)
        -Through Inspect|Plan|All        stop after inspection or after the plan (no changes)
        -Address 192.0.2.N               the head address (heads)
        -ClientMode cutover|gateway-only who may reach Ollama (heads)
        -Profiles CPU16[,EMBEDDINGS]     model profiles (heads)
        -Root <folder>                   state folder instead of C:\ProgramData\AgentArenaLab
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'cpu-head', 'gpu-head', 'hub', 'capstone', 'coder', 'existing', 'inspect')][string]$Role = '',
    [ValidateSet('Inspect', 'Plan', 'All')][string]$Through = 'All',
    [string]$Address = '', [string]$ClientMode = '', [string[]]$Profiles = @(), [string]$Root = '', [switch]$NoExport,
    [ValidateSet('', 'arena', 'gateway', 'migrate', 'verify', 'models')][string]$HubMode = '', [string]$MigrationTarget = ''
)
$ErrorActionPreference = 'Continue'
$Release = Split-Path -Parent $PSScriptRoot
$UsbRoot = Split-Path -Parent $Release
$Modules = Join-Path $PSScriptRoot 'modules'
$HeadEngine = Join-Path $PSScriptRoot 'head-engine\modules'
foreach ($m in 'Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Preflight') { Import-Module (Join-Path $HeadEngine "$m.psm1") -Force -Global -DisableNameChecking }
foreach ($m in 'Lab.Core', 'Lab.Firewall', 'Lab.Accounts', 'Lab.OllamaRuntime', 'Lab.Head', 'Lab.Gateway', 'Lab.Hub', 'Lab.Models', 'Lab.Arena', 'Lab.Apps', 'Lab.Services', 'Lab.Coder', 'Lab.Inspect', 'Lab.Plan', 'Lab.Report') { Import-Module (Join-Path $Modules "$m.psm1") -Force -Global -DisableNameChecking }
$Config = Get-LabConfig -Path (Join-Path $Release 'config\lab-network.json')
$CatalogPath = Join-Path $PSScriptRoot 'head-engine\catalog\model-catalog.json'
$Payload = Join-Path $Release 'payload'
$Packages = Join-Path $Release 'packages'
$null = Initialize-CapstoneHttp
. (Join-Path $PSScriptRoot 'Invoke-LabGatewayFlow.ps1')

# ---------------------------------------------------------------------------
# Release integrity: the engine and configuration must be exactly what was built.
# ---------------------------------------------------------------------------

function Test-LabReleaseIntegrity {
    param([string[]]$Prefixes = @('engine/', 'config/'))
    $manifest = Read-LabJson (Join-Path $Release 'release-manifest.json')
    if (-not $manifest) { return [pscustomobject]@{ Ok = $false; Detail = 'release-manifest.json is missing (a development copy?)'; Dev = $true } }
    $bad = @(); $n = 0
    foreach ($p in $manifest.files.PSObject.Properties) {
        $rel = $p.Name; if (-not (@($Prefixes | Where-Object { $rel.StartsWith($_) }).Count)) { continue }
        $n++; $f = Join-Path $Release ($rel -replace '/', '\')
        if (-not (Test-Path -LiteralPath $f) -or (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$p.Value) { $bad += $rel }
    }
    return [pscustomobject]@{ Ok = ($bad.Count -eq 0); Detail = $(if ($bad.Count) { 'damaged or changed: ' + ($bad -join ', ') } else { "$n engine and configuration files match release $($manifest.release_id)" }); Dev = $false }
}

# ---------------------------------------------------------------------------
# Shared prompts
# ---------------------------------------------------------------------------

function Read-LabHeadAddress {
    param($Inspection)
    if ($Address) { return $Address }
    Write-Host ''
    Write-Host '  Head address. Heads use 192.0.2.2-.199. Protected: ' -NoNewline; Write-Host ((@($Config.protected_addresses | ForEach-Object { $_.address }) -join ', ')) -ForegroundColor Yellow
    Write-Host ('  Existing heads: {0}. Reserve the address in the hub''s admin console (Machines > Addresses) first.' -f ($Config.known_heads -join ', '))
    $n = Read-LabInput -Prompt 'Type the head number N for 192.0.2.N (Enter to stop)'
    if ($n -match '^\d{1,3}$') { return ('{0}.{1}' -f $Config.prefix, [int]$n) }
    if ($n -match '^192\.0\.2\.\d{1,3}$') { return $n }
    return ''
}

function Read-LabClientMode {
    if ($ClientMode) { return $ClientMode }
    Write-Host ''
    Write-Host '  Who may reach this head''s Ollama port 11434?'
    Write-Host ('   1. cutover       {0}  (Gateway on the hub, plus the documented development exception) [default]' -f ($Config.ollama.client_modes.cutover -join ', '))
    Write-Host ('   2. gateway-only  {0}  (after the cutover is complete)' -f ($Config.ollama.client_modes.'gateway-only' -join ', '))
    $c = Read-LabInput -Prompt 'Choose 1 or 2'
    if ($c -eq '2') { return 'gateway-only' }
    return 'cutover'
}

function Read-LabModelChoice {
    param($Inspection, [string]$Default)
    if ($Profiles.Count) { return @{ Profiles = @($Profiles | ForEach-Object { $_ -split ',' } | Where-Object { $_ }); Add = @(); Exclude = @() } }
    $catalog = Get-LabCatalog -Path $CatalogPath
    Write-Host ''
    Write-Host '  Model profiles (approved catalog; nothing is bundled, models are pulled later in their own stage):'
    foreach ($p in $catalog.profiles.PSObject.Properties) { Write-Host ('   {0,-11} {1,3} models  {2,8}  min RAM {3} GB  {4}' -f $p.Name, $p.Value.model_count, $p.Value.total_display, $p.Value.min_ram_gb, $p.Value.description) }
    $p = Read-LabInput -Prompt ("Profiles, comma separated (Enter for the proposal $Default)")
    $add = Read-LabInput -Prompt 'Extra catalog tags to add, comma separated (Enter for none)'
    $ex = Read-LabInput -Prompt 'Tags to leave out, comma separated (Enter for none)'
    $list = @($Default); if ($p) { $list = @($p -split ',' | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ }) }
    return @{ Profiles = $list; Add = @($add -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }); Exclude = @($ex -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
}

function Get-LabHeadIp { param($Inspection) if ($Inspection.network -and @($Inspection.network.Lab).Count) { return $Inspection.network.Lab[0].Address }; return '' }
function Get-LabHeadAlias { param($Inspection) if ($Inspection.network -and @($Inspection.network.Lab).Count) { return $Inspection.network.Lab[0].Adapter }; return '' }

# ---------------------------------------------------------------------------
# Head steps
# ---------------------------------------------------------------------------

function Invoke-LabFirewallStep {
    param($Run, [string]$HeadIp, [string[]]$Clients, [string]$Alias, [string[]]$ServerPaths)
    $snap = Get-LabFirewallSnapshot
    $fw = New-LabFirewallPlan -Snapshot $snap -HeadIp $HeadIp -Clients $Clients -Config $Config -InterfaceAlias $Alias -ServerPaths $ServerPaths
    foreach ($a in @($fw.Actions)) { Write-LabLog ('  firewall will: ' + $a.Detail) }
    foreach ($r in @($fw.Review)) { Add-LabWarning ('firewall review: ' + $r) }
    foreach ($b in @($fw.Blockers)) { Add-LabWarning ('firewall blocker: ' + $b) }
    if ($fw.Unchanged) { return (New-LabOutcome 'UNCHANGED' ('the managed rule already admits exactly ' + ($Clients -join ', '))) }
    $backup = Backup-LabFirewall -Folder $Run.Backups -Snapshot $snap
    Write-LabLog ('  firewall backed up: {0} rules to {1}, full policy to {2}' -f $backup.RuleCount, $backup.Json, $backup.Policy)
    $approve = { param($a) Request-LabApproval -Prompt ('Firewall: ' + $a.Detail + '. This rule is not the provisioner''s own.') -Word 'DISABLE' -Kind 'disruptive' }
    $res = Invoke-LabFirewallPlan -Plan $fw -BackupFile $backup.Json -ApprovePerRule $approve
    foreach ($d in @($res.Declined)) { Add-LabPending -Area 'firewall' -Text ('not changed by the operator: ' + $d) }
    return (New-LabOutcome 'DONE' ('{0} change(s); backup {1}' -f @($res.Applied).Count, $backup.Json))
}

function Add-LabFirewallVerification {
    param([string]$HeadIp, [string[]]$Clients, [string]$Alias, [string[]]$ServerPaths)
    $denied = @($Config.ollama.denied_probe_sources); if ($Clients -notcontains $Config.dev_workstation) { $denied += $Config.dev_workstation }
    $exp = Get-LabOllamaExposure -Snapshot (Get-LabFirewallSnapshot) -HeadIp $HeadIp -Sources (@($Clients) + $denied) -InterfaceAlias $Alias -ServerPaths $ServerPaths -Config $Config
    foreach ($s in @($exp.Sources)) {
        $want = $(if ($Clients -contains $s.Source) { 'ADMITTED' } else { 'DENIED' })
        $st = $(if ($s.Verdict -eq $want) { 'PASS' } else { 'FAIL' })
        Add-LabVerification -Check ("firewall rules: Ollama from $($s.Source) is $want") -Status $st -Detail ("$($s.Verdict) - $($s.Reason) (rule analysis)") -Scope local | Out-Null
    }
    Add-LabVerification -Check 'Gateway on the hub reaches this head''s Ollama' -Status PENDING -Scope lan -Detail ('real connection from {0}: run the provisioner there (Administration hub > verify), or Test-NetConnection {1} -Port 11434 on the hub' -f $Config.hub, $HeadIp) | Out-Null
    Add-LabVerification -Check 'Application laptops cannot reach this head''s Ollama' -Status PENDING -Scope lan -Detail ('real connections from {0}: their Verify stage tests every head' -f ($Config.ollama.denied_probe_sources -join ', ')) | Out-Null
    return $exp
}

function Invoke-LabOllamaStart {
    param($Run, [string]$HeadIp)
    $exe = Get-LabServingOllamaExecutable -Root $Run.Root -Spec $Config.ollama.runtime   # 1.6.14: the server the boot task runs
    if (-not $exe) { throw 'Ollama is not installed' }
    $beforeBase = $null
    foreach ($b in @('http://127.0.0.1:11434', ('http://{0}:11434' -f $HeadIp))) { if ((Test-CapstoneOllamaApi -BaseUri $b -TimeoutSeconds 4).Healthy) { $beforeBase = $b; break } }
    $before = [pscustomobject]@{ Available = $false; Models = @() }
    if ($beforeBase) { $before = Get-LabModelSet -BaseUri $beforeBase }
    $loaded = Get-CapstoneRunningModels -BaseUri 'http://127.0.0.1:11434'
    $allow = $false
    if ($loaded.Available -and $loaded.Count -gt 0) { $allow = Request-LabApproval -Prompt ('{0} model(s) are loaded ({1}), possibly serving requests. Restarting Ollama interrupts them.' -f $loaded.Count, ($loaded.Names -join ', ')) -Word 'RESTART' -Kind 'disruptive'; if (-not $allow) { return (New-LabOutcome 'DECLINED' 'Ollama left running; the new boot configuration applies at its next start') } }
    $check = 'http://{0}:11434' -f $HeadIp
    $r = Restart-LabOllamaServer -Executable $exe -CheckBase $check -TaskName $Config.ollama.task_name -AllowLoaded:$allow
    if ($r.Outcome -eq 'DECLINED') { return $r }
    $after = Get-LabModelSet -BaseUri $check
    $cmp = Compare-LabModelSet -Before $before -After $after
    if ($before.Available -and -not $cmp.Same) { throw ('after the restart the boot server does not serve the same models: missing {0}; changed {1}. Undo this run to restore the earlier task.' -f ($cmp.Missing -join ', '), ($cmp.Changed -join ', ')) }
    $cp = Save-LabRebootCheckpoint -Run $Run -ModelSet $after -CheckBase $check -TaskName $Config.ollama.task_name
    return (New-LabOutcome 'DONE' ('{0}; {1} model(s) visible after the restart (was {2}); reboot checkpoint {3}' -f $r.Detail, $cmp.AfterCount, $cmp.BeforeCount, $cp))
}

function Invoke-LabClaimStep {
    param($Run, [string]$Addr, [string]$What)
    $h = Get-LabArenaHealth -Endpoint $Config.arena.endpoint -ExpectedServiceId $Config.arena.service_id
    if ($h.Verdict -ne 'ok') { Add-LabRegistration -Item "Arena address claim $Addr" -Status PENDING -Detail ("the event service at $($Config.arena.endpoint) is $($h.Verdict) $($h.ServiceId) $($h.Error)") | Out-Null; return (New-LabOutcome 'PENDING' 'Arena event service not answering as expected') }
    $who = Get-LabArenaWhoami -Endpoint $Config.arena.endpoint
    if ($who.Address -and $who.Address -ne $Addr) { Add-LabRegistration -Item "Arena address claim $Addr" -Status PENDING -Detail ("the service sees this machine as $($who.Address), not ${Addr}; the claim must be sent from $Addr") | Out-Null; return (New-LabOutcome 'PENDING' 'not sending from the claimed address') }
    if (@($who.Displays).Count) { Add-LabRegistration -Item "Arena registration $Addr" -Status ALREADY_REGISTERED -Detail ('registered displays: ' + ((@($who.Displays) | ForEach-Object { '{0} ({1})' -f $_.display_id, $_.kind }) -join ', ')) | Out-Null; return (New-LabOutcome 'UNCHANGED' 'already registered in Arena') }
    # An application laptop's claim has no display, so whoami cannot show it. This machine's own record of a claim
    # that Arena accepted counts instead: 1.4.7 asked DESKTOP-REDACT11 again for a code it had already used.
    $prior = Get-LabPriorClaim -ReportsDir $Run.ReportsDir -Address $Addr
    if ($prior) { Add-LabRegistration -Item "Arena address claim $Addr" -Status ALREADY_REGISTERED -Detail $prior | Out-Null; return (New-LabOutcome 'UNCHANGED' $prior) }
    for ($try = 1; $try -le 2; $try++) {
        # Where the code comes from, in the admin hub's own words, and always called the claim code.
        $where = $(if ($What -eq 'head') { 'Inference head' } else { 'Application laptop' })
        $code = Read-LabInput -Prompt ("Arena CLAIM CODE for $Addr ($What). Get it at the admin hub (ArenaSvcUser's screen): Machines > Machines and codes... > Addresses: role '$where', address $Addr > Reserve and get code. Type it here, or press Enter to do this later") -Secret
        if (-not $code) { Add-LabRegistration -Item "Arena address claim $Addr" -Status PENDING -Detail 'no claim code given: reserve the address in the admin console and run the Register step again' | Out-Null; return (New-LabOutcome 'PENDING' 'no claim code') }
        $r = Invoke-LabArenaClaim -Endpoint $Config.arena.endpoint -Address $Addr -Code $code -ExpectedServiceId $Config.arena.service_id
        $code = $null
        if ($r.Ok) {
            $id = $(if ($r.Data.display_id) { $r.Data.display_id } else { '{0}/{1}' -f $r.Data.app_id, $r.Data.orchestrator_id })
            Add-LabRegistration -Item "Arena address claim $Addr" -Status REGISTERED -Detail ('claimed as {0} ({1})' -f $id, $r.Data.role) | Out-Null
            return (New-LabOutcome 'DONE' ('claimed as ' + $id) $r.Data)
        }
        Write-LabLog ('  claim refused: {0} {1}' -f $r.Kind, $r.Message) -Level WARN
        if ($r.Kind -ne 'code_refused') { break }
    }
    Add-LabRegistration -Item "Arena address claim $Addr" -Status FAILED -Detail ('{0}: {1}' -f $r.Kind, $r.Message) | Out-Null
    return (New-LabOutcome 'FAILED' ('claim refused: ' + $r.Message))
}

function Invoke-LabViewerStep {
    param($Run, [string]$Addr, $ClaimData)
    $tool = Get-LabViewerToolset -Payload $Payload
    if (-not $tool) { throw "this stick has no AGENT-ARENA-<version> viewer toolset in $Payload" }
    $toolset = $tool.Path; $want = $tool.Version
    # Smart App Control blocks the unsigned viewer (head 10, 2026-11-07): offered before the viewer is touched.
    $sac = Request-LabSmartAppControlOff -Run $Run -Why 'It blocks the Arena viewer (AgentArena.exe is not code-signed), so this head''s Arena window cannot run.'
    if ($sac -and $sac.Changed -and -not $sac.Refreshed) {
        Add-LabPending -Area 'viewer' -Text 'Smart App Control was turned off; restart this computer, sign in, then START-LAB-PROVISIONER > Existing machine > 8 again'
        Add-LabRegistration -Item 'Arena head viewer' -Status PENDING -Detail ($sac.Detail + '; restart, then Existing machine > 8') | Out-Null
        return (New-LabOutcome 'PENDING' ($sac.Detail + '; restart this computer, then run Existing machine > 8 again'))
    }
    $v = Get-LabViewerState
    $updatedFrom = $null
    if ($v.Installed -and $v.CredentialPresent -and [version]$v.Version -lt [version]$want) {
        # Enrolled with an older viewer: updated in place, no window and no code (2.1.3 ends 2.1.2's memory growth; 2.1.4 also guards kernel memory; 2.1.5 ends the ticker signs' graphics memory leak).
        $updatedFrom = $v.Version
        $u = Update-LabViewerQuiet -Toolset $tool -EvidenceFolder $Run.Folder -Hub $Config.arena.endpoint
        if ($u.Outcome -ne 'DONE') { Add-LabRegistration -Item 'Arena head viewer' -Status PENDING -Detail $u.Detail | Out-Null; return $u }
        $v = Get-LabViewerState
    }
    if ($v.Installed -and $v.CredentialPresent -and $v.Version -eq $want) {
        # Installed and enrolled is not enough: it must ask the hub (heads enrolled before the move still asked 192.0.2.1).
        $e = Repair-LabViewerEndpoint -Run $Run -Hub $Config.arena.endpoint
        Add-LabRegistration -Item 'Arena head viewer' -Status $(if ($e.Outcome -eq 'DONE' -or $updatedFrom) { 'REGISTERED' } else { 'ALREADY_REGISTERED' }) -Detail ("$want for $env:USERNAME; " + $e.Detail) | Out-Null
        # 1.6.29: an update in this run is DONE ("updated 2.2.1 -> 2.3.0"). 1.6.28 reported the endpoint check's
        # UNCHANGED instead, so nine heads that were updated read as if nothing had happened.
        if ($updatedFrom) { return (New-LabOutcome 'DONE' ("viewer updated $updatedFrom -> $want and enrolled; " + $e.Detail)) }
        return (New-LabOutcome $e.Outcome ("viewer $want installed and enrolled; " + $e.Detail))
    }
    $display = ''
    if ($ClaimData -and $ClaimData.display_id) { $display = [string]$ClaimData.display_id }
    else { $who = Get-LabArenaWhoami -Endpoint $Config.arena.endpoint; $d = @($who.Displays) | Select-Object -First 1; if ($d) { $display = [string]$d.display_id } }
    if (-not $display) { Add-LabRegistration -Item 'Arena head viewer' -Status PENDING -Detail 'no display is registered for this address yet (claim the address first)' | Out-Null; return (New-LabOutcome 'PENDING' 'no registered display') }
    $code = ''
    if (-not $v.CredentialPresent) {
        $code = Read-LabInput -Prompt ("One-time enrollment code for display $display (admin console > Machines > issue code), or Enter to leave it pending") -Secret
        if (-not $code) { Add-LabRegistration -Item 'Arena head viewer' -Status PENDING -Detail "no enrollment code for $display" | Out-Null; return (New-LabOutcome 'PENDING' 'no enrollment code') }
    }
    $r = Invoke-LabViewerInstall -Run $Run -Toolset $toolset -DisplayId $display -Code $code
    $code = $null
    # An update keeps the old event service address; the toolset's own checks then fail on it (head 6: 2 FAILs,
    # both "service unreachable" at 192.0.2.1). Point it at the hub and prove it, then judge the result.
    $nv = Get-LabViewerState
    if ($nv.Installed -and $nv.CredentialPresent) {
        try { $e = Repair-LabViewerEndpoint -Run $Run -Hub $Config.arena.endpoint; $r = New-LabOutcome 'DONE' ('viewer {0} installed; {1}' -f $nv.Version, $e.Detail) }
        catch { $r = New-LabOutcome 'FAILED' ('viewer {0} installed, but {1}' -f $nv.Version, $_.Exception.Message) }
    }
    $status = @{ DONE = 'REGISTERED'; PENDING = 'PENDING'; FAILED = 'FAILED' }[$r.Outcome]
    Add-LabRegistration -Item 'Arena head viewer' -Status $status -Detail $r.Detail | Out-Null
    return $r
}

function Invoke-LabModelStage {
    param($Run, $Inspection, [hashtable]$Choice)
    $catalog = Get-LabCatalog -Path $CatalogPath
    $sel = Select-LabModels -Catalog $catalog -Profiles $Choice.Profiles -Add $Choice.Add -Exclude $Choice.Exclude
    foreach ($r in $sel.Refused) { Add-LabWarning $r }
    $headIp = Get-LabHeadIp $Inspection
    $base = 'http://127.0.0.1:11434'; if ($headIp -and (Test-CapstoneOllamaApi -BaseUri ('http://{0}:11434' -f $headIp) -TimeoutSeconds 4).Healthy) { $base = 'http://{0}:11434' -f $headIp }
    $dir = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine'); if (-not $dir) { $dir = (Resolve-LabModelDirectory -Candidates @(Get-LabModelDirectoryCandidates -NewDefault $Config.ollama.new_model_directory) -NewDefault $Config.ollama.new_model_directory).Path }
    Write-LabStage -Letter 'C' -Title ('Models (separate stage): {0} selected, about {1}' -f @($sel.Models).Count, (Format-CapstoneBytes -Bytes $sel.TotalBytes))
    foreach ($m in @($sel.Models)) { Write-LabLog ('  {0,-28} {1,-10} {2}' -f $m.tag, $m.kind, $m.size_display) }
    if (-not (Request-LabApproval -Prompt ('Pull and verify these {0} model(s) through {1}? Existing models are kept.' -f @($sel.Models).Count, $base) -Word 'APPROVE' -Kind 'change')) { Add-LabPending -Area 'models' -Text 'model installation declined; run Existing machine > Install or update models later'; return }
    $res = Invoke-LabStep -Id 'models' -Title 'Install and verify models' -ContinueOnFailure -Action {
        $out = Invoke-LabModelInstall -Run $Run -BaseUri $base -Models @($sel.Models) -ModelDirectory $dir
        $Run.Models = $out
        $bad = @($out.Records | Where-Object { $_.final -ne 'VERIFIED' })
        New-LabOutcome $(if ($bad.Count) { 'PENDING' } else { 'DONE' }) ('{0} verified, {1} not verified ({2})' -f (@($out.Records).Count - $bad.Count), $bad.Count, (($bad | Group-Object final | ForEach-Object { '{0} {1}' -f $_.Count, $_.Name }) -join ', '))
    }
    if ($Run.Models) {
        $recs = @($Run.Models.Records)
        Add-LabVerification -Check 'models installed locally (in /api/tags)' -Status $(if (@($recs | Where-Object { -not $_.installed_locally }).Count) { 'FAIL' } else { 'PASS' }) -Detail ('{0} of {1}' -f @($recs | Where-Object installed_locally).Count, $recs.Count) -Required $false | Out-Null
        Add-LabVerification -Check 'models verified by a real generate/embed request' -Status $(if (@($recs | Where-Object { -not $_.verified_by_inference }).Count) { 'FAIL' } else { 'PASS' }) -Detail ('{0} of {1}' -f @($recs | Where-Object verified_by_inference).Count, $recs.Count) -Required $false | Out-Null
        Add-LabVerification -Check 'models qualified by the Inference Gateway' -Status PENDING -Scope external -Detail 'only the Gateway qualifies models (inference-qualify on the hub); not claimed here' -Required $false | Out-Null
    }
}

function Invoke-LabHeadVerify {
    param($Run, [string]$HeadIp, [string[]]$Clients, [string]$Alias)
    Write-LabStage -Letter 'E' -Title 'Verify'
    $exe = Get-LabServingOllamaExecutable -Root $Run.Root -Spec $Config.ollama.runtime
    $private = Test-CapstoneOllamaApi -BaseUri ('http://{0}:11434' -f $HeadIp) -TimeoutSeconds 5
    Add-LabVerification -Check "Ollama answers on $HeadIp`:11434" -Status $(if ($private.Healthy) { 'PASS' } else { 'FAIL' }) -Detail $(if ($private.Healthy) { 'version ' + $private.Version } else { $private.Error }) | Out-Null
    $task = Get-LabOllamaTask -TaskName $Config.ollama.task_name
    $okTask = $task.Present -and $task.User -match 'SYSTEM' -and $task.Action -match 'ollama-serve\.cmd'
    Add-LabVerification -Check 'boot task runs the server as SYSTEM with an explicit model directory' -Status $(if ($okTask) { 'PASS' } else { 'FAIL' }) -Detail ('{0} as {1}' -f $task.Action, $task.User) | Out-Null
    $md = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine')
    if ($private.Healthy) {
        $set = Get-LabModelSet -BaseUri ('http://{0}:11434' -f $HeadIp)
        $cand = @(Get-LabModelDirectoryCandidates -NewDefault $Config.ollama.new_model_directory | Where-Object { $_.Manifests -gt 0 })
        # Only a folder with a model the server does not serve fails; a spare copy of the same models is reported.
        $sod = Test-LabServedOnDisk -Served @(@($set.Models) | ForEach-Object { [string]$_.name }) -Candidates $(if ($md) { $cand } else { @() }) -ServerPath $md
        Add-LabVerification -Check 'the served models are the ones on disk' -Status $(if ($sod.Ok) { 'PASS' } else { 'FAIL' }) -Detail ('{0} served from OLLAMA_MODELS={1}{2}' -f @($set.Models).Count, $md, $(if ($sod.Detail) { '; ' + $sod.Detail } else { '' })) | Out-Null
    }
    $exp = Add-LabFirewallVerification -HeadIp $HeadIp -Clients $Clients -Alias $Alias -ServerPaths @($exe)
    # The report's security section shows the firewall as it is now, after this run's changes (1.4.1 showed the
    # inspection, taken before them: head 6 looked unchanged although its new rule admitted the hub).
    if ($exp -and $Run.Inspection -and $Run.Inspection.firewall) {
        $exp | Add-Member -NotePropertyName Analysis -NotePropertyValue 'after this run''s changes; rule analysis (inferred); a real connection from each address is the field proof' -Force
        $Run.Inspection.firewall | Add-Member -NotePropertyName Exposure -NotePropertyValue $exp -Force
    }
    $off = Get-LabTimeOffset -Hub $Config.hub
    if ($null -eq $off) { Add-LabVerification -Check 'clock against the hub' -Status PENDING -Scope lan -Detail "the hub $($Config.hub) does not answer NTP yet" | Out-Null }
    else { Add-LabVerification -Check 'clock against the hub (Arena allows 120 s)' -Status $(if ([math]::Abs($off) -le 120) { 'PASS' } else { 'FAIL' }) -Scope lan -Detail ('offset {0:N3} s' -f $off) | Out-Null }
    $h = Get-LabArenaHealth -Endpoint $Config.arena.endpoint -ExpectedServiceId $Config.arena.service_id
    Add-LabVerification -Check 'Arena event service reachable and genuine' -Status $(if ($h.Verdict -eq 'ok') { 'PASS' } else { 'PENDING' }) -Scope lan -Detail ('{0} {1} {2}' -f $h.Verdict, $h.ServiceId, $h.Version) | Out-Null
    $who = Get-LabArenaWhoami -Endpoint $Config.arena.endpoint
    Add-LabVerification -Check 'Arena registry knows this head' -Status $(if (@($who.Displays).Count) { 'PASS' } else { 'PENDING' }) -Scope lan -Detail $(if (@($who.Displays).Count) { (@($who.Displays) | ForEach-Object { $_.display_id }) -join ', ' } else { 'no display registered for ' + $who.Address }) | Out-Null
    $v = Get-LabViewerState
    Add-LabVerification -Check 'Arena head viewer installed and enrolled' -Status $(if ($v.Installed -and $v.CredentialPresent) { 'PASS' } else { 'PENDING' }) -Detail ('version {0}, credential {1}, starts at sign-in {2}, event service {3}' -f $v.Version, $v.CredentialPresent, $v.StartAtSignIn, (@($v.Endpoints) -join ', ')) | Out-Null
    if ($v.Installed -and $v.CredentialPresent) {
        # The viewer's own diagnostics, not this provisioner's view of the hub: head 6 passed every check here while
        # its viewer asked 192.0.2.1 and showed "disconnected".
        $vd = Get-LabViewerDiagnostics -OutFile (Join-Path $Run.Folder 'viewer-diagnostics.txt')
        $blocked = ($vd -and $vd.PSObject.Properties.Name -contains 'Blocked' -and $vd.Blocked)
        if ($blocked) { Add-LabVerification -Check 'Arena viewer connects to the hub (its own diagnostics)' -Status 'FAIL' -Detail $vd.Detail | Out-Null }
        elseif ($vd) { Add-LabVerification -Check 'Arena viewer connects to the hub (its own diagnostics)' -Status $(if ($vd.Ok) { 'PASS' } else { 'FAIL' }) -Detail ('asks {0}; {1}{2}' -f $vd.Address, $vd.Detail, $(if (-not $vd.Ok -and $vd.Address -ne $Config.arena.endpoint) { '. Fix: Existing machine > 8 points it at ' + $Config.arena.endpoint } else { '' })) | Out-Null }
    }
    $rb = Test-LabRebootCheckpoint -StateDir $Run.StateDir
    Add-LabVerification -Check 'Ollama serves the same models after a reboot' -Status $rb.Status -Detail ($rb.Detail + $(if ($rb.Status -eq 'PENDING') { '. Reboot, sign in, then START-LAB-PROVISIONER > Existing machine > Post-reboot verification' } else { '' })) | Out-Null
}

function Invoke-LabVcRedistStep {
    <#  Microsoft's Visual C++ 2015-2022 runtime (x64) from the provisioner stick, when any of its DLLs is missing.
        Capstone's torch needs it, and the installers of Capstone and of its QMS and ERP services refuse without it.  #>
    param([Parameter(Mandatory)][string]$Who)
    Invoke-LabStep -Id 'vcredist' -Title ("Microsoft Visual C++ runtime ($Who needs it)") -Action {
        $vc = Get-LabVcRuntimeState
        if ($vc.Ok) { return (New-LabOutcome 'UNCHANGED' ('present (version {0})' -f $vc.Version)) }
        $spec = $Config.runtime.vcredist
        $inst = Join-Path $Packages ('runtime\' + [string]$spec.file)
        $chk = Test-LabRuntimeInstaller -Path $inst -Spec $spec
        if (-not $chk.Ok) { throw ('the Visual C++ runtime is missing ({0}) and the installer on the stick did not pass its checks: {1}' -f ($vc.Missing -join ', '), ($chk.Problems -join '; ')) }
        if (-not (Request-LabApproval -Prompt ("$Who cannot start without Microsoft's Visual C++ runtime (missing here: {0}). Install the Microsoft Visual C++ 2015-2022 Redistributable (x64) {1} from the provisioner stick (Microsoft's signed installer, checked)?" -f ($vc.Missing -join ', '), $spec.version) -Word 'APPROVE' -Kind 'privileged')) { throw "the Visual C++ runtime was declined; $Who cannot start without it" }
        $p = Start-Process -FilePath $inst -ArgumentList '/install', '/quiet', '/norestart' -PassThru -Wait
        Register-LabChange -Kind 'app' -Description ("Microsoft Visual C++ 2015-2022 Redistributable (x64) {0} installed (exit {1})" -f $spec.version, $p.ExitCode) -Undo ([pscustomobject]@{ type = 'manual'; instructions = 'Settings > Apps > Microsoft Visual C++ 2015-2022 Redistributable (x64) > Uninstall (other programs may need it)' }) | Out-Null
        $after = Get-LabVcRuntimeState
        if (-not $after.Ok) { throw ('the Visual C++ runtime installer ended with exit {0}, but still missing: {1}{2}' -f $p.ExitCode, ($after.Missing -join ', '), $(if ($p.ExitCode -eq 3010) { ' (Windows asks for a restart: restart, then run this again)' } else { '' })) }
        New-LabOutcome 'DONE' ('installed {0} (exit {1}); all five DLLs present' -f $after.Version, $p.ExitCode)
    }
}

function Invoke-LabBusinessService {
    <#  Capstone's QMS (head 3) or ERP (head 4) service on this head, with its own scripts: the runtime, the release,
        a token (carried to the Capstone laptop on this stick), its LOCAL SERVICE startup task, and one firewall rule
        admitting the Capstone laptop only. Each step says what it keeps; nothing touches Ollama or the models.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][string]$HeadIp)
    $log = Join-Path $Run.Folder ($Spec.Name + '-service.log')
    $label = '{0} ({1})' -f $Spec.Product, $Spec.Label
    if ($HeadIp -ne $Spec.Head) { Invoke-LabStep -Id 'service' -Title $label -Action { throw "$($Spec.Product) belongs on $($Spec.Head); this head is $HeadIp" } | Out-Null; return }
    Invoke-LabVcRedistStep -Who $Spec.Product | Out-Null
    Invoke-LabStep -Id 'service-install' -Title ("$label release (its own installer or updater)") -Action {
        $zip = Find-LabServicePackage -Folder (Join-Path $Packages 'services') -Spec $Spec
        $st = Get-LabServiceState -Spec $Spec
        $want = [string]$Spec.Approved.version
        if ($st.Installed -and $st.Version -eq $want) { return (New-LabOutcome 'UNCHANGED' ("{0} {1} installed (the approved release)" -f $Spec.Product, $st.Version)) }
        if (-not $zip) { Add-LabPending -Area 'service' -Text "no $($Spec.Product) package on this stick (packages\services)"; return (New-LabOutcome 'PENDING' 'no package') }
        if ((Split-Path -Leaf $zip.FullName) -ne [string]$Spec.Approved.file) { Add-LabPending -Area 'service' -Text ("packages\services holds {0}, not the approved {1}: nothing installed" -f $zip.Name, $Spec.Approved.file); return (New-LabOutcome 'PENDING' 'not the approved release') }
        $pkg = Expand-LabServicePackage -Run $Run -Zip $zip.FullName -Spec $Spec
        Write-LabLog ('  {0} {1}: archive SHA-256 approved; {2} files match its manifest' -f $Spec.Product, $pkg.Version, $pkg.Files)
        if ($st.Installed) {
            if (-not $st.Configured -or -not $st.Task.Present) { throw ("{0} {1} is installed but not configured with a startup task, so its updater's health check cannot pass. Ask the Capstone owner how to move it to {2}." -f $Spec.Product, $st.Version, $pkg.Version) }
            if (-not (Request-LabApproval -Prompt ("Update {0} {1} to {2} with its own Update-{0}.ps1? It backs up its data, switches the release and goes back to {1} if its health check fails." -f $Spec.Product, $st.Version, $pkg.Version) -Word 'UPDATE' -Kind 'disruptive')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            $r = Invoke-LabExternalScript -Script (Join-Path $pkg.Root ('tools\Update-{0}.ps1' -f $Spec.Product)) -Arguments @('-PackageRoot', $pkg.Root, '-InstallRoot', $Spec.InstallRoot, '-DataRoot', $Spec.DataRoot, '-TaskName', $Spec.TaskName) -LogFile $log
            if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"updated"\s*:\s*true') { throw ('Update-{0}.ps1 did not complete (exit {1}): {2}' -f $Spec.Product, $r.ExitCode, (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
            return (New-LabOutcome 'DONE' ("{0} updated {1} -> {2} by its own updater; back with Rollback-{0}.ps1" -f $Spec.Product, $st.Version, $pkg.Version))
        }
        if (-not (Request-LabApproval -Prompt ("Install {0} {1} into {2} (data in {3}) with its own Install-{0}.ps1? It does not touch Ollama, the models or the Arena viewer." -f $Spec.Product, $pkg.Version, $Spec.InstallRoot, $Spec.DataRoot) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        $r = Invoke-LabExternalScript -Script (Join-Path $pkg.Root ('tools\Install-{0}.ps1' -f $Spec.Product)) -Arguments @('-InstallRoot', $Spec.InstallRoot, '-DataRoot', $Spec.DataRoot) -LogFile $log
        if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"installed"\s*:\s*true') { throw ('Install-{0}.ps1 failed (exit {1}): {2}' -f $Spec.Product, $r.ExitCode, (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
        Register-LabChange -Kind 'app' -Description ("{0} {1} installed in {2}" -f $Spec.Product, $pkg.Version, $Spec.InstallRoot) -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("Unregister-ScheduledTask '{0}'; delete {1} (its data stays in {2})" -f $Spec.TaskName, $Spec.InstallRoot, $Spec.DataRoot) }) | Out-Null
        New-LabOutcome 'DONE' ("{0} {1} installed by its own installer" -f $Spec.Product, $pkg.Version)
    } | Out-Null
    Invoke-LabStep -Id 'service-token' -Title ("$label token for the Capstone laptop") -ContinueOnFailure -Action {
        $st = Get-LabServiceState -Spec $Spec
        if (-not $st.Installed) { return (New-LabOutcome 'PENDING' "$($Spec.Product) is not installed") }
        $ho = Get-LabServiceHandoff -UsbRoot $UsbRoot -Name $Spec.Name
        $why = ''
        if ($st.Configured -and $st.TokenFile) {
            if (Test-Path -LiteralPath $ho.Token) { return (New-LabOutcome 'UNCHANGED' ("configured; its token waits on {0} for the Capstone laptop" -f (Get-LabUsbLabel $UsbRoot))) }
            # Already configured, and the laptop presumably took the token. A new one is made only when asked: the
            # laptop's report says "token refused" when the two do not match.
            $ans = Read-LabInput -Prompt ("{0} already has a token, and the Capstone laptop took its copy earlier. Press Enter to keep it. Type NEW-TOKEN only if the Capstone laptop's report says the {1} token was refused" -f $Spec.Product, $Spec.Name.ToUpperInvariant())
            if ($ans -cne 'NEW-TOKEN') { return (New-LabOutcome 'UNCHANGED' 'configured; token kept') }
            $why = ' (replacing the one the Capstone laptop has: Capstone needs the new one too)'
        }
        if (-not (Request-LabApproval -Prompt ("Make a new token for {0}{1}, configure the service with it (its own Configure-{0}.ps1: listens on {2}:{3}), and put the token on {4} for the Capstone laptop? The laptop takes it from the stick and deletes it there." -f $Spec.Product, $why, $Spec.Head, $Spec.Port, (Get-LabUsbLabel $UsbRoot)) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        $t = Set-LabServiceToken -Run $Run -Spec $Spec -UsbRoot $UsbRoot -LogFile $log
        if ($st.Task.Present) { Restart-LabServiceTask -Spec $Spec }
        Add-LabPending -Area 'service' -Text ("take {0} to the Capstone laptop next: START-LAB-PROVISIONER > 4 picks up the {1} token (fingerprint {2}) and deletes it from the stick" -f (Get-LabUsbLabel $UsbRoot), $Spec.Name.ToUpperInvariant(), $t.Fingerprint)
        New-LabOutcome 'DONE' ("configured with a new token (fingerprint {0}); token and client file on {1}" -f $t.Fingerprint, $t.Handoff.Folder)
    } | Out-Null
    Invoke-LabStep -Id 'service-task' -Title ("$label startup task as LOCAL SERVICE (its own script)") -ContinueOnFailure -Action {
        $st = Get-LabServiceState -Spec $Spec
        if (-not $st.Configured) { return (New-LabOutcome 'PENDING' "$($Spec.Product) is not configured (no token yet)") }
        if ($st.Task.Present) {
            if ($st.Task.State -ne 'Running') { Start-ScheduledTask -TaskName $Spec.TaskName; return (New-LabOutcome 'DONE' ("task was {0} (last result 0x{1:X}); started" -f $st.Task.State, $st.Task.LastResult)) }
            return (New-LabOutcome 'UNCHANGED' ("task running as {0}" -f $st.Task.User))
        }
        if (-not (Request-LabApproval -Prompt ("Run {0}'s Register-{0}Task.ps1 (starts it at every boot as LOCAL SERVICE, and now)?" -f $Spec.Product) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        $r = Invoke-LabExternalScript -Script (Join-Path $Spec.InstallRoot ('tools\Register-{0}Task.ps1' -f $Spec.Product)) -Arguments @('-InstallRoot', $Spec.InstallRoot, '-DataRoot', $Spec.DataRoot, '-TaskName', $Spec.TaskName) -LogFile $log
        if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"registered"\s*:\s*true') { throw ('Register-{0}Task.ps1 refused: {1}' -f $Spec.Product, (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
        Register-LabChange -Kind 'app' -Description ("{0} startup task '{1}' registered (LOCAL SERVICE)" -f $Spec.Product, $Spec.TaskName) -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("Unregister-ScheduledTask '{0}'" -f $Spec.TaskName) }) | Out-Null
        New-LabOutcome 'DONE' 'startup task registered and started'
    } | Out-Null
    Invoke-LabStep -Id 'service-firewall' -Title ("$label firewall: TCP $($Spec.Port) from the Capstone laptop $($Spec.Client) only") -ContinueOnFailure -Action {
        $st = Get-LabServiceState -Spec $Spec
        if ((Test-LabServiceRule -Rule $st.Rule -Spec $Spec).Ok) { return (New-LabOutcome 'UNCHANGED' (Test-LabServiceRule -Rule $st.Rule -Spec $Spec).Detail) }
        if (-not (Request-LabApproval -Prompt ("Add the inbound rule '{0}': TCP {1} on {2}, from {3} only?" -f $Spec.RuleDisplay, $Spec.Port, $Spec.Head, $Spec.Client) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        Set-LabServiceFirewall -Run $Run -Spec $Spec
    } | Out-Null
}

function Add-LabBusinessServiceVerification {
    param([Parameter(Mandatory)]$Spec)
    $st = Get-LabServiceState -Spec $Spec
    if (-not $st.Health.Ok -and $st.Task.Present -and $st.Task.State -eq 'Running') { $null = Wait-LabServiceHealth -Spec $Spec -Seconds 30; $st = Get-LabServiceState -Spec $Spec }
    $want = [string]$Spec.Approved.version
    Add-LabVerification -Check ("{0} installed (approved release {1})" -f $Spec.Product, $want) -Status $(if ($st.Installed -and $st.Version -eq $want) { 'PASS' } elseif ($st.Installed) { 'FAIL' } else { 'PENDING' }) -Detail $(if ($st.Installed) { 'active ' + $st.Version } else { 'not installed' }) | Out-Null
    Add-LabVerification -Check ("{0} startup task runs as LOCAL SERVICE" -f $Spec.Product) -Status $(if ($st.Task.Present -and $st.Task.User -match 'LOCAL SERVICE|S-1-5-19' -and $st.Task.State -eq 'Running') { 'PASS' } elseif ($st.Task.Present) { 'FAIL' } else { 'PENDING' }) -Detail $(if ($st.Task.Present) { '{0} as {1}, last result 0x{2:X}' -f $st.Task.State, $st.Task.User, $st.Task.LastResult } else { 'not registered' }) | Out-Null
    Add-LabVerification -Check ("{0} answers its health check on {1}:{2}" -f $Spec.Product, $Spec.Head, $Spec.Port) -Status $(if ($st.Health.Ok) { 'PASS' } elseif ($st.Task.Present) { 'FAIL' } else { 'PENDING' }) -Scope local -Detail $(if ($st.Health.Ok) { 'service ' + $Spec.Name + ', ' + $st.Health.Format } else { 'no answer; ' + (Get-LabServiceLogTail -Spec $Spec) }) | Out-Null
    $rc = Test-LabServiceRule -Rule $st.Rule -Spec $Spec
    $other = @(Get-LabOtherPortRules -Spec $Spec)
    Add-LabVerification -Check ("firewall rules: {0} port {1} admits only the Capstone laptop {2}" -f $Spec.Product, $Spec.Port, $Spec.Client) -Status $(if ($rc.Ok -and -not $other.Count) { 'PASS' } elseif ($st.Rule -or $other.Count) { 'FAIL' } else { 'PENDING' }) -Scope local -Detail ($rc.Detail + $(if ($other.Count) { '; other rules also open it: ' + ($other -join ', ') } else { '' }) + ' (rule analysis)') | Out-Null
    $ho = Get-LabServiceHandoff -UsbRoot $UsbRoot -Name $Spec.Name
    if (Test-Path -LiteralPath $ho.Token) { Add-LabVerification -Check ("{0} token taken by the Capstone laptop" -f $Spec.Product) -Status PENDING -Required $false -Detail ("waiting on {0}: run START-LAB-PROVISIONER > 4 on the Capstone laptop next" -f (Get-LabUsbLabel $UsbRoot)) | Out-Null }
    Add-LabVerification -Check ("the Capstone laptop reaches {0} with its token" -f $Spec.Product) -Status PENDING -Scope lan -Detail 'proven from the laptop: its Verify stage calls the service with the token' | Out-Null
}

# ---------------------------------------------------------------------------
# Role workflows
# ---------------------------------------------------------------------------

function Complete-LabRun {
    param($Run, [bool]$Configured = $true, [bool]$Declined = $false)
    # The hub's own logs come back with the stick on every hub run: errors seen on the hub's screens (HTTP 400, a
    # certificate) are then on the stick, not only on the screen.
    if ($Run.Role -eq 'hub') {
        try {
            $hd = Save-LabHubDiagnostics -Run $Run -UsbRoot $UsbRoot
            # FAIL only for errors written in the last hour: older ones (already diagnosed) are named with their time.
            Add-LabVerification -Check 'no new HTTP 400 or certificate error in the hub''s logs (last hour)' -Status $(if ($hd.Recent) { 'FAIL' } else { 'PASS' }) -Required $false -Detail ('{0}{1}; logs copied: {2}' -f $(if ($hd.Recent) { "$($hd.Recent) log file(s) of the last hour name HTTP 400, a certificate, SSL or TLS; newest: $($hd.Last)" } else { 'none in the last hour' }), $(if (-not $hd.Recent -and $hd.Suspect) { ('; {0} older line(s), newest from {1:yyyy-MM-dd HH:mm}' -f $hd.Suspect, $hd.NewestAt) } else { '' }), $hd.File) | Out-Null
        } catch { Write-LabLog ('  hub diagnostics not saved: ' + $_.Exception.Message) -Level WARN }
    }
    Write-LabStage -Letter 'F' -Title 'Report'
    $r = New-LabReport -Run $Run -Configured $Configured -Declined $Declined
    $colour = @{ 'verified' = 'Green'; 'partially verified' = 'Yellow'; 'blocked' = 'Red'; 'failed' = 'Red'; 'inspection and plan only' = 'Cyan' }[$r.Status]
    Write-Host ''
    Write-Host ('  FINAL STATUS: {0}' -f $r.Status.ToUpperInvariant()) -ForegroundColor $colour
    foreach ($p in @($Run.Pending)) { Write-Host ('   pending [{0}] {1}' -f $p.area, $p.detail) -ForegroundColor Yellow }
    Write-Host ('  Report: {0}' -f $r.Text)
    if (-not $NoExport -and (Test-Path -LiteralPath (Join-Path $UsbRoot 'START-LAB-PROVISIONER.cmd'))) {
        $ans = Read-LabInput -Prompt 'Export a sanitized copy of the report to the USB? (Y/N, Enter = Y)' -Default 'Y'
        if ($ans -match '^[Yy]') { try { Write-Host ('  Exported to {0}' -f (Export-LabReport -Result $r -UsbRoot $UsbRoot)) } catch { Write-Host ('  Export failed: ' + $_.Exception.Message) -ForegroundColor Yellow } }
    }
    return $r
}

function Start-LabStages {
    <#  Stage A and B for any role; returns $null when the run ends there.  #>
    param([string]$RoleName, [scriptblock]$Choices)
    $run = New-LabRun -Role $RoleName -Root $Root -UsbRoot $UsbRoot -Config $Config
    Write-LabStage -Letter 'A' -Title 'Inspect (read-only)'
    $integrity = Test-LabReleaseIntegrity
    if (-not $integrity.Ok -and -not $integrity.Dev) { Write-LabLog ('USB integrity: ' + $integrity.Detail) -Level ERROR; $run.Plan = [pscustomobject]@{ blocked = @('the USB copy is damaged: ' + $integrity.Detail); steps = @() }; Complete-LabRun -Run $run -Configured $false | Out-Null; return $null }
    Write-LabLog ('USB integrity: ' + $integrity.Detail) -Level $(if ($integrity.Ok) { 'OK' } else { 'WARN' })
    $run.Inspection = Invoke-LabInspection -Config $Config -Role $RoleName -UsbRoot $UsbRoot
    Show-LabInspection -Inspection $run.Inspection
    if ($Through -eq 'Inspect' -or $RoleName -eq 'inspect') { Complete-LabRun -Run $run -Configured $false | Out-Null; return $null }
    Write-LabStage -Letter 'B' -Title 'Plan'
    $c = & $Choices $run.Inspection
    if ($null -eq $c) { $run.Plan = [pscustomobject]@{ blocked = @('stopped by the operator before planning'); steps = @() }; Complete-LabRun -Run $run -Configured $false -Declined $true | Out-Null; return $null }
    $catalog = $null; if ($RoleName -in 'cpu-head', 'gpu-head') { $catalog = Get-LabCatalog -Path $CatalogPath }
    $roleForPlan = $RoleName
    $run.Plan = New-LabPlan -Role $roleForPlan -Inspection $run.Inspection -Config $Config -Choices $c -Catalog $catalog
    Show-LabPlan -Plan $run.Plan
    if (@($run.Plan.blocked).Count) { Complete-LabRun -Run $run | Out-Null; return $null }
    if ($Through -eq 'Plan') { Complete-LabRun -Run $run -Configured $false | Out-Null; return $null }
    $work = @($run.Plan.steps | Where-Object { $_.approval -and $_.state -eq 'planned' })
    if ($work.Count -and -not (Request-LabApproval -Prompt ('Carry out this plan on {0}? {1} step(s) change this machine; each change is recorded with its undo.' -f $env:COMPUTERNAME, $work.Count) -Word 'APPROVE' -Kind 'privileged')) {
        Complete-LabRun -Run $run -Declined $true | Out-Null; return $null
    }
    return [pscustomobject]@{ Run = $run; Choices = $c }
}

function Invoke-LabNewHead {
    param([string]$RoleName)
    $s = Start-LabStages -RoleName $RoleName -Choices {
        param($insp)
        $a = Read-LabHeadAddress -Inspection $insp; if (-not $a) { return $null }
        $mode = Read-LabClientMode
        $prop = Get-LabProfileProposal -RamGB $(if ($insp.hardware) { [double]$insp.hardware.TotalMemoryGB } else { 0 }) -VramMB $(if ($insp.hardware) { [int]$insp.hardware.NvidiaVramMB } else { 0 })
        $m = Read-LabModelChoice -Inspection $insp -Default $prop.Profile
        @{ Address = $a; ClientMode = $mode; Profiles = $m.Profiles; ModelsAdd = $m.Add; ModelsExclude = $m.Exclude }
    }
    if (-not $s) { return }
    $run = $s.Run; $plan = $run.Plan; $ip = $plan.address
    Write-LabStage -Letter 'C' -Title 'Configure'
    $net = Invoke-LabStep -Id 'network' -Title 'Lab address' -Action {
        if ($plan.address_verdict.HeldHere) { return (New-LabOutcome 'UNCHANGED' "$ip/24 already set") }
        if (-not (Request-LabApproval -Prompt "Change the Ethernet adapter to $ip/24 now? Network connections on that adapter drop for a moment." -Word 'APPROVE' -Kind 'disruptive')) { throw 'the operator declined the address change' }
        Set-LabHeadNetwork -Run $run -TargetIp $ip
    }
    $alias = ''; try { $alias = (Get-NetIPAddress -IPAddress $ip -ErrorAction Stop | Select-Object -First 1).InterfaceAlias } catch { }
    Invoke-LabStep -Id 'time' -Title 'Time zone and time source' -Action { Set-LabTimeSync -Run $run -Hub $Config.hub -Zone $Config.time_zone } | Out-Null
    Invoke-LabStep -Id 'power' -Title 'Power and sleep' -Action { Set-LabPowerState -Run $run -Adapter $alias } | Out-Null
    Invoke-LabStep -Id 'ollama-install' -Title 'Ollama installation' -Action {
        $exe = (Get-LabServingOllamaExecutable -Root $run.Root -Spec $Config.ollama.runtime)
        if ($exe) { return (New-LabOutcome 'UNCHANGED' "installed at $exe") }
        # 1.6.14: a new head gets the pinned runtime from the stick (config ollama.runtime), never an unspecified latest.
        $pinned = Join-Path (Join-Path $Packages 'ollama') $Config.ollama.runtime.file
        if (Test-Path -LiteralPath $pinned) { $r = Install-LabOllamaRuntime -Archive $pinned -Spec $Config.ollama.runtime; return (New-LabOutcome 'DONE' ('Ollama {0} at {1} ({2}); run HEAD-OLLAMA next to qualify it' -f $Config.ollama.runtime.version, $r.Executable, $r.Method)) }
        $local = Join-Path $Packages 'ollama\OllamaSetup.exe'
        $path = ''; if (Test-Path -LiteralPath $local) { $path = $local } else { $net2 = Test-LabInternet -HostName 'ollama.com'; if (-not $net2.Online) { throw ('no internet to download Ollama and no packages\ollama\OllamaSetup.exe on the USB: ' + $net2.Detail) } }
        $r = Install-CapstoneOllama -Paths (Get-LabEnginePaths -Run $run) -InstallerPath $path
        if (-not $r.Success) { throw $r.Error }
        New-LabOutcome 'DONE' ('Ollama {0} at {1} ({2})' -f $r.Version, $r.Executable, $r.Method)
    } | Out-Null
    Invoke-LabStep -Id 'ollama-service' -Title 'Ollama boot task and model directory' -Action { Set-LabOllamaService -Run $run -Executable (Get-LabServingOllamaExecutable -Root $run.Root -Spec $Config.ollama.runtime) -Bind $Config.ollama.bind -ModelDirectory $plan.model_directory -TaskName $Config.ollama.task_name } | Out-Null
    Invoke-LabStep -Id 'ollama-start' -Title 'Start Ollama from the boot task' -Action { Invoke-LabOllamaStart -Run $run -HeadIp $ip } | Out-Null
    Invoke-LabStep -Id 'firewall' -Title 'Ollama firewall' -Action { Invoke-LabFirewallStep -Run $run -HeadIp $ip -Clients $plan.clients -Alias $alias -ServerPaths @(@(Get-CapstoneOllamaExecutable) + @((Get-LabServingOllamaExecutable -Root $run.Root -Spec $Config.ollama.runtime)) | Where-Object { $_ } | Select-Object -Unique) } | Out-Null
    Write-LabStage -Letter 'D' -Title 'Register'
    $claim = $null
    if (-not $run.Stopped) {
        $claim = Invoke-LabStep -Id 'arena-claim' -Title 'Arena address claim' -ContinueOnFailure -Action { Invoke-LabClaimStep -Run $run -Addr $ip -What 'head' }
        Invoke-LabStep -Id 'viewer' -Title 'Arena head viewer' -ContinueOnFailure -Action { Invoke-LabViewerStep -Run $run -Addr $ip -ClaimData $(if ($claim) { $claim.Data } else { $null }) } | Out-Null
    } else { Add-LabRegistration -Item 'Arena registration' -Status PENDING -Detail 'not attempted: configuration stopped' | Out-Null }
    if (-not $run.Stopped) {
        Invoke-LabHeadVerify -Run $run -HeadIp $ip -Clients $plan.clients -Alias $alias
        Invoke-LabModelStage -Run $run -Inspection $run.Inspection -Choice @{ Profiles = @($plan.profile.Chosen); Add = @($s.Choices.ModelsAdd); Exclude = @($s.Choices.ModelsExclude) }
    }
    Complete-LabRun -Run $run | Out-Null
}

function Read-LabHubMode {
    if ($HubMode) { return @{ HubMode = $HubMode; MigrationTarget = $MigrationTarget } }
    Write-Host ''
    Write-Host '  Administration hub: what to do'
    Write-Host '   1  Machine prerequisites and Agent Arena (address, profile, power, time, firewall, Python, Arena, Arena''s Gateway settings)'
    Write-Host '   2  Inference Gateway 0.4.0: guided installation, or update from 0.3.0 (resumable; each Gateway step runs the Gateway''s own command)'
    Write-Host '   3  Move Arena to a standard Windows account (needed by the Gateway; separately approved)'
    Write-Host '   4  Verify only (read-only)'
    Write-Host '   5  Gateway Models screen: install or update (choose models from the hub from then on, no USB)'
    $c = Read-LabInput -Prompt 'Choose' -Default '4'
    switch ($c) {
        '1' { return @{ HubMode = 'arena' } }
        '2' { return @{ HubMode = 'gateway' } }
        '5' { return @{ HubMode = 'models' } }
        '3' { $t = Read-LabInput -Prompt 'The standard local account that will run Arena (it must already exist)'; if (-not $t) { return $null }; return @{ HubMode = 'migrate'; MigrationTarget = $t } }
        default { return @{ HubMode = 'verify' } }
    }
}

function Invoke-LabHub {
    $s = Start-LabStages -RoleName 'hub' -Choices { param($insp) Read-LabHubMode }
    if (-not $s) { return }
    $run = $s.Run; $plan = $run.Plan; $insp = $run.Inspection; $hubFolder = Join-Path $Payload 'AGENT-ARENA-HUB'; $hs = $Config.hub_setup
    $mode = [string]$plan.hub_mode
    if ($mode -notin 'arena', 'gateway', 'migrate', 'verify', 'models') {
        # Never fall through to Verify after the operator approved changes (1.3.0 on DESKTOP-REDACT10 did).
        Invoke-LabStep -Id 'hub-mode' -Title 'Administration hub choice' -Action { throw ("the plan carries no known hub choice ('{0}'); no step was run. Keep this report for the provisioner owner." -f $mode) } | Out-Null
        Complete-LabRun -Run $run | Out-Null; return
    }
    Write-LabLog ('  hub choice: ' + $mode)
    # Every hub run: is the Gateway's rule for the Capstone laptop in force? (2026-11-11: a restart made the lab network Public.)
    try { $ln = Test-LabHubLabNetwork -HubAddress ([string]$Config.hub); Add-LabVerification -Check 'the lab cable''s network is Private, so the Inference Gateway''s 8443 rule applies (and stays so after a restart)' -Status $ln.Status -Scope local -Detail $ln.Detail | Out-Null } catch { Write-LabLog ('  lab network check: ' + $_.Exception.Message) -Level WARN }
    if ($mode -eq 'gateway') { Invoke-LabGatewayFlow -Run $run; Complete-LabRun -Run $run | Out-Null; return }
    if ($mode -eq 'models') {
        Write-LabStage -Letter 'C' -Title 'Gateway Models screen'
        Invoke-LabStep -Id 'g-models-console' -Title 'Gateway Models screen (install or update)' -Action {
            $svc = Get-LabGatewayService -Name ([string]$Config.gateway.service_name)
            if (-not $svc.Present) { throw 'the Inference Gateway is not installed on this computer: Administration hub > 2 first' }
            $py = [string]$Config.runtime.python.path
            if (-not (Test-Path -LiteralPath $py)) { throw "the machine-wide Python ($py) is missing: Administration hub > 2 installs it" }
            if (-not (Request-LabApproval -Prompt ('Install the Gateway Models screen in C:\Program Files\AgentArenaLab\GatewayModels with a "Gateway Models" shortcut on {0}''s desktop? It changes nothing in the Gateway; it only reads it to check itself.' -f $env:USERNAME) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            $arena = @(); try { $arena = @((Get-LabHubGatewayReadiness -Config $Config).ArenaSids) } catch { }
            $r = Install-LabModelsConsole -Run $run -Source (Join-Path $PSScriptRoot 'hub-tools\gateway-models') -Python $py -ArenaSids $arena
            Add-LabVerification -Check 'Gateway Models screen reads the Gateway (self-check, read-only)' -Status PASS -Detail $r.Detail | Out-Null
            New-LabOutcome $r.Outcome $r.Detail
        } | Out-Null
        # The admin hub's Fleet window (Arena 2.3.0) and its Commit helper, installed with the screen above.
        Invoke-LabStep -Id 'g-fleet' -Title 'Admin hub Fleet window: folders, catalog and the Commit helper' -ContinueOnFailure -Action {
            $py = [string]$Config.runtime.python.path
            $arena = @(); try { $arena = @((Get-LabHubGatewayReadiness -Config $Config).ArenaSids) } catch { }
            if (-not (Request-LabApproval -Prompt 'Set up the admin hub''s Fleet window: C:\ProgramData\AgentArenaLab\fleet (the catalog from this USB; the Arena account may write only its Commit requests) and C:\ProgramData\AgentArenaLab\model-library (administrators only)? It reads the Gateway once to publish its state; it changes nothing in the Gateway or on the heads.' -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            # The operator's plan spreadsheet as imported (config\model-catalog.json), not the 1.0.3 head profiles.
            $r = Install-LabFleet -Run $run -Python $py -Catalog (Join-Path $Release 'config\model-catalog.json') -ArenaSids $arena -Inventory @($Config.head_inventory)
            Add-LabVerification -Check 'Fleet Commit helper publishes the Gateway''s state for the admin hub (read-only)' -Status PASS -Detail $r.Detail | Out-Null
            New-LabOutcome $r.Outcome $r.Detail
        } | Out-Null
        # The Gateway's service account showed on the sign-in screen and looked like an unknown new user (2026-11-07).
        Invoke-LabStep -Id 'g-hide-account' -Title 'Hide the Gateway service account from the sign-in screen' -ContinueOnFailure -Action {
            $acct = [string]$Config.gateway.service_account
            if (-not (Request-LabApproval -Prompt ("Hide {0} from the sign-in and Switch user screens? It is the Inference Gateway's service account (created by Administration hub > 2 on 2026-11-05); the Gateway keeps running as it, and it cannot sign in." -f $acct) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            Set-LabHiddenSignInAccount -Name $acct -Run $run
        } | Out-Null
        $hubArena = $null; try { $hubArena = (Get-LabHubState -Config $Config).Account } catch { }
        # Asked only when the hub runs an older Arena than this stick carries (1.5.3 asked every time, also at 2.3.3).
        $stickArena = ''; try { $m = [regex]::Match([IO.File]::ReadAllText((Join-Path $hubFolder 'app\arena_service\__init__.py')), '__version__\s*=\s*"([^"]+)"'); if ($m.Success) { $stickArena = $m.Groups[1].Value } } catch { }
        $hubVer = ''; try { $hubVer = [string](Get-LabArenaHealth -Endpoint $Config.arena.endpoint -ExpectedServiceId $Config.arena.service_id).Version } catch { }
        if (-not $stickArena -or $hubVer -ne $stickArena) {
            Add-LabPending -Area 'hub' -Text ('Arena on this hub is {2}, this stick has {3}. Arena is updated by its own account: sign in as {0} (Switch user), open this USB in File Explorer, AGENT-ARENA-LAB-{1}\payload\AGENT-ARENA-HUB, double-click INSTALL-ADMIN-HUB.cmd. It keeps the data and the service identity.' -f $(if ($hubArena) { $hubArena } else { 'the Arena account' }), (Get-LabVersion), $(if ($hubVer) { $hubVer } else { 'not answering' }), $(if ($stickArena) { $stickArena } else { 'unknown' }))
        } else { Add-LabVerification -Check 'Arena on this hub is the version on this stick' -Status PASS -Required $false -Detail ('Arena {0}; nothing to install as {1}' -f $hubVer, $(if ($hubArena) { $hubArena } else { 'the Arena account' })) | Out-Null }
        Add-LabPending -Area 'gateway' -Text 'From now on, models and heads are managed in the admin hub: Fleet menu (Heads, Projects, Catalog, Changes), then Commit (one administrator password). "Gateway Models" on this desktop stays as the health view and as a fallback.'
        Complete-LabRun -Run $run | Out-Null; return
    }
    if ($mode -eq 'migrate') {
        Write-LabStage -Letter 'C' -Title 'Move Arena to a standard account'
        Invoke-LabStep -Id 'migrate-export' -Title 'Export Arena for the standard account' -Action {
            if (-not (Request-LabApproval -Prompt ('Stop Arena of {0} now and export it for {1}? The old copy is marked moved and will not start again unless you roll back.' -f $plan.migration.Source.Account, $plan.migration.Target.Name) -Word 'MIGRATE' -Kind 'disruptive')) { throw 'the migration was declined' }
            Invoke-LabArenaMigrationExport -Run $run -Plan $plan.migration -HubFolder $hubFolder -PythonInstaller (Join-Path $Packages ('runtime\' + $Config.runtime.python.file))
        } | Out-Null
        foreach ($t in @($plan.migration.Steps | Select-Object -Skip 1)) { Add-LabPending -Area 'arena-migration' -Text $t }
        Complete-LabRun -Run $run | Out-Null; return
    }
    $hubState = $insp.arena.Hub
    if ($mode -eq 'arena') {
    if (-not ($hubState -and $hubState.ProgramInstalled)) { Add-LabWarning ('The Arena hub is not installed here. A fresh install creates a NEW service identity; the fleet''s identity ' + $Config.arena.service_id + ' lives in the current hub''s data.') }
    Write-LabStage -Letter 'C' -Title 'Configure'
    # A. The settings of the field-verified SET-UP-THIS-COMPUTER, each recorded with its undo.
    Invoke-LabStep -Id 'hub-network' -Title 'Hub address' -Action {
        if ($plan.address_verdict.HeldHere) { return (New-LabOutcome 'UNCHANGED' "$($Config.hub)/24 already set") }
        if (-not (Request-LabApproval -Prompt "Change the lab Ethernet adapter to $($Config.hub)/24 now? Connections on that adapter drop for a moment." -Word 'APPROVE' -Kind 'disruptive')) { throw 'the operator declined the address change' }
        Set-LabHeadNetwork -Run $run -TargetIp $Config.hub
    } | Out-Null
    $alias = ''; try { $alias = (Get-NetIPAddress -IPAddress $Config.hub -ErrorAction Stop | Select-Object -First 1).InterfaceAlias } catch { }
    Invoke-LabStep -Id 'hub-profile' -Title 'Lab adapter network profile' -ContinueOnFailure -Action { if (-not $alias) { return (New-LabOutcome 'PENDING' "no adapter holds $($Config.hub)") }; Set-LabNetworkProfile -Run $run -Alias $alias -Category $hs.network_category } | Out-Null
    Invoke-LabStep -Id 'hub-power' -Title 'Stay awake' -Action { Set-LabPowerState -Run $run -Adapter $alias -StayOn } | Out-Null
    Invoke-LabStep -Id 'hub-time' -Title 'Time server for the lab' -Action { Set-LabTimeServer -Run $run -Zone $Config.time_zone -Upstream $hs.time_upstream } | Out-Null
    Invoke-LabStep -Id 'hub-firewall' -Title 'Hub firewall prerequisites' -Action {
        $a = Set-LabHubFirewallPrerequisites -Run $run -Lan $Config.subnet
        $b = Set-LabRemoteDesktopScope -Run $run -From $hs.remote_desktop_from
        New-LabOutcome $(if ($a.Outcome -eq 'DONE' -or $b.Outcome -eq 'DONE') { 'DONE' } else { 'UNCHANGED' }) ($a.Detail + '; ' + $b.Detail)
    } | Out-Null
    Invoke-LabStep -Id 'hub-python' -Title 'Python 3.12+ for the Arena installer' -ContinueOnFailure -Action {
        $py = Get-LabPython312
        if ($py.Found) { return (New-LabOutcome 'UNCHANGED' ('{0} at {1} ({2}, for {3})' -f $py.Version, $py.Executable, $py.Command, $py.Account)) }
        Write-LabLog ('  ' + $py.Reason) -Level WARN
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Add-LabPending -Area 'hub' -Text ('Python 3.12+ is missing and winget is not available: install it from python.org for this user (Add to PATH). ' + $py.Reason); return (New-LabOutcome 'PENDING' $py.Reason) }
        if (-not (Request-LabApproval -Prompt 'Install Python 3.12 for this user with winget (needs internet)?' -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        & winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements | Out-Null
        Add-LabPending -Area 'hub' -Text ('winget finished (exit {0}); open a new window and check py -3 --version, then run the hub role again' -f $LASTEXITCODE)
        New-LabOutcome 'PENDING' 'Python installed by winget; takes effect in a new window'
    } | Out-Null
    Invoke-LabStep -Id 'hub-install' -Title 'Arena event service and admin window (existing installer)' -ContinueOnFailure -Action {
        # Arena installs per user. Once it has moved to its standard account (Administration hub > 3), the
        # installer must run as THAT account: run here, as the maintenance administrator, it would set up a
        # second Arena (and start entry) for this administrator next to the moved copy.
        $idn = Get-LabArenaIdentity
        $live = Select-LabActiveArenaInstall -Installed @($idn.Installed) -RunningSids @(@($idn.Running) | ForEach-Object { $_.Sid })
        $mine = @(@($idn.Installed) | Where-Object { $_.Sid -eq $idn.Current.Sid }) | Select-Object -First 1
        if ($live -and $live.Sid -ne $idn.Current.Sid) { Add-LabPending -Area 'hub' -Text ('Arena runs as {0}, not as the signed-in {1}: to update it, sign in as {0} and run AGENT-ARENA-HUB\INSTALL-ADMIN-HUB.cmd from this USB (it keeps the data and the service identity)' -f $live.Account, $idn.Current.Account); return (New-LabOutcome 'PENDING' ('Arena belongs to {0}; not installed for {1}' -f $live.Account, $idn.Current.Account)) }
        if ($mine -and $mine.Moved) { Add-LabPending -Area 'hub' -Text ('the Arena data of {0} is marked moved ({1}): finish the migration as the target account, or roll that run back' -f $mine.Account, $mine.MovedReason); return (New-LabOutcome 'PENDING' 'this account''s Arena was moved to another account') }
        $py = Get-LabPython312
        if (-not $py.Found) { return (New-LabOutcome 'PENDING' $py.Reason) }
        if (-not (Request-LabApproval -Prompt ('Run INSTALL-ADMIN-HUB as the signed-in user {0}, not elevated, with Python {1} ({2})? It checks first and keeps the data and service identity.' -f $py.Account, $py.Version, $py.Executable) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'SKIPPED' 'operator skipped the hub install') }
        $how = Start-LabPayloadInstaller -Run $run -CmdFile (Join-Path $hubFolder 'INSTALL-ADMIN-HUB.cmd') -Python $py
        Write-LabLog "  The hub installer window opened ($how)." -Level WARN
        if (-not (Wait-LabOperator -What 'the hub installer')) { return (New-LabOutcome 'PENDING' 'the operator did not confirm the hub installer finished') }
        New-LabOutcome 'DONE' 'hub installer finished; checked below'
    } | Out-Null
    # C. Arena's own side of the observation: configuration only, never the Gateway's files.
    Invoke-LabStep -Id 'arena-gateway-config' -Title 'Arena Gateway observation configuration' -ContinueOnFailure -Action {
        # Arena installs per user: the configuration is in each Arena account's own profile.
        $paths = @(Get-LabArenaConfigPaths)
        if (-not $paths.Count) { return (New-LabOutcome 'PENDING' 'no Arena configuration found on this computer (install the hub first)') }
        $res = @(foreach ($p in $paths) { $r = Set-LabArenaGatewayConfig -Run $run -Config $Config -Path $p.Path; '{0}: {1}' -f $p.Account, $r.Outcome })
        New-LabOutcome $(if (@($res | Where-Object { $_ -match 'DONE' }).Count) { 'DONE' } else { 'UNCHANGED' }) ($res -join '; ')
    } | Out-Null
    Invoke-LabStep -Id 'gateway' -Title 'Inference Gateway (separate choice: Administration hub > 2)' -ContinueOnFailure -Action {
        # Gateway 0.3.0 is carried on this USB with its deployment manifest; it is installed by its own guided
        # choice, never as a side effect of this one.
        $zip = Find-LabGatewayPackage -Folder (Join-Path $Packages 'gateway')
        if (-not $zip) { Add-LabPending -Area 'gateway' -Text 'no inference-gateway-*.zip in packages\gateway on this USB'; return (New-LabOutcome 'PENDING' 'no Gateway release on this USB') }
        $svc = Get-LabGatewayService -Name ([string]$Config.gateway.service_name)
        if ($svc.Present) { return (New-LabOutcome 'UNCHANGED' ('InferenceGateway service {0} as {1}; managed by Administration hub > 2' -f $svc.State, $svc.Account)) }
        $acct = (Get-LabHubGatewayReadiness -Config $Config).Accounts
        Add-LabPending -Area 'gateway' -Text ('Inference Gateway: {0} is on this USB; install it with Administration hub > 2 (guided, each step separately approved).' -f $zip.Name)
        if ($acct -and $acct.State -eq 'BLOCKED') { Add-LabPending -Area 'gateway' -Text ('before Administration hub > 2 can run: ' + ($acct.Blockers -join '; ') + '. Administration hub > 3 moves Arena to an existing standard account (separately approved); no account was changed.') }
        New-LabOutcome 'PENDING' ('{0} on this USB; installed by Administration hub > 2' -f $zip.Name)
    } | Out-Null
    Write-LabStage -Letter 'D' -Title 'Register'
    Add-LabRegistration -Item 'Hub machine in Arena' -Status NOT_APPLICABLE -Detail 'the hub is the registry; admin addresses .250-.254 are not displays' | Out-Null
    Add-LabRegistration -Item 'Heads in the Inference Gateway' -Status PENDING -Detail 'enrolled by the Gateway''s own bootstrap (Administration hub > Inference Gateway guided installation); this mode only exports the verified inventory' | Out-Null
    }
    $alias = ''; try { $alias = (Get-NetIPAddress -IPAddress $Config.hub -ErrorAction Stop | Select-Object -First 1).InterfaceAlias } catch { }
    Write-LabStage -Letter 'E' -Title 'Verify'
    $h = Get-LabHubState -Config $Config
    $hd = { param($x) '{0}: {1} {2} {3}{4}' -f $x.Endpoint, $x.Verdict, $x.ServiceId, $x.Version, $(if ($x.Verdict -ne 'ok' -and $x.Error) { ' (' + $x.Error + ')' } else { '' }) }
    # Arena binds the address its aes.config.json names (the hub installer writes host=192.0.2.254), so the
    # local check probes that address; 127.0.0.1:8790 is refused there by design.
    Add-LabVerification -Check 'Arena event service answers where its configuration binds it, with the fleet identity' -Status $(if ($h.LocalHealth.Verdict -eq 'ok') { 'PASS' } else { 'FAIL' }) -Detail ('{0}; {1} of {2}' -f (& $hd $h.LocalHealth), $(if ($h.ConfigPresent) { $h.ConfigPath } else { 'no aes.config.json (defaults 127.0.0.1:8790)' }), $h.Account) | Out-Null
    Add-LabVerification -Check "Arena event service answers on the lab endpoint $($Config.hub):8790 with the fleet identity" -Status $(if ($h.HubHealth.Verdict -eq 'ok') { 'PASS' } else { 'FAIL' }) -Detail (& $hd $h.HubHealth) | Out-Null
    Add-LabVerification -Check 'event service firewall rule (TCP 8790 from the lab)' -Status $(if ($h.FirewallRule) { 'PASS' } else { 'FAIL' }) | Out-Null
    $ts = Get-LabTimeServerState
    Add-LabVerification -Check 'this computer serves time to the lab' -Status $(if (Test-LabTimeServerCompliant -State $ts -Zone $Config.time_zone -Upstream $hs.time_upstream) { 'PASS' } else { 'FAIL' }) -Detail ('{0}; source {1}; announce {2}; server provider {3}; service {4}' -f $ts.TimeZone, $ts.NtpServer, $ts.AnnounceFlags, $ts.ServerEnabled, $ts.Service) | Out-Null
    # B. Outbound policy, then real connections to every known head.
    $gwPy = [string]$Config.runtime.python.path; $gwSvc = [string]$Config.gateway.service_name
    $ob = $null; try { $ob = Get-LabOutboundVerdict -Snapshot (Get-LabOutboundSnapshotLive) -Heads @($Config.known_heads) -Port ([int]$Config.ollama.port) -InterfaceAlias $alias -Programs @($gwPy) -ServiceName $gwSvc } catch { }
    if ($ob) {
        # BLOCKED is a failure. UNCERTAIN (a block rule Windows decides by program path or network keyword) is
        # left to the proof: the Gateway's own heads command connects from its own process.
        $bad = @($ob.Heads | Where-Object { $_.Verdict -eq 'BLOCKED' }); $unsure = @($ob.Heads | Where-Object { $_.Verdict -eq 'UNCERTAIN' })
        Add-LabVerification -Check 'outbound policy lets the Gateway open TCP 11434 to the heads (rule analysis)' -Status $(if ($bad.Count) { 'FAIL' } elseif ($unsure.Count) { 'PENDING' } else { 'PASS' }) `
            -Detail $(if ($bad.Count -or $unsure.Count) { (@($bad + $unsure) | ForEach-Object { '{0} {1}: {2}' -f $_.Head, $_.Verdict, $_.Reason }) -join '; ' } else { 'default outbound {0} on {1}; no block rule applies to {2} (service {3})' -f $(if ($ob.DefaultOutbound) { $ob.DefaultOutbound } else { 'allow' }), $ob.ActiveProfile, $gwPy, $gwSvc }) | Out-Null
    }
    else { Add-LabVerification -Check 'outbound policy (rule analysis)' -Status PENDING -Detail 'the outbound rules could not be read' | Out-Null }
    $probes = @(Get-LabHeadProbe -Heads @($Config.known_heads) -Port ([int]$Config.ollama.port))
    foreach ($p in $probes) {
        $st = $(if ($p.Version) { 'PASS' } elseif (Test-Connection -ComputerName $p.Head -Count 1 -Quiet -ErrorAction SilentlyContinue) { 'FAIL' } else { 'PENDING' })
        Add-LabVerification -Check ("Gateway host reaches head $($p.Head) Ollama") -Status $st -Scope lan -Required $false -Detail $(if ($p.Version) { 'Ollama {0}, {1} model(s)' -f $p.Version, @($p.Models).Count } elseif ($st -eq 'FAIL') { 'the head answers ping but refuses TCP 11434: run Existing machine > firewall cutover on that head' } else { 'no answer (head off or not on this LAN)' }) | Out-Null
    }
    # D. The verified inventory, for the Gateway operator.
    $inv = New-LabHeadInventory -Probes $probes -Config $Config -From $(if ($alias) { $Config.hub } else { '' }) -Computer $env:COMPUTERNAME
    $file = Export-LabHeadInventory -Run $run -Inventory $inv
    Add-LabVerification -Check 'verified head inventory exported for the Gateway operator' -Status $(if (@($inv.heads).Count) { 'PASS' } else { 'PENDING' }) -Required $false -Detail ('{0} verified, {1} excluded: {2} (provisional format; nothing enrolled)' -f @($inv.heads).Count, @($inv.excluded).Count, $file) | Out-Null
    # C and E. The Gateway observation states and the account prerequisites, kept apart.
    # C and E, stage-aware: what a later stage installs is PENDING until that stage is complete (Gateway
    # installed = its service exists; Gateway complete = its listener rule exists, the sequence's last step).
    $gw = Get-LabHubGatewayReadiness -Config $Config
    $o = $gw.Observation
    $stage = Get-LabHubStageState
    $cfgStep = @($run.Steps | Where-Object { $_.id -eq 'arena-gateway-config' -and $_.status -in 'DONE', 'UNCHANGED' }).Count -gt 0
    Add-LabVerification -Check 'Arena configured to read the Gateway observation' -Status (Get-LabStagedStatus -Ok $o.configured -StageComplete ($cfgStep -or $stage.GatewayComplete)) `
        -Detail $(if ($o.configured) { '{0} (restart the admin console after a change)' -f $Config.gateway.observation_file } elseif ($cfgStep -or $stage.GatewayComplete) { 'not configured for every live Arena account: ' + ((@($gw.Configs) | ForEach-Object { $_.Account }) -join ', ') } else { 'not yet: Administration hub > 1 sets it (step "Arena Gateway observation configuration"), for the account that runs Arena' }) | Out-Null
    $notYet = $(if (-not $stage.GatewayInstalled) { 'pending Gateway installation (Administration hub > 2)' } elseif (-not $stage.GatewayComplete) { 'pending: the Gateway installation has not reached its listener step yet' } else { '' })
    Add-LabVerification -Check 'Gateway observation: file present' -Status (Get-LabStagedStatus -Ok $o.file_present -StageComplete $stage.GatewayComplete) -Scope external -Detail $(if ($o.file_present) { 'present' } elseif ($notYet) { $notYet } else { $o.summary }) | Out-Null
    Add-LabVerification -Check 'Gateway observation: Arena can read it and nothing private (ACL analysis)' -Status (Get-LabStagedStatus -Ok ($o.access -eq 'verified') -StageComplete $stage.GatewayComplete) -Scope external -Detail $(if ($notYet -and $o.access -ne 'verified') { '{0}; {1}' -f $o.access, $notYet } else { $o.access }) | Out-Null
    Add-LabVerification -Check 'Gateway observation: fresh, right contract, publisher running' -Status (Get-LabStagedStatus -Ok $o.live -StageComplete ($stage.GatewayComplete -and $o.configured)) -Scope external -Detail $o.summary | Out-Null
    $rt = Get-LabRuntimeState -Config $Config
    Add-LabVerification -Check 'machine-wide Python 3.12.10 and PowerShell 7.4+ for the Gateway' -Status $(if ($rt.Python.Ok -and $rt.Pwsh.Ok) { 'PASS' } else { 'PENDING' }) -Required $false -Detail ('python {0} (base {1}); pwsh {2}' -f $(if ($rt.Python.Version) { $rt.Python.Version } else { 'missing' }), $rt.Python.Base, $(if ($rt.Pwsh.Version) { $rt.Pwsh.Version } else { 'missing' })) | Out-Null
    Add-LabGatewayVerification -Run $run -Stage $stage
    Complete-LabRun -Run $run | Out-Null
}

function Invoke-LabAppLaptop {
    param([string]$RoleName)
    $s = Start-LabStages -RoleName $RoleName -Choices {
        param($insp)
        $c = @{ Address = '' }
        if ($RoleName -eq 'capstone') { $z = Find-LabCapstonePackage -Folder (Join-Path $Packages 'capstone'); if ($z) { $c.CapstonePackage = $z.FullName } }
        $c
    }
    if (-not $s) { return }
    $run = $s.Run; $plan = $run.Plan; $addr = $plan.address; $insp = $run.Inspection
    Write-LabStage -Letter 'C' -Title 'Configure'
    # The lab address first, as for heads: the Gateway, Arena and the claim all need it (2026-11-07: the operator
    # had to type it into Windows Settings, whose fields differ between builds).
    $addrHeld = [bool]$plan.address_verdict.HeldHere
    Invoke-LabStep -Id 'network' -Title 'Lab address' -Action {
        if ($addrHeld) { return (New-LabOutcome 'UNCHANGED' "$addr/24 already set") }
        if (-not (Request-LabApproval -Prompt "Set this laptop's Ethernet adapter to $addr/24 (no gateway, no DNS: the lab has no router)? The lab cable must be plugged in. Wi-Fi is not changed." -Word 'APPROVE' -Kind 'disruptive')) { throw 'the operator declined the address change' }
        Set-LabHeadNetwork -Run $run -TargetIp $addr
    } | Out-Null
    $addrHeld = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $addr }).Count -gt 0
    if ($RoleName -eq 'capstone') {
    # The lab's clock, as on heads: Arena refuses clocks more than 120 s off, and DESKTOP-REDACT11 still kept Pacific time
    # and time.windows.com (unreachable on the lab cable) on 2026-11-07.
    Invoke-LabStep -Id 'time' -Title 'Time zone and time source' -ContinueOnFailure -Action { Set-LabTimeSync -Run $run -Hub $Config.hub -Zone $Config.time_zone } | Out-Null
    # DESKTOP-REDACT11 has Smart App Control in evaluation: once it switches itself on it can block the unsigned parts of
    # Capstone's Python packages and the Arena Ticketing adapter, as it blocked the viewer on head 10.
    Invoke-LabStep -Id 'appcontrol' -Title 'Smart App Control' -ContinueOnFailure -Action {
        $sac = Request-LabSmartAppControlOff -Run $run -IncludeEvaluation -Why 'Once on, it can block unsigned parts of Capstone''s Python packages and of the Arena Ticketing adapter, as it blocked the Arena viewer on head 10.'
        if (-not $sac) { return (New-LabOutcome 'UNCHANGED' ('Smart App Control: ' + (Get-LabAppControlState).SmartAppControl)) }
        New-LabOutcome $(if ($sac.Changed) { 'DONE' } elseif ($sac.Off) { 'UNCHANGED' } else { 'DECLINED' }) $sac.Detail
    } | Out-Null
    } else {
        # Coder's laptop keeps its own clock source and its own security settings (operator, 2026-11-12): measured only.
        Invoke-LabStep -Id 'clock' -Title 'Clock against the hub (measured; nothing changed)' -ContinueOnFailure -Action {
            $o = Get-LabCoderClockOffset -Hub $Config.hub
            if (-not $o.Ok) { Add-LabPending -Area 'coder' -Text ('clock: ' + $o.Detail + '. Arena refuses events more than 120 s off; correct the time in Windows Settings if it is off (this provisioner does not change Coder''s clock)') }
            New-LabOutcome $(if ($o.Ok) { 'UNCHANGED' } else { 'PENDING' }) $o.Detail
        } | Out-Null
    }
    $gwZip = Find-LabGatewayPackage -Folder (Join-Path $Packages 'gateway')
    $gwm = $null; if ($gwZip) { $gwm = Read-LabGatewayManifestFromZip -Zip $gwZip.FullName }
    $usbCa = Join-Path $UsbRoot 'gateway-ca\inference-gateway-ca.pem'
    if ($RoleName -eq 'capstone') {
        $cs = $insp.capstone
        $appr = $Config.capstone.approved_release
        $layout = $null; if ($gwm) { $layout = Get-LabGatewayClientLayout -Manifest $gwm.Manifest -AppId 'capstone' }
        $gwUrl = $(if ($layout) { $layout.Url } else { 'https://{0}:{1}' -f $Config.hub, $Config.gateway.port })
        # Capstone RC.2 ships its own two client bootstraps (capstone, capstone-research) in its release's
        # config folder. F: checks them against the Gateway manifest and hands them to Configure-Capstone.ps1;
        # it writes no client configuration of its own and never a token.
        $capTemplates = {
            $now = Get-LabCapstoneState -Config $Config
            if (-not $now.Installed -or -not $layout) { return $null }
            Get-LabCapstoneBootstrapTemplates -ConfigDir (Join-Path $Config.capstone.install_root ('releases\{0}\application\config' -f $now.Version)) -Layout $layout
        }
        # Capstone 6.0.0-rc.4: QMS on head 3 and ERP on head 4. A service is handed to Capstone only once the laptop
        # holds its token and the service accepts it; otherwise Capstone keeps that data in-process, as before.
        $svcSpecs = @(); if (@($Config.PSObject.Properties.Name) -contains 'business_services') { $svcSpecs = @($Config.business_services.PSObject.Properties.Name | Where-Object { -not $_.StartsWith('_') } | ForEach-Object { Get-LabServiceSpecByName -Config $Config -Name $_ }) }
        $svcWanted = {
            $o = @{}
            foreach ($sp in $svcSpecs) { $cs2 = Get-LabServiceClientState -Spec $sp; if ($cs2.TokenFile -and $cs2.ClientFile -and $cs2.Authorized -eq $true) { $o[$sp.Name] = $sp.LaptopClient } }
            $o
        }
        $capConfigure = {
            $now = Get-LabCapstoneState -Config $Config
            $t = & $capTemplates
            if (-not $t) { return (New-LabOutcome 'PENDING' 'Capstone or the Gateway release is not available') }
            if (-not $t.Ok) { throw ("Capstone's Gateway bootstrap templates do not match the Gateway manifest: " + ($t.Problems -join '; ')) }
            $missing = @()
            foreach ($id in 'capstone', 'capstone-research') { if (-not (Test-Path -LiteralPath $t.Tokens[$id])) { $missing += ('{0} token -> {1}' -f $id, $t.Tokens[$id]) } }
            if (-not (Test-Path -LiteralPath $layout.Ca)) { $missing += ('Gateway CA -> {0} (the gateway-client step copies it from {1})' -f $layout.Ca, (Get-LabUsbLabel $UsbRoot)) }
            if ($missing.Count) {
                Add-LabPending -Area 'capstone' -Text ('carry the two token files issued on the hub (C:\ProgramData\AgentArenaLab\gateway\outbox) on a second USB stick (never the provisioner stick), into C:\ProgramData\Capstone\gateway, delete the hub copies, then run Application laptop again: ' + ($missing -join '; '))
                return (New-LabOutcome 'PENDING' 'Gateway token files not in place')
            }
            $dev = $(if ($now.DeveloperTokenPath) { $now.DeveloperTokenPath } else { [string]$Config.capstone.developer_token_file })
            if (-not (Test-Path -LiteralPath $dev)) {
                # The developer token is the password for Capstone's developer pages. Capstone reads the file as
                # UTF-8 and strips only whitespace, so a byte-order mark (PowerShell 5.1's -Encoding UTF8) would
                # become part of the password. F: writes it itself: typed twice, never shown or logged, no BOM.
                # Configure-Capstone.ps1 then gives LOCAL SERVICE read access, as it does for the token files.
                if (-not (Request-LabApproval -Prompt ("Capstone needs its developer password (for its developer pages) at $dev. Create it now? You type it twice; it is never shown, logged or put on the USB.") -Word 'APPROVE' -Kind 'change')) { Add-LabPending -Area 'capstone' -Text "Capstone developer password file not created at $dev"; return (New-LabOutcome 'PENDING' 'no developer password file') }
                $sec = Read-LabSecret -Prompt 'Capstone developer password (12 or more characters)' -Confirm
                $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
                try {
                    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)
                    if ($plain.Trim().Length -lt 12) { throw 'the developer password must have 12 or more characters; nothing was written' }
                    $dir = Split-Path -Parent $dev
                    New-Item -ItemType Directory -Path $dir -Force | Out-Null
                    & icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
                    if ($LASTEXITCODE) { throw "could not restrict $dir" }
                    [IO.File]::WriteAllText($dev, $plain.Trim(), (New-Object Text.UTF8Encoding $false))
                } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b); $plain = $null; $sec = $null }
                Register-LabChange -Kind 'secret-file' -Description "Capstone developer password file created at $dev (SYSTEM and Administrators; Configure adds LOCAL SERVICE read)" -Undo ([pscustomobject]@{ type = 'manual'; instructions = "delete $dev" }) | Out-Null
            }
            $settings = Join-Path $Config.capstone.data_root 'config\appsettings.json'
            if (Test-Path -LiteralPath $settings) { Copy-Item -LiteralPath $settings -Destination (Join-Path $run.Backups 'capstone-appsettings-before.json') -Force }
            $registered = (Get-LabTicketingState).CredentialPresent
            $regText = $(if ($registered) { ', -ArenaAdapterRegistered' } else { '' })
            # Configure-Capstone.ps1 rewrites appsettings.json whole: the services are passed every time, or dropped.
            $svcArgs = @(); $svcText = ''
            $sw = & $svcWanted
            if ($sw.qms) { $svcArgs += '-QmsServiceConfig', $sw.qms }; if ($sw.erp) { $svcArgs += '-ErpServiceConfig', $sw.erp }
            if ($sw.Count) { $svcText = ('; {0} served by {1}' -f (($sw.Keys | Sort-Object | ForEach-Object { $_.ToUpperInvariant() }) -join ' and '), (($svcSpecs | Where-Object { $sw.ContainsKey($_.Name) } | ForEach-Object { 'head ' + $_.HeadNumber }) -join ' and ')) }
            if (-not (Request-LabApproval -Prompt ('Run Capstone''s Configure-Capstone.ps1 with its capstone and capstone-research bootstraps (gateway mode, -VerifyLocalAddress{0}{1})? It applies its LOCAL SERVICE ACLs to both token files, the CA and the developer token.' -f $regText, $(if ($svcArgs.Count) { ', ' + (($svcArgs | Where-Object { $_ -like '-*' }) -join ' ') } else { '' })) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            $args2 = @('-OperationalGatewayBootstrap', $t.Operational, '-ResearchGatewayBootstrap', $t.Research, '-DeveloperTokenFile', $dev, '-DataRoot', $Config.capstone.data_root, '-VerifyLocalAddress') + $svcArgs; if ($registered) { $args2 += '-ArenaAdapterRegistered' }
            $r = Invoke-LabExternalScript -Script (Join-Path $Config.capstone.install_root 'tools\Configure-Capstone.ps1') -Arguments $args2 -LogFile (Join-Path $run.Folder 'capstone.log')
            if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"configured"\s*:\s*true') { throw ('Configure-Capstone.ps1 refused: ' + (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
            # Capstone reads appsettings.json when it starts: a running Capstone is restarted to use the new settings.
            $restarted = ''
            $tk = $null; try { $tk = Get-ScheduledTask -TaskName ([string]$Config.capstone.task_name) -ErrorAction Stop } catch { }
            if ($tk -and [string]$tk.State -eq 'Running') {
                $null = Invoke-LabExternalScript -Script (Join-Path $Config.capstone.install_root 'tools\Stop-Capstone.ps1') -Arguments @('-TaskName', [string]$Config.capstone.task_name) -LogFile (Join-Path $run.Folder 'capstone.log')
                # Up to rc.5, Stop-Capstone.ps1 ends the task's launcher but leaves the Python web process serving the
                # port; rc.6's stops it too, and then this finds nothing.
                $left = Stop-LabCapstoneProcesses -InstallRoot ([string]$Config.capstone.install_root)
                if ($left) { Write-LabLog ("  Stop-Capstone.ps1 left {0} Capstone process(es) running; stopped them so the new settings take effect" -f $left) -Level WARN }
                Start-Sleep -Seconds 2
                Start-ScheduledTask -TaskName ([string]$Config.capstone.task_name)
                $restarted = '; Capstone restarted to use it'
            }
            New-LabOutcome 'DONE' ('Capstone configured by its own script: capstone and capstone-research, Arena registration {0}{1}{2}' -f $(if ($registered) { 'complete' } else { 'external_registration_required' }), $svcText, $restarted)
        }
        # Capstone's torch needs Microsoft's Visual C++ runtime; Capstone's installer neither carries nor checks it.
        Invoke-LabVcRedistStep -Who 'Capstone' | Out-Null
        Invoke-LabStep -Id 'capstone-install' -Title 'Capstone release (its own installer or updater)' -Action {
            $zip = $s.Choices.CapstonePackage
            if (-not $zip) {
                if ($cs -and $cs.Installed) { return (New-LabOutcome 'UNCHANGED' ('Capstone {0} installed; no release package on this USB' -f $cs.Version)) }
                Add-LabPending -Area 'capstone' -Text 'no Capstone release package on this USB (packages\capstone)'; return (New-LabOutcome 'PENDING' 'no package')
            }
            if ($appr -and (Split-Path -Leaf $zip) -ne [string]$appr.file) { Add-LabPending -Area 'capstone' -Text ('packages\capstone holds {0}, not the approved {1}: nothing installed' -f (Split-Path -Leaf $zip), $appr.file); return (New-LabOutcome 'PENDING' 'not the approved release') }
            if ($cs -and $cs.Installed -and $appr -and $cs.Version -eq [string]$appr.version) { return (New-LabOutcome 'UNCHANGED' ('Capstone {0} installed (the approved release)' -f $cs.Version)) }
            $pkg = Expand-LabCapstonePackage -Run $run -Zip $zip -ExpectedFormat $Config.capstone.package_format -ExpectedSha256 $(if ($appr) { [string]$appr.sha256 } else { '' })
            if ($appr -and $pkg.Version -ne [string]$appr.version) { throw ('the package manifest says {0}, not the approved {1}' -f $pkg.Version, $appr.version) }
            Write-LabLog ('  Capstone {0}: archive SHA-256 approved; {1} files match its manifest; compatibility {2}' -f $pkg.Version, $pkg.Files, ($pkg.Compatibility | ConvertTo-Json -Compress))
            if ($cs -and $cs.Installed) {
                if ($cs.Version -eq $pkg.Version) { return (New-LabOutcome 'UNCHANGED' ('Capstone {0} installed' -f $cs.Version)) }
                # Capstone's updater keeps ProgramData (both databases backed up first), switches its pointer and runs
                # its own health check, which needs both Gateway identities to authenticate; on failure it restores the
                # previous release and data itself. So it runs only where that check can pass.
                $pre = @()
                if (-not ($cs.Task -and $cs.Task.Present)) { $pre += 'its startup task is not registered (the updater stops and restarts it)' }
                $ready = Test-LabCapstoneBootstraps -Operational $cs.Gateway -Research $cs.ResearchGateway -Url $gwUrl
                if (-not $ready.Ok) { $pre += ('the installed release is not configured with both Gateway identities (' + ($ready.Problems -join '; ') + '), so the updater''s health check would fail and roll back') }
                if ($pre.Count) { Add-LabPending -Area 'capstone' -Text ('Capstone {0} not updated to {1}: {2}. How this installation moves to {1} is the Capstone owner''s decision for a maintenance window.' -f $cs.Version, $pkg.Version, ($pre -join '; ')); return (New-LabOutcome 'PENDING' ('update {0} -> {1} not ready' -f $cs.Version, $pkg.Version)) }
                # The 1.5.0 attempt on DESKTOP-REDACT11 rolled back and copied the databases back under a running Capstone:
                # SQLite's own check first.
                $dbc = Test-LabCapstoneDatabase -Config $Config -WorkDir $run.Folder
                Write-LabLog ('  operations.db ' + $dbc.Detail)
                if (-not $dbc.Ok) { throw ("operations.db does not pass SQLite's integrity check ({0}); nothing was updated. Its backups are in {1}\backups (newest: operations-before-...db)" -f $dbc.Detail, $Config.capstone.data_root) }
                # An updater that waits for Capstone's own address, stops before a rollback and stops every process left
                # running from the install folder is run as shipped (rc.6: the sweep is in its Stop-Capstone.ps1). rc.5's
                # asks 127.0.0.1, where Capstone does not listen, and rc.4's waited 5 s: each gets the lab's exact edit
                # in a copy that runs from the run folder against the untouched, verified package.
                $updText = [IO.File]::ReadAllText((Join-Path $pkg.Root 'tools\Update-Capstone.ps1'))
                $stopFile = Join-Path $pkg.Root 'tools\Stop-Capstone.ps1'
                $stopText = if (Test-Path -LiteralPath $stopFile) { [IO.File]::ReadAllText($stopFile) } else { '' }
                $fixed = Test-LabCapstoneUpdaterFixed -Text $updText -StopText $stopText
                if (-not (Request-LabApproval -Prompt ('Update Capstone {0} to {1} with its own Update-Capstone.ps1? It stops Capstone, backs up operations.db and ai_configuration.db, and restores both and the previous release if its health check fails. It waits up to 4 minutes for Capstone to start{2}. History, settings and tokens in C:\ProgramData\Capstone are kept.' -f $cs.Version, $pkg.Version, $(if ($fixed) { '' } else { ' (the lab points that wait at Capstone''s own address)' })) -Word 'UPDATE' -Kind 'disruptive')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
                if ($fixed) {
                    $upd = Join-Path $pkg.Root 'tools\Update-Capstone.ps1'
                    Write-LabLog "  Capstone's own updater, as shipped: $upd"
                } else {
                    $upd = Join-Path $run.Folder 'Update-Capstone.lab.ps1'
                    [IO.File]::WriteAllText($upd, (ConvertTo-LabPatchedCapstoneUpdate -Text $updText), (New-Object Text.UTF8Encoding $true))
                    Write-LabLog "  Capstone's updater, its health wait pointed at Capstone's own address (4 minutes; stop before rollback): $upd"
                }
                $r = Invoke-LabExternalScript -Script $upd -Arguments @('-PackageRoot', $pkg.Root, '-InstallRoot', $Config.capstone.install_root, '-DataRoot', $Config.capstone.data_root, '-TaskName', $Config.capstone.task_name) -LogFile (Join-Path $run.Folder 'capstone.log')
                if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"updated"\s*:\s*true') { throw ('Update-Capstone.ps1 did not complete (exit {0}; on a failed health check it restores the previous release and data itself): {1}' -f $r.ExitCode, (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
                return (New-LabOutcome 'DONE' ('Capstone updated {0} -> {1} by its own updater; return with Rollback-Capstone.ps1 -Version {0}' -f $cs.Version, $pkg.Version))
            }
            # rc.5 and rc.6 no longer carry payload\models (no local encoder: Gateway only), but their installer still
            # copies payload\models\huggingface unless the data folder already has one, so a fresh install stops there.
            # An empty folder makes it skip that copy; nothing reads it.
            if (-not (Test-Path -LiteralPath (Join-Path $pkg.Root 'payload\models\huggingface'))) {
                $hf = Join-Path $Config.capstone.data_root 'models\huggingface'
                if (-not (Test-Path -LiteralPath $hf)) { New-Item -ItemType Directory -Path $hf -Force | Out-Null; Write-LabLog "  the package has no payload\models (Gateway only); made the empty $hf its installer checks for, so it does not stop copying it" }
            }
            $r = Invoke-LabExternalScript -Script (Join-Path $pkg.Root 'tools\Install-Capstone.ps1') -Arguments @('-InstallRoot', $Config.capstone.install_root, '-DataRoot', $Config.capstone.data_root) -LogFile (Join-Path $run.Folder 'capstone.log')
            if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"installed"\s*:\s*true') { throw ('Install-Capstone.ps1 failed (exit {0}): {1}' -f $r.ExitCode, (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
            New-LabOutcome 'DONE' ('Capstone {0} installed by its own installer' -f $pkg.Version)
        } | Out-Null
        # Before Configure: Capstone refuses a bootstrap whose CA or token file is missing.
        Invoke-LabStep -Id 'gateway-client' -Title 'Gateway client folder and CA (no token, no client configuration)' -ContinueOnFailure -Action {
            if (-not $layout) { return (New-LabOutcome 'PENDING' 'no Gateway release on this USB') }
            $st = Get-LabGatewayClientState -Layout $layout -UsbCa $usbCa
            $folderAcl = $null
            if (Test-Path -LiteralPath $layout.Folder) { $sids = Get-LabAclAllowSids -Path $layout.Folder; if ($sids -isnot [string]) { $folderAcl = Test-LabTokenFileAcl -AllowSids $sids -ReaderSid $(if (Resolve-LabSid $layout.Reader) { Resolve-LabSid $layout.Reader } else { 'S-1-5-19' }) } }
            if ($st.CaPresent -and $st.CaMatchesUsb -eq $true -and $folderAcl -and $folderAcl.Ok) { return (New-LabOutcome 'UNCHANGED' ('{0}: CA identical to the provisioner stick''s, {1} read, nobody broad' -f $layout.Folder, $layout.Reader)) }
            if (-not (Request-LabApproval -Prompt ('Prepare {0} (SYSTEM/Administrators full, {1} read) and copy the Gateway CA from {2}? No token and no client configuration is written: Capstone supplies its own two bootstraps.' -f $layout.Folder, $layout.Reader, (Get-LabUsbLabel $UsbRoot)) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            Set-LabGatewayClientFiles -Run $run -Layout $layout -Manifest $gwm.Manifest -UsbCa $usbCa -NoClientConfig
        } | Out-Null
        # The QMS and ERP tokens that heads 3 and 4 put on this stick: taken into ProgramData\Capstone\services and
        # deleted from the stick, then each service is called with its token from here.
        if ($svcSpecs.Count) {
            Invoke-LabStep -Id 'business-clients' -Title 'QMS (head 3) and ERP (head 4) tokens from the provisioner stick' -ContinueOnFailure -Action {
                $notes = @(); $any = $false; $wait = @()
                foreach ($sp in $svcSpecs) {
                    $c0 = Get-LabServiceClientState -Spec $sp -UsbRoot $UsbRoot
                    $nm = $sp.Name.ToUpperInvariant()
                    if ($c0.OnStick) {
                        if (-not (Request-LabApproval -Prompt ("Take the {0} token that head {1} put on {2} into {3}? It is deleted from the stick afterwards." -f $nm, $sp.HeadNumber, (Get-LabUsbLabel $UsbRoot), (Split-Path -Parent $sp.LaptopToken)) -Word 'APPROVE' -Kind 'change')) { $notes += "$nm token left on the stick (declined)"; continue }
                        $fp = Receive-LabServiceHandoff -Run $run -Spec $sp -UsbRoot $UsbRoot
                        $any = $true
                        $c0 = Get-LabServiceClientState -Spec $sp
                        $notes += ('{0} token taken (fingerprint {1}), deleted from the stick' -f $nm, $fp)
                    }
                    if (-not $c0.TokenFile) {
                        $wait += $nm
                        Add-LabPending -Area 'capstone' -Text ("{0}: no token on this laptop yet. On head {1}: START-LAB-PROVISIONER > 6 Existing machine > 10, then bring the stick back here and run 4 again (until then Capstone keeps {0} in-process)" -f $nm, $sp.HeadNumber)
                    } elseif ($c0.Authorized -eq $true) { $notes += ('{0} on head {1} accepts the token ({2})' -f $nm, $sp.HeadNumber, $c0.Detail) }
                    elseif ($c0.Authorized -eq $false) {
                        $notes += ('{0} REFUSED the token ({1})' -f $nm, $c0.Detail)
                        Add-LabPending -Area 'capstone' -Text ("{0} token refused by head {1}. On head {1}: START-LAB-PROVISIONER > 6 Existing machine > 10, type NEW-TOKEN, then bring the stick back here and run 4 again" -f $nm, $sp.HeadNumber)
                    } else {
                        $notes += ('{0}: {1}' -f $nm, $c0.Detail)
                        Add-LabPending -Area 'capstone' -Text ("{0} on head {1} does not answer ({2}): on head {1} run START-LAB-PROVISIONER > 6 Existing machine > 1 to see why" -f $nm, $sp.HeadNumber, $c0.Detail)
                    }
                }
                $o = $(if ($any) { 'DONE' } elseif ($wait.Count -eq $svcSpecs.Count) { 'PENDING' } else { 'UNCHANGED' })
                New-LabOutcome $o ($notes -join '; ')
            } | Out-Null
        }
        Invoke-LabStep -Id 'capstone-configure' -Title 'Capstone Gateway clients capstone and capstone-research, and the QMS and ERP heads (its own Configure-Capstone.ps1)' -ContinueOnFailure -Action {
            $now = Get-LabCapstoneState -Config $Config
            if (-not $now.Installed) { return (New-LabOutcome 'PENDING' 'Capstone is not installed') }
            if (-not $layout) { return (New-LabOutcome 'PENDING' 'no Gateway release on this USB (its manifest declares the client layout)') }
            $ready = Test-LabCapstoneBootstraps -Operational $now.Gateway -Research $now.ResearchGateway -Url $layout.Url -CaPath $layout.Ca
            $recordStale = ($now.ArenaRegistration -ne 'complete' -and (Get-LabTicketingState).CredentialPresent)
            # Each service Capstone should reach on its head, and only those, must be in its settings.
            $sw = & $svcWanted
            $svcStale = @($svcSpecs | Where-Object { [bool]$sw.ContainsKey($_.Name) -ne [bool]$now.Services.ContainsKey($_.Name) } | ForEach-Object { $_.Name.ToUpperInvariant() })
            if ($now.InferenceMode -eq 'gateway' -and $ready.Ok -and $now.DeveloperTokenFile -eq 'present' -and -not $recordStale -and -not $svcStale.Count) { return (New-LabOutcome 'UNCHANGED' ('gateway mode; capstone and capstone-research complete; Arena registration {0}; QMS/ERP on heads: {1}' -f $now.ArenaRegistration, $(if ($now.Services.Count) { ($now.Services.Keys | Sort-Object | ForEach-Object { $_.ToUpperInvariant() }) -join ', ' } else { 'none (in-process)' }))) }
            & $capConfigure
        } | Out-Null
        Invoke-LabStep -Id 'capstone-task' -Title 'Capstone startup task as LOCAL SERVICE (its own script)' -ContinueOnFailure -Action {
            $now = Get-LabCapstoneState -Config $Config
            if ($now.Task -and $now.Task.Present) {
                # A task that ended with an error (DESKTOP-REDACT11: Ready, last result 0x1, after its 3 retries) is started
                # again, so a fix made in this run (the Visual C++ runtime) takes effect now.
                $t = $null; $i = $null; try { $t = Get-ScheduledTask -TaskName ([string]$Config.capstone.task_name) -ErrorAction Stop; $i = $t | Get-ScheduledTaskInfo } catch { }
                if ($t -and [string]$t.State -ne 'Running' -and $i -and [int64]$i.LastTaskResult -ne 0) {
                    Start-ScheduledTask -TaskName ([string]$Config.capstone.task_name)
                    return (New-LabOutcome 'DONE' ('task had stopped (last result 0x{0:X}); started again as {1}' -f [int64]$i.LastTaskResult, $now.Task.User))
                }
                return (New-LabOutcome 'UNCHANGED' ('task {0} as {1}' -f $now.Task.State, $now.Task.User))
            }
            $ready = Test-LabCapstoneBootstraps -Operational $now.Gateway -Research $now.ResearchGateway -Url $gwUrl
            if (-not ($now.InferenceMode -eq 'gateway' -and $ready.Ok -and $now.DeveloperTokenFile -eq 'present')) { Add-LabPending -Area 'capstone' -Text ('startup task not registered: Capstone fails closed until both Gateway identities, the CA and the developer token are provisioned' + $(if ($ready.Problems.Count) { ' (' + ($ready.Problems -join '; ') + ')' } else { '' })); return (New-LabOutcome 'PENDING' 'Gateway configuration incomplete') }
            if (-not (Request-LabApproval -Prompt 'Run Capstone''s Register-CapstoneTask.ps1 (starts Capstone at machine startup as LOCAL SERVICE)?' -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            $r = Invoke-LabExternalScript -Script (Join-Path $Config.capstone.install_root 'tools\Register-CapstoneTask.ps1') -Arguments @('-InstallRoot', $Config.capstone.install_root, '-DataRoot', $Config.capstone.data_root, '-TaskName', $Config.capstone.task_name) -LogFile (Join-Path $run.Folder 'capstone.log')
            if ($r.ExitCode -ne 0) { throw ('Register-CapstoneTask.ps1 refused: ' + (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
            New-LabOutcome 'DONE' 'Capstone startup task registered and started'
        } | Out-Null
        Invoke-LabStep -Id 'capstone-shortcut' -Title 'Capstone icon on the desktop (opens it in the browser)' -ContinueOnFailure -Action { Set-LabCapstoneShortcut -Config $Config -Run $run } | Out-Null
        # Capstone 6.0.0-rc.5's Research Lab > Heads: the lab's heads, names and hardware (the Gateway decides what each serves).
        Invoke-LabStep -Id 'capstone-lab-heads' -Title 'Capstone Research Lab head list (lab-heads.json: heads 2-10, names and hardware)' -ContinueOnFailure -Action { Set-LabCapstoneLabHeads -Config $Config -Run $run } | Out-Null
    } else {
        # Coder's laptop (2026-11-12): Ethernet and Wi-Fi apart, the Coder account, Remote Desktop over Wi-Fi from the
        # control workstation only, Coder's own Gateway files and token (Lab.Coder.psm1). Coder itself stays PENDING.
        $coderState = $null
        if (-not $run.Stopped) { $coderState = Invoke-LabCoderConfigure -Run $run -Config $Config -Inspection $insp -Address $addr -UsbRoot $UsbRoot -Packages $Packages }
    }
    Write-LabStage -Letter 'D' -Title 'Register'
    $held = $addrHeld
    if ($held) { Invoke-LabStep -Id 'arena-claim' -Title 'Arena address claim' -ContinueOnFailure -Action { Invoke-LabClaimStep -Run $run -Addr $addr -What ("$RoleName/main") } | Out-Null }
    else { Add-LabRegistration -Item "Arena address claim $addr" -Status PENDING -Detail "this laptop does not hold $addr yet" | Out-Null }
    if ($RoleName -eq 'capstone') {
        Invoke-LabStep -Id 'ticketing' -Title 'Arena Ticketing adapter (existing installer)' -ContinueOnFailure -Action {
            $t = Get-LabTicketingState
            $tUsb = Join-Path $Payload 'AGENT-ARENA-TICKETING'
            if ($t.ProgramInstalled -and $t.CredentialPresent) {
                Add-LabRegistration -Item 'Ticketing adapter producer credential' -Status ALREADY_REGISTERED -Detail 'installed with its DPAPI credential' | Out-Null
                # An older adapter does not draw Capstone's QMS and ERP calls on heads 3 and 4: its program files are
                # replaced by the same installer, which keeps the credential when it is given no app code.
                $diff = @(Compare-LabTicketingProgram -UsbFolder $tUsb)
                if (-not $diff.Count) { return (New-LabOutcome 'UNCHANGED' 'installed and enrolled; program files match this stick') }
                if (-not (Request-LabApproval -Prompt ("The Ticketing adapter here is older than the one on this stick ({0} file(s) differ, among them {1}). Update it? It is stopped, INSTALL-TICKETING-ADAPTER replaces its program files and keeps its credential (no app code is asked), then starts it again." -f $diff.Count, (($diff | Select-Object -First 3) -join ', ')) -Word 'UPDATE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'adapter update declined') }
                $py = Get-LabPython312
                if (-not $py.Found) { return (New-LabOutcome 'PENDING' 'no Python 3.12 for this user to run the adapter installer') }
                $stopped = Stop-LabTicketingAdapter
                $how = Start-LabPayloadInstaller -Run $run -CmdFile (Join-Path $tUsb 'INSTALL-TICKETING-ADAPTER.cmd') -Arguments ('--capstone-db "{0}"' -f $Config.capstone.operations_database) -Python $py
                Write-LabLog "  The Ticketing adapter installer opened ($how); it needs no app code this time." -Level WARN
                $null = Wait-LabOperator -What 'the Ticketing adapter installer'
                $left = @(Compare-LabTicketingProgram -UsbFolder $tUsb); $t = Get-LabTicketingState
                return (New-LabOutcome $(if (-not $left.Count -and $t.CredentialPresent) { 'DONE' } else { 'FAILED' }) ('stopped {0} adapter process(es); {1} file(s) still differ; credential {2}; running {3}' -f $stopped, $left.Count, $t.CredentialPresent, $t.Running))
            }
            $py = Get-LabPython312
            if (-not $py.Found) {
                # The adapter's installer needs Python 3.12 for the signed-in user (Capstone's own runtime is private to
                # Capstone). 1.4.5 only reported it missing (DESKTOP-REDACT11, 2026-11-07); the stick carries the official installer.
                $inst = Join-Path $Packages ('runtime\' + [string]$Config.runtime.python.file)
                $chk = Test-LabRuntimeInstaller -Path $inst -Spec $Config.runtime.python
                if (-not $chk.Ok) { Add-LabRegistration -Item 'Ticketing adapter' -Status PENDING -Detail ('needs Python 3.12 for this user; the installer on the stick did not pass its checks: ' + ($chk.Problems -join '; ')) | Out-Null; return (New-LabOutcome 'PENDING' 'no Python 3.12') }
                if (-not (Request-LabApproval -Prompt ("The Arena Ticketing adapter needs Python {0} for {1}. Install it for {1} only, from the provisioner stick (the official python.org installer, its signature checked)? Capstone's own Python is not touched." -f $Config.runtime.python.version, $env:USERNAME) -Word 'APPROVE' -Kind 'change')) { Add-LabRegistration -Item 'Ticketing adapter' -Status PENDING -Detail 'Python for this user declined' | Out-Null; return (New-LabOutcome 'PENDING' 'no Python 3.12') }
                $p = Start-Process -FilePath $inst -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0' -PassThru -Wait
                Register-LabChange -Kind 'app' -Description ("Python {0} installed for {1} (per user, exit {2})" -f $Config.runtime.python.version, $env:USERNAME, $p.ExitCode) -Undo ([pscustomobject]@{ type = 'manual'; instructions = "Settings > Apps > Python $($Config.runtime.python.version) > Uninstall (as $env:USERNAME)" }) | Out-Null
                $py = Get-LabPython312
                if (-not $py.Found) { Add-LabRegistration -Item 'Ticketing adapter' -Status FAILED -Detail ("the Python installer ended with exit {0}, but no Python 3.12 is found for {1}: {2}" -f $p.ExitCode, $env:USERNAME, $py.Reason) | Out-Null; return (New-LabOutcome 'FAILED' 'Python for this user did not install') }
                Write-LabLog ('  Python {0} for {1}: {2}' -f $py.Version, $env:USERNAME, $py.Executable) -Level OK
            }
            $db = $Config.capstone.operations_database
            if (-not (Test-Path -LiteralPath $db)) { Add-LabRegistration -Item 'Ticketing adapter' -Status PENDING -Detail "Capstone's database is not at $db yet" | Out-Null; return (New-LabOutcome 'PENDING' 'no Capstone database') }
            if (-not (Request-LabApproval -Prompt 'Run INSTALL-TICKETING-ADAPTER as the signed-in user? Its window asks for the one-time APP CODE: get it at the admin hub (ArenaSvcUser''s screen): Machines > Machines and codes... > Applications, the Ticketing (capstone) row > App code.' -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            $how = Start-LabPayloadInstaller -Run $run -CmdFile (Join-Path $Payload 'AGENT-ARENA-TICKETING\INSTALL-TICKETING-ADAPTER.cmd') -Arguments ('--capstone-db "{0}"' -f $db) -Python $py
            Write-LabLog "  The Ticketing adapter installer opened ($how)." -Level WARN
            $null = Wait-LabOperator -What 'the Ticketing adapter installer'
            $t = Get-LabTicketingState
            $ok = $t.ProgramInstalled -and $t.CredentialPresent
            Add-LabRegistration -Item 'Ticketing adapter producer credential' -Status $(if ($ok) { 'REGISTERED' } else { 'PENDING' }) -Detail ('installed {0}; credential {1}; running {2}' -f $t.ProgramInstalled, $t.CredentialPresent, $t.Running) | Out-Null
            New-LabOutcome $(if ($ok) { 'DONE' } else { 'PENDING' }) ('installed {0}, credential {1}' -f $t.ProgramInstalled, $t.CredentialPresent)
        } | Out-Null
        # Capstone records the adapter's registration only through its own Configure-Capstone.ps1
        # (-ArenaAdapterRegistered). The adapter is enrolled after Capstone is configured, so this follows it.
        Invoke-LabStep -Id 'capstone-arena-record' -Title 'Capstone''s record of the Arena adapter registration (its own Configure-Capstone.ps1)' -ContinueOnFailure -Action {
            $now = Get-LabCapstoneState -Config $Config
            if (-not $now.Installed -or $now.Settings -ne 'present' -or $now.InferenceMode -ne 'gateway') { return (New-LabOutcome 'PENDING' 'Capstone is not configured yet') }
            if ($now.ArenaRegistration -eq 'complete') { return (New-LabOutcome 'UNCHANGED' 'registration_status complete') }
            if (-not (Get-LabTicketingState).CredentialPresent) { return (New-LabOutcome 'PENDING' 'the Ticketing adapter is not enrolled yet') }
            & $capConfigure
        } | Out-Null
        # Capstone's own Agent Arena room (Ticketing Operations, Agent Arena 2.1.6) for the laptop's second monitor: the
        # same viewer and enrollment as on the heads, on the display the hub registered for this address.
        Invoke-LabStep -Id 'capstone-viewer' -Title 'Agent Arena viewer showing Capstone''s room (for the second monitor)' -ContinueOnFailure -Action {
            $v = Get-LabViewerState; $who = $null; try { $who = Get-LabArenaWhoami -Endpoint $Config.arena.endpoint } catch { }
            if (-not ($v.Installed -and $v.CredentialPresent) -and -not @($who.Displays).Count) {
                Add-LabRegistration -Item 'Arena viewer for Capstone''s room' -Status PENDING -Detail "no display registered for $addr" | Out-Null
                return (New-LabOutcome 'PENDING' ("the hub has no display for $addr yet: admin hub (ArenaSvcUser) > Machines > Add a machine..., address $addr, Ticketing coordinator (orch:capstone/main), Add, then Code; then run this again"))
            }
            if (-not $v.Installed -and -not (Request-LabApproval -Prompt ("Install the Agent Arena viewer for {0}? It shows Capstone's agents in their Ticketing Operations room and opens at sign-in. Move its window to the second monitor and press F11 once: it comes back there, full screen. Next it asks for the display's one-time code (admin hub > Machines, the {1} row > Code)." -f $env:USERNAME, $addr) -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
            Invoke-LabViewerStep -Run $run -Addr $addr -ClaimData $null
        } | Out-Null
    } else { Add-LabRegistration -Item 'Coder Arena publisher' -Status PENDING -Detail 'Coder has no Arena publisher yet (Coder project): its app code is issued in the admin hub only when that publisher can redeem it, under the Coder account (DPAPI)' | Out-Null }
    Write-LabStage -Letter 'E' -Title 'Verify'
    foreach ($r in @(Test-LabDirectOllama -Heads @($Config.known_heads))) {
        Add-LabVerification -Check ("no direct Ollama access to head $($r.Head) from this laptop") -Status $(if ($r.Open) { 'FAIL' } else { 'PASS' }) -Scope lan -Detail $(if ($r.Open) { 'port 11434 accepted a connection' + $(if ($r.Ollama) { ' and Ollama answered' } else { '' }) + ': correct that head''s firewall (Existing machine > firewall)' } else { $r.Detail }) | Out-Null
    }
    $h = Get-LabArenaHealth -Endpoint $Config.arena.endpoint -ExpectedServiceId $Config.arena.service_id
    Add-LabVerification -Check 'Arena event service reachable and genuine' -Status $(if ($h.Verdict -eq 'ok') { 'PASS' } else { 'PENDING' }) -Scope lan -Detail ('{0} {1}' -f $h.Verdict, $h.ServiceId) | Out-Null
    if ($RoleName -eq 'capstone') {
        $c = Get-LabCapstoneState -Config $Config
        $want = $(if ($appr) { [string]$appr.version } else { '' })
        Add-LabVerification -Check ('Capstone installed{0}' -f $(if ($want) { " (approved release $want)" } else { '' })) -Status $(if ($c.Installed -and (-not $want -or $c.Version -eq $want)) { 'PASS' } else { 'PENDING' }) -Detail $(if ($c.Installed) { 'active ' + $c.Version } else { 'not installed' }) | Out-Null
        Add-LabVerification -Check 'Capstone runs in gateway mode (not direct-head development mode)' -Status $(if ($c.InferenceMode -eq 'gateway') { 'PASS' } elseif ($c.InferenceMode) { 'FAIL' } else { 'PENDING' }) -Detail ('inference_mode "{0}" ({1})' -f $c.InferenceMode, $c.ModeSource) | Out-Null
        if ($c.MachineModeVariable -and $c.MachineModeVariable -ne 'gateway') { Add-LabVerification -Check 'no machine-wide override of Capstone''s mode' -Status FAIL -Detail ("CAPSTONE_INFERENCE_MODE=$($c.MachineModeVariable)") | Out-Null }
        $retired = @(Get-LabRetiredTokenVariables)
        if ($retired.Count) { Add-LabVerification -Check 'no retired Gateway token variables at machine scope' -Status FAIL -Required $false -Detail (($retired -join ', ') + ' set (values not read); Gateway 0.3 deployments use token_file only: remove them through their owner') | Out-Null }
        $bt = Test-LabCapstoneBootstraps -Operational $c.Gateway -Research $c.ResearchGateway -Url $gwUrl -CaPath $(if ($layout) { $layout.Ca } else { '' })
        $hard = @($bt.Problems | Where-Object { $_ -match 'embeds|token environment|share one token|scoped to' })
        Add-LabVerification -Check 'Capstone Gateway identities capstone and capstone-research: own bootstrap, own token file (LOCAL SERVICE read, nobody broad), CA, token_file only' -Status $(if ($bt.Ok) { 'PASS' } elseif ($hard.Count) { 'FAIL' } else { 'PENDING' }) -Scope external -Detail $(if ($bt.Ok) { '{0}; {1}' -f $c.Gateway.TokenPath, $c.ResearchGateway.TokenPath } else { $bt.Problems -join '; ' }) | Out-Null
        if ($layout) {
            $st = Get-LabGatewayClientState -Layout $layout -UsbCa $usbCa
            Add-LabVerification -Check 'Gateway CA installed (and identical to the hub''s, via the provisioner stick)' -Status $(if ($st.CaPresent -and $st.CaMatchesUsb -ne $false) { 'PASS' } elseif ($st.CaPresent) { 'FAIL' } else { 'PENDING' }) -Detail $layout.Ca | Out-Null
            $h = Test-LabGatewayHealth -BaseUrl $layout.Url -CaFile $layout.Ca -ExpectedServiceId ([string]$gwm.Manifest.observation.service_id)
            Add-LabVerification -Check 'TLS to the Gateway with its CA; service identity shared-inference' -Status $(if ($h.Ok) { 'PASS' } elseif ($st.CaPresent) { 'FAIL' } else { 'PENDING' }) -Scope lan -Detail $h.Detail | Out-Null
        } else { Add-LabVerification -Check 'Gateway reachable from this laptop' -Status PENDING -Scope lan -Detail 'no Gateway release on this USB' | Out-Null }
        # Capstone's own verifier: application health and both identities authenticated against the Gateway
        # (models, policy, availability; no secret printed). Needs RC.2's appsettings (two client configs).
        if ($c.Installed -and $c.Gateway -and $c.Gateway.Present -and $c.ResearchGateway -and $c.ResearchGateway.Present) {
            # Capstone needs time to start after its task starts (rehearsal: tens of seconds); its own health script
            # waits 10 s. 1.4.5 asked seconds after Register-CapstoneTask and reported a FAIL (DESKTOP-REDACT11, 2026-11-07).
            if ($c.Task -and $c.Task.Present) { $wait = Wait-LabCapstoneHealth -Config $Config -Seconds 180; Write-LabLog ('  Capstone: ' + $wait.Detail) }
            $r = Invoke-LabExternalScript -Script (Join-Path $Config.capstone.install_root 'tools\Test-CapstoneHealth.ps1') -Arguments @('-InstallRoot', $Config.capstone.install_root, '-DataRoot', $Config.capstone.data_root) -LogFile (Join-Path $run.Folder 'capstone.log')
            $hr = ConvertFrom-LabCapstoneHealthOutput -Text $r.Output
            $running = ($c.Task -and $c.Task.Present)
            $why = ''
            if ($running -and -not ($hr -and $hr.application)) { $why = Save-LabCapstoneDiagnostics -Run $run -Config $Config -UsbRoot $UsbRoot }
            if ($hr) {
                Add-LabVerification -Check 'Capstone health (its Test-CapstoneHealth.ps1: /api/health)' -Status $(if ($hr.application) { 'PASS' } elseif ($running) { 'FAIL' } else { 'PENDING' }) -Detail $(if ($hr.application) { 'version ' + $hr.version } else { 'no healthy answer after waiting up to 3 minutes' + $(if ($running) { '; ' + $why } else { ' (startup task not registered)' }) }) | Out-Null
                # The release that answers must be the active one: Stop-Capstone.ps1 can leave an older one serving.
                if ($hr.application -and $hr.version) { Add-LabVerification -Check 'the Capstone that answers is the active release (no older process left serving)' -Status $(if ([string]$hr.version -eq [string]$c.Version) { 'PASS' } else { 'FAIL' }) -Detail $(if ([string]$hr.version -eq [string]$c.Version) { "both $($c.Version)" } else { "active $($c.Version), but $($hr.version) answers: restart this laptop so the active release starts" }) | Out-Null }
                $gv = $hr.gateway_verification
                Add-LabVerification -Check 'both Gateway identities authenticate (Capstone''s Test-CapstoneGateway.ps1)' -Status $(if ($hr.gateway_authenticated) { 'PASS' } elseif ($bt.Ok) { 'FAIL' } else { 'PENDING' }) -Scope lan -Detail $(if ($gv -and $gv.status) { [string]$gv.status } elseif ($hr.gateway_authenticated) { 'verified' } else { 'not verified' }) | Out-Null
                $ext = @($hr.external_steps | Where-Object { $_ })
                Add-LabVerification -Check 'Capstone reports no outstanding external steps' -Status $(if ($ext.Count) { 'PENDING' } else { 'PASS' }) -Required $false -Scope external -Detail $(if ($ext.Count) { $ext -join ' | ' } else { 'none' }) | Out-Null
            } else {
                Add-LabVerification -Check 'Capstone health (its Test-CapstoneHealth.ps1)' -Status $(if ($running) { 'FAIL' } else { 'PENDING' }) -Detail ('no result (exit {0}): {1}{2}' -f $r.ExitCode, (($r.Output -split "`r?`n" | Select-Object -Last 2) -join ' '), $(if ($why) { '; ' + $why } else { '' })) | Out-Null
            }
        } else {
            Add-LabVerification -Check 'Capstone health (/api/health)' -Status $(if ($c.Health -and $c.Health.Ok) { 'PASS' } elseif ($c.Task -and $c.Task.Present) { 'FAIL' } else { 'PENDING' }) -Detail $(if ($c.Health) { 'answered {0}, version {1}' -f $c.Health.Answered, $c.Health.Version } else { 'not configured for two Gateway identities yet' }) | Out-Null
        }
        # Capstone 6.0.0-rc.4: QMS and ERP on their heads, reached from here with the token; Capstone told to use them;
        # and the laptop's own copy of their data the same as the services' (else it has to be moved once).
        foreach ($sp in $svcSpecs) {
            $nm = $sp.Name.ToUpperInvariant()
            $cl = Get-LabServiceClientState -Spec $sp -UsbRoot $UsbRoot
            Add-LabVerification -Check ("{0} on head {1} answers this laptop and accepts its token" -f $nm, $sp.HeadNumber) -Status $(if ($cl.Authorized -eq $true) { 'PASS' } elseif ($cl.Authorized -eq $false) { 'FAIL' } else { 'PENDING' }) -Scope lan -Detail $(if ($cl.TokenFile) { $cl.Detail + $(if ($cl.Fingerprint) { " (token fingerprint $($cl.Fingerprint))" } else { '' }) } else { 'no token on this laptop yet (head ' + $sp.HeadNumber + ': Existing machine > 10)' }) | Out-Null
            Add-LabVerification -Check ("Capstone uses {0} on head {1} (its settings name the service)" -f $nm, $sp.HeadNumber) -Status $(if ($c.Services.ContainsKey($sp.Name)) { 'PASS' } else { 'PENDING' }) -Detail $(if ($c.Services.ContainsKey($sp.Name)) { $c.Services[$sp.Name] } else { "not yet: Capstone keeps $nm in-process" }) | Out-Null
            if ($cl.OnStick) { Add-LabVerification -Check ("no {0} token left on the provisioner stick" -f $nm) -Status FAIL -Detail ("still at {0}: run 4 again and approve taking it" -f (Get-LabServiceHandoff -UsbRoot $UsbRoot -Name $sp.Name).Token) | Out-Null }
            $zp = Find-LabServicePackage -Folder (Join-Path $Packages 'services') -Spec $sp
            if ($zp) {
                $cmp = Compare-LabServiceData -Spec $sp -Zip $zp.FullName -CapstoneData ([string]$Config.capstone.data_root)
                Add-LabVerification -Check ("this laptop's {0} data is the same as the service's (nothing to move)" -f $nm) -Status $(if ($cmp.Same) { 'PASS' } else { 'PENDING' }) -Required $false -Detail $(if ($cmp.Same) { '{0} files identical (SHA-256)' -f $cmp.Files } else { 'differs: ' + ((@($cmp.Different) + @($cmp.Extra) | Select-Object -First 5) -join ', ') + '. Capstone''s one-time move (Export-CapstoneBusinessData.ps1, then the service''s Import script) is needed; ask before it is done' }) | Out-Null
            }
        }
        # Capstone's own end-to-end proof through the Gateway: an operational generation, an operational embedding and
        # a research generation must all complete (real requests to the heads, through the Gateway).
        $e2e = Join-Path $Config.capstone.install_root 'tools\Test-CapstoneEndToEnd.ps1'
        if ($c.Installed -and (Test-Path -LiteralPath $e2e) -and $c.Gateway -and $c.Gateway.Present -and $c.ResearchGateway -and $c.ResearchGateway.Present) {
            $r = Invoke-LabExternalScript -Script $e2e -Arguments @('-InstallRoot', $Config.capstone.install_root, '-DataRoot', $Config.capstone.data_root) -LogFile (Join-Path $run.Folder 'capstone.log')
            $sum = ConvertTo-LabEndToEndSummary -Text $r.Output
            Add-LabVerification -Check 'Capstone end to end through the Gateway (its Test-CapstoneEndToEnd.ps1: generation, embedding, research)' -Status $(if ($r.ExitCode -eq 0) { 'PASS' } else { 'FAIL' }) -Scope lan -Detail ('exit {0}; {1}' -f $r.ExitCode, $sum) | Out-Null
        }
        # Capstone 6.0.0-rc.5: one Research Lab Smoke case pinned to every head the Gateway serves to capstone-research,
        # each of which must complete and write rows to the live arena_events (so the hub's board shows it).
        $lh = Join-Path $Config.capstone.install_root 'tools\Test-CapstoneLabHeads.ps1'
        if ($c.Installed -and (Test-Path -LiteralPath $lh) -and $c.ResearchGateway -and $c.ResearchGateway.Present) {
            if (Request-LabApproval -Prompt "Run Capstone's Test-CapstoneLabHeads.ps1 now? It runs one Smoke case pinned to each head the Gateway serves to the Research Lab, one head after another (about 2-5 minutes per head; watch the hub's board). Results stay in Capstone's Research Lab > Results." -Word 'APPROVE' -Kind 'change') {
                Write-LabLog '  Smoke on every serving head, one after another; this takes a while.'
                $lhSince = (Get-Date).AddSeconds(-5)
                $r = Invoke-LabExternalScript -Script $lh -Arguments @('-InstallRoot', $Config.capstone.install_root, '-DataRoot', $Config.capstone.data_root, '-TimeoutSeconds', '900') -LogFile (Join-Path $run.Folder 'capstone.log')
                $lr = ConvertFrom-LabLabHeadsOutput -Text $r.Output
                # The test's JSON says only "failed": the cause is in each run's worker.log, which comes back with the stick.
                $rl = $null
                if (-not $lr.Passed) {
                    try { $rl = Save-LabResearchLabDiagnostics -Run $run -Config $Config -UsbRoot $UsbRoot -Since $lhSince; Write-LabLog ('  Research Lab runs, their errors and worker logs: ' + $rl.File) } catch { Write-LabLog ('  Research Lab diagnostics not saved: ' + $_.Exception.Message) -Level WARN }
                }
                foreach ($hh in $lr.Heads) {
                    $why = if (-not $hh.Ok -and $rl -and $rl.ByHead.ContainsKey([string]$hh.ResourceId)) { '; cause: ' + $rl.ByHead[[string]$hh.ResourceId] } else { '' }
                    # 1.6.7: a run held for a person's review after its model calls on the pinned head is served (PASS); a
                    # paused run whose output does not prove the calls is PENDING, not FAIL (2026-11-12: 10 such "FAIL" lines).
                    $st = $(if ($hh.Ok -or $hh.Held) { 'PASS' } elseif ($hh.Paused) { 'PENDING' } else { 'FAIL' })
                    Add-LabVerification -Check ("Research Lab Smoke pinned to {0} reaches its head and shows in the Arena" -f $hh.Head) -Status $st -Scope lan -Required $false -Detail ($hh.Text + $why) | Out-Null
                }
                if ($rl) { Add-LabVerification -Check 'Research Lab run errors and worker logs copied for diagnosis' -Status PASS -Required $false -Detail ('{0} experiment(s); {1}' -f $rl.Experiments, $rl.File) | Out-Null }
                # 2026-11-14: rc.13 reported 0 target heads and this read "0 of 0 served" as PASS. No head found is a failure.
                $allOk = (($lr.Passed -or $lr.AllServed) -and [int]$lr.Targets -gt 0 -and @($lr.Heads).Count -gt 0)
                $anyPaused = @($lr.Heads | Where-Object { $_.Paused -and -not $_.Held }).Count
                Add-LabVerification -Check "Research Lab reaches every serving head (Capstone's Test-CapstoneLabHeads.ps1)" -Status $(if ($allOk) { 'PASS' } elseif ($anyPaused -and -not @($lr.Heads | Where-Object { -not $_.Ok -and -not $_.Held -and -not $_.Paused }).Count) { 'PENDING' } else { 'FAIL' }) -Scope lan -Required $false -Detail $(if ($lr.Read) { '{0} of {1} heads served (completed, or held for a person''s review after their model calls; exit {2}){3}' -f $lr.Served, $lr.Targets, $r.ExitCode, $(if ($anyPaused) { "; $anyPaused paused for review without proof of a model call in Capstone's output" } else { '' }) } else { 'no result (exit {0}): {1}' -f $r.ExitCode, (($r.Output -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' ') }) | Out-Null
            } else { Add-LabVerification -Check "Research Lab reaches every serving head (Capstone's Test-CapstoneLabHeads.ps1)" -Status PENDING -Required $false -Detail 'not run (declined); run it later with stick > 4' | Out-Null }
        }
        Add-LabVerification -Check 'Capstone records the Arena Ticketing adapter as registered' -Status $(if ($c.ArenaRegistration -eq 'complete') { 'PASS' } else { 'PENDING' }) -Required $false -Detail $(if ($c.ArenaRegistration) { $c.ArenaRegistration } else { 'not configured' }) | Out-Null
        $t = Get-LabTicketingState
        Add-LabVerification -Check 'Arena Ticketing adapter installed, enrolled and running' -Status $(if ($t.ProgramInstalled -and $t.CredentialPresent -and $t.Running) { 'PASS' } else { 'PENDING' }) -Detail ('installed {0}; credential {1}; running {2} (starts at sign-in of {3})' -f $t.ProgramInstalled, $t.CredentialPresent, $t.Running, $env:USERNAME) | Out-Null
    } else {
        Add-LabCoderVerification -Run $run -Config $Config -Address $addr -State $coderState
    }
    Complete-LabRun -Run $run | Out-Null
}

function Invoke-LabExisting {
    $s = Start-LabStages -RoleName 'existing' -Choices {
        param($insp)
        $role = $insp.detected.Role
        Write-Host ''
        Write-Host ('  This machine appears to be: {0} ({1})' -f $role, $insp.detected.Reason) -ForegroundColor Cyan
        if ($role -in 'hub', 'capstone', 'coder') { Write-Host ("  For this role choose it from the main menu: its workflow inspects, keeps what is installed and only updates with approval.") -ForegroundColor Yellow; return $null }
        if ($role -notin 'head', 'gpu-workstation') { Write-Host '  No lab head was found here. Use "New CPU/GPU inference head" for a new machine.' -ForegroundColor Yellow; return $null }
        $menu = [ordered]@{ '1' = 'verify'; '2' = 'firewall-cutover'; '3' = 'firewall-gateway-only'; '4' = 'time-power'; '5' = 'ollama-service'; '6' = 'reboot-check'; '7' = 'models'; '8' = 'viewer'; '9' = 'rollback' }
        Write-Host '   1 Verify only (read-only)'
        Write-Host ('   2 Ollama firewall: cutover ({0})' -f ($Config.ollama.client_modes.cutover -join ', '))
        Write-Host ('   3 Ollama firewall: Gateway only ({0})' -f ($Config.ollama.client_modes.'gateway-only' -join ', '))
        Write-Host '   4 Time sync with the hub, and power settings'
        Write-Host '   5 Ollama boot task: explicit model directory (so the SYSTEM server serves the installed models)'
        Write-Host '   6 Post-reboot verification'
        Write-Host '   7 Install or update models'
        Write-Host '   8 Repair or update the Arena head viewer (keeps its credential)'
        Write-Host '   9 Roll back a previous run''s changes'
        # Capstone's business service for this head (QMS on head 3, ERP on head 4), offered only there.
        $svc = Get-LabBusinessServiceSpec -Config $Config -HeadIp (Get-LabHeadIp $insp)
        if ($svc) { $menu['10'] = 'business-service'; Write-Host ('  10 Install or update {0} ({1}) on this head, for the Capstone laptop' -f $svc.Product, $svc.Label) }
        $pick = Read-LabInput -Prompt 'Numbers, comma separated (for example 1 or 2,4)'
        $actions = @(); $mode = ''; $rollback = ''
        foreach ($p in @($pick -split '[,\s]+' | Where-Object { $_ })) {
            $a = $menu[$p]; if (-not $a) { continue }
            if ($a -eq 'firewall-cutover') { $actions += 'firewall'; $mode = 'cutover' }
            elseif ($a -eq 'firewall-gateway-only') { $actions += 'firewall'; $mode = 'gateway-only' }
            elseif ($a -eq 'rollback') {
                $runs = @(Get-ChildItem -LiteralPath (Join-Path (Get-LabRoot $Root) 'runs') -Directory -ErrorAction SilentlyContinue | Where-Object { $r = Read-LabJson (Join-Path $_.FullName 'changes.json'); $r -and @($r.changes | Where-Object { -not $_.undone }).Count } | Sort-Object Name -Descending)
                if (-not $runs.Count) { Write-Host '  No run with changes to undo.' -ForegroundColor Yellow; continue }
                for ($k = 0; $k -lt $runs.Count; $k++) { Write-Host ('   {0}. {1}' -f ($k + 1), $runs[$k].Name) }
                $w = Read-LabInput -Prompt 'Which run'; if ($w -match '^\d+$' -and [int]$w -ge 1 -and [int]$w -le $runs.Count) { $rollback = $runs[[int]$w - 1].FullName; $actions += 'rollback' }
            } else { $actions += $a }
        }
        if (-not $actions.Count) { return $null }
        $c = @{ Actions = $actions; ClientMode = $(if ($mode) { $mode } else { $Config.ollama.default_client_mode }); RollbackRun = $rollback }
        if ($actions -contains 'ollama-service') {
            $sv = Get-LabServingArgs -Ollama $insp.ollama   # the folder the running server already serves wins, as in the plan
            $md = Resolve-LabModelDirectory -MachineValue ([string]$insp.ollama.Env.MachineModels) -Candidates @($insp.ollama.ModelDirectories) -NewDefault $Config.ollama.new_model_directory -ServingPath $sv.ServingPath -Served $sv.Served
            if ($md.Decision -eq 'Ambiguous') { for ($k = 0; $k -lt $md.Choices.Count; $k++) { Write-Host ('   {0}. {1} ({2} manifests)' -f ($k + 1), $md.Choices[$k].Path, $md.Choices[$k].Manifests) }; $w = Read-LabInput -Prompt 'Which folder should the server use'; if ($w -match '^\d+$') { $c.ModelDirectory = $md.Choices[[int]$w - 1].Path } }
        }
        if ($actions -contains 'models') { $prop = Get-LabProfileProposal -RamGB ([double]$insp.hardware.TotalMemoryGB) -VramMB ([int]$insp.hardware.NvidiaVramMB); $m = Read-LabModelChoice -Inspection $insp -Default $prop.Profile; $c.Profiles = $m.Profiles; $c.ModelsAdd = $m.Add; $c.ModelsExclude = $m.Exclude }
        $c
    }
    if (-not $s) { return }
    $run = $s.Run; $plan = $run.Plan; $insp = $run.Inspection
    $ip = Get-LabHeadIp $insp; $alias = Get-LabHeadAlias $insp
    if ($insp.detected.Role -eq 'gpu-workstation' -and @($plan.steps | Where-Object { $_.kind -ne 'read' }).Count) {
        if (-not (Request-LabApproval -Prompt 'This is the GPU workstation (192.0.2.2), configured by hand. Changing it needs a separately approved plan. Type the word to confirm that plan exists.' -Word 'GPU-WORKSTATION' -Kind 'disruptive')) { Complete-LabRun -Run $run -Declined $true | Out-Null; return }
    }
    Write-LabStage -Letter 'C' -Title 'Configure (maintenance)'
    foreach ($step in @($plan.steps)) {
        switch ($step.id) {
            'firewall' { Invoke-LabStep -Id 'firewall' -Title 'Ollama firewall' -Action { Invoke-LabFirewallStep -Run $run -HeadIp $ip -Clients $plan.clients -Alias $alias -ServerPaths @(@(Get-CapstoneOllamaExecutable) + @((Get-LabServingOllamaExecutable -Root $run.Root -Spec $Config.ollama.runtime)) + @($insp.ollama.Processes | ForEach-Object { $_.Path } | Where-Object { $_ })) } | Out-Null }
            'time' { Invoke-LabStep -Id 'time' -Title 'Time zone and time source' -Action { Set-LabTimeSync -Run $run -Hub $Config.hub -Zone $Config.time_zone } | Out-Null }
            'power' { Invoke-LabStep -Id 'power' -Title 'Power and sleep' -Action { Set-LabPowerState -Run $run -Adapter $alias } | Out-Null }
            'ollama-service' { Invoke-LabStep -Id 'ollama-service' -Title 'Ollama boot task and model directory' -Action { $exe = (Get-LabServingOllamaExecutable -Root $run.Root -Spec $Config.ollama.runtime); if (-not $exe) { throw 'Ollama is not installed' }; Set-LabOllamaService -Run $run -Executable $exe -Bind $Config.ollama.bind -ModelDirectory $plan.model_directory -TaskName $Config.ollama.task_name } | Out-Null }
            'ollama-start' { Invoke-LabStep -Id 'ollama-start' -Title 'Restart Ollama from the boot task' -Action { Invoke-LabOllamaStart -Run $run -HeadIp $ip } | Out-Null }
            'rollback' { Invoke-LabStep -Id 'rollback' -Title ('Undo run ' + (Split-Path -Leaf $s.Choices.RollbackRun)) -Action {
                    $res = Undo-LabRun -ChangesFile (Join-Path $s.Choices.RollbackRun 'changes.json') -Confirm { param($c) Request-LabApproval -Prompt ('Undo: ' + $c.description) -Word 'UNDO' -Kind 'privileged' }
                    foreach ($r in $res) { Write-LabLog ('  ' + $r) }
                    New-LabOutcome $(if (@($res | Where-Object { $_ -like 'FAILED*' }).Count) { 'FAILED' } else { 'DONE' }) ($res -join '; ') } | Out-Null }
            'business-service' { $svc = Get-LabBusinessServiceSpec -Config $Config -HeadIp $ip; if ($svc) { Invoke-LabBusinessService -Run $run -Spec $svc -HeadIp $ip } }
        }
    }
    Write-LabStage -Letter 'D' -Title 'Register'
    if ($plan.steps | Where-Object { $_.id -eq 'viewer' }) { Invoke-LabStep -Id 'viewer' -Title 'Arena head viewer' -ContinueOnFailure -Action { Invoke-LabViewerStep -Run $run -Addr $ip -ClaimData $null } | Out-Null }
    else { $who = Get-LabArenaWhoami -Endpoint $Config.arena.endpoint; Add-LabRegistration -Item 'Arena registration' -Status $(if (@($who.Displays).Count) { 'ALREADY_REGISTERED' } else { 'PENDING' }) -Detail $(if (@($who.Displays).Count) { (@($who.Displays) | ForEach-Object { $_.display_id }) -join ', ' } else { 'no display registered for this address' }) | Out-Null }
    Invoke-LabHeadVerify -Run $run -HeadIp $ip -Clients $plan.clients -Alias $alias
    $svc = Get-LabBusinessServiceSpec -Config $Config -HeadIp $ip
    if ($svc -and (@($plan.steps | Where-Object { $_.id -eq 'business-service' }).Count -or (Get-LabServiceState -Spec $svc).Installed)) { Add-LabBusinessServiceVerification -Spec $svc }
    if ($plan.steps | Where-Object { $_.id -eq 'models' }) { Invoke-LabModelStage -Run $run -Inspection $insp -Choice @{ Profiles = @($s.Choices.Profiles); Add = @($s.Choices.ModelsAdd); Exclude = @($s.Choices.ModelsExclude) } }
    Complete-LabRun -Run $run | Out-Null
}

# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

function Show-LabMenu {
    Write-Host ''
    Write-Host ('  AGENT ARENA LAB PROVISIONER {0}' -f (Get-LabVersion)) -ForegroundColor Cyan
    Write-Host ('  {0}   running on {1} as {2}{3}' -f $Release, $env:COMPUTERNAME, $env:USERNAME, $(if (Test-LabElevated) { ' (administrator)' } else { ' (NOT administrator)' }))
    Write-Host ''
    Write-Host '   1  New CPU inference head'
    Write-Host '   2  New GPU inference head'
    Write-Host ('   3  Administration hub ({0})' -f $Config.hub)
    Write-Host ('   4  Capstone application laptop ({0})' -f $Config.roles.capstone.address)
    Write-Host ('   5  Coder application laptop ({0})' -f $Config.roles.coder.address)
    Write-Host '   6  Existing machine: inspect, verify, repair or update'
    Write-Host '   7  Inspect only (read-only report)'
    Write-Host '   Q  Quit'
    $c = Read-LabInput -Prompt 'Choose'
    return @{ '1' = 'cpu-head'; '2' = 'gpu-head'; '3' = 'hub'; '4' = 'capstone'; '5' = 'coder'; '6' = 'existing'; '7' = 'inspect' }[$c.ToUpperInvariant()]
}

if (-not (Test-LabElevated) -and $Through -eq 'All') { Write-Host '  Not running as administrator: inspection and planning work, changes will fail. Start START-LAB-PROVISIONER.cmd and approve the prompt.' -ForegroundColor Yellow }
$chosen = $Role
if (-not $chosen) { $chosen = Show-LabMenu }
switch ($chosen) {
    'cpu-head' { Invoke-LabNewHead -RoleName 'cpu-head' }
    'gpu-head' { Invoke-LabNewHead -RoleName 'gpu-head' }
    'hub' { Invoke-LabHub }
    'capstone' { Invoke-LabAppLaptop -RoleName 'capstone' }
    'coder' { Invoke-LabAppLaptop -RoleName 'coder' }
    'existing' { Invoke-LabExisting }
    'inspect' { $null = Start-LabStages -RoleName 'inspect' -Choices { param($i) $null } }
    default { Write-Host '  Nothing chosen.' }
}
