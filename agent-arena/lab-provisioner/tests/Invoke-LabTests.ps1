<#
    Unit and scenario tests for the Agent Arena Lab Provisioner. Windows PowerShell 5.1.

    Uses the 1.0.3 harness (TestHarness.psm1, unchanged). Nothing here touches the machine:
    the firewall is a stand-in backend over an in-memory rule set, Ollama and the network are
    replaced by functions that return recorded answers, and every state folder is temporary.
    Physical behaviour (real firewall, real reboot, real LAN) is in the field checklist.
#>
[CmdletBinding()]
param([string]$ResultPath = '')
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
Import-Module (Join-Path $here 'TestHarness.psm1') -Force
foreach ($m in 'Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Preflight') { Import-Module (Join-Path $root "engine\head-engine\modules\$m.psm1") -Force -Global -DisableNameChecking }
foreach ($m in 'Lab.Core', 'Lab.Firewall', 'Lab.Accounts', 'Lab.OllamaRuntime', 'Lab.Head', 'Lab.Gateway', 'Lab.Hub', 'Lab.Models', 'Lab.Arena', 'Lab.Apps', 'Lab.Services', 'Lab.Coder', 'Lab.Inspect', 'Lab.Plan', 'Lab.Report') { Import-Module (Join-Path $root "engine\modules\$m.psm1") -Force -Global -DisableNameChecking }
$Config = Get-LabConfig -Path (Join-Path $root 'config\lab-network.json')
$Catalog = Get-LabCatalog -Path (Join-Path $root 'engine\head-engine\catalog\model-catalog.json')
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('lab-tests-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $Temp -Force | Out-Null
Reset-TestRun
function Quiet { param([scriptblock]$B) $null = & $B 6>$null }

# ---------------------------------------------------------------------------
# A stand-in firewall
# ---------------------------------------------------------------------------
$script:Rules = New-Object System.Collections.ArrayList
$script:FwProfiles = @()
$script:FwCategory = 'Public'
function New-TestRule {
    param([string]$Name, [string]$DisplayName = '', [string]$Action = 'Allow', [string[]]$Profiles = @('Domain', 'Private', 'Public'), [string]$Protocol = 'TCP',
          [string[]]$LocalPorts = @('11434'), [string[]]$Remote = @('Any'), [string[]]$Local = @('Any'), [string]$Program = 'Any', [bool]$Enabled = $true, [string]$PolicySource = 'Local')
    if (-not $DisplayName) { $DisplayName = $Name }
    return [pscustomobject][ordered]@{ Name = $Name; DisplayName = $DisplayName; Group = ''; Enabled = $Enabled; Direction = 'Inbound'; Action = $Action; Profiles = $Profiles; Protocol = $Protocol
                                       LocalPorts = $LocalPorts; RemoteAddresses = $Remote; LocalAddresses = $Local; Program = $Program; Service = 'Any'; PolicySource = $PolicySource; Description = '' }
}
function Reset-Fw { param([array]$Rules, [bool]$PublicEnabled = $true, [string]$Default = 'Block')
    $script:Rules.Clear(); foreach ($r in $Rules) { [void]$script:Rules.Add($r) }
    $script:FwProfiles = @('Domain', 'Private', 'Public' | ForEach-Object { [pscustomobject]@{ Name = $_; Enabled = $(if ($_ -eq 'Public') { $PublicEnabled } else { $true }); DefaultInboundAction = $Default; AllowLocalFirewallRules = 'True' } }) }
function Get-FwSnapshot { return [pscustomobject]@{ TakenAt = 'test'; Computer = 'TEST'; Rules = @($script:Rules | ForEach-Object { $_ | Select-Object * }); Profiles = $script:FwProfiles; Connections = @([pscustomobject]@{ InterfaceAlias = 'Ethernet'; InterfaceIndex = 7; NetworkCategory = $script:FwCategory }) } }
Set-LabFirewallBackend @{
    Snapshot = { Get-FwSnapshot }
    NewRule = { param($p) $r = New-TestRule -Name $p.Name -DisplayName $p.DisplayName -LocalPorts @($p.LocalPort) -Remote @($p.RemoteAddress) -Local @($p.LocalAddress); if ($p.Profile -and $p.Profile -ne 'Any') { $r.Profiles = @(ConvertTo-LabProfileList $p.Profile) }; [void]$script:Rules.Add($r) }
    SetRule = { param($name, $p) $r = $script:Rules | Where-Object Name -eq $name; if ($p.DisplayName) { $r.DisplayName = $p.DisplayName }; if ($p.LocalPort) { $r.LocalPorts = @($p.LocalPort) }; if ($p.LocalAddress) { $r.LocalAddresses = @($p.LocalAddress) }; if ($p.RemoteAddress) { $r.RemoteAddresses = @($p.RemoteAddress) }; if ($p.Enabled) { $r.Enabled = ($p.Enabled -eq 'True') }; if ($p.Profile) { $r.Profiles = @(ConvertTo-LabProfileList $p.Profile) } }
    Enable = { param($name) ($script:Rules | Where-Object Name -eq $name).Enabled = $true }
    Disable = { param($name) ($script:Rules | Where-Object Name -eq $name).Enabled = $false }
    Remove = { param($name) $r = $script:Rules | Where-Object Name -eq $name; [void]$script:Rules.Remove($r) }
    Export = { param($path) Set-Content -LiteralPath $path -Value 'netsh export stand-in' }
}
$Head = '192.0.2.11'
$Legacy = New-TestRule -Name '{L-1}' -DisplayName "Capstone Ollama Head $Head from Coordinator 192.0.2.1" -Remote @('192.0.2.1') -Local @($Head)
$cutover = @($Config.ollama.client_modes.cutover)
$sources = @('192.0.2.254', '192.0.2.1', '192.0.2.201', '192.0.2.202', '192.0.2.50')
function Get-Verdicts { param($Exposure) $h = @{}; foreach ($s in $Exposure.Sources) { $h[$s.Source] = $s.Verdict }; return $h }
function New-TestRun { $r = New-LabRun -Role 'test' -Root (Join-Path $Temp ([guid]::NewGuid().ToString('N').Substring(0, 6))); return $r }

# ---------------------------------------------------------------------------
Start-TestGroup 'arena.v2 signing (CONTRACT.md test vectors)'
Test-Case 'K = HMAC(secret, "arena.v2")' { Assert-Equal '34bc38d7c6bb14fadd26112263bf7d37138e1f80a50316f03caf27b8ac657839' (-join ((Get-LabArenaKey 'example-secret-not-real') | ForEach-Object { $_.ToString('x2') })) }
Test-Case 'body hash' { Assert-Equal '7d71c2c5ab1ea2bc946d7a2981eec10cd45efb816bfd85832936cb020dea0e89' (Get-LabSha256Hex ([Text.Encoding]::UTF8.GetBytes('{"contract":"arena.v2","events":[]}'))) }
Test-Case 'request signature' {
    $k = Get-LabArenaKey 'example-secret-not-real'
    Assert-Equal 'glFbgD8F8Pzztqqz0-wAPey64bdz-_sK7EMwrQQjft0' (Get-LabArenaSignature -Key $k -Method POST -Target '/v2/events' -Timestamp '1793486400' -Nonce 'AAAAAAAAAAAAAAAAAAAAAA' -Body ([Text.Encoding]::UTF8.GetBytes('{"contract":"arena.v2","events":[]}')))
}
Test-Case 'response signature accepted, tampering refused' {
    $k = Get-LabArenaKey 'example-secret-not-real'; $b = [Text.Encoding]::UTF8.GetBytes('{"ok":true}')
    Assert-True (Test-LabArenaResponse -Key $k -Status 200 -Nonce 'AAAAAAAAAAAAAAAAAAAAAA' -Body $b -Signature '_4NCUlK3947l6ssVjMWD2eaKK1ylrr0w0doNQh4m-nk')
    Assert-False (Test-LabArenaResponse -Key $k -Status 201 -Nonce 'AAAAAAAAAAAAAAAAAAAAAA' -Body $b -Signature '_4NCUlK3947l6ssVjMWD2eaKK1ylrr0w0doNQh4m-nk')
    Assert-False (Test-LabArenaResponse -Key $k -Status 200 -Nonce 'AAAAAAAAAAAAAAAAAAAAAA' -Body $b -Signature '')
}
Test-Case 'code normalisation matches arena_service.crypto.normalize_code' {
    Assert-Equal 'ARENA-ABCD-EFGH-JKLM-NPQR' (ConvertTo-LabArenaCode 'arena abcd-efgh jklm npqr')
    Assert-Equal 'ARENA-ABCD-EFGH-JKLM-NPQR' (ConvertTo-LabArenaCode 'ABCDEFGHJKLMNPQR')
}
Test-Case 'machine hash has the desktop app''s shape' { Assert-Match '^[0-9a-f]{64}$' (Get-LabMachineHash) }

# ---------------------------------------------------------------------------
Start-TestGroup 'firewall address, port and program matching'
Test-Case 'single, range, CIDR, mask, Any, LocalSubnet, keywords' {
    Assert-Equal 'yes' (Test-LabAddressSpec -Spec '192.0.2.254' -Source '192.0.2.254'); Assert-Equal 'no' (Test-LabAddressSpec -Spec '192.0.2.1' -Source '192.0.2.254')
    Assert-Equal 'yes' (Test-LabAddressSpec -Spec '192.0.2.200-192.0.2.249' -Source '192.0.2.201'); Assert-Equal 'no' (Test-LabAddressSpec -Spec '192.0.2.200-192.0.2.249' -Source '192.0.2.254')
    Assert-Equal 'yes' (Test-LabAddressSpec -Spec '192.0.2.0/24' -Source '192.0.2.5'); Assert-Equal 'yes' (Test-LabAddressSpec -Spec '192.0.2.0/255.255.255.0' -Source '192.0.2.5')
    Assert-Equal 'no' (Test-LabAddressSpec -Spec '192.168.0.0/16' -Source '192.0.2.5'); Assert-Equal 'yes' (Test-LabAddressSpec -Spec 'Any' -Source '192.0.2.5')
    Assert-Equal 'yes' (Test-LabAddressSpec -Spec 'LocalSubnet' -Source '192.0.2.201'); Assert-Equal 'uncertain' (Test-LabAddressSpec -Spec 'Intranet' -Source '192.0.2.201')
    Assert-Equal 'no' (Test-LabAddressSpec -Spec 'fe80::/64' -Source '192.0.2.201')
}
Test-Case 'ports' { Assert-True (Test-LabPortSpec @('Any')); Assert-True (Test-LabPortSpec @('11434')); Assert-True (Test-LabPortSpec @('11000-12000')); Assert-False (Test-LabPortSpec @('8000', 'RPC')) }
Test-Case 'program rules' {
    Assert-Equal 'yes' (Test-LabProgramMatch -Program 'Any'); Assert-Equal 'yes' (Test-LabProgramMatch -Program 'C:\O\ollama.exe' -ServerPaths @('C:\O\ollama.exe'))
    Assert-Equal 'uncertain' (Test-LabProgramMatch -Program 'C:\Other\ollama.exe' -ServerPaths @('C:\O\ollama.exe')); Assert-Equal 'no' (Test-LabProgramMatch -Program 'C:\x\python.exe')
}
Test-Case 'rule classes' {
    Assert-Equal 'LEGACY_PROVISIONER' (Get-LabRuleClass -Rule $Legacy -Config $Config)
    Assert-Equal 'OLLAMA_PROGRAM' (Get-LabRuleClass -Rule (New-TestRule -Name 'p' -Program 'C:\O\ollama.exe' -LocalPorts @('Any')) -Config $Config)
    Assert-Equal 'BROAD' (Get-LabRuleClass -Rule (New-TestRule -Name 'b' -LocalPorts @('1-65535')) -Config $Config)
    Assert-Equal 'GROUP_POLICY' (Get-LabRuleClass -Rule (New-TestRule -Name 'g' -PolicySource 'GroupPolicy') -Config $Config)
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Ollama exposure (rule analysis)'
Test-Case '1.0.3 head: only .1 admitted; the Gateway on .254 is refused' {
    Reset-Fw @($Legacy); $v = Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config)
    Assert-Equal 'ADMITTED' $v['192.0.2.1']; Assert-Equal 'DENIED' $v['192.0.2.254']; Assert-Equal 'DENIED' $v['192.0.2.201']
}
Test-Case 'an "Allow" program rule for ollama.exe admits Capstone and Coder' {
    Reset-Fw @($Legacy, (New-TestRule -Name 'ollama.exe' -Program 'C:\O\ollama.exe' -LocalPorts @('Any') -Profiles @('Public')))
    $v = Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -ServerPaths @('C:\O\ollama.exe') -Config $Config)
    Assert-Equal 'ADMITTED' $v['192.0.2.201']; Assert-Equal 'ADMITTED' $v['192.0.2.202']
}
Test-Case 'a rule for the Private profile does not apply on a Public network' {
    Reset-Fw @((New-TestRule -Name 'priv' -Profiles @('Private')))
    Assert-Equal 'DENIED' (Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config))['192.0.2.201']
}
Test-Case 'a firewall profile that is off admits everyone' {
    Reset-Fw @($Legacy) -PublicEnabled $false
    Assert-Equal 'ADMITTED' (Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config))['192.0.2.201']
}
Test-Case 'a block rule wins over an allow rule' {
    Reset-Fw @((New-TestRule -Name 'all' -LocalPorts @('Any')), (New-TestRule -Name 'blk' -Action 'Block' -Remote @('192.0.2.201')))
    $v = Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config)
    Assert-Equal 'DENIED' $v['192.0.2.201']; Assert-Equal 'ADMITTED' $v['192.0.2.202']
}
Test-Case 'keyword remote addresses are reported as uncertain, not as safe' {
    Reset-Fw @((New-TestRule -Name 'intra' -Remote @('Intranet')))
    Assert-Equal 'UNCERTAIN' (Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config))['192.0.2.201']
}

# ---------------------------------------------------------------------------
Start-TestGroup 'firewall plan, backup, apply, idempotency and exact undo'
Test-Case 'cutover on a 1.0.3 head: create the managed rule, disable the legacy rule' {
    Reset-Fw @($Legacy); $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config
    Assert-Equal 'Create,Disable' (($p.Actions | ForEach-Object Action) -join ','); Assert-Equal 0 @($p.Blockers).Count
}
Test-Case 'an Ollama program rule is offered for disabling one by one; a broad rule is only reported' {
    Reset-Fw @($Legacy, (New-TestRule -Name 'ollama.exe' -Program 'C:\O\ollama.exe' -LocalPorts @('Any')), (New-TestRule -Name 'everything' -LocalPorts @('1-65535')))
    $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config -ServerPaths @('C:\O\ollama.exe')
    $per = @($p.Actions | Where-Object Approval -eq 'per-rule'); Assert-Equal 1 $per.Count; Assert-Equal 'ollama.exe' $per[0].Name
    Assert-Equal 1 @($p.Review).Count; Assert-True ($p.Review[0] -match 'everything')
    Assert-False (@($p.Actions | Where-Object Name -eq 'everything').Count -gt 0) 'the broad rule must not be changed'
}
Test-Case 'Group Policy rules and a disabled profile are blockers, never changed' {
    Reset-Fw @((New-TestRule -Name 'gpo' -PolicySource 'GroupPolicy')) -PublicEnabled $false
    $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config
    Assert-Equal 2 @($p.Blockers).Count; Assert-False (@($p.Actions | Where-Object Name -eq 'gpo').Count -gt 0)
}
Test-Case 'apply: backup first; then .254 and .1 admitted, laptops denied; a second run changes nothing' {
    Reset-Fw @($Legacy, (New-TestRule -Name 'ollama.exe' -Program 'C:\O\ollama.exe' -LocalPorts @('Any')))
    $run = New-TestRun; Quiet { }
    $before = (Get-FwSnapshot | ConvertTo-Json -Depth 6)
    $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config -ServerPaths @('C:\O\ollama.exe')
    $bk = Backup-LabFirewall -Folder $run.Backups -Snapshot (Get-FwSnapshot)
    Assert-True (Test-Path $bk.Json); Assert-True (Test-Path $bk.Policy)
    $res = Invoke-LabFirewallPlan -Plan $p -BackupFile $bk.Json -ApprovePerRule { param($a) $true } 6>$null
    Assert-Equal 3 @($res.Applied).Count
    $v = Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -ServerPaths @('C:\O\ollama.exe') -Config $Config)
    Assert-Equal 'ADMITTED' $v['192.0.2.254']; Assert-Equal 'ADMITTED' $v['192.0.2.1']; Assert-Equal 'DENIED' $v['192.0.2.201']; Assert-Equal 'DENIED' $v['192.0.2.202']; Assert-Equal 'DENIED' $v['192.0.2.50']
    Assert-True (New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config -ServerPaths @('C:\O\ollama.exe')).Unchanged 'second run must be a no-op'
    $undo = Undo-LabRun -ChangesFile $run.ChangesFile 6>$null
    Assert-Equal 3 @($undo | Where-Object { $_ -like 'undone*' }).Count
    Assert-Equal $before (Get-FwSnapshot | ConvertTo-Json -Depth 6) 'undo must restore the exact earlier rule set'
}
Test-Case 'a declined per-rule change is left exactly as it was' {
    Reset-Fw @($Legacy, (New-TestRule -Name 'ollama.exe' -Program 'C:\O\ollama.exe' -LocalPorts @('Any')))
    $run = New-TestRun
    $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config -ServerPaths @('C:\O\ollama.exe')
    $res = Invoke-LabFirewallPlan -Plan $p -BackupFile 'x' -ApprovePerRule { param($a) $false } 6>$null
    Assert-Equal 1 @($res.Declined).Count; Assert-True (($script:Rules | Where-Object Name -eq 'ollama.exe').Enabled)
}
Test-Case 'gateway-only after cutover: the update drops .1; undo puts it back' {
    Reset-Fw @($Legacy); $run = New-TestRun
    $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config; $null = Invoke-LabFirewallPlan -Plan $p -BackupFile 'x' 6>$null
    $run2 = New-TestRun
    $p2 = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients @('192.0.2.254') -Config $Config
    Assert-Equal 'Update' (($p2.Actions | ForEach-Object Action) -join ',')
    $null = Invoke-LabFirewallPlan -Plan $p2 -BackupFile 'x' 6>$null
    $v = Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config)
    Assert-Equal 'DENIED' $v['192.0.2.1']; Assert-Equal 'ADMITTED' $v['192.0.2.254']
    $null = Undo-LabRun -ChangesFile $run2.ChangesFile 6>$null
    Assert-Equal 'ADMITTED' (Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config))['192.0.2.1']
}

# ---------------------------------------------------------------------------
Start-TestGroup 'failure classification (defect 1 of 1.0.3)'
$dns = 'pull model manifest: Get "https://registry.ollama.ai/v2/library/qwen3/manifests/0.6b": dial tcp: lookup registry.ollama.ai: no such host'
Test-Case 'the real 2026-10-30 DNS failure is NO_INTERNET_CONNECTION and retryable' { $c = Get-CapstoneFailureCategory -Message $dns -Stage Models; Assert-Equal 'NO_INTERNET_CONNECTION' $c.Category; Assert-True $c.Retryable }
Test-Case 'a genuinely missing tag is still MODEL_TAG_INVALID' { Assert-Equal 'MODEL_TAG_INVALID' (Get-CapstoneFailureCategory -Message 'pull model manifest: file does not exist' -HttpStatus 404).Category }
Test-Case 'disk and interrupted transfers keep their categories' {
    Assert-Equal 'DISK_SPACE_INADEQUATE' (Get-CapstoneFailureCategory -Message 'write: There is not enough space on the disk').Category
    Assert-Equal 'MODEL_DOWNLOAD_INTERRUPTED' (Get-CapstoneFailureCategory -Message 'An existing connection was forcibly closed by the remote host').Category
}
Test-Case 'the unchanged 1.0.3 module still has the defect (evidence the fix is real)' {
    $orig = Join-Path (Split-Path -Parent $root) 'provisioner\engine\modules\Capstone.Core.psm1'
    if (-not (Test-Path $orig)) { return }
    $m = Import-Module $orig -PassThru -Force -Prefix Orig -DisableNameChecking
    try { Assert-Equal 'MODEL_TAG_INVALID' (Get-OrigCapstoneFailureCategory -Message $dns).Category } finally { Remove-Module $m }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'models: profiles, selection, states'
Test-Case 'profile proposal uses the 1.0.3 thresholds' {
    Assert-Equal 'CPU8' (Get-LabProfileProposal -RamGB 8).Profile; Assert-Equal 'CPU16' (Get-LabProfileProposal -RamGB 15.5).Profile
    Assert-Equal 'CPU32' (Get-LabProfileProposal -RamGB 32).Profile; Assert-Equal 'GPU' (Get-LabProfileProposal -RamGB 32 -VramMB 16380).Profile
}
Test-Case 'CPU8 selects its 10 models and 2 embeddings; extra tags must be in the catalog' {
    $s = Select-LabModels -Catalog $Catalog -Profiles @('CPU8'); Assert-Equal 12 @($s.Models).Count
    $s2 = Select-LabModels -Catalog $Catalog -Profiles @('CPU8') -Add @('qwen3:8b', 'mystery:1b') -Exclude @('gemma3:270m')
    Assert-Equal 12 @($s2.Models).Count; Assert-Equal 1 @($s2.Refused).Count
}
Test-Case 'profiles combine without duplicates' { $a = Select-LabModels -Catalog $Catalog -Profiles @('CPU8', 'EMBEDDINGS'); Assert-Equal 16 @($a.Models).Count }
Test-Case 'manifest path for library and namespaced tags' {
    Assert-Equal 'C:\M\manifests\registry.ollama.ai\library\qwen3\0.6b' (Get-LabManifestPath -ModelDirectory 'C:\M' -Tag 'qwen3:0.6b')
    Assert-Equal 'C:\M\manifests\registry.ollama.ai\library\all-minilm\latest' (Get-LabManifestPath -ModelDirectory 'C:\M' -Tag 'all-minilm')
}
Test-Case 'offline: absent models are NOT attempted and NOT called invalid; present ones are verified' {
    $dir = Join-Path $Temp 'models'; $entry = @($Catalog.models | Where-Object tag -eq 'qwen3:0.6b')[0]
    $mp = Get-LabManifestPath -ModelDirectory $dir -Tag 'qwen3:0.6b'; New-Item -ItemType Directory -Force (Split-Path $mp) | Out-Null
    Set-Content -LiteralPath $mp -Value (@{ config = @{ digest = $entry.config_digest } } | ConvertTo-Json)
    $saved = @{}; foreach ($n in 'Get-CapstoneInstalledModels', 'Get-CapstoneDiskFacts', 'Invoke-CapstoneModelQueue') { $saved[$n] = (Get-Item "function:$n").ScriptBlock }
    try {
        function global:Get-CapstoneInstalledModels { param($BaseUri, $TimeoutSeconds) [pscustomobject]@{ Available = $true; Models = @([pscustomobject]@{ Name = 'qwen3:0.6b'; Digest = 'abc'; Size = 1 }); Error = '' } }
        function global:Get-CapstoneDiskFacts { param($Path) [pscustomobject]@{ FreeBytes = 500GB } }
        function global:Invoke-CapstoneModelQueue { param($Paths, $BaseUri, $Plan, $MaxAttempts, [switch]$SkipInference) foreach ($i in $Plan.Items) { if ($i.Status -eq 'Pending') { $i.Status = 'AlreadyInstalled' }; $i.InferenceOk = $true; $i.Status = 'Verified' }; [pscustomobject]@{ Items = $Plan.Items } }
        & (Get-Module Lab.Models) { function script:Test-LabInternet { param($HostName, $TimeoutMs) [pscustomobject]@{ Online = $false; Category = 'NO_INTERNET_CONNECTION'; Detail = 'DNS cannot resolve registry.ollama.ai' } } }
        $run = New-TestRun
        $models = @($Catalog.models | Where-Object { $_.tag -in 'qwen3:0.6b', 'qwen3:1.7b', 'all-minilm' })
        $out = Invoke-LabModelInstall -Run $run -BaseUri 'http://127.0.0.1:11434' -Models $models -ModelDirectory $dir 6>$null
        $by = @{}; foreach ($r in $out.Records) { $by[$r.tag] = $r }
        Assert-False $out.Online
        Assert-Equal 'VERIFIED' $by['qwen3:0.6b'].final; Assert-Equal 'match' $by['qwen3:0.6b'].digest; Assert-False $by['qwen3:0.6b'].download_attempted
        Assert-Equal 'NOT_DOWNLOADED_OFFLINE' $by['qwen3:1.7b'].final; Assert-Equal 'NO_INTERNET_CONNECTION' $by['qwen3:1.7b'].failure_category; Assert-False $by['qwen3:1.7b'].download_attempted
        Assert-True ($by['qwen3:1.7b'].gateway_qualified -like 'not claimed*')
    } finally {
        foreach ($n in $saved.Keys) { Set-Item -Path "function:global:$n" -Value $saved[$n] }
        Import-Module (Join-Path $root 'engine\modules\Lab.Models.psm1') -Force -Global -DisableNameChecking
    }
}
Test-Case 'a downloaded model that fails inference is installed but not verified' {
    $i = [pscustomobject]@{ Tag = 'x:1b'; Kind = 'generate'; Status = 'Failed'; Attempts = 1; FailureCategory = 'MODEL_VERIFICATION_FAILURE'; InferenceOk = $false; InferenceNote = 'empty'; LastError = 'empty' }
    $r = ConvertTo-LabModelRecord -Item $i -Digest 'match'; Assert-Equal 'INSTALLED_UNVERIFIED' $r.final; Assert-True $r.installed_locally; Assert-True $r.download_completed
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Ollama boot task: model directory (defect 3 of 1.0.3)'
Test-Case 'an explicit machine OLLAMA_MODELS is kept and stray models are named' {
    $d = Resolve-LabModelDirectory -MachineValue 'D:\models' -Candidates @([pscustomobject]@{ Path = 'C:\Users\a\.ollama\models'; Source = 'user a'; Manifests = 3 })
    Assert-Equal 'Keep' $d.Decision; Assert-Match 'C:\\Users\\a' $d.Note
}
Test-Case 'the one folder that holds models is adopted, never moved' { $d = Resolve-LabModelDirectory -Candidates @([pscustomobject]@{ Path = 'C:\Users\op\.ollama\models'; Source = 'u'; Manifests = 21 }, [pscustomobject]@{ Path = 'C:\Windows\System32\config\systemprofile\.ollama\models'; Source = 's'; Manifests = 0 }); Assert-Equal 'Adopt' $d.Decision; Assert-Equal 'C:\Users\op\.ollama\models' $d.Path }
Test-Case 'models in two folders need the operator' { $d = Resolve-LabModelDirectory -Candidates @([pscustomobject]@{ Path = 'A'; Manifests = 2 }, [pscustomobject]@{ Path = 'B'; Manifests = 5 }); Assert-Equal 'Ambiguous' $d.Decision; Assert-Equal 2 @($d.Choices).Count }
Test-Case 'a fresh head uses the lab default' { Assert-Equal 'New' (Resolve-LabModelDirectory -Candidates @()).Decision }
# Head 6 (DESKTOP-REDACT6), field run 2026-11-06 14:10: the same 12 models in LabOperator1's profile and in the SYSTEM
# profile; the 1.0.3 boot task runs ollama serve as SYSTEM without OLLAMA_MODELS, so it serves the SYSTEM copy.
$h6Names = @('all-minilm:latest', 'gemma3:1b', 'gemma3:270m', 'granite4:350m', 'granite-embedding:30m', 'llama3.2:1b', 'qwen2.5:1.5b', 'qwen3.5:0.8b', 'qwen3.5:2b', 'qwen3:0.6b', 'qwen3:1.7b', 'smollm2:360m')
$h6Sys = 'C:\WINDOWS\System32\config\systemprofile\.ollama\models'; $h6User = 'C:\Users\LabOperator1\.ollama\models'
$h6Dirs = @([pscustomobject]@{ Path = $h6User; Source = 'user profile LabOperator1'; Manifests = 12; Status = 'Observed'; Names = $h6Names },
    [pscustomobject]@{ Path = $h6Sys; Source = 'SYSTEM profile'; Manifests = 12; Status = 'Observed'; Names = $h6Names },
    [pscustomobject]@{ Path = 'C:\ProgramData\Ollama\models'; Source = 'lab default'; Manifests = 0; Status = 'Missing'; Names = @() })
$h6Task = [pscustomobject]@{ Present = $true; User = 'SYSTEM'; Action = 'C:\Users\LabOperator1\AppData\Local\Programs\Ollama\ollama.exe serve' }
Test-Case 'head 6: manifest paths become the names Ollama lists' {
    Assert-Equal 'gemma3:4b' (ConvertTo-LabManifestModelName 'registry.ollama.ai\library\gemma3\4b')
    Assert-Equal 'alibayram/smollm3:latest' (ConvertTo-LabManifestModelName 'registry.ollama.ai\alibayram\smollm3\latest')
    Assert-Equal 'hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M' (ConvertTo-LabManifestModelName 'hf.co\unsloth\SmolLM3-3B-GGUF\Q4_K_M')
    Assert-True ($null -eq (ConvertTo-LabManifestModelName 'registry.ollama.ai\library\x')) 'not a manifest path'
}
Test-Case 'head 6: the folder the running server reads is the SYSTEM profile; nothing named it' {
    Assert-Equal $h6Sys (Get-LabServingModelDirectory -Task $h6Task -Candidates $h6Dirs)
    Assert-Equal '' (Get-LabServingModelDirectory -Task $h6Task -MachineValue 'D:\m' -Candidates $h6Dirs) 'a machine variable names it'
    Assert-Equal '' (Get-LabServingModelDirectory -Task ([pscustomobject]@{ Present = $true; User = 'SYSTEM'; Action = 'cmd /c "C:\x\ollama-serve.cmd"' }) -Candidates $h6Dirs) 'the lab wrapper names it'
}
Test-Case 'head 6: two copies of the same models no longer stop option 5; the served one is kept' {
    $d = Resolve-LabModelDirectory -Candidates $h6Dirs -ServingPath $h6Sys -Served $h6Names
    Assert-Equal 'Adopt' $d.Decision; Assert-Equal $h6Sys $d.Path; Assert-Match 'left in place' $d.Note
    Assert-Equal 'Ambiguous' (Resolve-LabModelDirectory -Candidates $h6Dirs).Decision 'without knowing what is served, still the operator''s choice'
    Assert-Equal 'Ambiguous' (Resolve-LabModelDirectory -Candidates $h6Dirs -ServingPath $h6Sys -Served (@($h6Names) + 'qwen3:8b')).Decision 'a served model missing from that folder: the operator chooses'
    $insp = [pscustomobject]@{ computer = 'DESKTOP-REDACT6'; detected = [pscustomobject]@{ Role = 'head'; Reason = 'holds head address 192.0.2.6' }; os = [pscustomobject]@{ Caption = 'Microsoft Windows 11 Pro'; Arch = '64-bit' }
        hardware = [pscustomobject]@{ TotalMemoryGB = 8; NvidiaVramMB = 0; Volumes = @([pscustomobject]@{ Drive = 'C:'; FreeBytes = 100GB }) }
        network = [pscustomobject]@{ Lab = @([pscustomobject]@{ Address = '192.0.2.6'; Adapter = 'Ethernet' }) }; ollama = $null; arena = $null; capstone = $null }
    $insp.ollama = [pscustomobject]@{ Executable = 'x'; Version = '0.40.13'; ModelDirectories = $h6Dirs; Env = [pscustomobject]@{ MachineModels = '' }; Task = $h6Task
        Models = [pscustomobject]@{ Available = $true; Models = @($h6Names | ForEach-Object { [pscustomobject]@{ name = $_; digest = 'd' } }) } }
    $p = New-LabPlan -Role 'existing' -Inspection $insp -Config $Config -Choices @{ Actions = @('ollama-service'); ClientMode = 'cutover' } -Catalog $Catalog
    Assert-Equal 0 @($p.blocked).Count ($p.blocked -join '; '); Assert-Equal $h6Sys $p.model_directory
}
Test-Case 'head 6: the viewer still asking the old laptop is found by its own diagnostics' {
    $t = Get-Content -LiteralPath (Join-Path $root 'tests\fixtures\head6-20261106\diagnostics-after.txt') -Raw
    $d = Get-LabViewerDiagnosticsVerdict -Text $t
    Assert-Equal 'http://192.0.2.1:8790' $d.Address; Assert-False $d.Reachable; Assert-False $d.Authenticated; Assert-False $d.Ok
    $ok = "[INFO] Event service address     http://192.0.2.254:8790  - from installed configuration`r`n[PASS] Service reachability      192.0.2.254:8790 accepts connections`r`n[PASS] Authentication            credential v-abc accepted`r`n[PASS] Viewer subscription       snapshot at sequence 5031 · 0 assignment(s) here now"
    $d2 = Get-LabViewerDiagnosticsVerdict -Text $ok
    Assert-True $d2.Ok; Assert-Equal 'http://192.0.2.254:8790' $d2.Address
    Assert-False (Get-LabViewerDiagnosticsVerdict -Text '').Ok 'no report is not a pass'
}
Test-Case 'head 6: the viewer is pointed at the hub; every other setting is kept' {
    $cfg = '{"schema":1,"display_id":"head-192-0-2-6","role":"head","location_id":"head:head-192-0-2-6","service_id":"aes-a1e79a8b3902","version":"2.1.2","endpoints":["http://192.0.2.1:8790"],"keep_awake":true,"start_at_sign_in":true,"telemetry":true}'
    $r = ConvertTo-LabViewerConfigText -Text $cfg -Hub 'http://192.0.2.254:8790'
    Assert-True $r.Changed; $n = $r.Text | ConvertFrom-Json
    Assert-Equal 'http://192.0.2.254:8790' ((@($n.endpoints)) -join ','); Assert-True ($n.endpoints -is [array]) 'still a list'
    Assert-Equal 'head-192-0-2-6' $n.display_id; Assert-Equal 1 $n.schema; Assert-True ($n.keep_awake -eq $true) 'booleans stay booleans'
    Assert-Equal 'aes-a1e79a8b3902' $n.service_id; Assert-Equal 'http://192.0.2.1:8790' ((@($r.Before)) -join ',')
    Assert-False (ConvertTo-LabViewerConfigText -Text $r.Text -Hub 'http://192.0.2.254:8790').Changed 'already the hub: nothing to change'
}
Test-Case 'head 6: the provisioner reads the viewer''s address from its config.json "endpoints"' {
    $saved = $env:LOCALAPPDATA; $tmp = Join-Path $Temp ('la-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    try {
        New-Item -ItemType Directory -Path (Join-Path $tmp 'AgentArena'), (Join-Path $tmp 'Programs\AgentArena') -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $tmp 'Programs\AgentArena\current.txt') -Value '2.1.2'
        Set-Content -LiteralPath (Join-Path $tmp 'AgentArena\config.json') -Value '{"schema":1,"display_id":"head-192-0-2-6","endpoints":["http://192.0.2.1:8790"]}'
        $env:LOCALAPPDATA = $tmp
        Assert-Equal 'http://192.0.2.1:8790' ((@((Get-LabViewerState).Endpoints)) -join ',') '1.4.2 read "event_service_endpoints" and saw nothing'
    } finally { $env:LOCALAPPDATA = $saved }
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-True (([regex]::Matches($sp, 'Repair-LabViewerEndpoint -Run \$Run -Hub \$Config\.arena\.endpoint')).Count -ge 2) 'repaired after an install and on an installed viewer'
    Assert-Match 'Arena viewer connects to the hub \(its own diagnostics\)' $sp
}
Test-Case 'head 6: a spare copy of the served models passes; a model that is not served fails' {
    Assert-True (Test-LabServedOnDisk -Served $h6Names -Candidates $h6Dirs -ServerPath $h6Sys).Ok 'spare copy'
    Assert-Match 'spare copy' (Test-LabServedOnDisk -Served $h6Names -Candidates $h6Dirs -ServerPath $h6Sys).Detail
    $extra = @($h6Dirs[0] | Select-Object *); $extra[0].Names = @($h6Names) + 'qwen3:8b'
    $r = Test-LabServedOnDisk -Served $h6Names -Candidates @($extra + $h6Dirs[1]) -ServerPath $h6Sys
    Assert-False $r.Ok; Assert-Match 'not served: qwen3:8b' $r.Detail
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match 'Test-LabServedOnDisk -Served' $flow; Assert-Match 'NotePropertyName Exposure -NotePropertyValue \$exp' $flow 'the report shows the firewall after the run'
}
Test-Case 'the wrapper sets the binding and the model directory for the SYSTEM server' {
    $t = New-LabOllamaWrapperText -Executable 'C:\Users\op\AppData\Local\Programs\Ollama\ollama.exe' -Bind '0.0.0.0:11434' -ModelDirectory 'C:\Users\op\.ollama\models'
    Assert-Match 'set "OLLAMA_MODELS=C:\\Users\\op\\.ollama\\models"' $t; Assert-Match 'set "OLLAMA_HOST=0.0.0.0:11434"' $t; Assert-Match '"C:\\Users\\op\\AppData\\Local\\Programs\\Ollama\\ollama.exe" serve' $t
}
Test-Case 'model sets: a model missing after restart is detected' {
    $c = Compare-LabModelSet -Before ([pscustomobject]@{ Available = $true; Models = @([pscustomobject]@{ name = 'a'; digest = '1' }, [pscustomobject]@{ name = 'b'; digest = '2' }) }) -After ([pscustomobject]@{ Available = $true; Models = @([pscustomobject]@{ name = 'a'; digest = '1' }) })
    Assert-False $c.Same; Assert-Equal 'b' ($c.Missing -join ',')
}
Test-Case 'only a lab-managed OLLAMA_HOST may be replaced' { Assert-True (Test-CapstoneManagedHostValueLab '0.0.0.0:11434'); Assert-True (Test-CapstoneManagedHostValueLab '192.0.2.2:11434'); Assert-False (Test-CapstoneManagedHostValueLab '192.168.50.2:11434') }

# ---------------------------------------------------------------------------
Start-TestGroup 'roles, addresses and plans'
Test-Case 'role detection from the address plan' {
    Assert-Equal 'hub' (Get-LabDetectedRole -LabAddresses @('192.0.2.254')).Role; Assert-Equal 'gpu-workstation' (Get-LabDetectedRole -LabAddresses @('192.0.2.2')).Role
    Assert-Equal 'head' (Get-LabDetectedRole -LabAddresses @('192.0.2.7')).Role; Assert-Equal 'capstone' (Get-LabDetectedRole -LabAddresses @('192.0.2.201')).Role
    Assert-Equal 'new' (Get-LabDetectedRole).Role; Assert-Equal 'head' (Get-LabDetectedRole -OllamaInstalled $true -ModelsPresent $true).Role
}
Test-Case 'head addresses: range and protected addresses are enforced' {
    Assert-True (Get-LabAddressVerdict -Address '192.0.2.11' -Role 'cpu-head' -Config $Config).Allowed
    foreach ($a in '192.0.2.1', '192.0.2.2', '192.0.2.105', '192.0.2.201', '192.0.2.254', '192.0.2.200', '203.0.113.15') { Assert-False (Get-LabAddressVerdict -Address $a -Role 'cpu-head' -Config $Config).Allowed "$a must be refused for a head" }
}
Test-Case 'application addresses: planned address, range' {
    Assert-True (Get-LabAddressVerdict -Address '192.0.2.201' -Role 'capstone' -Config $Config).Allowed
    $v = Get-LabAddressVerdict -Address '192.0.2.210' -Role 'capstone' -Config $Config; Assert-True $v.Allowed; Assert-Equal 1 @($v.Warnings).Count
    Assert-False (Get-LabAddressVerdict -Address '192.0.2.202' -Role 'capstone' -Config $Config).Allowed
    Assert-False (Get-LabAddressVerdict -Address '192.0.2.20' -Role 'coder' -Config $Config).Allowed
}
$fakeInspection = { param([string]$Detected = 'new', [string[]]$Lab = @(), [double]$Ram = 16, [int]$Vram = 0)
    [pscustomobject]@{ computer = 'TEST'; detected = [pscustomobject]@{ Role = $Detected; Reason = 'test' }; os = [pscustomobject]@{ Caption = 'Microsoft Windows 11 Pro'; Arch = '64-bit' }
        hardware = [pscustomobject]@{ TotalMemoryGB = $Ram; NvidiaVramMB = $Vram; Volumes = @([pscustomobject]@{ Drive = 'C:'; FreeBytes = 400GB }) }
        network = [pscustomobject]@{ Lab = @($Lab | ForEach-Object { [pscustomobject]@{ Address = $_; Adapter = 'Ethernet' } }) }
        ollama = [pscustomobject]@{ Executable = ''; Version = ''; ModelDirectories = @(); Env = [pscustomobject]@{ MachineModels = '' } }; arena = $null; capstone = $null } }
function Get-TestPlan { param([string]$Role, $Insp, [hashtable]$Choices)
    $saved = (Get-Item function:Test-CapstoneAddressInUse).ScriptBlock
    function global:Test-CapstoneAddressInUse { param($IPAddress, $TcpPort, $HeldLocally) [pscustomobject]@{ Verdict = 'Clear'; Reason = 'test' } }
    try { return (New-LabPlan -Role $Role -Inspection $Insp -Config $Config -Choices $Choices -Catalog $Catalog) } finally { Set-Item function:global:Test-CapstoneAddressInUse -Value $saved }
}
Test-Case 'new CPU head: full plan, cutover clients, CPU16 proposal, nothing blocked' {
    $p = Get-TestPlan 'cpu-head' (& $fakeInspection) @{ Address = '192.0.2.11' }
    Assert-Equal 0 @($p.blocked).Count ($p.blocked -join '; ')
    Assert-Equal 'network,time,power,ollama-install,ollama-service,ollama-start,firewall,arena-claim,viewer,models' (($p.steps | ForEach-Object id) -join ',')
    Assert-Equal 'CPU16' $p.profile.Proposed; Assert-Equal '192.0.2.254,192.0.2.1' ($p.clients -join ','); Assert-Equal 'C:\ProgramData\Ollama\models' $p.model_directory
}
Test-Case 'a new-head install refuses an existing head and the GPU workstation' {
    Assert-True (@((Get-TestPlan 'cpu-head' (& $fakeInspection 'head' @('192.0.2.7')) @{ Address = '192.0.2.7' }).blocked).Count -gt 0)
    Assert-True (@((Get-TestPlan 'gpu-head' (& $fakeInspection 'gpu-workstation' @('192.0.2.2') 32 16380) @{ Address = '192.0.2.2' }).blocked).Count -gt 0)
}
Test-Case 'a GPU head needs a qualifying GPU' {
    Assert-True (@((Get-TestPlan 'gpu-head' (& $fakeInspection) @{ Address = '192.0.2.12' }).blocked | Where-Object { $_ -match 'VRAM' }).Count -eq 1)
    Assert-Equal 0 @((Get-TestPlan 'gpu-head' (& $fakeInspection 'new' @() 32 16380) @{ Address = '192.0.2.12' }).blocked).Count
}
Test-Case 'Capstone without a package: install PENDING, never invented' {
    $p = Get-TestPlan 'capstone' (& $fakeInspection 'capstone' @('192.0.2.201')) @{}
    Assert-Equal 'pending' (@($p.steps | Where-Object id -eq 'capstone-install')[0].state)
    Assert-True (@($p.external | Where-Object { $_ -match 'capstone-research\.token' }).Count -eq 1)
    Assert-Equal 'gateway-client,capstone-configure,capstone-task' ((@($p.steps | Where-Object { $_.id -in 'gateway-client', 'capstone-configure', 'capstone-task' }) | ForEach-Object id) -join ',') 'the CA folder is prepared before Capstone''s Configure, which refuses a missing CA'
}
Test-Case 'Capstone RC.2 over an older installation: Update-Capstone, not a second install' {
    $insp = & $fakeInspection 'capstone' @('192.0.2.201')
    $insp | Add-Member -NotePropertyName capstone -NotePropertyValue ([pscustomobject]@{ Installed = $true; Version = '5.0.0-alpha.2' }) -Force
    $p = Get-TestPlan 'capstone' $insp @{ CapstonePackage = 'F:\AGENT-ARENA-LAB\packages\capstone\Capstone-6.0.0-rc.2-windows-x64.zip' }
    $st = @($p.steps | Where-Object id -eq 'capstone-install')[0]
    Assert-Match 'Update-Capstone' $st.title; Assert-Match 'Candidate A' $st.preserves
    $insp.capstone.Version = '6.0.0-rc.2'
    Assert-Match '^Keep Capstone 6\.0\.0-rc\.2' (@((Get-TestPlan 'capstone' $insp @{ CapstonePackage = 'x\Capstone-6.0.0-rc.2-windows-x64.zip' }).steps | Where-Object id -eq 'capstone-install')[0].title)
}
Test-Case 'Coder: installation PENDING; the Gateway PENDING on the hub' {
    Assert-Equal 'pending' (@((Get-TestPlan 'coder' (& $fakeInspection 'coder' @('192.0.2.202')) @{}).steps | Where-Object id -eq 'coder-install')[0].state)
    Assert-Equal 'pending' (@((Get-TestPlan 'hub' (& $fakeInspection 'hub' @('192.0.2.254')) @{}).steps | Where-Object id -eq 'gateway')[0].state)
}

# ---------------------------------------------------------------------------
Start-TestGroup 'stages, final status and reports'
Test-Case 'a failed step stops the stage: later steps do not run and nothing reports success' {
    $run = New-TestRun
    $null = Invoke-LabStep -Id 'a' -Title 'first' -Action { New-LabOutcome 'DONE' 'ok' } 6>$null
    $null = Invoke-LabStep -Id 'b' -Title 'second' -Action { throw 'boom' } 6>$null
    $null = Invoke-LabStep -Id 'c' -Title 'third' -Action { New-LabOutcome 'DONE' 'should not run' } 6>$null
    Assert-Equal 'DONE,FAILED,NOT_RUN' (($run.Steps | ForEach-Object status) -join ',')
    Assert-Equal 'failed' (Get-LabFinalStatus -Steps @($run.Steps) -Verification @([pscustomobject]@{ status = 'PASS'; required = $true }))
}
Test-Case 'final status rules' {
    $pass = [pscustomobject]@{ status = 'PASS'; required = $true }; $pend = [pscustomobject]@{ status = 'PENDING'; required = $true }; $optFail = [pscustomobject]@{ status = 'FAIL'; required = $false }
    Assert-Equal 'verified' (Get-LabFinalStatus -Steps @([pscustomobject]@{ status = 'DONE' }) -Verification @($pass))
    Assert-Equal 'partially verified' (Get-LabFinalStatus -Steps @([pscustomobject]@{ status = 'DONE' }) -Verification @($pass, $pend))
    Assert-Equal 'partially verified' (Get-LabFinalStatus -Steps @([pscustomobject]@{ status = 'DONE' }) -Verification @($pass, $optFail) -Pending @('x'))
    Assert-Equal 'blocked' (Get-LabFinalStatus -Blocked @('refused'))
    Assert-Equal 'blocked' (Get-LabFinalStatus -Declined $true)
    Assert-Equal 'failed' (Get-LabFinalStatus -Registration @([pscustomobject]@{ status = 'FAILED' }))
    Assert-Equal 'partially verified' (Get-LabFinalStatus -Steps @([pscustomobject]@{ status = 'DONE' }) -Verification @()) 'no passed check is never verified'
}
Test-Case 'redaction: codes, tokens and secret-named keys never reach a report' {
    $o = [pscustomobject]@{ note = 'code ARENA-ABCD-EFGH-JKLM-NPQR was typed'; secret = 'hunter2'; nested = @{ sealed_secret = 'xyz'; token_file = 'C:\t'; exit_code = 3 }; header = 'X-Infrastructure-Token: abc123' }
    $j = Protect-LabObject $o | ConvertTo-Json -Depth 6
    Assert-False ($j -match 'ABCD-EFGH|hunter2|xyz|abc123') $j; Assert-Match '"exit_code":\s*3' $j
}
Test-Case 'a report is written in JSON and text, sanitized, with its final status' {
    $run = New-TestRun
    $run.Plan = [pscustomobject]@{ blocked = @(); steps = @(); address = '192.0.2.11'; external = @(); warnings = @(); preserves = @() }
    $null = Add-LabVerification -Check 'x' -Status PASS 6>$null; $null = Add-LabPending -Area 'gateway' -Text 'not deployed; code ARENA-ABCD-EFGH-JKLM-NPQR' 6>$null
    $r = New-LabReport -Run $run
    Assert-Equal 'partially verified' $r.Status; Assert-True (Test-Path $r.Json); Assert-True (Test-Path $r.Text)
    Assert-False ((Get-Content $r.Text -Raw) -match 'ABCD-EFGH'); Assert-Match 'FINAL STATUS : PARTIALLY VERIFIED' (Get-Content $r.Text -Raw)
}
Test-Case 'Undo-LabRun refuses an unknown change type and stops there' {
    $run = New-TestRun
    $null = Register-LabChange -Kind 'mystery' -Description 'unknown' -Undo ([pscustomobject]@{ type = 'mystery' }) 6>$null
    $res = @(Undo-LabRun -ChangesFile $run.ChangesFile 6>$null)
    Assert-Equal 1 $res.Count; Assert-True ($res[0] -like 'FAILED*') $res[0]
}

# ---------------------------------------------------------------------------
Start-TestGroup 'hub A: reversible network, power, time and firewall prerequisites'
Test-Case 'the hub time-server check matches SET-UP-THIS-COMPUTER''s settings' {
    $ok = [pscustomobject]@{ TimeZone = 'Eastern Standard Time'; NtpServer = 'time.windows.com,0x8'; Type = 'NTP'; AnnounceFlags = 5; ServerEnabled = 1; StartMode = 'Auto' }
    Assert-True (Test-LabTimeServerCompliant -State $ok -Zone 'Eastern Standard Time' -Upstream 'time.windows.com')
    $client = $ok | Select-Object *; $client.AnnounceFlags = 10; Assert-False (Test-LabTimeServerCompliant -State $client -Zone 'Eastern Standard Time' -Upstream 'time.windows.com')
}
Test-Case 'the hub''s power check also needs the screen on and the lid ignored' {
    $s = [pscustomobject]@{ StandbyAcSeconds = 0; HibernateAcSeconds = 0; MonitorAcSeconds = 600; LidAc = 1; AdapterPowerOff = 'Disabled'; Eee = @() }
    Assert-True (Test-LabPowerCompliant -State $s); Assert-False (Test-LabPowerCompliant -State $s -StayOn)
    $s.MonitorAcSeconds = 0; $s.LidAc = 0; Assert-True (Test-LabPowerCompliant -State $s -StayOn)
}
Test-Case 'time and ping rules are added through the journal and removed by undo' {
    Reset-Fw @(); $run = New-TestRun
    $r = Set-LabHubFirewallPrerequisites -Run $run -Lan '192.0.2.0/24' 6>$null
    if (@(Get-NetFirewallRule -DisplayName 'Arena time server' -ErrorAction SilentlyContinue).Count) { Assert-Equal 'UNCHANGED' $r.Outcome; return }   # this machine already has them
    Assert-Equal 2 @($script:Rules).Count; Assert-Equal 2 @($run.Changes).Count
    $null = Undo-LabRun -ChangesFile $run.ChangesFile 6>$null; Assert-Equal 0 @($script:Rules).Count
}
Test-Case 'registry and file changes undo exactly' {
    $key = 'HKCU:\Software\AgentArenaLabTest'; New-Item -Path $key -Force | Out-Null; Set-ItemProperty -Path $key -Name v -Value 7
    $f = Join-Path $Temp 'cfg.json'; Set-Content -LiteralPath $f -Value '{"a":1}' -NoNewline; $bk = Join-Path $Temp 'cfg.bak'; Copy-Item $f $bk
    try {
        $run = New-TestRun
        Set-ItemProperty -Path $key -Name v -Value 0; $null = Register-LabChange -Kind 'registry' -Description 't' -Undo ([pscustomobject]@{ type = 'registry'; path = $key; name = 'v'; value = 7 }) 6>$null
        Set-Content -LiteralPath $f -Value '{"a":2}' -NoNewline; $null = Register-LabChange -Kind 'arena-config' -Description 't' -Undo ([pscustomobject]@{ type = 'file-restore'; path = $f; backup = $bk }) 6>$null
        $null = Undo-LabRun -ChangesFile $run.ChangesFile 6>$null
        Assert-Equal 7 (Get-ItemProperty -Path $key -Name v).v; Assert-Equal '{"a":1}' (Get-Content -LiteralPath $f -Raw)
    } finally { Remove-Item -Path $key -Recurse -Force -ErrorAction SilentlyContinue }
}
Test-Case 'hub plan: reversible steps replace the one-shot setup tool; Gateway stays pending' {
    $insp = & $fakeInspection 'hub' @('192.0.2.254'); $insp | Add-Member -NotePropertyName hubgw -NotePropertyValue $null
    $p = Get-TestPlan 'hub' $insp @{}
    Assert-Equal 'hub-network,hub-profile,hub-power,hub-time,hub-firewall,hub-python,hub-install,arena-gateway-config,gateway' (($p.steps | ForEach-Object id) -join ',')
    Assert-Equal 'pending' (@($p.steps | Where-Object id -eq 'gateway')[0].state)
}
Test-Case 'hub plan warns, without blocking Arena, when a Gateway account rule is unmet' {
    $insp = & $fakeInspection 'hub' @('192.0.2.254'); $insp | Add-Member -NotePropertyName hubgw -NotePropertyValue ([pscustomobject]@{ Accounts = [pscustomobject]@{ State = 'BLOCKED'; Blockers = @('the Arena account X is a direct member of Administrators'); Unknown = @() } })
    $p = Get-TestPlan 'hub' $insp @{}
    Assert-Equal 0 @($p.blocked).Count; Assert-True (@($p.warnings | Where-Object { $_ -match 'Administrators' }).Count -eq 1)
    $d = @($p.steps | Where-Object id -eq 'gateway')[0].detail
    Assert-Match 'Before that choice can run: the Arena account X' $d; Assert-Match 'Administration hub > 3' $d
}
Test-Case 'DESKTOP-REDACT10 (1.3.0): no stale "Gateway installer not delivered" text; 0.3.0 is on the USB, installed by choice 2' {
    $insp = & $fakeInspection 'hub' @('192.0.2.254'); $insp | Add-Member -NotePropertyName hubgw -NotePropertyValue $null
    $p = Get-TestPlan 'hub' $insp @{}
    $all = (@($p.steps | ForEach-Object { $_.title + ' ' + $_.detail }) + @($p.external) + @($p.warnings)) -join ' | '
    Assert-False ($all -match 'no Windows installer|deployment contract|has not supplied|When it arrives|release with its installer') $all
    $g = @($p.steps | Where-Object id -eq 'gateway')[0]
    Assert-Match ('Inference Gateway {0}' -f [regex]::Escape($Config.gateway.expected_release)) $g.title; Assert-Match 'Administration hub > 2' $g.title; Assert-Match 'is on this USB' $g.detail
    $src = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-False ($src -match 'has not supplied its Windows installer|no Gateway deployment contract|when the Gateway package arrives') 'the arena-mode gateway step still carries the pre-0.3.0 text'
}

Start-TestGroup 'hub B: outbound policy (rule analysis)'
function New-OutSnap { param([array]$Rules = @(), [string]$Default = 'NotConfigured') [pscustomobject]@{ Rules = $Rules; Profiles = @([pscustomobject]@{ Name = 'Private'; Enabled = $true; DefaultOutboundAction = $Default }); Connections = @([pscustomobject]@{ InterfaceAlias = 'Ethernet'; NetworkCategory = 'Private' }) } }
function New-OutRule { param([string]$Action = 'Block', [string[]]$Ports = @('11434'), [string[]]$Remote = @('Any'), [string]$Program = 'Any') [pscustomobject]@{ Name = 'o'; DisplayName = "out-$Action"; Enabled = $true; Action = $Action; Profiles = @('Domain', 'Private', 'Public'); Protocol = 'TCP'; RemotePorts = $Ports; RemoteAddresses = $Remote; Program = $Program; PolicySource = 'Local' } }
Test-Case 'default policy: allowed' { Assert-Equal 'ALLOWED' (Get-LabOutboundVerdict -Snapshot (New-OutSnap) -Heads @('192.0.2.3')).Heads[0].Verdict }
Test-Case 'a block rule for the lab on 11434: blocked; another subnet: allowed' {
    Assert-Equal 'BLOCKED' (Get-LabOutboundVerdict -Snapshot (New-OutSnap @((New-OutRule -Remote @('192.0.2.0/24')))) -Heads @('192.0.2.3')).Heads[0].Verdict
    Assert-Equal 'ALLOWED' (Get-LabOutboundVerdict -Snapshot (New-OutSnap @((New-OutRule -Remote @('192.168.0.0/16')))) -Heads @('192.0.2.3')).Heads[0].Verdict
}
Test-Case 'outbound blocked by default: needs an allow rule' {
    Assert-Equal 'BLOCKED' (Get-LabOutboundVerdict -Snapshot (New-OutSnap @() 'Block') -Heads @('192.0.2.3')).Heads[0].Verdict
    Assert-Equal 'ALLOWED' (Get-LabOutboundVerdict -Snapshot (New-OutSnap @((New-OutRule -Action 'Allow')) 'Block') -Heads @('192.0.2.3')).Heads[0].Verdict
}
Test-Case 'a program-specific rule is uncertain until the Gateway names its runtime' { Assert-Equal 'UNCERTAIN' (Get-LabOutboundVerdict -Snapshot (New-OutSnap @((New-OutRule -Program 'C:\x\python.exe'))) -Heads @('192.0.2.3')).Heads[0].Verdict }

Start-TestGroup 'hub C: Arena''s Gateway observation configuration and states'
$cfgJson = '{"db_path":"C:\\d\\arena.db","public_endpoints":["http://192.0.2.254:8790"],"admin":{"port":8791,"tags":["a"]},"gateway":{"observation_file":"old","resource_heads":{"gpu":"head-192-0-2-2"}}}'
Test-Case 'merge replaces only the gateway block and keeps the operator''s resource_heads' {
    $t = Merge-LabGatewayConfig -Json $cfgJson -Config $Config; $o = $t | ConvertFrom-Json
    Assert-Equal $Config.gateway.observation_file $o.gateway.observation_file; Assert-Equal 'gateway-254' $o.gateway.gateway_id; Assert-Equal 30 $o.gateway.stale_seconds
    Assert-Equal 'head-192-0-2-2' $o.gateway.resource_heads.gpu
    Assert-Equal 'http://192.0.2.254:8790' (@($o.public_endpoints)[0]); Assert-True ($o.public_endpoints -is [array]) 'a one-element list must stay a list'
    Assert-Equal 'a' (@($o.admin.tags)[0]); Assert-True ($o.admin.tags -is [array]); Assert-Equal 'C:\d\arena.db' $o.db_path
}
Test-Case 'writing the block keeps a backup; undo restores the file byte for byte' {
    $f = Join-Path $Temp 'aes.config.json'; [IO.File]::WriteAllText($f, $cfgJson); $before = [IO.File]::ReadAllBytes($f)
    $run = New-TestRun
    $r = Set-LabArenaGatewayConfig -Run $run -Config $Config -Path $f 6>$null; Assert-Equal 'DONE' $r.Outcome
    Assert-Equal 'UNCHANGED' (Set-LabArenaGatewayConfig -Run $run -Config $Config -Path $f 6>$null).Outcome
    Assert-False ([IO.File]::ReadAllBytes($f)[0] -eq 0xEF) 'no BOM for the Python service'
    $null = Undo-LabRun -ChangesFile $run.ChangesFile 6>$null
    Assert-Equal ([Convert]::ToBase64String($before)) ([Convert]::ToBase64String([IO.File]::ReadAllBytes($f)))
}
Test-Case 'observation states are kept apart; live only with a running publisher' {
    Assert-Match '^PENDING GATEWAY INSTALLATION' (Get-LabObservationState -Configured $true -FilePresent $false).summary
    Assert-False (Get-LabObservationState -Configured $true -FilePresent $true -Access 'verified' -Fresh $true -Contract 'shared.inference.observation/1' -ServiceRunning $false).live
    Assert-Match '^STALE' (Get-LabObservationState -Configured $true -FilePresent $true -Access 'verified' -Fresh $false -Contract 'shared.inference.observation/1' -ServiceRunning $true).summary
    Assert-Match '^NOT CONFIGURED' (Get-LabObservationState -Configured $false -FilePresent $true -ServiceRunning $true).summary
    Assert-True (Get-LabObservationState -Configured $true -FilePresent $true -Access 'verified' -Fresh $true -Contract 'shared.inference.observation/1' -ServiceRunning $true).live
}
Test-Case 'Arena''s access: read on the publication, nothing on ledger/state/logs' {
    $arena = 'S-1-5-21-1-2-3-1001'
    $ok = [pscustomobject]@{ publication = [pscustomobject]@{ AllowSids = @('S-1-5-18', $arena) }; ledger = [pscustomobject]@{ AllowSids = @('S-1-5-18') }; state = [pscustomobject]@{ AllowSids = @('S-1-5-18') }; logs = [pscustomobject]@{ AllowSids = @('S-1-5-18') } }
    Assert-Equal 'verified' (Get-LabArenaObservationAccess -ArenaSids @($arena) -Folders $ok)
    $bad = $ok | Select-Object *; $bad.ledger = [pscustomobject]@{ AllowSids = @($arena) }; Assert-Match 'ledger' (Get-LabArenaObservationAccess -ArenaSids @($arena) -Folders $bad)
    $none = $ok | Select-Object *; $none.publication = [pscustomobject]@{ AllowSids = @('S-1-5-18') }; Assert-Match 'no read grant' (Get-LabArenaObservationAccess -ArenaSids @($arena) -Folders $none)
}

Start-TestGroup 'hub D: verified head inventory'
Test-Case 'only heads that answered as Ollama are listed, under their Arena node IDs; nothing is enrolled' {
    $probes = @([pscustomobject]@{ Head = '192.0.2.3'; Open = $true; Version = '0.40.13'; Models = @([pscustomobject]@{ name = 'qwen3:4b'; digest = 'd1' }); ModelsRead = $true; CheckedAt = 't' },
                [pscustomobject]@{ Head = '192.0.2.4'; Open = $false; Version = ''; Models = @(); ModelsRead = $false; CheckedAt = 't' },
                [pscustomobject]@{ Head = '192.0.2.5'; Open = $true; Version = ''; Models = @(); ModelsRead = $false; CheckedAt = 't' },
                [pscustomobject]@{ Head = '192.0.2.254'; Open = $true; Version = '1'; Models = @(); ModelsRead = $false; CheckedAt = 't' })
    $inv = New-LabHeadInventory -Probes $probes -Config $Config -From '192.0.2.254' -Computer 'HUB'
    Assert-Equal 1 @($inv.heads).Count; Assert-Equal 'head-192-0-2-3' $inv.heads[0].resource_id; Assert-Equal 'http://192.0.2.3:11434' $inv.heads[0].endpoint
    Assert-Equal 3 @($inv.excluded).Count; Assert-Match '^accepted' $inv.status; Assert-Match 'nothing has been enrolled' $inv.purpose
    Assert-False (($inv | ConvertTo-Json -Depth 8) -match 'token|secret|password')
}

Start-TestGroup 'hub E: Arena identity and the Gateway''s account rules (read-only)'
$arenaSid = 'S-1-5-21-1-2-3-1001'; $gwSid = 'S-1-5-21-1-2-3-1002'; $std = @([pscustomobject]@{ Sid = 'S-1-5-21-1-2-3-1001'; State = 'standard'; Groups = @(); Unreadable = @() })
Test-Case 'an Arena account in Administrators (directly or nested) blocks the Gateway stage' { $v = Get-LabGatewayAccountVerdict -ArenaSids @($arenaSid) -Membership @([pscustomobject]@{ Sid = $arenaSid; State = 'privileged'; Groups = @('Administrators'); Unreadable = @() }) -GatewaySid $gwSid; Assert-Equal 'BLOCKED' $v.State; Assert-Match 'Administrators' ($v.Blockers -join ' ') }
Test-Case 'Arena and the Gateway on one account blocks it' { Assert-Equal 'BLOCKED' (Get-LabGatewayAccountVerdict -ArenaSids @($arenaSid) -Membership $std -GatewaySid $arenaSid).State }
Test-Case 'Users or Arena on the ledger folder blocks it' {
    $folders = [pscustomobject]@{ ledger = [pscustomobject]@{ Path = 'L'; AllowSids = @('S-1-5-32-545') }; state = [pscustomobject]@{ Path = 'S'; AllowSids = @() }; logs = [pscustomobject]@{ Path = 'G'; AllowSids = $null } }
    Assert-Equal 'BLOCKED' (Get-LabGatewayAccountVerdict -ArenaSids @($arenaSid) -Membership $std -GatewaySid $gwSid -Folders $folders).State
}
Test-Case 'unknown Arena account or unreadable Administrators: UNKNOWN, never OK' {
    Assert-Equal 'UNKNOWN' (Get-LabGatewayAccountVerdict -ArenaSids @()).State
    Assert-Equal 'UNKNOWN' (Get-LabGatewayAccountVerdict -ArenaSids @($arenaSid) -Membership $std -MembershipStatus 'inaccessible').State; Assert-Equal 'UNKNOWN' (Get-LabGatewayAccountVerdict -ArenaSids @($arenaSid) -Membership @([pscustomobject]@{ Sid = $arenaSid; State = 'unknown'; Groups = @(); Unreadable = @('Backup Operators') })).State
}
Test-Case 'a standard Arena account with clean folders is OK' { Assert-Equal 'OK' (Get-LabGatewayAccountVerdict -ArenaSids @($arenaSid) -Membership $std -GatewaySid $gwSid).State }
Test-Case 'the live identity read works on this machine (read-only)' { $id = Get-LabArenaIdentity; Assert-True ($id.Current.Sid -match '^S-1-5-'); Assert-True ($id.AdministratorsStatus -in 'observed', 'inaccessible') }

# ---------------------------------------------------------------------------
# The first physical run on DESKTOP-REDACT10 (192.0.2.254), Lab Provisioner 1.3.0, 2026-11-05. The two reports
# are kept in tests\fixtures\device1-20261105; the rule sets below are the rules those reports name,
# with the filters Windows 11 26200 gives them (read on the development laptop, which has the same).
# ---------------------------------------------------------------------------
$fx = Join-Path $here 'fixtures\device1-20261105'
$d1Hub = Get-Content -LiteralPath (Join-Path $fx 'DESKTOP-REDACT10-20261105-110826-DESKTOP-REDACT10-hub.json') -Raw | ConvertFrom-Json
$d1Insp = Get-Content -LiteralPath (Join-Path $fx 'DESKTOP-REDACT10-20261105-105852-DESKTOP-REDACT10-inspect.json') -Raw | ConvertFrom-Json
function New-D1OutRule { param([string]$Name, [string]$Program = 'Any', [string]$Service = 'Any', [string]$Package = '', [string]$Action = 'Allow', [string]$Protocol = 'TCP', [string[]]$Remote = @('Any'))
    [pscustomobject]@{ Name = $Name; DisplayName = $Name; Enabled = $true; Action = $Action; Profiles = @('Domain', 'Private', 'Public'); Protocol = $Protocol; RemotePorts = @('Any'); RemoteAddresses = $Remote; Program = $Program; Service = $Service; Package = $Package; PolicySource = 'Local' } }
$sys32 = 'C:\WINDOWS\system32'
$d1OutRules = @(
    (New-D1OutRule 'Wi-Fi Direct Spooler Use (Out)' "$sys32\spoolsv.exe" 'Spooler' -Protocol 'Any'), (New-D1OutRule 'Connected Devices Platform - Wi-Fi Direct Transport (TCP-Out)' "$sys32\svchost.exe" 'CDPSvc'),
    (New-D1OutRule 'Windows Device Management Enrollment Service (TCP out)' "$sys32\svchost.exe" 'DmEnrollmentSvc'), (New-D1OutRule 'Windows Device Management Certificate Installer (TCP out)' "$sys32\dmcertinst.exe"),
    (New-D1OutRule 'Wireless Display (TCP-Out)' "$sys32\WUDFHost.exe"), (New-D1OutRule 'Proximity sharing over TCP (TCP sharing-Out)' "$sys32\proximityuxhost.exe"),
    (New-D1OutRule 'Wi-Fi Direct Network Discovery (Out)' "$sys32\dashost.exe" -Protocol 'Any'), (New-D1OutRule 'WFD Driver-only (TCP-Out)' 'System'),
    (New-D1OutRule 'Wi-Fi Direct Scan Service Use (Out)' "$sys32\svchost.exe" 'stisvc' -Protocol 'Any'), (New-D1OutRule 'Windows Device Management Sync Client (TCP out)' "$sys32\omadmclient.exe"),
    (New-D1OutRule 'ms-resource:ProductPkgDisplayName' 'C:\WINDOWS\SystemApps\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\WindowsBackupClient.exe'), (New-D1OutRule 'ms-resource:ProductPkgDisplayName' 'C:\WINDOWS\SystemApps\MicrosoftWindows.Client.OOBE_cw5n1h2txyewy\OobeHostApp.exe'),
    (New-D1OutRule 'Microsoft 365 Copilot' -Package 'Microsoft.MicrosoftOfficeHub_8wekyb3d8bbwe' -Remote @('Intranet')), (New-D1OutRule 'Microsoft Teams' 'C:\Program Files\WindowsApps\MSTeams_26276.513.5241.9675_x64__8wekyb3d8bbwe\ms-teams.exe'),
    (New-D1OutRule 'Microsoft Teams' -Package 'MSTeams_8wekyb3d8bbwe'))
function New-D1OutSnap { param([array]$Rules, [string]$Default = 'Allow') [pscustomobject]@{ Rules = $Rules; Profiles = @([pscustomobject]@{ Name = 'Public'; Enabled = $true; DefaultOutboundAction = $Default }); Connections = @([pscustomobject]@{ InterfaceAlias = 'Ethernet'; NetworkCategory = 'Public' }) } }
$gwPy = [string]$Config.runtime.python.path; $gwSvc = [string]$Config.gateway.service_name
$d1Heads = @($Config.known_heads)
$d1InRules = @(foreach ($r in @($d1Insp.security.RelevantRules)) { $t = New-TestRule -Name $r.Name -DisplayName $r.DisplayName -LocalPorts @('Any'); $t | Add-Member -NotePropertyName Package -NotePropertyValue (($r.Name -split 'S-1-5-')[0] -replace '-In-Allow-ServerCapability$', ''); $t })

Start-TestGroup 'DESKTOP-REDACT10 (1.3.0) finding 2: firewall analysis without false failures'
Test-Case 'the report''s own outbound rules: every head ALLOWED (1.3.0 said UNCERTAIN for all nine)' {
    Assert-True (@($d1Insp.inventory | Where-Object { $_.area -eq 'outbound' -and $_.value -like 'UNCERTAIN*' }).Count -eq 9) 'the fixture is the failing 1.3.0 report'
    foreach ($withRuntime in $true, $false) {
        $v = Get-LabOutboundVerdict -Snapshot (New-D1OutSnap $d1OutRules) -Heads $d1Heads -InterfaceAlias 'Ethernet' -Programs $(if ($withRuntime) { @($gwPy) } else { @() }) -ServiceName $gwSvc
        foreach ($h in $v.Heads) { Assert-Equal 'ALLOWED' $h.Verdict ("{0}: {1}" -f $h.Head, $h.Reason) }
    }
}
Test-Case 'real outbound blocks still fail: Any program, the Gateway runtime, the Gateway service' {
    foreach ($r in @((New-D1OutRule 'block all' -Action 'Block'), (New-D1OutRule 'block py' $gwPy -Action 'Block'), (New-D1OutRule 'block svc' 'Any' $gwSvc -Action 'Block'), (New-D1OutRule 'block lab' -Action 'Block' -Remote @('192.0.2.0/24')))) {
        Assert-Equal 'BLOCKED' (Get-LabOutboundVerdict -Snapshot (New-D1OutSnap (@($d1OutRules) + $r)) -Heads @('192.0.2.3') -Programs @($gwPy) -ServiceName $gwSvc).Heads[0].Verdict $r.Name
    }
}
Test-Case 'blocks for other programs, services or app packages do not apply to the Gateway; doubtful ones stay UNCERTAIN' {
    $one = { param($r) (Get-LabOutboundVerdict -Snapshot (New-D1OutSnap @($r)) -Heads @('192.0.2.3') -Programs @($gwPy) -ServiceName $gwSvc).Heads[0].Verdict }
    Assert-Equal 'ALLOWED' (& $one (New-D1OutRule 'spooler' "$sys32\spoolsv.exe" -Action 'Block'))
    Assert-Equal 'ALLOWED' (& $one (New-D1OutRule 'svc' 'Any' 'Spooler' -Action 'Block'))
    Assert-Equal 'ALLOWED' (& $one (New-D1OutRule 'pkg' -Package 'MSTeams_8wekyb3d8bbwe' -Action 'Block'))
    Assert-Equal 'UNCERTAIN' (& $one (New-D1OutRule 'other python' 'C:\Tools\Python312\python.exe' -Action 'Block'))
    Assert-Equal 'UNCERTAIN' (& $one (New-D1OutRule 'keyword' -Action 'Block' -Remote @('Intranet')))
    Assert-Equal 'UNCERTAIN' ((Get-LabOutboundVerdict -Snapshot (New-D1OutSnap @((New-D1OutRule 'x' "$sys32\spoolsv.exe" -Action 'Block'))) -Heads @('192.0.2.3')).Heads[0].Verdict) 'with the runtime unknown, a program rule stays uncertain'
}
Test-Case 'outbound blocked by default: only an allow rule that covers the Gateway opens it' {
    Assert-Equal 'BLOCKED' (Get-LabOutboundVerdict -Snapshot (New-D1OutSnap $d1OutRules 'Block') -Heads @('192.0.2.3') -Programs @($gwPy) -ServiceName $gwSvc).Heads[0].Verdict
    Assert-Equal 'ALLOWED' (Get-LabOutboundVerdict -Snapshot (New-D1OutSnap (@($d1OutRules) + (New-D1OutRule 'gw out' $gwPy)) 'Block') -Heads @('192.0.2.3') -Programs @($gwPy) -ServiceName $gwSvc).Heads[0].Verdict
}
Test-Case 'the report''s inbound "BROAD" rules are Store-app rules: they admit no one to a desktop program' {
    Assert-Equal 8 @($d1InRules).Count
    Assert-True (@($d1InRules | Where-Object { -not $_.Package }).Count -eq 0) (($d1InRules | ForEach-Object Name) -join ', ')
    Reset-Fw $d1InRules
    $v = Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp '192.0.2.254' -Sources $sources -Config $Config)
    foreach ($s in $sources) { Assert-Equal 'DENIED' $v[$s] $s }
    $p = New-LabFirewallPlan -Snapshot (Get-FwSnapshot) -HeadIp $Head -Clients $cutover -Config $Config
    Assert-Equal 0 @($p.Review).Count ($p.Review -join '; '); Assert-Equal 'APP_PACKAGE' (Get-LabRuleClass -Rule $d1InRules[0] -Config $Config)
    # the same rules without the package binding are a genuine broad rule, and are still caught
    Reset-Fw @($d1InRules | ForEach-Object { $c = $_ | Select-Object *; $c.Package = ''; $c })
    Assert-Equal 'ADMITTED' (Get-Verdicts (Get-LabOllamaExposure -Snapshot (Get-FwSnapshot) -HeadIp $Head -Sources $sources -Config $Config))['192.0.2.201']
}
Test-Case 'the live snapshot records the package of Store-app rules on this machine (read-only)' {
    $snap = Get-LabFirewallSnapshotLive
    $cap = @($snap.Rules | Where-Object { $_.Name -like '*-In-Allow-ServerCapability' })
    if (-not $cap.Count) { Add-TestResult -Name 'no Store-app rule on this machine: skipped' -Passed $true -Detail ''; return }
    Assert-Equal 0 @($cap | Where-Object { -not $_.Package }).Count
    $out = Get-LabOutboundSnapshotLive
    Assert-True (@($out.Rules | Where-Object { $_.Service -and $_.Service -ne 'Any' }).Count -gt 0) 'outbound service filters are read'
}

Start-TestGroup 'DESKTOP-REDACT10 (1.3.0) findings 1, 3, 4: listener address, staged verification, the ArenaSvcUser account'
Test-Case 'Arena is probed where its configuration binds it (DESKTOP-REDACT10: 192.0.2.254 only, never 127.0.0.1)' {
    Assert-Equal 'http://192.0.2.254:8790' (Get-LabArenaListenEndpoint -ArenaConfig ([pscustomobject]@{ host = '192.0.2.254'; port = 8790; db_path = 'x' }))
    Assert-Equal 'http://127.0.0.1:8790' (Get-LabArenaListenEndpoint -ArenaConfig $null) 'server.py defaults'
    Assert-Equal 'http://127.0.0.1:9001' (Get-LabArenaListenEndpoint -ArenaConfig ([pscustomobject]@{ host = '0.0.0.0'; port = 9001 })) 'a wildcard bind is probed on loopback'
    Assert-Match '192\.0\.2\.254:8790: ok aes-a1e79a8b3902' (@($d1Insp.inventory | Where-Object name -eq 'event service')[0].value) 'the LAN endpoint answered in the report'
}
Test-Case 'staged status: PENDING before the stage completes, FAIL only after it' {
    Assert-Equal 'PASS' (Get-LabStagedStatus -Ok $true -StageComplete $false); Assert-Equal 'PENDING' (Get-LabStagedStatus -Ok $false -StageComplete $false)
    Assert-Equal 'FAIL' (Get-LabStagedStatus -Ok $false -StageComplete $true); Assert-Equal 'PENDING' (Get-LabStagedStatus -Ok '' -StageComplete $null)
}
Test-Case 'DESKTOP-REDACT10\ArenaSvcUser names the local account; another computer or a domain is refused' {
    $n = ConvertTo-LabLocalAccountName -Name 'DESKTOP-REDACT10\ArenaSvcUser' -Computer 'DESKTOP-REDACT10'; Assert-True $n.Local; Assert-Equal 'ArenaSvcUser' $n.Name; Assert-Equal 'DESKTOP-REDACT10\ArenaSvcUser' $n.Qualified
    Assert-True (ConvertTo-LabLocalAccountName -Name 'Desktop-Redact10\ArenaSvcUser' -Computer 'DESKTOP-REDACT10').Local 'the report shows Desktop-Redact10 and DESKTOP-REDACT10 for the same computer'
    Assert-True (ConvertTo-LabLocalAccountName -Name '.\ArenaSvcUser' -Computer 'DESKTOP-REDACT10').Local; Assert-True (ConvertTo-LabLocalAccountName -Name 'ArenaSvcUser' -Computer 'DESKTOP-REDACT10').Local
    Assert-False (ConvertTo-LabLocalAccountName -Name 'OTHERPC\ArenaSvcUser' -Computer 'DESKTOP-REDACT10').Local; Assert-False (ConvertTo-LabLocalAccountName -Name 'arena@corp.example' -Computer 'DESKTOP-REDACT10').Local
}
$agent1 = [pscustomobject]@{ Sid = 'S-1-5-21-7-7-7-1001'; Account = 'Desktop-Redact10\LabOperator1'; Profile = 'C:\Users\LabOperator1'; ConfigPath = 'C:\Users\LabOperator1\AppData\Local\AgentArenaService\aes.config.json'; ConfigPresent = $true; Moved = $false; MovedReason = '' }
$arenaSvc = [pscustomobject]@{ Sid = 'S-1-5-21-7-7-7-1005'; Account = 'DESKTOP-REDACT10\ArenaSvcUser'; Profile = 'C:\Users\ArenaSvcUser'; ConfigPath = 'C:\Users\ArenaSvcUser\AppData\Local\AgentArenaService\aes.config.json'; ConfigPresent = $true; Moved = $false; MovedReason = '' }
$agent1Moved = $agent1 | Select-Object *; $agent1Moved.Moved = $true; $agent1Moved.MovedReason = 'moved to a standard account (ArenaSvcUser)'
Test-Case 'after the move: the live Arena is ArenaSvcUser; the moved LabOperator1 copy is not an Arena account unless it runs' {
    $id = [pscustomobject]@{ Installed = @($agent1Moved, $arenaSvc); Running = @([pscustomobject]@{ What = 'event service'; Sid = $arenaSvc.Sid; Account = $arenaSvc.Account }) }
    Assert-Equal $arenaSvc.Sid (Select-LabActiveArenaInstall -Installed $id.Installed -RunningSids @($arenaSvc.Sid)).Sid
    Assert-Equal $arenaSvc.Sid ((Get-LabArenaAccountSids -Identity $id) -join ',')
    $id.Running += [pscustomobject]@{ What = 'event service'; Sid = $agent1.Sid; Account = $agent1.Account }
    Assert-Equal 2 @(Get-LabArenaAccountSids -Identity $id).Count 'a running process always counts'
    Assert-Equal $agent1.Sid ((Get-LabArenaAccountSids -Identity ([pscustomobject]@{ Installed = @($agent1); Running = @() })) -join ',') 'before the move LabOperator1 is the Arena account'
    Assert-Equal $null (Select-LabActiveArenaInstall -Installed @($agent1Moved) -RunningSids @())
}
Test-Case 'migration plan for DESKTOP-REDACT10\ArenaSvcUser: accepted as typed, refused when already exported, when not standard, or not local' {
    $savedName = $env:COMPUTERNAME; $savedFns = @{}; foreach ($n in @('Get-LocalUser')) { $c = Get-Item "function:$n" -ErrorAction SilentlyContinue; $savedFns[$n] = $(if ($c) { $c.ScriptBlock } else { $null }) }
    $cfgDir = Join-Path $Temp 'LabOperator1'; $cfgFile = Join-Path $cfgDir 'AppData\Local\AgentArenaService\aes.config.json'; New-Item -ItemType Directory (Split-Path -Parent $cfgFile) -Force | Out-Null; Set-Content -LiteralPath $cfgFile -Value '{"host":"192.0.2.254","port":8790,"db_path":"x"}'
    $src = $agent1 | Select-Object *; $src.Profile = $cfgDir; $src.ConfigPath = $cfgFile
    try {
        $env:COMPUTERNAME = 'DESKTOP-REDACT10'
        # Only the bare name reaches Get-LocalUser: "DESKTOP-REDACT10\ArenaSvcUser" itself would not be found.
        function global:Get-LocalUser { [CmdletBinding()] param([string]$Name) if ($Name -notmatch '\\') { [pscustomobject]@{ Name = $Name; SID = [pscustomobject]@{ Value = 'S-1-5-21-7-7-7-1005' }; Enabled = $true; PasswordExpires = $null } } }
        $std = @([pscustomobject]@{ Sid = 'S-1-5-21-7-7-7-1005'; State = 'standard'; Groups = @(); Unreadable = @() })
        $mp = Get-LabArenaMigrationPlan -Config $Config -Target 'DESKTOP-REDACT10\ArenaSvcUser' -Identity ([pscustomobject]@{ Installed = @($src); Running = @() }) -Membership $std -GatewayAccount $Config.gateway.service_account
        Assert-True $mp.Ok ($mp.Blockers -join '; '); Assert-Equal 'ArenaSvcUser' $mp.Target.Name; Assert-Equal 'S-1-5-21-7-7-7-1005' $mp.Target.Sid; Assert-Equal 'Desktop-Redact10\LabOperator1' $mp.Source.Account
        $moved = $src | Select-Object *; $moved.Moved = $true; $moved.MovedReason = 'moved to a standard account (ArenaSvcUser)'
        $mp2 = Get-LabArenaMigrationPlan -Config $Config -Target 'DESKTOP-REDACT10\ArenaSvcUser' -Identity ([pscustomobject]@{ Installed = @($moved); Running = @() }) -Membership $std
        Assert-False $mp2.Ok; Assert-Match 'already exported' ($mp2.Blockers -join ' ')
        $mp3 = Get-LabArenaMigrationPlan -Config $Config -Target 'DESKTOP-REDACT10\ArenaSvcUser' -Identity ([pscustomobject]@{ Installed = @($src); Running = @() }) -Membership @([pscustomobject]@{ Sid = 'S-1-5-21-7-7-7-1005'; State = 'privileged'; Groups = @('Administrators'); Unreadable = @() })
        Assert-False $mp3.Ok; Assert-Match 'not verified as a standard account' ($mp3.Blockers -join ' ')
        $mp4 = Get-LabArenaMigrationPlan -Config $Config -Target 'OTHERPC\ArenaSvcUser' -Identity ([pscustomobject]@{ Installed = @($src); Running = @() }) -Membership $std
        Assert-False $mp4.Ok; Assert-Match 'not a local account' ($mp4.Blockers -join ' ')
        $mp5 = Get-LabArenaMigrationPlan -Config $Config -Target 'DESKTOP-REDACT10\svc-inference-gw' -Identity ([pscustomobject]@{ Installed = @($src); Running = @() }) -Membership $std -GatewayAccount $Config.gateway.service_account
        Assert-False $mp5.Ok; Assert-Match 'cannot be the Gateway service account' ($mp5.Blockers -join ' ')
    } finally {
        $env:COMPUTERNAME = $savedName
        # 'function:global:<name>' is not a removable path in 5.1: 'Function:\<name>' is.
        foreach ($n in $savedFns.Keys) { if ($savedFns[$n]) { Set-Item -Path "function:global:$n" -Value $savedFns[$n] } else { Remove-Item -LiteralPath "Function:\$n" -Force -ErrorAction SilentlyContinue } }
        Assert-Equal 'Cmdlet' ([string](Get-Command Get-LocalUser).CommandType) 'the stand-in for Get-LocalUser must be gone'
    }
}
Test-Case 'the migration helper finds the Python Arena itself runs on (DESKTOP-REDACT10 had none on PATH)' {
    Assert-Equal 'C:\Users\LabOperator1\AppData\Local\Programs\Python\Python312\python.exe' (Get-LabVenvBasePython -Text "home = C:\Users\LabOperator1\AppData\Local\Programs\Python\Python312`r`ninclude-system-site-packages = false`r`nversion = 3.12.10")
    # (no quote characters inside: Windows PowerShell 5.1 strips them from native arguments)
    $real = ''; foreach ($c in @(@('py', '-3'), @('python'))) { if ($real) { break }; try { $exe = $c[0]; $more = @($c | Select-Object -Skip 1); $v = [string](& $exe @more -c 'import sys; print(sys.executable if sys.version_info >= (3, 12) else 0)' 2>$null); if ($v -and $v -ne '0' -and (Test-Path -LiteralPath $v)) { $real = $v } } catch { } }
    if (-not $real -or -not (Test-Path $real)) { Add-TestResult -Name 'no Python 3.12 on this machine: skipped' -Passed $true -Detail ''; return }
    $prof = Join-Path $Temp 'venvprofile'; $venv = Join-Path $prof 'AppData\Local\Programs\AgentArenaHub\app\.venv'; New-Item -ItemType Directory $venv -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $venv 'pyvenv.cfg') -Value ("home = " + (Split-Path -Parent $real))
    Assert-Equal $real (Get-LabArenaMigrationPython -Source ([pscustomobject]@{ Sid = 'S-1-5-21-0-0-0-0'; Profile = $prof }) -MachineWide 'C:\nonexistent\python.exe')
}

# ---------------------------------------------------------------------------
# Gateway 0.3.0. The real release is used when it is present (on F: in packages\gateway; in the
# repository, the Gateway project's release folder). Nothing here installs or changes the machine.
# ---------------------------------------------------------------------------
$gwZip = @(@(Get-ChildItem (Join-Path $root 'packages\gateway') -Filter 'inference-gateway-*.zip' -ErrorAction SilentlyContinue) + @(Get-ChildItem (Join-Path $env:USERPROFILE 'PycharmProjects\Inference Gateway\release') -Filter ('inference-gateway-{0}.zip' -f $Config.gateway.expected_release) -ErrorAction SilentlyContinue)) | Select-Object -First 1   # the release this stick carries (0.4.0 since 1.6.6)
$gw = $null
if ($gwZip) { $gw = Expand-LabGatewayPackage -Zip $gwZip.FullName -Staging (Join-Path $Temp 'gw') }
# 1.6.14: the stick carries Gateway 0.4.4 (another project builds it). Until it exists here, the test that only reads a
# real release's manifest and settings template uses the newest one present; the flow tests wait for 0.4.4.
$gwAny = $gw
if (-not $gwAny) { $z = @(Get-ChildItem (Join-Path $env:USERPROFILE 'PycharmProjects\Inference Gateway\release') -Filter 'inference-gateway-*.zip' -ErrorAction SilentlyContinue | Sort-Object { [version]($_.BaseName -replace '^inference-gateway-', '') }) | Select-Object -Last 1
                   if ($z) { $gwAny = Expand-LabGatewayPackage -Zip $z.FullName -Staging (Join-Path $Temp 'gw-any') } }
Start-TestGroup 'Gateway package intake (the release this stick carries)'
Test-Case 'the supplied release passes intake: archive hash, SHA256SUMS, manifest against the lab' {
    if (-not $gw) { Add-TestResult -Name 'Gateway release not available here: skipped' -Passed $true -Detail ''; return }
    $r = Test-LabGatewayPackage -Root $gw.Root -Config $Config
    Assert-True $r.Ok ($r.Findings -join '; '); Assert-Equal ([string]$Config.gateway.expected_release) $r.Release; Assert-True ($r.Files -ge 16) ('files: ' + $r.Files)
    Assert-True (@($r.Warnings | Where-Object { $_ -match 'O3' }).Count -eq 1) 'open items are reported'
}
Test-Case 'a changed file, an unlisted file or a broad listener rule is refused' {
    if (-not $gw) { return }
    $copy = Join-Path $Temp 'gw-bad'; Copy-Item $gw.Root $copy -Recurse
    Add-Content (Join-Path $copy 'docs\OPERATOR-CHECKLIST.md') 'tampered'
    Set-Content (Join-Path $copy 'extra.txt') 'x'
    $r = Test-LabGatewayPackage -Root $copy -Config $Config
    Assert-False $r.Ok; Assert-True (@($r.Findings | Where-Object { $_ -match 'mismatch: docs/OPERATOR-CHECKLIST.md' }).Count -eq 1); Assert-True (@($r.Findings | Where-Object { $_ -match 'extra.txt' }).Count -eq 1)
    $copy2 = Join-Path $Temp 'gw-any'; Copy-Item $gw.Root $copy2 -Recurse
    $mf = Join-Path $copy2 'deployment-manifest.json'; $m = Get-Content $mf -Raw | ConvertFrom-Json; $m.listener.firewall_rule.remote_addresses = 'Any'
    [IO.File]::WriteAllText($mf, ($m | ConvertTo-Json -Depth 12)); $h = (Get-FileHash $mf).Hash.ToLowerInvariant()
    $sums = Join-Path $copy2 'SHA256SUMS'; [IO.File]::WriteAllText($sums, ([IO.File]::ReadAllText($sums) -replace '[0-9a-f]{64}(  deployment-manifest\.json)', ($h + '$1')))
    $r2 = Test-LabGatewayPackage -Root $copy2 -Config $Config
    Assert-False $r2.Ok; Assert-True (@($r2.Findings | Where-Object { $_ -match 'not the restricted rule' }).Count -eq 1) ($r2.Findings -join '; ')
}
Test-Case 'an archive that does not match its published .sha256 is refused before unpacking' {
    if (-not $gwZip) { return }
    $z = Join-Path $Temp 'fake-gw.zip'; Copy-Item $gwZip.FullName $z; Set-Content ($z + '.sha256') ('{0}  fake-gw.zip' -f ('0' * 64))
    $threw = $false; try { $null = Expand-LabGatewayPackage -Zip $z -Staging (Join-Path $Temp 'gw-x') } catch { $threw = ($_.Exception.Message -match 'published SHA-256') }
    Assert-True $threw
}

Start-TestGroup 'Gateway 0.3.0 settings and command contract'
Test-Case 'settings come from the Gateway template; only the accounts are filled in or qualified' {
    if (-not $gw) { return }
    $m = Get-Content (Join-Path $gw.Root 'deployment-manifest.json') -Raw | ConvertFrom-Json
    $s = New-LabGatewaySettingsText -TemplateJson ([IO.File]::ReadAllText((Join-Path $gw.Root 'config\deployment-settings.template.json'))) -Manifest $m -ArenaAccounts @('arena-hub') -Computer 'DESKTOP-REDACT10' | ConvertFrom-Json
    Assert-Equal 'DESKTOP-REDACT10\arena-hub' (@($s.arena_accounts) -join ','); Assert-Equal 'DESKTOP-REDACT10\svc-inference-gw' $s.service_account; Assert-Equal 'C:\Program Files\Python312\python.exe' $s.python; Assert-Equal 8443 $s.listen.port
    foreach ($bad in @(@(), @('svc-inference-gw'))) { $threw = $false; try { $null = New-LabGatewaySettingsText -TemplateJson ([IO.File]::ReadAllText((Join-Path $gw.Root 'config\deployment-settings.template.json'))) -Manifest $m -ArenaAccounts $bad } catch { $threw = $true }; Assert-True $threw "refuses arena accounts '$($bad -join ',')'" }
}
Test-Case 'command forms follow the manifest' {
    $a = Get-LabGatewayDeployArgs -Package 'F:\pkg' -Verb 'plan' -Settings 'C:\s.json'
    Assert-Equal '-I F:\pkg\bin\gateway-deploy.py plan --settings C:\s.json' ($a -join ' ')
    Assert-Equal '-I C:\Program Files\InferenceGateway\releases\0.3.0\inference_gateway\service_launcher.py heads' ((Get-LabGatewayLauncherArgs -Manifest ([pscustomobject]@{ release = '0.3.0' }) -Arguments @('heads')) -join ' ')
}
Test-Case 'exit codes keep the Gateway''s meaning; exit 0 without ok:true is a failure' {
    $script:calls = @()
    Set-LabGatewayRunner { param($f, $a, $in, $wd) $script:calls += , @($a); $v = $a[-1]
        switch ($v) { 'ok' { [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true}'; Stderr = '' } } 'lie' { [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": false}'; Stderr = '' } }
                      'refuse' { [pscustomobject]@{ ExitCode = 1; Stdout = "{`n `"ok`": false,`n `"error`": `"Prerequisite failed: python`"`n}"; Stderr = '' } } default { [pscustomobject]@{ ExitCode = 2; Stdout = '{"ok": false, "error": "bad"}'; Stderr = 'usage' } } } }
    try {
        Assert-True (Invoke-LabGatewayCommand -FileName 'py' -Arguments @('ok') -Label 't').Ok
        $l = Invoke-LabGatewayCommand -FileName 'py' -Arguments @('lie') -Label 't'; Assert-False $l.Ok; Assert-Match 'treated as a failure' $l.Meaning
        $r = Invoke-LabGatewayCommand -FileName 'py' -Arguments @('refuse') -Label 't'; Assert-False $r.Ok; Assert-Equal 'failed check or refused step' $r.Meaning; Assert-Equal 'Prerequisite failed: python' $r.Json.error
        Assert-Equal 'bad input' (Invoke-LabGatewayCommand -FileName 'py' -Arguments @('x') -Label 't').Meaning
    } finally { Set-LabGatewayRunner $null }
}
Test-Case 'the service password goes to stdin only and appears in no log or record' {
    $run = New-TestRun; $script:stdin = ''
    Set-LabGatewayRunner { param($f, $a, $in, $wd) $script:stdin = $in; [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true}'; Stderr = '' } }
    try {
        $pw = ConvertTo-SecureString 'Sup3r-Secret-Pass!' -AsPlainText -Force
        $r = Invoke-LabGatewayCommand -FileName 'py' -Arguments @('install', '--password-stdin') -Label 'install' -Password $pw -Run $run 6>$null
        Assert-True $r.Ok; Assert-Equal 'Sup3r-Secret-Pass!' $script:stdin
        foreach ($f in Get-ChildItem $run.Folder -File) { Assert-False ((Get-Content $f.FullName -Raw) -match 'Sup3r') "password in $($f.Name)" }
        Assert-False (($r.Json | ConvertTo-Json) -match 'Sup3r')
    } finally { Set-LabGatewayRunner $null }
}
Test-Case 'the real Gateway plan runs read-only from the package and prints its checks (this laptop: refused)' {
    if (-not $gw) { return }
    $base = ''; try { $base = (& (Join-Path (Split-Path -Parent $root) '.venv\Scripts\python.exe') -c 'import sys;print(sys._base_executable)' 2>$null) } catch { }
    if (-not $base -or -not (Test-Path $base)) { return }
    $settings = Join-Path $Temp 'settings.json'
    $s = Get-Content (Join-Path $gw.Root 'config\deployment-settings.template.json') -Raw | ConvertFrom-Json; $s.python = $base; $s.arena_accounts = @('.\no-such-arena-account'); $s.data_root = (Join-Path $Temp 'gw-data'); $s.install_root = (Join-Path $Temp 'gw-install')
    [IO.File]::WriteAllText($settings, ($s | ConvertTo-Json -Depth 5))
    $r = Invoke-LabGatewayCommand -FileName $base -Arguments (Get-LabGatewayDeployArgs -Package $gw.Root -Verb 'plan' -Settings $settings) -Label 'plan'
    Assert-True ($r.Json -ne $null) ("no JSON: exit $($r.ExitCode) $($r.Stderr)")
    $c = $r.Json.checks
    foreach ($k in 'python', 'elevated', 'service_account', 'arena_accounts', 'service', 'package', 'tls', 'ledger') { Assert-True ($c.PSObject.Properties.Name -contains $k) "check $k" }
    Assert-True $c.package.ok 'the Gateway''s own package verification passes'
    Assert-False $c.arena_accounts.ok 'an unknown Arena account is refused'
    Assert-False (Test-Path (Join-Path $Temp 'gw-data')) 'plan created nothing'
}

Start-TestGroup 'Gateway 0.3.0 listener rule and TLS'
$mf = $null; if ($gw) { $mf = Get-Content (Join-Path $gw.Root 'deployment-manifest.json') -Raw | ConvertFrom-Json }
Test-Case 'the listener rule is exactly the manifest''s; the .1 cutover rule is separate' {
    if (-not $mf) { return }
    $r = New-LabGatewayListenerRuleSpec -Manifest $mf
    Assert-Equal 'InferenceGateway-HTTPS-8443' $r.Name; Assert-Equal '192.0.2.201,192.0.2.202' ($r.RemoteAddress -join ','); Assert-Equal 'Private' $r.Profile; Assert-Equal '8443' $r.LocalPort; Assert-Equal '192.0.2.254' $r.LocalAddress
    $c = New-LabGatewayListenerRuleSpec -Manifest $mf -Cutover -Until '2026-12-01'
    Assert-Equal 'InferenceGateway-HTTPS-8443-cutover' $c.Name; Assert-Equal '192.0.2.1' ($c.RemoteAddress -join ','); Assert-Match '2026-12-01' $c.DisplayName
}
Test-Case 'apply, re-apply, analysis and undo of the listener rule' {
    if (-not $mf) { return }
    Reset-Fw @(); $script:FwCategory = 'Private'; $run = New-TestRun
    try {
        Assert-Equal 'DONE' (Set-LabGatewayListenerRule -Run $run -Spec (New-LabGatewayListenerRuleSpec -Manifest $mf) 6>$null).Outcome
        Assert-Equal 'UNCHANGED' (Set-LabGatewayListenerRule -Run $run -Spec (New-LabGatewayListenerRuleSpec -Manifest $mf) 6>$null).Outcome
        $v = Get-Verdicts (Get-LabListenerExposure -Snapshot (Get-FwSnapshot) -Manifest $mf -Config $Config)
        Assert-Equal 'ADMITTED' $v['192.0.2.201']; Assert-Equal 'ADMITTED' $v['192.0.2.202']; Assert-Equal 'DENIED' $v['192.0.2.1']; Assert-Equal 'DENIED' $v['192.0.2.50']; Assert-Equal 'DENIED' $v['192.0.2.3']
        $script:FwCategory = 'Public'; Assert-Equal 'DENIED' (Get-Verdicts (Get-LabListenerExposure -Snapshot (Get-FwSnapshot) -Manifest $mf -Config $Config))['192.0.2.201'] 'a Private-profile rule does not apply on Public'
        $null = Undo-LabRun -ChangesFile $run.ChangesFile 6>$null; Assert-Equal 0 @($script:Rules).Count
        [void]$script:Rules.Add((New-TestRule -Name 'InferenceGateway-HTTPS-8443' -LocalPorts @('8443') -Remote @('Any')))
        $threw = $false; try { $null = Set-LabGatewayListenerRule -Run $run -Spec (New-LabGatewayListenerRuleSpec -Manifest $mf) 6>$null } catch { $threw = $true }
        Assert-True $threw 'an existing rule with other settings is never silently changed'
    } finally { $script:FwCategory = 'Public' }
}
Test-Case 'TLS: trusted only with the Gateway CA and the right address (real certificates from the Gateway script)' {
    $pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source; $py = Join-Path (Split-Path -Parent $root) '.venv\Scripts\python.exe'
    if (-not $gw -or -not $pwsh -or -not (Test-Path $py)) { return }
    $cert = Join-Path $Temp 'tls'; $cert2 = Join-Path $Temp 'tls2'
    foreach ($pair in @(@($cert, 'ca'), @($cert2, 'ca2'))) { $o = & $pwsh -NoProfile -File (Join-Path $gw.Root 'scripts\New-GatewayCertificate.ps1') -CaDirectory (Join-Path $Temp $pair[1]) -OutputDirectory $pair[0] -IpAddress '127.0.0.1' -NewCa -AdministratorAccount ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME) 2>&1 | Out-String; Assert-Match '"ok":true' $o }
    $port = Get-Random -Minimum 20000 -Maximum 40000
    $srv = "import http.server,ssl,json,sys`nclass H(http.server.BaseHTTPRequestHandler):`n  def do_GET(s):`n    b=json.dumps({'ok':True,'service_id':'shared-inference','contract':'shared.inference/1'}).encode();s.send_response(200);s.send_header('Content-Type','application/json');s.end_headers();s.wfile.write(b)`n  def log_message(s,*a):pass`nh=http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),H);c=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);c.load_cert_chain(sys.argv[2],sys.argv[3]);h.socket=c.wrap_socket(h.socket,server_side=True);h.handle_request();h.handle_request();h.handle_request();h.handle_request();h.handle_request();h.handle_request()"
    $sf = Join-Path $Temp 'srv.py'; Set-Content $sf $srv
    $p = Start-Process -FilePath $py -ArgumentList @('-I', $sf, $port, (Join-Path $cert 'server.pem'), (Join-Path $cert 'server.key')) -PassThru -WindowStyle Hidden
    try {
        Start-Sleep -Seconds 2
        $ok = Test-LabGatewayHealth -BaseUrl "https://127.0.0.1:$port" -CaFile (Join-Path $cert 'inference-gateway-ca.pem')
        Assert-True $ok.Ok $ok.Detail; Assert-Equal 'shared-inference' $ok.ServiceId
        $wrongCa = Test-LabGatewayHealth -BaseUrl "https://127.0.0.1:$port" -CaFile (Join-Path $cert2 'inference-gateway-ca.pem')
        Assert-False $wrongCa.Ok; Assert-Match 'not issued by the Gateway CA|chain' $wrongCa.Detail
        $wrongName = Test-LabGatewayHealth -BaseUrl "https://localhost:$port" -CaFile (Join-Path $cert 'inference-gateway-ca.pem')
        Assert-False $wrongName.Ok; Assert-Match 'SAN|match' $wrongName.Detail
    } finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
}

Start-TestGroup 'Gateway 0.3.0 accounts, identities and clients'
Test-Case 'transitive membership: a nested group makes an account privileged; an unreadable group is unknown' {
    $lookup = { param($g) switch ($g) { 'S-1-5-32-544' { @([pscustomobject]@{ Sid = 'S-1-5-21-9-9-9-2000'; Class = 'Group' }) } 'S-1-5-21-9-9-9-2000' { @([pscustomobject]@{ Sid = 'S-1-5-21-9-9-9-1001'; Class = 'User' }) } 'S-1-5-32-551' { @() } } }
    $m = Get-LabPrivilegedMembership -Sids @('S-1-5-21-9-9-9-1001', 'S-1-5-21-9-9-9-1002') -Lookup $lookup
    Assert-Equal 'privileged' $m[0].State; Assert-Equal 'Administrators' ($m[0].Groups -join ','); Assert-Equal 'standard' $m[1].State
    $broken = { param($g) if ($g -eq 'S-1-5-32-551') { throw 'unresolvable member (1789)' }; @() }
    Assert-Equal 'unknown' (Get-LabPrivilegedMembership -Sids @('S-1-5-21-9-9-9-1002') -Lookup $broken)[0].State
}
Test-Case 'the live transitive membership read works on this machine (read-only)' { $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $m = @(Get-LabPrivilegedMembership -Sids @($me))[0]; Assert-True ($m.State -in 'privileged', 'standard', 'unknown') }
Test-Case 'policy identities: capstone-research is separate and lower; template placeholders are refused' {
    if (-not $gw) { return }
    $p = Get-Content (Join-Path $gw.Root 'config\policy.template.json') -Raw | ConvertFrom-Json
    $r = Test-LabGatewayPolicyIdentities -Policy $p; Assert-False $r.Ok; Assert-Match 'placeholder' ($r.Findings -join ' ')
    foreach ($c in $p.clients) { $c.models = @('qwen3:4b') }
    Assert-True (Test-LabGatewayPolicyIdentities -Policy $p).Ok
    ($p.clients | Where-Object app_id -eq 'capstone-research').priority = 9; Assert-Match 'priority' ((Test-LabGatewayPolicyIdentities -Policy $p).Findings -join ' ')
    ($p.clients | Where-Object app_id -eq 'capstone-research').priority = 2; ($p.clients | Where-Object app_id -eq 'capstone-research').max_concurrency = 3
    # The operator's call since 2026-11-12 (the contract says "should"): a note, not a finding.
    $r = Test-LabGatewayPolicyIdentities -Policy $p; Assert-True $r.Ok 'research above 1 is allowed'; Assert-Match 'may hold 3 heads at once' ($r.Notes -join ' ')
}
Test-Case 'client layout and configuration for Capstone come from the manifest and template' {
    if (-not $mf) { return }
    $l = Get-LabGatewayClientLayout -Manifest $mf -AppId 'capstone'
    Assert-Equal 'C:\ProgramData\Capstone\gateway\gateway-client.json' $l.Config; Assert-Equal 'https://192.0.2.254:8443' $l.Url; Assert-Equal 'NT AUTHORITY\LOCAL SERVICE' $l.Reader
    $j = New-LabGatewayClientConfigText -TemplateJson ([IO.File]::ReadAllText((Join-Path $gw.Root 'config\client.template.json'))) -Layout $l -Manifest $mf | ConvertFrom-Json
    Assert-Equal 'capstone' $j.application_id; Assert-Equal $l.Token $j.token_file; Assert-False ($j.PSObject.Properties.Name -contains 'token')
    Assert-False (Get-LabGatewayClientLayout -Manifest $mf -AppId 'coder').Declared 'Coder''s files are not declared yet: nothing is invented'
}
Test-Case 'token file ACL: the service account reads it, no broad group does' {
    Assert-True (Test-LabTokenFileAcl -AllowSids @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-19')).Ok
    Assert-False (Test-LabTokenFileAcl -AllowSids @('S-1-5-18', 'S-1-5-19', 'S-1-5-32-545')).Ok
    Assert-False (Test-LabTokenFileAcl -AllowSids @('S-1-5-18', 'S-1-5-32-544')).Ok
}
Test-Case 'client files are written with backup and undo; no token is ever written' {
    if (-not $mf) { return }
    $run = New-TestRun; $dir = Join-Path $Temp 'capgw'
    $l = Get-LabGatewayClientLayout -Manifest $mf -AppId 'capstone'; $l.Folder = $dir; $l.Config = Join-Path $dir 'gateway-client.json'; $l.Ca = Join-Path $dir 'inference-gateway-ca.pem'; $l.Token = Join-Path $dir 'capstone.token'
    $usb = Join-Path $Temp 'usbca'; New-Item -ItemType Directory $usb -Force | Out-Null; $ca = Join-Path $usb 'inference-gateway-ca.pem'; Set-Content $ca 'PUBLIC CA'; Set-Content ($ca + '.sha256') ((Get-FileHash $ca).Hash.ToLowerInvariant() + '  inference-gateway-ca.pem')
    $r = Set-LabGatewayClientFiles -Run $run -Layout $l -TemplateJson ([IO.File]::ReadAllText((Join-Path $gw.Root 'config\client.template.json'))) -Manifest $mf -UsbCa $ca 6>$null
    $acl = Get-LabAclAllowSids -Path $dir; Assert-True ($acl -contains 'S-1-5-19') 'LOCAL SERVICE reads'; Assert-False ($acl -contains 'S-1-5-32-545') 'Users do not'
    & icacls.exe $dir /grant ('{0}\{1}:(OI)(CI)F' -f $env:USERDOMAIN, $env:USERNAME) /T /Q | Out-Null   # the test runs unelevated: as owner it takes access back to check and undo
    Assert-Equal 'DONE' $r.Outcome; Assert-True (Test-Path $l.Config); Assert-True (Test-Path $l.Ca); Assert-False (Test-Path $l.Token)
    $s = Get-LabGatewayClientState -Layout $l -UsbCa $ca; Assert-True $s.ConfigOk; Assert-True $s.CaMatchesUsb; Assert-False $s.TokenPresent
    $null = Undo-LabRun -ChangesFile $run.ChangesFile 6>$null; Assert-False (Test-Path $l.Config); Assert-False (Test-Path $l.Ca)
}
Start-TestGroup 'Capstone 6.0.0-rc.3 as a Gateway 0.3 client (two identities)'
# On the stick the release sits at the drive root (F:\AGENT-ARENA-LAB-x), where "two folders up" is empty: then only
# the stick's own packages\capstone is looked at.
$repoParent = Split-Path -Parent (Split-Path -Parent $root)
$capZip = @(@(Get-ChildItem (Join-Path $root 'packages\capstone') -Filter 'Capstone-*-windows-x64.zip' -ErrorAction SilentlyContinue) + @(if ($repoParent) { Get-ChildItem (Join-Path $repoParent 'Capstone\deploy\windows\dist') -Filter ([string]$Config.capstone.approved_release.file) -ErrorAction SilentlyContinue })) | Where-Object { $_.Name -eq [string]$Config.capstone.approved_release.file } | Select-Object -First 1
$capDir = $null
if ($capZip) {
    # Only the small files these tests need, read from the approved archive (its 450 MB payload is not unpacked).
    $capDir = Join-Path $Temp 'rc2'; New-Item -ItemType Directory (Join-Path $capDir 'config') -Force | Out-Null; New-Item -ItemType Directory (Join-Path $capDir 'tools') -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($capZip.FullName)
    try {
        foreach ($e in $z.Entries) {
            if ($e.FullName -match '(^|/)payload/application/config/(inference-gateway\.(research-)?client\.template\.json)$') { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $capDir ('config\' + $Matches[2])), $true) }
            elseif ($e.FullName -match '(^|/)tools/(Configure-Capstone\.ps1)$') { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $capDir ('tools\' + $Matches[2])), $true) }
        }
    } finally { $z.Dispose() }
}
Test-Case 'Capstone''s developer password file is written by F: without a byte-order mark' {
    # Capstone reads it as UTF-8 and strips whitespace only: a BOM would become part of the password.
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match '\[IO\.File\]::WriteAllText\(\$dev, \$plain\.Trim\(\), \(New-Object Text\.UTF8Encoding \$false\)\)' $sp
    Assert-Match 'Read-LabSecret -Prompt ''Capstone developer password \(12 or more characters\)'' -Confirm' $sp
    Assert-False ($sp -match 'F: checks only that it exists') 'the old prompt that left it to the operator is gone'
}
Test-Case 'the approved archive is RC.3 by SHA-256 (verified here, not taken from its name)' {
    if (-not $capZip) { return }
    Assert-Equal ([string]$Config.capstone.approved_release.sha256) (Get-FileHash $capZip.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
Test-Case 'Capstone''s own two bootstrap templates match the Gateway 0.3.0 manifest; altered ones are refused' {
    if (-not $capDir -or -not $mf) { return }
    $l = Get-LabGatewayClientLayout -Manifest $mf -AppId 'capstone'
    $t = Get-LabCapstoneBootstrapTemplates -ConfigDir (Join-Path $capDir 'config') -Layout $l
    Assert-True $t.Ok ($t.Problems -join '; ')
    Assert-Equal 'C:\ProgramData\Capstone\gateway\capstone.token' $t.Tokens['capstone']; Assert-Equal 'C:\ProgramData\Capstone\gateway\capstone-research.token' $t.Tokens['capstone-research']
    Assert-Equal $l.Token $t.Tokens['capstone'] 'the operational token path is the Gateway manifest''s'
    $bad = Join-Path $Temp 'rc2bad'; New-Item -ItemType Directory $bad -Force | Out-Null
    foreach ($case in @(@{ f = 'token_file'; v = 'C:\ProgramData\Capstone\gateway\capstone.token'; m = 'share one token' }, @{ f = 'url'; v = 'https://192.0.2.254:9443'; m = 'not the Gateway listener' }, @{ f = 'token_environment'; v = 'CAPSTONE_INFERENCE_TOKEN'; m = 'names a credential' })) {
        Copy-Item (Join-Path $capDir 'config\*.json') $bad -Force
        $p = Join-Path $bad 'inference-gateway.research-client.template.json'; $j = Get-Content $p -Raw | ConvertFrom-Json
        $j | Add-Member -NotePropertyName $case.f -NotePropertyValue $case.v -Force; [IO.File]::WriteAllText($p, ($j | ConvertTo-Json))
        $r = Get-LabCapstoneBootstrapTemplates -ConfigDir $bad -Layout $l; Assert-False $r.Ok $case.f; Assert-Match $case.m ($r.Problems -join ' ')
    }
}
function New-TestBootstrapPair { param([string]$Dir, [switch]$SharedToken, [switch]$ResearchEnv)
    New-Item -ItemType Directory $Dir -Force | Out-Null
    $ca = Join-Path $Dir 'inference-gateway-ca.pem'; Set-Content $ca 'PUBLIC CA'
    $out = @{}
    foreach ($id in 'capstone', 'capstone-research') {
        $tok = Join-Path $Dir $(if ($SharedToken) { 'capstone.token' } else { "$id.token" }); if (-not (Test-Path $tok)) { Set-Content $tok 'not-a-real-token'; & icacls.exe $tok /grant '*S-1-5-19:(R)' /Q | Out-Null }
        $b = [ordered]@{ configuration_version = 1; contract = 'shared.inference/1'; service_id = 'shared-inference'; url = 'https://192.0.2.254:8443'; application_id = $id; token_file = $tok; ca_file = $ca }
        if ($ResearchEnv -and $id -eq 'capstone-research') { $b.token_environment = 'CAPSTONE_INFERENCE_TOKEN' }
        $p = Join-Path $Dir "$id.json"; [IO.File]::WriteAllText($p, ([pscustomobject]$b | ConvertTo-Json)); $out[$id] = $p
    }
    return $out
}
Test-Case 'two independent identities pass; a shared token file or a token variable is refused (token content never read)' {
    $ok = New-TestBootstrapPair -Dir (Join-Path $Temp 'bp-ok')
    $r = Test-LabCapstoneBootstraps -Operational (Get-LabGatewayBootstrapState $ok['capstone']) -Research (Get-LabGatewayBootstrapState $ok['capstone-research']) -Url 'https://192.0.2.254:8443' -CaPath (Join-Path $Temp 'bp-ok\inference-gateway-ca.pem')
    Assert-True $r.Ok ($r.Problems -join '; ')
    $sh = New-TestBootstrapPair -Dir (Join-Path $Temp 'bp-shared') -SharedToken
    Assert-Match 'share one token file' ((Test-LabCapstoneBootstraps -Operational (Get-LabGatewayBootstrapState $sh['capstone']) -Research (Get-LabGatewayBootstrapState $sh['capstone-research']) -Url 'https://192.0.2.254:8443').Problems -join ' ')
    $ev = New-TestBootstrapPair -Dir (Join-Path $Temp 'bp-env') -ResearchEnv
    Assert-Match 'retired' ((Test-LabCapstoneBootstraps -Operational (Get-LabGatewayBootstrapState $ev['capstone']) -Research (Get-LabGatewayBootstrapState $ev['capstone-research']) -Url 'https://192.0.2.254:8443').Problems -join ' ')
    Remove-Item (Join-Path $Temp 'bp-ok\capstone-research.token')
    Assert-Match 'capstone-research token file missing' ((Test-LabCapstoneBootstraps -Operational (Get-LabGatewayBootstrapState $ok['capstone']) -Research (Get-LabGatewayBootstrapState $ok['capstone-research']) -Url 'https://192.0.2.254:8443').Problems -join ' ')
    Assert-Match 'research bootstrap missing|capstone-research bootstrap missing' ((Test-LabCapstoneBootstraps -Operational (Get-LabGatewayBootstrapState $ok['capstone']) -Research $null -Url 'https://192.0.2.254:8443').Problems -join ' ')
}
Test-Case 'RC.2''s own Configure-Capstone.ps1 accepts the arguments F: passes (temporary data root, no network change)' {
    if (-not $capDir) { return }
    $pair = New-TestBootstrapPair -Dir (Join-Path $Temp 'cfg-in')
    $dev = Join-Path $Temp 'cfg-in\developer.token'; Set-Content $dev 'not-a-real-token'
    $data = Join-Path $Temp 'cfg-data'
    $r = Invoke-LabExternalScript -Script (Join-Path $capDir 'tools\Configure-Capstone.ps1') -Arguments @('-OperationalGatewayBootstrap', $pair['capstone'], '-ResearchGatewayBootstrap', $pair['capstone-research'], '-DeveloperTokenFile', $dev, '-DataRoot', $data, '-ArenaAdapterRegistered') -LogFile (Join-Path $Temp 'cfg.log')
    Assert-Equal 0 $r.ExitCode $r.Output; Assert-Match '"configured"\s*:\s*true' $r.Output
    $s = Get-Content (Join-Path $data 'config\appsettings.json') -Raw | ConvertFrom-Json
    Assert-Equal 'gateway' $s.inference_mode; Assert-Equal 'complete' $s.arena_adapter.registration_status
    Assert-Equal (Join-Path $data 'config\inference-gateway.research-client.json') $s.research_gateway_client_config
    $o = Get-LabGatewayBootstrapState ([string]$s.gateway_client_config); $re = Get-LabGatewayBootstrapState ([string]$s.research_gateway_client_config)
    $v = Test-LabCapstoneBootstraps -Operational $o -Research $re -Url 'https://192.0.2.254:8443'; Assert-True $v.Ok ($v.Problems -join '; ')
    $ev = New-TestBootstrapPair -Dir (Join-Path $Temp 'cfg-env') -ResearchEnv
    $r2 = Invoke-LabExternalScript -Script (Join-Path $capDir 'tools\Configure-Capstone.ps1') -Arguments @('-OperationalGatewayBootstrap', $ev['capstone'], '-ResearchGatewayBootstrap', $ev['capstone-research'], '-DeveloperTokenFile', $dev, '-DataRoot', (Join-Path $Temp 'cfg-data2')) -LogFile (Join-Path $Temp 'cfg.log')
    Assert-True ($r2.ExitCode -ne 0) 'Capstone refuses a token environment variable as F: does'
    foreach ($d in 'cfg-in', 'cfg-env', 'cfg-data') { & icacls.exe (Join-Path $Temp $d) /grant ('{0}\{1}:(OI)(CI)F' -f $env:USERDOMAIN, $env:USERNAME) /T /Q | Out-Null }
}
Test-Case 'a Capstone archive whose SHA-256 is not the approved one is refused before anything is unpacked' {
    $run = New-TestRun; $zip = Join-Path $Temp 'Capstone-6.0.0-rc.2-windows-x64.zip'
    $src = Join-Path $Temp 'capsrc'; New-Item -ItemType Directory $src -Force | Out-Null; Set-Content (Join-Path $src 'package-manifest.json') '{}'
    Compress-Archive -Path (Join-Path $src '*') -DestinationPath $zip -Force
    $threw = $false; try { Expand-LabCapstonePackage -Run $run -Zip $zip -ExpectedSha256 ([string]$Config.capstone.approved_release.sha256) | Out-Null } catch { $threw = $true; Assert-Match 'not the released' $_.Exception.Message }
    Assert-True $threw; Assert-False (Test-Path (Join-Path $run.Root 'staging\Capstone-6.0.0-rc.2-windows-x64'))
}
Test-Case 'Capstone''s health output is read after any warnings; retired token variables are reported by name only' {
    $h = ConvertFrom-LabCapstoneHealthOutput -Text ("WARNING: TCP connect to (192.0.2.254 : 8443) failed`r`n{`r`n    ""application"":  true,`r`n    ""gateway_authenticated"":  false,`r`n    ""external_steps"":  [ ""x"" ]`r`n}")
    Assert-True $h.application; Assert-False $h.gateway_authenticated; Assert-Equal 1 @($h.external_steps).Count
    Assert-True ($null -eq (ConvertFrom-LabCapstoneHealthOutput -Text 'Get-Content : Cannot find path'))
    Assert-True (@(Get-LabRetiredTokenVariables) -is [array])
}
Test-Case 'runtime installers: publisher hash and signature, and a changed file is refused' {
    $exe = @(Get-ChildItem (Join-Path $root 'packages\runtime') -Filter 'python-*.exe' -ErrorAction SilentlyContinue) + @(Get-ChildItem (Join-Path (Split-Path -Parent $root) 'dist\_runtime-cache') -Filter 'python-*.exe' -ErrorAction SilentlyContinue) | Select-Object -First 1
    if (-not $exe) { return }
    Assert-True (Test-LabRuntimeInstaller -Path $exe.FullName -Spec $Config.runtime.python).Ok
    $bad = Join-Path $Temp $exe.Name; Copy-Item $exe.FullName $bad; Add-Content $bad 'x'
    $r = Test-LabRuntimeInstaller -Path $bad -Spec $Config.runtime.python; Assert-False $r.Ok; Assert-Match 'MD5' ($r.Problems -join ' ')
}
Test-Case 'Arena migration plan: an existing standard target is required, never the Gateway account' {
    $id = [pscustomobject]@{ Installed = @([pscustomobject]@{ Sid = 'S-1-5-21-1-1-1-500'; Account = 'HUB\admin'; Profile = 'C:\Users\admin' }); Running = @(); Current = [pscustomobject]@{ Sid = 'S-1-5-21-1-1-1-500'; Account = 'HUB\admin' } }
    $p = Get-LabArenaMigrationPlan -Config $Config -Target 'no-such-account-xyz' -Identity $id
    Assert-False $p.Ok; Assert-Match 'does not exist' ($p.Blockers -join ' ')
    $p2 = Get-LabArenaMigrationPlan -Config $Config -Target 'svc-inference-gw' -Identity $id -GatewayAccount 'svc-inference-gw'
    Assert-False $p2.Ok
    Assert-Equal 5 @($p.Steps).Count; Assert-Equal 4 @($p.Rollback).Count
}

Start-TestGroup 'Gateway 0.3.0 guided sequence: simulated failures stop it'
. (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1')
Test-Case 'plan refuses (exit 1): nothing after it runs, the run is failed' {
    if (-not $gwZip) { return }
    $Packages = Join-Path $Temp 'pk'; New-Item -ItemType Directory (Join-Path $Packages 'gateway') -Force | Out-Null; Copy-Item $gwZip.FullName (Join-Path $Packages 'gateway'); Copy-Item ($gwZip.FullName + '.sha256') (Join-Path $Packages 'gateway')
    $UsbRoot = $Temp
    $saved = @{}; foreach ($n in 'Get-LabRuntimeState', 'Get-LabLocalAccount', 'Get-LabPrivilegedMembership', 'Get-LabUserRights', 'Get-LabHubGatewayReadiness', 'New-LabGatewaySettings', 'Grant-LabUserRight') { $saved[$n] = (Get-Item "function:$n").ScriptBlock }
    try {
        function global:Get-LabRuntimeState { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = $true }; Pwsh = [pscustomobject]@{ Ok = $true } } }
        function global:Get-LabLocalAccount { param($Name) [pscustomobject]@{ Exists = $true; Name = $Name; Sid = 'S-1-5-21-9-9-9-3000'; Enabled = $true; InUsers = $true } }
        function global:Get-LabPrivilegedMembership { param($Sids, $Lookup) @($Sids | ForEach-Object { [pscustomobject]@{ Sid = $_; State = 'standard'; Groups = @(); Unreadable = @() } }) }
        function global:Get-LabUserRights { param($Sid) @('SeServiceLogonRight', 'SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight') }
        function global:Get-LabHubGatewayReadiness { param($Config, $LedgerAccepted, $MembershipLookup) [pscustomobject]@{ Accounts = [pscustomobject]@{ State = 'OK'; Blockers = @(); Unknown = @() }; ArenaSids = @('S-1-5-21-9-9-9-1001') } }
        function global:New-LabGatewaySettings { param($Run, $Root, $Manifest, $ArenaAccounts) New-LabOutcome 'DONE' 'settings' 'C:\fake\settings.json' }
        function global:Grant-LabUserRight { param($Sid, $Right, $Account) $false }
        $script:verbs = @()
        Set-LabGatewayRunner { param($f, $a, $in, $wd) $script:verbs += $a[2]; [pscustomobject]@{ ExitCode = 1; Stdout = '{"ok": false, "checks": {"python": {"ok": false}, "elevated": {"ok": true}, "service_account": {"ok": true}, "arena_accounts": {"ok": true}, "service": {"ok": true, "state": null}, "tls": {"ok": false}, "ledger": {"ok": false}}}'; Stderr = '' } }
        $run = New-TestRun; $run.Plan = [pscustomobject]@{ blocked = @(); steps = @() }
        Invoke-LabGatewayFlow -Run $run 6>$null
        $st = @{}; foreach ($s in $run.Steps) { $st[$s.id] = $s.status }
        Assert-Equal 'FAILED' $st['g-plan'] (($run.Steps | ForEach-Object { "$($_.id)=$($_.status)" }) -join ' ')
        Assert-Match 'python' (@($run.Steps | Where-Object id -eq 'g-plan')[0].detail)
        foreach ($later in 'g-certificate', 'g-install', 'g-heads', 'g-credentials', 'g-bootstrap', 'g-accept', 'g-acl', 'g-verify', 'g-listener') { Assert-Equal 'NOT_RUN' $st[$later] $later }
        Assert-Equal 'plan' ($script:verbs -join ',') 'no Gateway command after the refused plan'
        Assert-Equal 'failed' (Get-LabFinalStatus -Steps @($run.Steps) -Verification @($run.Verification))
    } finally {
        Set-LabGatewayRunner $null
        foreach ($n in $saved.Keys) { Set-Item -Path "function:global:$n" -Value $saved[$n] }
    }
}
Test-Case 'bad input (exit 2) from the certificate command stops the sequence before install' {
    if (-not $gwZip) { return }
    $Packages = Join-Path $Temp 'pk'; $UsbRoot = $Temp
    $saved = @{}; foreach ($n in 'Get-LabRuntimeState', 'Get-LabLocalAccount', 'Get-LabPrivilegedMembership', 'Get-LabUserRights', 'Get-LabHubGatewayReadiness', 'New-LabGatewaySettings', 'Grant-LabUserRight') { $saved[$n] = (Get-Item "function:$n").ScriptBlock }
    try {
        function global:Get-LabRuntimeState { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = $true }; Pwsh = [pscustomobject]@{ Ok = $true } } }
        function global:Get-LabLocalAccount { param($Name) [pscustomobject]@{ Exists = $true; Name = $Name; Sid = 'S-1-5-21-9-9-9-3000'; Enabled = $true; InUsers = $true } }
        function global:Get-LabPrivilegedMembership { param($Sids, $Lookup) @($Sids | ForEach-Object { [pscustomobject]@{ Sid = $_; State = 'standard'; Groups = @(); Unreadable = @() } }) }
        function global:Get-LabUserRights { param($Sid) @('SeServiceLogonRight', 'SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight') }
        function global:Get-LabHubGatewayReadiness { param($Config, $LedgerAccepted, $MembershipLookup) [pscustomobject]@{ Accounts = [pscustomobject]@{ State = 'OK'; Blockers = @(); Unknown = @() }; ArenaSids = @('S-1-5-21-9-9-9-1001') } }
        function global:New-LabGatewaySettings { param($Run, $Root, $Manifest, $ArenaAccounts) New-LabOutcome 'DONE' 'settings' 'C:\fake\settings.json' }
        function global:Grant-LabUserRight { param($Sid, $Right, $Account) $false }
        $script:verbs = @()
        Set-LabGatewayRunner { param($f, $a, $in, $wd) $v = $a[2]; $script:verbs += $v
            if ($v -eq 'plan') { [pscustomobject]@{ ExitCode = 1; Stdout = '{"ok": false, "checks": {"python": {"ok": true}, "elevated": {"ok": true}, "service_account": {"ok": true}, "arena_accounts": {"ok": true}, "service": {"ok": true, "state": null}, "package": {"ok": true}, "tls": {"ok": false}, "ledger": {"ok": false}}}'; Stderr = '' } }
            else { [pscustomobject]@{ ExitCode = 2; Stdout = '{"ok": false, "error": "settings: listen.port is required"}'; Stderr = '' } } }
        Set-LabApprovalProvider { param($p, $w, $k) $w }
        $run = New-TestRun; $run.Plan = [pscustomobject]@{ blocked = @(); steps = @() }
        Invoke-LabGatewayFlow -Run $run 6>$null
        $st = @{}; foreach ($s in $run.Steps) { $st[$s.id] = $s.status }
        Assert-Equal 'DONE' $st['g-plan'] 'a first install may miss only tls and ledger'
        Assert-Equal 'FAILED' $st['g-certificate']; Assert-Match 'bad input' (@($run.Steps | Where-Object id -eq 'g-certificate')[0].detail)
        Assert-Equal 'NOT_RUN' $st['g-install']; Assert-Equal 'plan,certificate' ($script:verbs -join ',')
    } finally {
        Set-LabGatewayRunner $null; Set-LabApprovalProvider $null
        foreach ($n in $saved.Keys) { Set-Item -Path "function:global:$n" -Value $saved[$n] }
    }
}
Test-Case 'staged sequence: safe stop with no head, then resume without re-issuing, re-installing or re-accepting' {
    if (-not $gwZip -or -not $mf) { return }
    $Packages = Join-Path $Temp 'pk'; $UsbRoot = $Temp
    $gwd = Join-Path $Temp 'gwdata'; New-Item -ItemType Directory (Join-Path $gwd 'config') -Force | Out-Null
    $hubRoot = Join-Path $Temp 'hubroot'
    $approved = Join-Path $Temp 'approved-policy.json'
    $pol = Get-Content (Join-Path $gw.Root 'config\policy.template.json') -Raw | ConvertFrom-Json
    foreach ($c in $pol.clients) { $c.models = @('qwen3:4b') }
    $withCoder = Join-Path $Temp 'approved-with-coder.json'; [IO.File]::WriteAllText($withCoder, ($pol | ConvertTo-Json -Depth 6))
    $pol.clients = @($pol.clients | Where-Object app_id -ne 'coder'); [IO.File]::WriteAllText($approved, ($pol | ConvertTo-Json -Depth 6))
    # Everything that would touch this machine is replaced for the test (local definitions win by scope).
    function Get-LabGatewayPaths { param($Manifest) [pscustomobject]@{ Data = $gwd; Ledger = "$gwd\state\capacity-ledger.db"; PublisherState = "$gwd\observation-state\publisher-state.json"; Observation = "$gwd\observation\obs.json"; Config = "$gwd\config\gateway-service.json"; Review = "$gwd\config\policy-review.json"; Ca = "$gwd\ca\none.pem"; CaKey = "$gwd\ca\none.key"; Backups = "$gwd\backups" } }
    function Get-LabRuntimeState { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = $true }; Pwsh = [pscustomobject]@{ Ok = $true } } }
    function Get-LabLocalAccount { param($Name) [pscustomobject]@{ Exists = $true; Name = $Name; Sid = 'S-1-5-21-9-9-9-3000'; Enabled = $true; InUsers = $true } }
    function Get-LabPrivilegedMembership { param($Sids, $Lookup) @($Sids | ForEach-Object { [pscustomobject]@{ Sid = $_; State = 'standard'; Groups = @(); Unreadable = @() } }) }
    function Get-LabUserRights { param($Sid) @('SeServiceLogonRight', 'SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight') }
    function Grant-LabUserRight { param($Sid, $Right, $Account) $false }
    function Get-LabHubGatewayReadiness { param($Config, $LedgerAccepted, $MembershipLookup) [pscustomobject]@{ Accounts = [pscustomobject]@{ State = 'OK'; Blockers = @(); Unknown = @() }; ArenaSids = @('S-1-5-21-9-9-9-1001'); Observation = [pscustomobject]@{ live = ($script:phase -gt 1); summary = 'test'; configured = $true; file_present = $true; access = 'ok'; fresh = $true; service_running = $true; ledger_accepted = $LedgerAccepted } } }
    function New-LabGatewaySettings { param($Run, $Root, $Manifest, $ArenaAccounts) New-LabOutcome 'UNCHANGED' 'settings' "$gwd\settings.json" }
    function Get-LabGatewayService { param($Name) if ($script:phase -eq 1) { [pscustomobject]@{ Present = $false } } else { [pscustomobject]@{ Present = $true; State = 'Running'; Account = '.\svc-inference-gw'; StartMode = 'Auto'; Delayed = $true } } }
    function Get-LabHeadProbe { param($Heads, $Port) @() }
    function New-LabHeadInventory { param($Probes, $Config, $From, $Computer) [pscustomobject]@{ schema = 'agent-arena.verified-head-inventory/1'; heads = @($(if ($script:phase -gt 1) { [pscustomobject]@{ resource_id = 'head-192-0-2-3'; arena_node_id = 'head-192-0-2-3'; address = '192.0.2.3'; endpoint = 'http://192.0.2.3:11434' } })); excluded = @() } }
    function Test-LabGatewayHealth { param($BaseUrl, $CaFile, $ExpectedServiceId) [pscustomobject]@{ Ok = $true; Detail = 'test' } }
    function Get-LabFirewallSnapshot { $null }
    function Get-NetIPAddress { [CmdletBinding()] param($IPAddress) [pscustomobject]@{ InterfaceAlias = 'Lab' } }
    function Get-NetConnectionProfile { [CmdletBinding()] param($InterfaceAlias) [pscustomobject]@{ NetworkCategory = [string]$mf.listener.firewall_rule.profile } }
    function Set-LabGatewayListenerRule { param($Run, $Spec) New-LabOutcome 'UNCHANGED' 'rule as the manifest says' }
    $script:issued = @(); $script:accepted = ''
    Set-LabGatewayRunner { param($f, $a, $in, $wd)
        $v = [string]$a[2]; if ($v -in 'credentials', 'publisher', 'lifecycle') { $v = $v + ' ' + $a[3] }; if ($v -eq '-Command') { $v = 'acl' }
        $script:verbs += $v
        $ok = { param($o) [pscustomobject]@{ ExitCode = 0; Stdout = (([ordered]@{ ok = $true } + $o) | ConvertTo-Json -Compress -Depth 5); Stderr = '' } }
        switch ($v) {
            'plan' { if ($script:phase -eq 1) { return [pscustomobject]@{ ExitCode = 1; Stdout = '{"ok": false, "checks": {"python": {"ok": true}, "elevated": {"ok": true}, "service_account": {"ok": true}, "arena_accounts": {"ok": true}, "service": {"ok": true, "state": null}, "package": {"ok": true}, "tls": {"ok": false}, "ledger": {"ok": false}}}'; Stderr = '' } }
                     return [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true, "checks": {"python": {"ok": true}, "service": {"ok": true, "state": "Running"}, "tls": {"ok": true}, "ledger": {"ok": true}}}'; Stderr = '' } }
            'heads' { $i = [array]::IndexOf($a, '--output'); $p = [array]::IndexOf($a, '--policy'); Copy-Item $a[$p + 1] $a[$i + 1]; return (& $ok @{ resources = @('head-192-0-2-3') }) }
            'credentials list' { return (& $ok @{ applications = @($script:issued | ForEach-Object { @{ app_id = $_ } }) }) }
            'credentials issue' { $i = [array]::IndexOf($a, '--app-id'); $script:issued += $a[$i + 1]; return (& $ok @{ action = 'issued' }) }
            'lifecycle status' { return (& $ok @{ ledger_id = 'LEDGER-1' }) }
            'publisher status' { return (& $ok @{ ledger_id = 'LEDGER-1'; accepted_ledger_id = $(if ($script:accepted) { $script:accepted } else { $null }); blocked = $(if ($script:accepted) { $null } else { 'ledger not accepted' }) }) }
            'publisher accept-ledger' { $script:accepted = 'LEDGER-1'; return (& $ok @{}) }
            default { return (& $ok @{}) }
        } }
    Set-LabApprovalProvider { param($p, $w, $k) $w }
    Set-LabSecretProvider { param($p) ConvertTo-SecureString 'not-a-real-password' -AsPlainText -Force }
    Set-LabInputProvider { param($p)
        if ($p -match '^Ledger: type NEW') { return 'NEW' }
        if ($p -match 'APPROVED Gateway policy') { return $script:policyAnswer }
        if ($p -match 'Applications the heads serve') { return 'capstone,capstone-research' }
        if ($p -match 'Operator ID') { return 'op' }
        return '' }
    $runOnce = { $script:verbs = @(); $r = New-LabRun -Role 'hub' -Root $hubRoot; $r.Plan = [pscustomobject]@{ blocked = @(); steps = @() }; Invoke-LabGatewayFlow -Run $r 6>$null; $st = @{}; foreach ($s in $r.Steps) { $st[$s.id] = $s }; [pscustomobject]@{ Run = $r; St = $st; Verbs = @($script:verbs) } }
    try {
        # 1. hub prepared, Gateway installed, no head admits the hub yet: the planned safe stop.
        $script:phase = 1; $script:policyAnswer = ''
        $x = & $runOnce
        Assert-Equal 'plan,certificate,install' ($x.Verbs -join ',') 'nothing after install runs'
        Assert-Equal 'PENDING' $x.St['g-inventory'].status
        foreach ($later in 'g-heads', 'g-credentials', 'g-bootstrap', 'g-qualify', 'g-accept', 'g-acl', 'g-verify', 'g-listener') { Assert-Equal 'PENDING' $x.St[$later].status $later }
        Assert-True (@($x.Run.Pending | Where-Object { $_.detail -match 'SAFE STOP' }).Count -eq 1)
        Assert-True ((Get-LabFinalStatus -Steps @($x.Run.Steps) -Verification @($x.Run.Verification)) -ne 'failed') 'a safe stop is not a failure'
        # 2. one CPU head prepared: resume. Install and certificate are not repeated; credentials come before bootstrap.
        # The policy travels on the USB (reports\GATEWAY); the model menu keeps it with Enter.
        $usbPol = Join-Path $UsbRoot 'reports\GATEWAY\gateway-policy-approved.json'; New-Item -ItemType Directory (Split-Path -Parent $usbPol) -Force | Out-Null
        $script:phase = 2; $script:policyAnswer = $approved; Copy-Item $approved $usbPol -Force
        $x = & $runOnce
        Assert-Equal 'UNCHANGED' $x.St['g-install'].status; Assert-False ($x.Verbs -contains 'install'); Assert-False ($x.Verbs -contains 'certificate')
        Assert-Equal 'DONE' $x.St['g-inventory'].status; Assert-Equal 'DONE' $x.St['g-heads'].status
        Assert-Equal 2 @($x.Verbs | Where-Object { $_ -eq 'credentials issue' }).Count
        Assert-True ([array]::IndexOf($x.Verbs, 'credentials issue') -lt [array]::IndexOf($x.Verbs, 'bootstrap')) 'issued before bootstrap (the Gateway refuses a provisioned client without one)'
        Assert-Equal 'DONE' $x.St['g-bootstrap'].status; Assert-Equal 'DONE' $x.St['g-accept'].status; Assert-Equal 'DONE' $x.St['g-verify'].status
        Assert-Equal 1 @(Get-ChildItem "$gwd\config" -Filter 'policy-review*.json').Count
        # 3. a later resume: no new policy given, credentials kept, ledger already accepted.
        $script:phase = 3; $script:policyAnswer = ''; Remove-Item $usbPol -Force; Get-ChildItem (Split-Path -Parent $usbPol) -Filter 'gateway-policy-approved*' | Remove-Item -Force
        $x = & $runOnce
        Assert-Equal 'PENDING' $x.St['g-heads'].status
        Assert-False ($x.Verbs -contains 'credentials issue'); Assert-False ($x.Verbs -contains 'credentials rotate'); Assert-Match 'capstone kept; capstone-research kept' $x.St['g-credentials'].detail
        Assert-Equal 'DONE' $x.St['g-bootstrap'].status 'the newest review is applied again (an update, harmless)'
        Assert-Equal 'UNCHANGED' $x.St['g-accept'].status; Assert-False ($x.Verbs -contains 'publisher accept-ledger')
        Assert-Equal 'passed' $(if ((Get-LabFinalStatus -Steps @($x.Run.Steps) -Verification @($x.Run.Verification)) -ne 'failed') { 'passed' } else { 'failed' })
        # 4. an approved policy that still declares coder: a new review file, and bootstrap waits instead of failing.
        $script:phase = 4; $script:policyAnswer = $withCoder; Copy-Item $withCoder $usbPol -Force
        $x = & $runOnce
        Assert-Equal 2 @(Get-ChildItem "$gwd\config" -Filter 'policy-review*.json').Count 'a new review file; the earlier one is kept'
        Assert-Equal 'PENDING' $x.St['g-bootstrap'].status; Assert-Match 'coder' $x.St['g-bootstrap'].detail; Assert-False ($x.Verbs -contains 'bootstrap')
        Assert-True (@($x.Run.Warnings | Where-Object { $_ -match 'declares coder' }).Count -ge 1)
    } finally {
        Set-LabGatewayRunner $null; Set-LabApprovalProvider $null; Set-LabSecretProvider $null; Set-LabInputProvider $null
        $ob = Join-Path $hubRoot 'gateway\outbox'; if (Test-Path $ob) { & icacls.exe $ob /grant ('{0}\{1}:(OI)(CI)F' -f $env:USERDOMAIN, $env:USERNAME) /T /Q | Out-Null }   # unelevated owner takes access back for cleanup
    }
}

# ---------------------------------------------------------------------------
# DESKTOP-REDACT10 (1.3.0), findings 5, 3 and 6 end to end: the real Invoke-LabHub, taken from the engine's own
# source, with everything that would touch the machine replaced by the facts of the DESKTOP-REDACT10 reports.
# ---------------------------------------------------------------------------
Start-TestGroup 'DESKTOP-REDACT10 (1.3.0) finding 5: the hub choice reaches its workflow; findings 3 and 6 in Verify'
$mainAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'engine\Start-LabProvisioner.ps1'), [ref]$null, [ref]$null)
$hubFnText = $mainAst.Find({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'Invoke-LabHub' }, $true).Extent.Text
$d1Accounts = Get-LabGatewayAccountVerdict -ArenaSids @($agent1.Sid) -Membership @([pscustomobject]@{ Sid = $agent1.Sid; State = 'privileged'; Groups = @('Administrators'); Unreadable = @() })
$hubInsp = & $fakeInspection 'hub' @('192.0.2.254'); $hubInsp | Add-Member -NotePropertyName hubgw -NotePropertyValue ([pscustomobject]@{ Accounts = $d1Accounts })
Test-Case 'every hub choice survives planning (the 1.3.0 report''s plan carries none)' {
    Assert-False ($d1Hub.plan.PSObject.Properties.Name -contains 'hub_mode') 'the fixture is the 1.3.0 report'
    Assert-Equal 0 @($d1Hub.actual).Count; Assert-Equal 0 @($d1Hub.changes).Count; Assert-Equal 7 @($d1Hub.plan.steps | Where-Object { $_.approval -and $_.state -eq 'planned' }).Count
    foreach ($m in 'arena', 'gateway', 'verify') { Assert-Equal $m (Get-TestPlan 'hub' $hubInsp @{ HubMode = $m }).hub_mode $m }
    $p = Get-TestPlan 'hub' $hubInsp @{ HubMode = 'migrate'; MigrationTarget = "$env:COMPUTERNAME\NoSuchArenaAccount-T" }
    Assert-Equal 'migrate' $p.hub_mode; Assert-True ($null -ne $p.migration) 'the migration plan must reach the step'; Assert-Match 'does not exist' ($p.migration.Blockers -join ' ')
}
Test-Case 'Invoke-LabHub runs the chosen workflow, and a plan without a choice fails loudly instead of running Verify' {
    . ([scriptblock]::Create($hubFnText))
    $Payload = Join-Path $Temp 'payload'; $Packages = Join-Path $Temp 'pk'
    function Start-LabStages { param($RoleName, $Choices) [pscustomobject]@{ Run = $script:hubRun; Choices = @{} } }
    function Complete-LabRun { param($Run, $Configured, $Declined) $script:called += 'report' }
    function Invoke-LabGatewayFlow { param($Run) $script:called += 'gateway-flow' }
    function Invoke-LabArenaMigrationExport { param($Run, $Plan, $HubFolder, $PythonInstaller) $script:called += ('export:' + $Plan.Target.Name); New-LabOutcome 'DONE' 'test' }
    function Write-LabStage { param($Letter, $Title) if ($Letter -eq 'E') { throw 'STOP-AT-VERIFY' } }
    $mk = { param($plan) $r = New-TestRun; $r.Plan = $plan; $r.Inspection = $hubInsp; $script:hubRun = $r; $r }
    try {
        # 1. the 1.3.0 shape (the choice lost): a FAILED step, no Verify, nothing else
        $script:called = @(); $r = & $mk ([pscustomobject]@{ steps = @($d1Hub.plan.steps); blocked = @() })
        Invoke-LabHub 6>$null
        Assert-Equal 'hub-mode' ((@($r.Steps) | ForEach-Object id) -join ','); Assert-Equal 'FAILED' $r.Steps[0].status; Assert-Equal 0 @($r.Verification).Count; Assert-Equal 'report' ($script:called -join ',')
        # 2. Inference Gateway
        $script:called = @(); $r = & $mk (Get-TestPlan 'hub' $hubInsp @{ HubMode = 'gateway' }); Invoke-LabHub 6>$null
        Assert-Equal 'gateway-flow,report' ($script:called -join ',')
        # 3. the standard-account migration: its plan reaches the export step
        Set-LabApprovalProvider { param($p, $w, $k) $w }
        $plan = Get-TestPlan 'hub' $hubInsp @{ HubMode = 'migrate'; MigrationTarget = 'x' }
        $plan.migration = [pscustomobject]@{ Ok = $true; Blockers = @(); Source = $agent1; Target = [pscustomobject]@{ Name = 'ArenaSvcUser'; Sid = $arenaSvc.Sid }; Steps = @('1', '2', '3'); SourceConfig = 'x' }
        $script:called = @(); $r = & $mk $plan; Invoke-LabHub 6>$null
        Assert-Equal 'export:ArenaSvcUser,report' ($script:called -join ','); Assert-Equal 'DONE' (@($r.Steps | Where-Object id -eq 'migrate-export')[0].status)
        Assert-True (@($r.Approvals | Where-Object { $_.word -eq 'MIGRATE' -and $_.result -eq 'approved' }).Count -eq 1) 'the approval is in the report'
        # 4. machine prerequisites and Arena: every planned step is attempted (actions replaced here)
        function Invoke-LabStep { param($Id, $Title, $Action, [switch]$ContinueOnFailure) [void]$script:hubRun.Steps.Add([pscustomobject]@{ id = $Id; status = 'TEST' }); $null }
        $r = & $mk (Get-TestPlan 'hub' $hubInsp @{ HubMode = 'arena' })
        $stopped = ''; try { Invoke-LabHub 6>$null } catch { $stopped = $_.Exception.Message }
        Assert-Match 'STOP-AT-VERIFY' $stopped
        Assert-Equal 'hub-network,hub-profile,hub-power,hub-time,hub-firewall,hub-python,hub-install,arena-gateway-config,gateway' ((@($r.Steps) | ForEach-Object id) -join ',')
    } finally { Set-LabApprovalProvider $null }
}
Test-Case 'an approved plan after which no step ran is named in the report' {
    $run = New-TestRun; $run.Plan = [pscustomobject]@{ steps = @($d1Hub.plan.steps); blocked = @(); external = @(); warnings = @(); preserves = @(); address = '192.0.2.254' }
    [void]$run.Approvals.Add([pscustomobject]@{ at = 'test'; kind = 'privileged'; word = 'APPROVE'; prompt = 'Carry out this plan on DESKTOP-REDACT10? 7 step(s) change this machine'; result = 'approved' })
    $r = New-LabReport -Run $run
    Assert-True (@($r.Report.warnings | Where-Object { $_ -match 'none of its 7 step' }).Count -eq 1) ($r.Report.warnings -join ' | ')
    $t = Get-Content $r.Text -Raw; Assert-Match 'APPROVALS' $t; Assert-Match 'approved\s+\[privileged, APPROVE\]' $t; Assert-Match 'no step ran; nothing was changed' $t
}
Test-Case 'Verify on the DESKTOP-REDACT10 facts: the 1.3.0 false FAILs become PASS or PENDING, real failures stay FAIL' {
    . ([scriptblock]::Create($hubFnText))
    $Payload = Join-Path $Temp 'payload'; $Packages = Join-Path $Temp 'pk'
    function Start-LabStages { param($RoleName, $Choices) [pscustomobject]@{ Run = $script:hubRun; Choices = @{} } }
    function Complete-LabRun { param($Run, $Configured, $Declined) }
    function Get-NetIPAddress { [CmdletBinding()] param($IPAddress) [pscustomobject]@{ InterfaceAlias = 'Ethernet' } }
    # Arena binds the LAN address only (hub installer: host=192.0.2.254), as on DESKTOP-REDACT10.
    function Get-LabHubState { param($Config) $cfg = [pscustomobject]@{ host = '192.0.2.254'; port = 8790 }; $ep = Get-LabArenaListenEndpoint -ArenaConfig $cfg
        $probe = { param($e) [pscustomobject]@{ Endpoint = $e; Verdict = $(if ($e -eq 'http://192.0.2.254:8790') { 'ok' } else { 'unreachable' }); ServiceId = 'aes-a1e79a8b3902'; Version = '2.2.0'; Error = '' } }
        [pscustomobject]@{ ProgramInstalled = $true; ConfigPresent = $true; ConfigPath = $agent1.ConfigPath; Account = $agent1.Account; ListenEndpoint = $ep; LocalHealth = (& $probe $ep); HubHealth = (& $probe 'http://192.0.2.254:8790'); FirewallRule = $true } }
    function Get-LabTimeServerState { [pscustomobject]@{ TimeZone = 'Eastern Standard Time'; NtpServer = 'time.windows.com,0x8'; Type = 'NTP'; AnnounceFlags = 5; ServerEnabled = 1; StartMode = 'Auto'; Service = 'Running' } }
    function Get-LabOutboundSnapshotLive { $script:outSnap }
    $answering = @('192.0.2.3', '192.0.2.4', '192.0.2.5')
    function Get-LabHeadProbe { param($Heads, $Port) foreach ($h in $Heads) { [pscustomobject]@{ Head = $h; Open = ($answering -contains $h); Version = $(if ($answering -contains $h) { '0.40.13' } else { '' }); Models = @(); ModelsRead = $true; CheckedAt = 'test' } } }
    function Test-Connection { [CmdletBinding()] param($ComputerName, $Count, [switch]$Quiet) $false }
    function Get-LabHubGatewayReadiness { param($Config, $LedgerAccepted, $MembershipLookup) [pscustomobject]@{ Accounts = $d1Accounts; ArenaSids = @($agent1.Sid); Configs = @(); Observation = (Get-LabObservationState -Configured $false -FilePresent $false -Access 'publication folder not present' -ServiceRunning ($script:svc.Present) -LedgerAccepted $LedgerAccepted) } }
    function Get-LabGatewayService { param($Name) $script:svc }
    function Get-LabFirewallSnapshot { [pscustomobject]@{ Rules = @(); Profiles = @(); Connections = @() } }
    function Get-LabRuntimeState { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = $false; Version = ''; Base = $false }; Pwsh = [pscustomobject]@{ Ok = $false; Version = '' } } }
    function Test-LabGatewayHealth { param($BaseUrl, $CaFile, $ExpectedServiceId) [pscustomobject]@{ Ok = $true; Detail = 'test' } }
    Set-LabGatewayRunner { param($f, $a, $in, $wd) [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true, "blocked": null}'; Stderr = '' } }
    $verifyOnce = { $r = New-TestRun; $r.Plan = Get-TestPlan 'hub' $hubInsp @{ HubMode = 'verify' }; $r.Inspection = $hubInsp; $script:hubRun = $r; Invoke-LabHub 6>$null
        $c = @{}; foreach ($v in $r.Verification) { $c[$v.check] = $v }; [pscustomobject]@{ Run = $r; C = $c; Status = (Get-LabFinalStatus -Steps @($r.Steps) -Verification @($r.Verification)) } }
    $local = 'Arena event service answers where its configuration binds it, with the fleet identity'; $out = 'outbound policy lets the Gateway open TCP 11434 to the heads (rule analysis)'
    $cfgC = 'Arena configured to read the Gateway observation'; $accC = 'Arena accounts meet the Gateway account rule (transitive)'; $obsC = 'Arena observation (separate from the Gateway''s own health)'
    $noSvc = [pscustomobject]@{ Present = $false; State = ''; StartMode = ''; Delayed = $null; Account = '' }
    try {
        # DESKTOP-REDACT10 as reported: Arena on the LAN address, the report's outbound rules, LabOperator1 an administrator, no Gateway yet
        $script:outSnap = New-D1OutSnap $d1OutRules; $script:svc = $noSvc
        $x = & $verifyOnce
        Assert-Equal 'PASS' $x.C[$local].status $x.C[$local].detail; Assert-Match '192\.0\.2\.254:8790: ok aes-a1e79a8b3902 2\.2\.0' $x.C[$local].detail
        Assert-Equal 'PASS' $x.C[$out].status $x.C[$out].detail
        Assert-Equal 'PENDING' $x.C[$cfgC].status; Assert-Match 'Administration hub > 1' $x.C[$cfgC].detail
        Assert-Equal 0 @($x.Run.Verification | Where-Object { $_.status -eq 'FAIL' -and $_.required }).Count (($x.Run.Verification | Where-Object { $_.status -eq 'FAIL' } | ForEach-Object check) -join ' | ')
        Assert-Equal 'partially verified' $x.Status
        if ($gwZip) {
            Assert-Equal 'PENDING' $x.C[$accC].status; Assert-Match 'Administration hub > 3' $x.C[$accC].detail; Assert-Match 'is a member of Administrators' $x.C[$accC].detail
            Assert-Equal 'PENDING' $x.C[$obsC].status; Assert-Match 'pending Gateway installation' $x.C[$obsC].detail
            # the same administrator account once the Gateway is installed: a real failure
            $script:svc = [pscustomobject]@{ Present = $true; State = 'Running'; StartMode = 'Auto'; Delayed = $true; Account = '.\svc-inference-gw' }
            $y = & $verifyOnce
            Assert-Equal 'FAIL' $y.C[$accC].status; Assert-Equal 'failed' $y.Status
            Assert-Equal 'PENDING' $y.C[$obsC].status 'the observation waits for the rest of the Gateway sequence'
        }
        # a real outbound block on the Gateway's runtime still fails
        $script:svc = $noSvc; $script:outSnap = New-D1OutSnap (@($d1OutRules) + (New-D1OutRule 'block python' $gwPy -Action 'Block'))
        $z = & $verifyOnce
        Assert-Equal 'FAIL' $z.C[$out].status; Assert-Equal 'failed' $z.Status
    } finally { Set-LabGatewayRunner $null }
}

# ---------------------------------------------------------------------------
# DESKTOP-REDACT10 (1.3.1, 2026-11-05): "Python 3.12+ not found on PATH" while py -3 worked. Windows PowerShell 5.1
# strips inner double quotes from native arguments; the same bug was in the Gateway runtime check.
# ---------------------------------------------------------------------------
Start-TestGroup 'DESKTOP-REDACT10 (1.3.1): native arguments and Python discovery'
$localPy = ''; foreach ($c in @(@('py', '-3'), @('python'))) { if ($localPy) { break }; try { $exe = $c[0]; $more = @($c | Select-Object -Skip 1); $v = [string](& $exe @more -c 'import sys; print(sys.executable if sys.version_info >= (3, 12) else 0)' 2>$null); if ($v -and $v -ne '0' -and (Test-Path -LiteralPath $v)) { $localPy = $v } } catch { } }
Test-Case 'Arena installer Python: a working Python 3.12 is found, with its executable and version' {
    if (-not $localPy) { Add-TestResult -Name 'no Python 3.12 on this machine: skipped' -Passed $true -Detail ''; return }
    $p = Get-LabPython312
    Assert-True $p.Found ($p.Reason); Assert-Match '^3\.(1[2-9]|[2-9]\d)\.' $p.Version; Assert-True (Test-Path -LiteralPath $p.Executable) $p.Executable
}
Test-Case 'Arena installer Python: when none is reachable the reason names the account, elevation and each attempt' {
    $savedPath = $env:PATH; $savedLocal = $env:LOCALAPPDATA
    try { $env:PATH = "$env:WINDIR\system32;$env:WINDIR"; $env:LOCALAPPDATA = Join-Path $Temp 'no-python'; $p = Get-LabPython312 } finally { $env:PATH = $savedPath; $env:LOCALAPPDATA = $savedLocal }
    Assert-False $p.Found; Assert-Match ([regex]::Escape("$env:USERDOMAIN\$env:USERNAME")) $p.Reason; Assert-Match 'elevated' $p.Reason; Assert-Match 'py -3 -> ' $p.Reason
}
Test-Case 'Gateway runtime check reads the version of a real Python (the probe no longer breaks)' {
    if (-not $localPy) { Add-TestResult -Name 'no Python 3.12 on this machine: skipped' -Passed $true -Detail ''; return }
    $c2 = $Config | ConvertTo-Json -Depth 12 | ConvertFrom-Json; $c2.runtime.python.path = $localPy
    $st = Get-LabRuntimeState -Config $c2
    Assert-Match '^3\.\d+\.\d+$' $st.Python.Version ('version read: ' + $st.Python.Version); Assert-True $st.Python.Base 'a base interpreter, not a venv'
}
Test-Case 'Gateway runner quoting: every argument reaches Python exactly' {
    Assert-Equal 'plain' (ConvertTo-LabWindowsArgument 'plain'); Assert-Equal '""' (ConvertTo-LabWindowsArgument '')
    Assert-Equal '"C:\Program Files\x"' (ConvertTo-LabWindowsArgument 'C:\Program Files\x'); Assert-Equal '"C:\dir with space\\"' (ConvertTo-LabWindowsArgument 'C:\dir with space\')
    if (-not $localPy) { return }
    $vals = @('C:\Program Files\InferenceGateway\x.json', 'a "quoted" word', 'C:\dir with space\', 'back\\"slash', '', "& 'C:\p\Set-Acl.ps1' -ArenaAccount 'DESKTOP-REDACT10\ArenaSvcUser'")
    $r = & (Get-LabDefaultGatewayRunner) $localPy (@('-I', '-c', 'import sys, json; print(json.dumps(sys.argv[1:]))') + $vals) $null ''
    $got = @(($r.Stdout | ConvertFrom-Json) | ForEach-Object { $_ })     # 5.1 returns a JSON array as one object
    Assert-Equal $vals.Count $got.Count $r.Stdout
    for ($k = 0; $k -lt $vals.Count; $k++) { Assert-Equal $vals[$k] ([string]$got[$k]) ("argument $k") }
}
Test-Case 'another project''s script is never handed an argument 5.1 would change' {
    foreach ($a in @('', 'say "hi"', 'C:\dir with space\')) {
        $threw = ''; try { $null = Invoke-LabExternalScript -Script 'C:\none.ps1' -Arguments @('-X', $a) -LogFile (Join-Path $Temp 'x.log') } catch { $threw = $_.Exception.Message }
        Assert-Match 'cannot be passed safely' $threw "argument '$a'"
    }
}
Test-Case 'DESKTOP-REDACT10 power: an adapter whose power management is Unsupported is already compliant' {
    $s = [pscustomobject]@{ StandbyAcSeconds = 0; HibernateAcSeconds = 0; MonitorAcSeconds = 0; LidAc = $null; AdapterPowerOff = 'Unsupported'; Eee = @([pscustomobject]@{ DisplayName = 'Energy Efficient Ethernet'; DisplayValue = 'Off' }) }
    Assert-True (Test-LabPowerCompliant -State $s -StayOn) 'DESKTOP-REDACT10 reported "may be turned off: Unsupported" and the step changed something on every run'
}

# ---------------------------------------------------------------------------
Start-TestGroup 'DESKTOP-REDACT10 (1.3.2): the machine-wide Python installer (exit 1603)'
Test-Case 'installer exit codes are named' {
    Assert-Equal 'fatal error during installation' (Get-LabInstallerExitMeaning 1603)
    Assert-Match 'another installation is already running' (Get-LabInstallerExitMeaning 1618)
    Assert-Equal '' (Get-LabInstallerExitMeaning 0)
}
Test-Case 'installer log: the lines that say why, without their prefixes' {
    $burn = @('[1F2C:1A30][2026-11-05T18:11:10]i001: Burn v3.11.2.4516, Windows v10.0 (Build 26200: Service Pack 0)',
              '[1F2C:1A30][2026-11-05T18:11:20]e000: Error 0x80070643: Failed to install MSI package.',
              '[1F2C:1A30][2026-11-05T18:11:20]e000: Error 0x80070643: Failed to install MSI package.',
              '[1F2C:1A30][2026-11-05T18:11:21]i399: Apply complete, result: 0x80070643, restart: None')
    $msi = @('MSI (s) (A4:B8) [18:11:19:120]: Product: Python 3.12.10 Core Interpreter (64-bit) -- Error 1925. You do not have sufficient privileges.', 'Action ended 18:11:19: InstallFinalize. Return value 3.', 'Property(S): ALLUSERS = 1')
    $s = @(Get-LabInstallerLogSummary -Lines ($burn + $msi))
    Assert-Equal 'Error 0x80070643: Failed to install MSI package.' $s[0]
    Assert-Equal 1 @($s | Where-Object { $_ -eq 'Error 0x80070643: Failed to install MSI package.' }).Count 'de-duplicated'
    Assert-True (@($s | Where-Object { $_ -like 'Product: Python 3.12.10 Core Interpreter (64-bit) -- Error 1925*' }).Count -eq 1) ($s -join ' | ')
    Assert-True (@($s | Where-Object { $_ -match 'Return value 3' }).Count -eq 1) 'MSI failure line kept'
    Assert-Equal 0 @($s | Where-Object { $_ -match 'ALLUSERS|Burn v' }).Count 'no noise'
}
Test-Case 'a one-time scheduled task returns the program''s own exit code and is removed' {
    $cmd = Join-Path $env:WINDIR 'System32\cmd.exe'
    try { $a = Invoke-LabProcessAsSystem -FilePath $cmd -Arguments '/c exit 1603' -TimeoutSeconds 60 -AsCurrentUser; $b = Invoke-LabProcessAsSystem -FilePath $cmd -Arguments '/c exit 0' -TimeoutSeconds 60 -AsCurrentUser }
    catch { if ($_.Exception.Message -match 'denied|0x80070005') { Add-TestResult -Name 'this account may not register scheduled tasks: skipped' -Passed $true -Detail $_.Exception.Message; return }; throw }
    Assert-Equal 1603 $a; Assert-Equal 0 $b
    Assert-Equal 0 @(Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like 'AgentArenaLab-oneshot-*' }).Count 'no task left behind'
}
$gwm = Get-Module Lab.Gateway
$runtimeMocks = {
    param($Code)
    $script:asSystem = @()
    ${function:script:Test-LabRuntimeInstaller} = { param($Path, $Spec) [pscustomobject]@{ Ok = $true; Problems = @() } }
    ${function:script:Get-LabPerUserPython} = { param($Series) @("$Series at C:\Users\LabOperator1\AppData\Local\Programs\Python\Python312") }
    ${function:script:Get-LabRuntimeState} = { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = [bool]$script:pyInstalled; Version = $(if ($script:pyInstalled) { '3.12.10' } else { '' }); Base = $true }; Pwsh = [pscustomobject]@{ Ok = $false; Version = '' } } }
    $script:pyInstalled = $false; $script:mockCode = $Code
    ${function:script:Invoke-LabProcessAsSystem} = {
        param($FilePath, $Arguments, $TimeoutSeconds, [switch]$AsCurrentUser)
        $script:asSystem += [pscustomobject]@{ File = $FilePath; Arguments = $Arguments; AsCurrentUser = [bool]$AsCurrentUser }
        $log = [regex]::Match($Arguments, '/log "([^"]+)"').Groups[1].Value
        [IO.File]::WriteAllText($log, "[1F2C:1A30][2026-11-05T18:11:20]e000: Error 0x80070643: Failed to install MSI package.`r`n")
        [IO.File]::WriteAllText(($log -replace '\.log$', '_000_core_AllUsers.log'), "Action ended 18:11:19: InstallFinalize. Return value 3.`r`n", [Text.Encoding]::Unicode)
        if ($script:mockCode -eq 0) { $script:pyInstalled = $true }
        return $script:mockCode
    }
}
$saveRuntime = { @{ T = ${function:Test-LabRuntimeInstaller}; U = ${function:Get-LabPerUserPython}; S = ${function:Get-LabRuntimeState}; P = ${function:Invoke-LabProcessAsSystem} } }
$restoreRuntime = { param($s) ${function:script:Test-LabRuntimeInstaller} = $s.T; ${function:script:Get-LabPerUserPython} = $s.U; ${function:script:Get-LabRuntimeState} = $s.S; ${function:script:Invoke-LabProcessAsSystem} = $s.P }
Test-Case 'Python installs machine-wide as SYSTEM, from a checked copy in the run folder, with its own log' {
    $saved = & $gwm $saveRuntime
    try {
        & $gwm $runtimeMocks 0
        $r = New-TestRun; $pk = Join-Path $Temp 'pkg-ok'; New-Item -ItemType Directory $pk -Force | Out-Null; [IO.File]::WriteAllText((Join-Path $pk $Config.runtime.python.file), 'stand-in')
        $o = Install-LabRuntime -Run $r -Config $Config -Kind python -PackageDir $pk 6>$null
        $calls = @(& $gwm { $script:asSystem })
        Assert-Equal 'DONE' $o.Outcome; Assert-Match 'as SYSTEM' $o.Detail; Assert-Match 'own Python left as it is: 3\.12 at C:\\Users\\LabOperator1' $o.Detail
        Assert-Equal 1 $calls.Count; Assert-False $calls[0].AsCurrentUser 'SYSTEM, not the operator'
        Assert-Equal (Join-Path $r.Folder ('staging\runtime\' + $Config.runtime.python.file)) $calls[0].File 'run from the local copy, not the USB'
        Assert-True ($calls[0].Arguments.StartsWith($Config.runtime.python.arguments)) $calls[0].Arguments
        Assert-Match ([regex]::Escape(' /log "' + (Join-Path $r.Folder 'installer-logs\python-3.12.10-amd64-install.log') + '"')) $calls[0].Arguments
    } finally { & $gwm $restoreRuntime $saved }
}
Test-Case 'a failed Python install says why from its own log and copies the logs to the USB' {
    $saved = & $gwm $saveRuntime
    try {
        & $gwm $runtimeMocks 1603
        $r = New-TestRun; $usb = Join-Path $Temp 'usb-1603'; $r.UsbRoot = $usb
        $pk = Join-Path $Temp 'pkg-1603'; New-Item -ItemType Directory $pk -Force | Out-Null; [IO.File]::WriteAllText((Join-Path $pk $Config.runtime.python.file), 'stand-in')
        $msg = ''; try { $null = Install-LabRuntime -Run $r -Config $Config -Kind python -PackageDir $pk 6>$null } catch { $msg = $_.Exception.Message }
        Assert-Match 'exited 1603 \(fatal error during installation\)' $msg
        Assert-Match 'Its log says: Error 0x80070643: Failed to install MSI package\. \| Action ended 18:11:19: InstallFinalize\. Return value 3\.' $msg
        $dest = Join-Path $usb ('reports\{0}\{1}-installer-logs' -f $env:COMPUTERNAME, $r.Id)
        Assert-Match ([regex]::Escape("copied to $dest")) $msg
        Assert-Equal 2 @(Get-ChildItem -LiteralPath $dest -Filter *.log).Count 'bundle log and package log on the USB'
        Assert-Equal 0 @($r.Changes).Count 'nothing recorded as changed'
    } finally { & $gwm $restoreRuntime $saved }
}
Test-Case 'this account''s per-user Python is read from its registry (report only)' {
    $p = @(Get-LabPerUserPython -Series '3.12')
    foreach ($x in $p) { Assert-Match '^3\.12(-32)? at .+' $x }
    Assert-Equal 0 @(Get-LabPerUserPython -Series '9.99').Count
}
Test-Case 'Gateway: the heads serve the policy''s capstone identities, and qualification uses 8192 tokens (no prompts)' {
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-False ($flow -match 'Applications the heads serve') 'no applications prompt'
    Assert-False ($flow -match "Read-LabInput -Prompt 'Qualify: resource id") 'no per-head typing'
    Assert-False ($flow -match "Read-LabInput -Prompt 'Operator ID") 'no operator prompt'
    # 1.6.7: the step qualifies through qualify_pairs.py, i.e. gateway_models.GatewayTools.qualify at QUALIFY_CONTEXT.
    Assert-Match 'qualify_pairs\.py' $flow
    $gmpy = [IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\gateway-models\gateway_models.py'))
    Assert-Match '(?m)^QUALIFY_CONTEXT = 8192\b' $gmpy; Assert-Match '"--max-context", str\(QUALIFY_CONTEXT\)' $gmpy
    Assert-False ($flow -match "-Default 'capstone,coder'") 'the old example named coder, which has no credential, and left out capstone-research'
}

# ---------------------------------------------------------------------------
Start-TestGroup 'DESKTOP-REDACT10 (1.3.3): the Gateway account, its name and its password'
$flowAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1'), [ref]$null, [ref]$null)
$accountCalls = @($flowAst.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'New-LabStandardAccount' }, $true))
Test-Case 'Windows accepts the Gateway account''s description (DESKTOP-REDACT10: 55 characters refused, the limit is 48)' {
    Assert-True ($accountCalls.Count -ge 1) 'the flow creates the account'
    foreach ($c in $accountCalls) {
        $i = [array]::IndexOf(@($c.CommandElements | ForEach-Object { [string]$_ }), '-Description'); Assert-True ($i -gt 0) 'a description is given'
        $d = $c.CommandElements[$i + 1].Value
        Assert-True ($d.Length -le 48) ("'$d' is $($d.Length) characters")
        $err = ''; try { New-LocalUser -Name 'svc-inference-gw' -NoPassword -Description $d -WhatIf -ErrorAction Stop 6>$null *>$null } catch { $err = $_.Exception.Message }
        Assert-Equal '' $err 'Windows'' own parameter check, as a dry run (-WhatIf: nothing is created)'
    }
}
Test-Case 'the account function itself keeps a description within Windows'' limit' {
    $body = (Get-Command New-LabStandardAccount).Definition
    Assert-Match 'Description\.Length -gt 48' $body; Assert-Match 'Name\.Length -gt 20' $body
}
Test-Case '".\name" becomes "<computer>\name"; other forms are unchanged' {
    Assert-Equal 'DESKTOP-REDACT10\svc-inference-gw' (ConvertTo-LabResolvableAccount '.\svc-inference-gw' -Computer 'DESKTOP-REDACT10')
    foreach ($a in 'Desktop-Redact10\ArenaSvcUser', 'NT AUTHORITY\LOCAL SERVICE', 'svc-inference-gw', '') { Assert-Equal $a (ConvertTo-LabResolvableAccount $a -Computer 'DESKTOP-REDACT10') }
}
Test-Case 'Windows cannot resolve ".\name", the converted form resolves (real lookup on this machine)' {
    $adm = Get-LocalUser -ErrorAction SilentlyContinue | Where-Object { $_.SID.Value -like '*-500' } | Select-Object -First 1
    if (-not $adm) { Add-TestResult -Name 'no local administrator account readable: skipped' -Passed $true -Detail ''; return }
    $raw = ''; try { $raw = (New-Object Security.Principal.NTAccount(".\$($adm.Name)")).Translate([Security.Principal.SecurityIdentifier]).Value } catch { }
    Assert-Equal $adm.SID.Value (Resolve-LabSid ".\$($adm.Name)") 'Resolve-LabSid converts first'
    if ($raw) { Add-TestResult -Name ('note: this machine also resolves .\name (' + $raw + ')') -Passed $true -Detail '' }
}
Test-Case 'Gateway settings name every account as <computer>\name (the Gateway resolves them; ".\" does not resolve)' {
    if (-not $gwAny) { Add-TestResult -Name 'no Gateway release found' -Passed $false -Detail 'this test needs the real Gateway release'; return }
    $m = Get-Content (Join-Path $gwAny.Root 'deployment-manifest.json') -Raw | ConvertFrom-Json
    $s = New-LabGatewaySettingsText -TemplateJson ([IO.File]::ReadAllText((Join-Path $gwAny.Root 'config\deployment-settings.template.json'))) -Manifest $m -ArenaAccounts @('Desktop-Redact10\ArenaSvcUser', 'Other', '.\Third') -Computer 'DESKTOP-REDACT10' | ConvertFrom-Json
    Assert-Equal 'DESKTOP-REDACT10\svc-inference-gw' $s.service_account
    Assert-Equal 'Desktop-Redact10\ArenaSvcUser|DESKTOP-REDACT10\Other|DESKTOP-REDACT10\Third' (@($s.arena_accounts) -join '|')
}
Test-Case 'the publication ACL script is given the resolvable Gateway account' {
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-Match "-GatewayAccount '\`$\(ConvertTo-LabResolvableAccount \`$ctx\.Manifest\.service_account\.qualified\)'" $flow
}
Test-Case 'the install password is typed twice' {
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-Match 'Read-LabSecret -Prompt \("Password of \{0\}, the same one[^\r\n]*-Confirm' $flow
}
Test-Case 'a password with symbols and accented letters reaches Python''s stdin unchanged (real Python, -I)' {
    if (-not $localPy) { Add-TestResult -Name 'no Python 3.12 on this machine: skipped' -Passed $true -Detail ''; return }
    $pw = 'Pässwörd£§!@#$%^&*()-_=+[]{};:,.<>/?|\~`' + "'x"
    Assert-True (Test-LabSecretPortable $pw) 'portable on this code page'
    $r = & (Get-LabDefaultGatewayRunner) $localPy @('-I', '-c', 'import sys; print(*[ord(c) for c in sys.stdin.readline().rstrip(chr(13) + chr(10))])') $pw ''
    Assert-Equal ((@($pw.ToCharArray() | ForEach-Object { [int]$_ })) -join ' ') ([string]$r.Stdout).Trim() 'code points Python received'
}
Test-Case 'a password that would not survive the code page is refused before any account exists' {
    Assert-True (Test-LabSecretPortable 'Abcdefgh1234!@') 'plain keyboard symbols'
    Assert-False (Test-LabSecretPortable ('Abcdefgh1234' + [char]0x4E2D + [char]0x6587)) 'characters outside the code page'
    Assert-False (Test-LabSecretPortable "Abcdefgh1234`t!") 'a control character'
}
Test-Case 'a Gateway result without "ok" is a success on exit 0 (bootstrap, lifecycle, publisher print none)' {
    $cases = @(
        @{ Out = '{"clients": 2, "resources": 4, "qualification": "required separately", "implicit_revocations": false}'; Exit = 0; Ok = $true },
        @{ Out = '{"ledger_id": "LEDGER-1", "accepted_ledger_id": "LEDGER-1", "cursor": 11, "blocked": null}'; Exit = 0; Ok = $true },
        @{ Out = '{"ok": true, "verb": "verify"}'; Exit = 0; Ok = $true },
        @{ Out = '{"ok": false, "error": "x"}'; Exit = 0; Ok = $false },
        @{ Out = 'not json'; Exit = 0; Ok = $false },
        @{ Out = '{"resource_id": "head-192-0-2-3", "passed": false}'; Exit = 1; Ok = $false })
    try {
        foreach ($c in $cases) {
            $script:cmdOut = $c; Set-LabGatewayRunner { param($f, $a, $in, $wd) [pscustomobject]@{ ExitCode = $script:cmdOut.Exit; Stdout = $script:cmdOut.Out; Stderr = '' } }
            $r = Invoke-LabGatewayCommand -FileName 'python.exe' -Arguments @('x') -Label 'x' 6>$null
            Assert-Equal $c.Ok $r.Ok ("exit $($c.Exit): $($c.Out) -> $($r.Meaning)")
        }
    } finally { Set-LabGatewayRunner $null }
}
Test-Case 'diagnostics for a service that does not start are read-only and never throw' {
    $lines = @(Get-LabServiceDiagnostics -Name 'AgentArenaLab-no-such-service' -DataRoot (Join-Path $Temp 'none') -InstallRoot (Join-Path $Temp 'none') -Hours 1)
    Assert-True ($lines.Count -gt 5) ($lines -join ' | ')
    foreach ($h in 'sc.exe queryex', 'Service Control Manager events', 'Application errors', 'gateway-fault.log', 'permissions') { Assert-True (@($lines | Where-Object { $_ -match [regex]::Escape($h) }).Count -ge 1) "section: $h" }
}
$installMocks = {
    function Get-LabGatewayPaths { param($Manifest) $d = Join-Path $Temp 'gw-install-data'; [pscustomobject]@{ Data = $d; Ledger = "$d\state\l.db"; PublisherState = "$d\ps.json"; Observation = "$d\o.json"; Config = "$d\c.json"; Review = "$d\config\policy-review.json"; Ca = "$d\tls\ca.pem"; CaKey = "$d\ca\ca.key"; Backups = "$d\backups" } }
    function Get-LabRuntimeState { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = $true }; Pwsh = [pscustomobject]@{ Ok = $true } } }
    function Get-LabLocalAccount { param($Name) [pscustomobject]@{ Exists = $true; Name = $Name; Sid = 'S-1-5-21-9-9-9-3000'; Enabled = $true; InUsers = $true } }
    function Get-LabPrivilegedMembership { param($Sids, $Lookup) @($Sids | ForEach-Object { [pscustomobject]@{ Sid = $_; State = 'standard'; Groups = @(); Unreadable = @() } }) }
    function Get-LabUserRights { param($Sid) @('SeServiceLogonRight', 'SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight') }
    function Grant-LabUserRight { param($Sid, $Right, $Account) $false }
    function Get-LabHubGatewayReadiness { param($Config, $LedgerAccepted, $MembershipLookup) [pscustomobject]@{ Accounts = [pscustomobject]@{ State = 'OK'; Blockers = @(); Unknown = @() }; ArenaSids = @('S-1-5-21-9-9-9-1001') } }
    function New-LabGatewaySettings { param($Run, $Root, $Manifest, $ArenaAccounts) New-LabOutcome 'UNCHANGED' 'settings' 'C:\fake\settings.json' }
    function Get-LabGatewayService { param($Name) [pscustomobject]@{ Present = [bool]$script:svcPresent; State = $(if ($script:svcPresent) { 'Stopped' } else { '' }); Account = 'DESKTOP-REDACT10\svc-inference-gw'; StartMode = 'Auto'; Delayed = $true } }
    function Start-LabGatewayService { param($Name, $WaitSeconds) $script:starts++; [pscustomobject]@{ Ok = [bool]$script:startOk; Status = $(if ($script:startOk) { 'Running' } else { 'Stopped' }); Error = $(if ($script:startOk) { '' } else { 'The service did not respond to the start or control request in a timely fashion' }); Seconds = 31 } }
    function Save-LabGatewayDiagnostics { param($Run, $Name, $Python) $script:diag++; [pscustomobject]@{ Where = 'C:\run\gateway-service-diagnostics.txt (copied to E:\reports\DESKTOP-REDACT10\x)'; Summary = '2026-11-05T20:41:57 id 7009: A timeout was reached (30000 milliseconds)' } }
    function Get-LabHeadProbe { param($Heads, $Port) @() }
    function Add-LabGatewayVerification { param($Run, $Manifest, $Paths, $Stage) }
}
$installRun = {
    param([bool]$Present, [bool]$StartOk, [int]$InstallExit)
    $script:svcPresent = $Present; $script:startOk = $StartOk; $script:starts = 0; $script:diag = 0; $script:verbs = @(); $script:installExit = $InstallExit
    Set-LabGatewayRunner { param($f, $a, $in, $wd) $v = $a[2]; $script:verbs += $v
        switch ($v) {
            'plan' { [pscustomobject]@{ ExitCode = 0; Stdout = ('{"ok": true, "checks": {"python": {"ok": true}, "elevated": {"ok": true}, "service_account": {"ok": true}, "arena_accounts": {"ok": true}, "service": {"ok": ' + $(if ($script:svcPresent) { 'false, "state": "stopped"' } else { 'true, "state": null' }) + '}, "package": {"ok": true}, "tls": {"ok": true}, "ledger": {"ok": ' + $(if ($script:svcPresent) { 'true' } else { 'false' }) + '}}}'); Stderr = '' } }
            'install' { $script:svcPresent = $true; [pscustomobject]@{ ExitCode = $script:installExit; Stdout = '{"ok": false, "error": "[Errno 1053] StartServiceW failed"}'; Stderr = '' } }
            default { [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true}'; Stderr = '' } } } }
    Set-LabApprovalProvider { param($p, $w, $k) $w }; Set-LabInputProvider { param($p) if ($p -match '^Ledger') { 'NEW' } else { '' } }; Set-LabSecretProvider { param($p) ConvertTo-SecureString 'Abcdefgh1234!@' -AsPlainText -Force }
    $run = New-TestRun; $run.Plan = [pscustomobject]@{ blocked = @(); steps = @() }
    try { Invoke-LabGatewayFlow -Run $run 6>$null } finally { Set-LabGatewayRunner $null; Set-LabApprovalProvider $null; Set-LabInputProvider $null; Set-LabSecretProvider $null }
    $st = @{}; foreach ($s in $run.Steps) { $st[$s.id] = $s }
    return [pscustomobject]@{ Steps = $st; Verbs = ($script:verbs -join ','); Starts = $script:starts; Diag = $script:diag; Changes = @($run.Changes) }
}
Test-Case 'DESKTOP-REDACT10 1.3.4: the installer''s first start times out (1053): F: starts the created service once more and continues' {
    if (-not $gwZip) { return }
    $Packages = Join-Path $Temp 'pk'; $UsbRoot = $Temp
    . $installMocks
    $o = & $installRun $false $true 1
    Assert-Equal 'DONE' $o.Steps['g-install'].status $o.Steps['g-install'].detail
    Assert-Match 'second start ran' $o.Steps['g-install'].detail
    Assert-Equal 1 $o.Starts; Assert-Equal 0 $o.Diag
    Assert-Equal 'PENDING' $o.Steps['g-inventory'].status 'continues to the head inventory'
    Assert-Equal 1 @($o.Changes | Where-Object kind -eq 'gateway').Count 'the installation is recorded with its undo'
}
Test-Case 'DESKTOP-REDACT10 resume: the service exists but is stopped: F: starts it instead of reinstalling' {
    if (-not $gwZip) { return }
    $Packages = Join-Path $Temp 'pk'; $UsbRoot = $Temp
    . $installMocks
    $o = & $installRun $true $true 0
    Assert-Equal 'DONE' $o.Steps['g-install'].status $o.Steps['g-install'].detail
    Assert-Match 'was installed but Stopped; started now' $o.Steps['g-install'].detail
    Assert-False ($o.Verbs -match 'install') ('no second installation: ' + $o.Verbs)
    Assert-Equal 1 $o.Starts
}
Test-Case 'a service that still does not start: the step fails with Windows'' own words and the diagnostics on the USB' {
    if (-not $gwZip) { return }
    $Packages = Join-Path $Temp 'pk'; $UsbRoot = $Temp
    . $installMocks
    $o = & $installRun $true $false 0
    Assert-Equal 'FAILED' $o.Steps['g-install'].status
    Assert-Match 'does not start \(Stopped: The service did not respond' $o.Steps['g-install'].detail
    Assert-Match 'Windows says: .*id 7009: A timeout was reached' $o.Steps['g-install'].detail
    Assert-Match 'Diagnostics: .*copied to' $o.Steps['g-install'].detail
    Assert-Equal 1 $o.Diag
    Assert-Equal 'NOT_RUN' $o.Steps['g-inventory'].status
}
Test-Case 'DESKTOP-REDACT10 1.3.5: release files made owner-only by pip --target are reset to inherit Users read' {
    $ig = Join-Path $Temp 'acl-ig'; $rel = Join-Path $ig 'releases\0.3.0\inference_gateway'; New-Item -ItemType Directory $rel -Force | Out-Null
    & icacls.exe $ig /grant '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
    $f = Join-Path $rel 'service_launcher.py'; [IO.File]::WriteAllText($f, 'x')
    # The DACL pip leaves behind under Python 3.12.4+: SYSTEM, Administrators, OWNER RIGHTS, not inherited.
    & icacls.exe $f /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-3-4:F' /Q | Out-Null
    Assert-False (Test-LabUsersCanRead $f) 'reproduces DESKTOP-REDACT10: Users cannot read service_launcher.py'
    $o = Repair-LabGatewayReleaseAcl -InstallRoot $ig
    Assert-Equal 'DONE' $o.Outcome $o.Detail; Assert-True (Test-LabUsersCanRead $f) 'after the reset'
    Assert-Equal 'UNCHANGED' (Repair-LabGatewayReleaseAcl -InstallRoot $ig).Outcome 'a second run changes nothing'
}
Test-Case 'the release ACL is repaired before each service start, and the policy on the USB is found by itself' {
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-Equal 2 ([regex]::Matches($flow, 'Repair-LabGatewayReleaseAcl -Run \$Run; Write-LabLog[^\r\n]*\r?\n\s+(Write-LabLog[^\r\n]*\r?\n\s+)?\$st = Start-LabGatewayService')).Count 'both start paths'
    Assert-Match "Join-Path \`$UsbRoot 'reports\\GATEWAY\\gateway-policy-approved.json'" $flow
}
$qualifyRun = {
    param($Pairs)
    $d = Join-Path $Temp ('q-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory $d -Force | Out-Null
    $inv = [pscustomobject]@{ heads = @(
        [pscustomobject]@{ resource_id = 'head-192-0-2-3'; verified = [pscustomobject]@{ models = @([pscustomobject]@{ name = 'qwen3.5:4b' }, [pscustomobject]@{ name = 'gemma3:4b' }) } },
        [pscustomobject]@{ resource_id = 'head-192-0-2-6'; verified = [pscustomobject]@{ models = @([pscustomobject]@{ name = 'gemma3:1b' }) } }) }
    $rev = [pscustomobject]@{ clients = @([pscustomobject]@{ app_id = 'capstone'; models = @('qwen3.5:4b', 'gemma3:4b') }, [pscustomobject]@{ app_id = 'capstone-research'; models = @('qwen3.5:4b') }) }
    [IO.File]::WriteAllText("$d\inv.json", ($inv | ConvertTo-Json -Depth 8)); [IO.File]::WriteAllText("$d\rev.json", ($rev | ConvertTo-Json -Depth 8))
    $script:qargs = @(); $script:approvals = @()
    Set-LabApprovalProvider { param($p, $w, $k) $script:approvals += $p; $w }
    # 1.6.7: the step runs qualify_pairs.py twice (the plan, then the tests); a stand-in "python" records its arguments
    # and answers as the script does: nothing usable yet, both pairs to test, both pass.
    $py = Join-Path $d 'fakepy.ps1'
    [IO.File]::WriteAllText($py, @'
$a = @($args); $out = $a[[array]::IndexOf($a, '--out') + 1]; $pairs = Get-Content -Raw $a[[array]::IndexOf($a, '--pairs') + 1] | ConvertFrom-Json
Add-Content -LiteralPath (Join-Path (Split-Path $out) 'calls.txt') -Value ($a -join ' ')
$to = @($pairs | ForEach-Object { ,@($_[0], $_[1]) })
if ($a -contains '--plan-only') { @{ ok = $true; kept = @(); to_test = $to } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $out; '0 usable now (kept), 2 to test' }
else { @{ ok = $true; results = @($pairs | ForEach-Object { @{ resource = $_[0]; model = $_[1]; purpose = 'completion'; passed = $true; why = '' } }) } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $out; '  qwen3.5:4b on head 3: passed, usable now' }
'@)
    $Run = [pscustomobject]@{ Folder = $d; Root = $d }
    $script:LabGatewayFlowRoot = Join-Path $root 'engine'
    try {
        $ctx = @{ Review = "$d\rev.json"; Inventory = "$d\inv.json"; Paths = [pscustomobject]@{ Ledger = "$d\l.db"; Backups = "$d\b" }; Manifest = [pscustomobject]@{ release = '0.3.0' } }
        $launch = { param([string]$Label, [string[]]$Arguments) throw 'the 1.6.7 qualification does not call the Gateway command per pair' }
        $flowAst2 = [Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1'), [ref]$null, [ref]$null)
        $step = $flowAst2.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Invoke-LabStep' -and $n.Extent.Text -match "-Id 'g-qualify'" }, $true) | Select-Object -First 1
        $body = $step.CommandElements | Where-Object { $_ -is [Management.Automation.Language.ScriptBlockExpressionAst] } | Select-Object -First 1
        $o = & ([scriptblock]::Create($body.ScriptBlock.EndBlock.Extent.Text)) 6>$null
        $calls = @(Get-Content -LiteralPath (Join-Path $d 'calls.txt') -ErrorAction SilentlyContinue)
        $sent = $(if (Test-Path "$d\qualify-pairs.json") { Get-Content -Raw "$d\qualify-pairs.json" } else { '' })
        return [pscustomobject]@{ Outcome = $o; Calls = $calls; Pairs = $sent; Approvals = $script:approvals }
    } finally { Set-LabApprovalProvider $null }
}
Test-Case 'qualification tests the policy models each head has and is not using yet, after one approval (1.6.7)' {
    $r = & $qualifyRun
    Assert-Equal 1 @($r.Approvals).Count 'one approval for the whole list'
    Assert-Match 'Qualify the 2 head/model pair\(s\) not usable now \(0 usable now are kept\)' $r.Approvals[0]
    Assert-Equal '[["head-192-0-2-3","qwen3.5:4b"],["head-192-0-2-3","gemma3:4b"]]' $r.Pairs.Trim() 'head .6 lacks both models and is skipped'
    Assert-Equal 2 @($r.Calls).Count 'the plan, then the tests'; Assert-Match '--plan-only' $r.Calls[0]; Assert-False ($r.Calls[1] -match '--plan-only')
    Assert-Match 'qualify_pairs\.py' $r.Calls[0]
    Assert-Equal 'DONE' $r.Outcome.Outcome $r.Outcome.Detail
}
Test-Case 'updating or restoring an existing head rule passes Windows'' own parameter check (-WhatIf, real cmdlet)' {
    $any = Get-NetFirewallRule -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $any) { Add-TestResult -Name 'no firewall rule readable here: skipped' -Passed $true -Detail ''; return }
    $update = @{ DisplayName = 'Agent Arena Lab - Ollama 11434 from the Gateway hub'; LocalPort = '11434'; LocalAddress = 'Any'; RemoteAddress = @('192.0.2.254'); Protocol = 'TCP'; Profile = 'Any'; Enabled = 'True'; Action = 'Allow' }
    $undo = @{ RemoteAddress = @('192.0.2.1'); Enabled = 'True' }
    $raw = ''; try { Set-NetFirewallRule -Name $any.Name @update -WhatIf -ErrorAction Stop *>$null } catch { $raw = $_.Exception.Message }
    Assert-Match 'Parameter set cannot be resolved' $raw 'reproduces the bug: -Name with -DisplayName'
    foreach ($p in $update, $undo) {
        $q = ConvertTo-LabSetRuleParameters $p
        $err = ''; try { Set-NetFirewallRule -Name $any.Name @q -WhatIf -ErrorAction Stop *>$null } catch { $err = $_.Exception.Message }
        Assert-Equal '' $err ('keys: ' + ($q.Keys -join ','))
    }
    Assert-Match 'ConvertTo-LabSetRuleParameters' ((Get-LabDefaultFirewallBackend).SetRule.ToString()) 'the real backend converts'
}
Test-Case 'DESKTOP-REDACT10 1.3.6: the listener analysis uses the lab adapter''s profile, not the first adapter (Public Wi-Fi)' {
    $m = [pscustomobject]@{ listener = [pscustomobject]@{ bind = '192.0.2.254'; port = 8443; allowed_clients = @('192.0.2.201', '192.0.2.202'); cutover_clients = @('192.0.2.1') } }
    $rule = New-TestRule -Name 'InferenceGateway-HTTPS-8443' -Profiles @('Private') -LocalPorts @('8443') -Remote @('192.0.2.201', '192.0.2.202') -Local @('192.0.2.254')
    $cut = New-TestRule -Name 'InferenceGateway-HTTPS-8443-cutover' -Profiles @('Private') -LocalPorts @('8443') -Remote @('192.0.2.1') -Local @('192.0.2.254')
    $snap = [pscustomobject]@{ Rules = @($rule, $cut); Profiles = @('Domain', 'Private', 'Public' | ForEach-Object { [pscustomobject]@{ Name = $_; Enabled = $true; DefaultInboundAction = 'Block' } })
        Connections = @([pscustomobject]@{ InterfaceAlias = 'Wi-Fi'; NetworkCategory = 'Public' }, [pscustomobject]@{ InterfaceAlias = 'Ethernet'; NetworkCategory = 'Private' }) }
    $wrong = Get-LabListenerExposure -Snapshot $snap -Manifest $m
    Assert-Equal 'DENIED' (@($wrong.Sources | Where-Object Source -eq '192.0.2.201')[0].Verdict) 'reproduces DESKTOP-REDACT10: no adapter given, the Wi-Fi decides'
    $right = Get-LabListenerExposure -Snapshot $snap -Manifest $m -InterfaceAlias 'Ethernet'
    foreach ($s in '192.0.2.201', '192.0.2.202', '192.0.2.1') { Assert-Equal 'ADMITTED' (@($right.Sources | Where-Object Source -eq $s)[0].Verdict) $s }
    Assert-Equal 'DENIED' (@($right.Sources | Where-Object Source -eq '192.0.2.3')[0].Verdict)
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-Match 'Get-LabListenerExposure -Snapshot \$snap -Manifest \$Manifest -Config \$Config -InterfaceAlias \$lalias' $flow
}
$menuInv = [pscustomobject]@{ heads = @(
    [pscustomobject]@{ resource_id = 'head-192-0-2-3'; verified = [pscustomobject]@{ models = @('qwen3.5:4b', 'gemma3:4b', 'qwen3:4b', 'nomic-embed-text:latest', 'all-minilm:latest', 'granite4:3b' | ForEach-Object { [pscustomobject]@{ name = $_ } }) } },
    [pscustomobject]@{ resource_id = 'head-192-0-2-4'; verified = [pscustomobject]@{ models = @('qwen3.5:4b', 'gemma3:4b' | ForEach-Object { [pscustomobject]@{ name = $_ } }) } },
    [pscustomobject]@{ resource_id = 'head-192-0-2-6'; verified = [pscustomobject]@{ models = @('gemma3:1b' | ForEach-Object { [pscustomobject]@{ name = $_ } }) } }) }
Test-Case 'model menu: the chat models on the verified heads, with their heads, embeddings left out' {
    $c = @(Get-LabModelChoices -Inventory $menuInv)
    Assert-Equal 'gemma3:1b|gemma3:4b|granite4:3b|qwen3.5:4b|qwen3:4b' (($c | ForEach-Object Name) -join '|')
    Assert-Equal '.3 .4' ((@($c | Where-Object Name -eq 'qwen3.5:4b')[0].Heads) -join ' ')
    Assert-Match 'reasons before answering' (@($c | Where-Object Name -eq 'qwen3:4b')[0].Note)
}
Test-Case 'model menu: numbers become model names; anything else is refused with the reason' {
    $c = @(Get-LabModelChoices -Inventory $menuInv)
    Assert-Equal 'gemma3:4b|qwen3.5:4b' (((ConvertFrom-LabModelSelection -Answer '4, 2' -Choices $c).Models) -join '|')
    Assert-Equal 'gemma3:4b|granite4:3b' (((ConvertFrom-LabModelSelection -Answer '2 3 3' -Choices $c).Models) -join '|')
    foreach ($bad in '', '0', '9', 'two', '1,x') { Assert-False (ConvertFrom-LabModelSelection -Answer $bad -Choices $c).Ok "'$bad'" }
}
Test-Case 'model menu: the policy keeps every other setting; research keeps its chosen models, else the first' {
    $pol = [pscustomobject]@{ configuration_version = 1; contract = 'shared.inference/1'; resources = @(); clients = @(
        [pscustomobject]@{ app_id = 'capstone'; credential = 'provisioned'; models = @('qwen3.5:4b', 'gemma3:4b'); max_concurrency = 1; queue_limit = 16; priority = 6 },
        [pscustomobject]@{ app_id = 'capstone-research'; credential = 'provisioned'; models = @('qwen3.5:4b'); max_concurrency = 1; queue_limit = 32; priority = 2 }) }
    $n = New-LabPolicyWithModels -Policy $pol -Models @('gemma3:4b', 'granite4:3b', 'qwen3.5:4b')
    Assert-Equal 'gemma3:4b|granite4:3b|qwen3.5:4b' ((@($n.clients | Where-Object app_id -eq 'capstone')[0].models) -join '|')
    Assert-Equal 'qwen3.5:4b' ((@($n.clients | Where-Object app_id -eq 'capstone-research')[0].models) -join '|')
    $n2 = New-LabPolicyWithModels -Policy $pol -Models @('granite4:3b')
    Assert-Equal 'granite4:3b' ((@($n2.clients | Where-Object app_id -eq 'capstone-research')[0].models) -join '|') 'research falls back to the first chosen'
    Assert-Equal 2 (@($n.clients | Where-Object app_id -eq 'capstone-research')[0].priority); Assert-Equal 16 (@($n.clients | Where-Object app_id -eq 'capstone')[0].queue_limit)
    Assert-True (Test-LabGatewayPolicyIdentities -Policy $n).Ok 'still passes the identity rules'
    Assert-Equal 'qwen3.5:4b|gemma3:4b' ((@($pol.clients | Where-Object app_id -eq 'capstone')[0].models) -join '|') 'the input is not changed'
    # A one-model list must stay a JSON array in the written file (the rehearsal wrote "qwen3.5:4b" as a string).
    $one = New-LabPolicyWithModels -Policy $pol -Models @('granite4:3b')
    $json = $one | ConvertTo-Json -Depth 10
    Assert-False ($json -match '"models":\s+"') ('a one-model list must stay a JSON array: ' + (($json -split "`r?`n" | Where-Object { $_ -match 'models' }) -join ' '))
    foreach ($c in @(($json | ConvertFrom-Json).clients)) { Assert-True ($c.models -is [array]) ("$($c.app_id) models is an array") }
    if ($gw) {
        $t = Get-Content (Join-Path $gw.Root 'config\policy.template.json') -Raw | ConvertFrom-Json
        $n3 = New-LabPolicyWithModels -Models @('gemma3:4b') -Template $t
        Assert-Equal 'capstone|capstone-research' ((@($n3.clients) | ForEach-Object app_id) -join '|') 'from the template, without coder'
        Assert-True (Test-LabGatewayPolicyIdentities -Policy $n3).Ok 'template-based policy passes'
    }
}
Start-TestGroup 'Gateway Models screen (1.4.0): the hub''s model choices without the USB'
Test-Case 'Gateway Models: a standard account that can write is refused; read-only and inherit-only entries are not' {
    Assert-True (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $env:windir)) 'C:\Windows: Users read and execute only'
    $d = Join-Path $Temp ('acl-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $d | Out-Null
    & icacls.exe $d /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' "*$([Security.Principal.WindowsIdentity]::GetCurrent().User.Value):(OI)(CI)F" '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
    Assert-True (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $d)) 'Users RX only'
    & icacls.exe $d /grant '*S-1-5-32-545:(OI)(CI)M' /Q | Out-Null
    Assert-False (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $d)) 'Users Modify'
    & icacls.exe $d /remove:g '*S-1-5-32-545' /Q | Out-Null
    & icacls.exe $d /grant '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value     # stands in for an Arena account that may write
    Assert-True (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $d)) 'an unlisted account is not judged'
    Assert-False (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $d) -Extra @($me)) 'an Arena account that may write is refused'
}
Test-Case 'Gateway Models: hub > 2 reads the screen''s Do not use list and policy copy, and refuses an unreadable list' {
    $d = Join-Path $Temp ('mc-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $d | Out-Null
    Assert-Equal 0 @(Get-LabDoNotUse -Folder $d).Count 'no list yet'
    Assert-True ($null -eq (Get-LabHubApprovedPolicy -Folder $d)) 'no policy copy yet'
    '{"entries": [{"resource_id": "head-192-0-2-4", "model": "gemma3:4b", "since": "x", "by": "LabOperator1"}]}' | Set-Content -LiteralPath (Join-Path $d 'do-not-use.json') -Encoding UTF8
    Assert-Equal 'head-192-0-2-4|gemma3:4b' ((@(Get-LabDoNotUse -Folder $d)) -join ',')
    '{"configuration_version": 1, "contract": "shared.inference/1", "clients": [{"app_id": "capstone", "credential": "provisioned", "models": ["llama3.2:3b"], "max_concurrency": 1, "queue_limit": 16, "priority": 6}]}' | Set-Content -LiteralPath (Join-Path $d 'gateway-policy-approved.json') -Encoding UTF8
    Assert-Equal 'llama3.2:3b' ((@((Get-LabHubApprovedPolicy -Folder $d).Policy.clients)[0].models) -join ',')
    'not json' | Set-Content -LiteralPath (Join-Path $d 'do-not-use.json')
    $threw = $false; try { Get-LabDoNotUse -Folder $d | Out-Null } catch { $threw = $true }; Assert-True $threw 'an unreadable list stops qualification'
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-Match '\$blocked = @\(Get-LabDoNotUse\)' $flow; Assert-Match '\$blocked -contains' $flow
    Assert-Match '\$hubCopy = Get-LabHubApprovedPolicy' $flow
    Assert-Match 'if \(\$hubCopy\) \{ \[IO\.File\]::WriteAllText\(\$hubCopy\.Path' $flow 'a menu choice updates the screen''s copy too'
}
Test-Case 'Gateway Models: install copies and checks every file, removes old ones, restricts its settings, makes the shortcut, runs its self-check' {
    $w = Join-Path $Temp ('mi-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); $src = Join-Path $root 'engine\hub-tools\gateway-models'
    $tgt = Join-Path $w 'GatewayModels'; $data = Join-Path $w 'models-console'; $desk = Join-Path $w 'Desktop'; New-Item -ItemType Directory -Path $desk -Force | Out-Null
    $py = Join-Path $w 'fakepy.cmd'
    Set-Content -LiteralPath $py -Encoding ASCII -Value '@echo {"ok": true, "version": "1.0.0", "release": "0.3.0", "ledger_id": "LEDGER-test", "orchestrators": {"capstone": ["qwen3.5:4b"]}, "heads": ["192.0.2.3"], "models": 3}'
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value     # unelevated test: this account owns the folders
    $msg = ''; try { Install-LabModelsConsole -Source $src -Python $py -Target $tgt -DataDir $data -Desktop $desk | Out-Null } catch { $msg = $_.Exception.Message }
    Assert-Match 'not to Administrators' $msg 'a settings folder owned by a standard account is refused'
    Remove-Item -LiteralPath $data -Recurse -Force
    $r = Install-LabModelsConsole -Source $src -Python $py -Target $tgt -DataDir $data -Desktop $desk -TrustedOwners @($me)
    Assert-Equal 'DONE' $r.Outcome; Assert-True (Test-Path (Join-Path $desk 'Gateway Models.lnk')) 'shortcut'
    foreach ($f in Get-ChildItem $src -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' }) {
        $rel = $f.FullName.Substring($src.Length + 1)
        Assert-Equal (Get-FileHash $f.FullName).Hash (Get-FileHash (Join-Path $tgt $rel)).Hash $rel
    }
    Assert-Equal (Get-FileHash (Join-Path $root 'engine\hub-tools\model-library\model_library.py')).Hash (Get-FileHash (Join-Path $tgt 'model_library.py')).Hash 'the Fleet Commit helper''s model library sits beside it'
    Assert-True (Test-Path (Join-Path $tgt 'fleet_commit.py')) 'the Fleet Commit helper'
    Assert-True (Test-Path (Join-Path $tgt 'lab_traffic.py')) 'Lab Traffic'
    # The reference copy: the stick's hub payload, or the repository's when the suite runs there.
    $arenaRef = @((Join-Path $root 'payload\AGENT-ARENA-HUB\app\arena_service'), (Join-Path (Split-Path -Parent $root) 'arena_service')) | Where-Object { Test-Path (Join-Path $_ 'client.py') } | Select-Object -First 1
    foreach ($n in '__init__.py', 'client.py', 'crypto.py', 'secretfile.py') { Assert-Equal (Get-FileHash (Join-Path $arenaRef $n)).Hash (Get-FileHash (Join-Path $tgt "arena_service\$n")).Hash "Arena's client $n beside it" }
    Assert-True (Test-Path (Join-Path $desk 'Lab Traffic.lnk')) 'the Lab Traffic shortcut'
    Assert-Match 'LEDGER-test' $r.Detail; Assert-Match 'capstone: qwen3.5:4b' $r.Detail
    $sids = @((Get-Acl -LiteralPath $data).Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique)
    Assert-Equal 'S-1-5-18|S-1-5-32-544' ($sids -join '|') 'settings folder: SYSTEM and Administrators only'
    Assert-True ((Get-Acl -LiteralPath $data).AreAccessRulesProtected) 'inheritance removed'
    Set-Content -LiteralPath (Join-Path $tgt 'old-file.txt') -Value 'x'
    $r2 = Install-LabModelsConsole -Source $src -Python $py -Target $tgt -DataDir $data -Desktop $desk -TrustedOwners @($me)
    Assert-Equal 'DONE' $r2.Outcome; Assert-False (Test-Path (Join-Path $tgt 'old-file.txt')) 'a file an older version left is removed'
    Assert-Equal 'UNCHANGED' (Install-LabModelsConsole -Source $src -Python $py -Target $tgt -DataDir $data -Desktop $desk -TrustedOwners @($me)).Outcome
    Set-Content -LiteralPath $py -Encoding ASCII -Value '@echo {"ok": false, "error": "the Gateway''s observe tool failed"}'
    $msg = ''; try { Install-LabModelsConsole -Source $src -Python $py -Target $tgt -DataDir $data -Desktop $desk -TrustedOwners @($me) | Out-Null } catch { $msg = $_.Exception.Message }
    Assert-Match 'self-check failed: the Gateway.s observe tool failed' $msg
}
Test-Case 'Fleet: the admin hub reads its folders and may write only its Commit requests; the library is administrators only' {
    $plan = Get-LabFleetAclPlan -FleetDir 'C:\ProgramData\AgentArenaLab\fleet' -LibraryDir 'C:\ProgramData\AgentArenaLab\model-library' -ArenaSids @('S-1-5-21-1-2-3-1005')
    $root0 = $plan | Where-Object { $_.Path -eq 'C:\ProgramData\AgentArenaLab\fleet' }; $req = $plan | Where-Object { $_.Path -like '*\requests' }; $lib = $plan | Where-Object { $_.Path -like '*model-library' }
    Assert-True ($root0.Grants -contains '*S-1-5-32-545:(OI)(CI)RX') 'Users read the fleet folder'; Assert-False (($root0.Grants -join ' ') -match 'S-1-5-21-1-2-3-1005') 'Arena cannot write it'
    Assert-True ($req.Grants -contains '*S-1-5-21-1-2-3-1005:(OI)(CI)M') 'Arena writes its requests'
    Assert-Equal '*S-1-5-18:(OI)(CI)F|*S-1-5-32-544:(OI)(CI)F' ($lib.Grants -join '|') 'library: SYSTEM and Administrators only'
}
Test-Case 'Fleet: install sets the folders, copies the catalog, and needs the helper''s published state' {
    $w = Join-Path $Temp ('fl-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $w | Out-Null
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $helper = Join-Path $w 'fleet_commit.py'; Set-Content -LiteralPath $helper -Value '# stand-in'
    $py = Join-Path $w 'fakepy.cmd'; Set-Content -LiteralPath $py -Encoding ASCII -Value '@echo {"ok": true, "version": "1.0.0", "heads": ["192.0.2.3", "192.0.2.4"], "library": 0}'
    $cat = Join-Path $root 'config\model-catalog.json'; $fd = Join-Path $w 'fleet'; $ld = Join-Path $w 'model-library'
    try {
        $msg = ''; try { Install-LabFleet -Python $py -Catalog $cat -Helper $helper -FleetDir $fd -LibraryDir $ld -ArenaSids @($me) | Out-Null } catch { $msg = $_.Exception.Message }
        Assert-Match 'not to Administrators' $msg 'a folder made by a standard account is refused'
        $r = Install-LabFleet -Python $py -Catalog $cat -Helper $helper -FleetDir $fd -LibraryDir $ld -ArenaSids @($me) -TrustedOwners @($me)
        Assert-Equal 'DONE' $r.Outcome; Assert-Match 'published state for 2 head' $r.Detail
        Assert-Equal (Get-FileHash $cat).Hash (Get-FileHash (Join-Path $fd 'catalog\model-catalog.json')).Hash 'the catalog from the USB, readable by a standard account'
        $fileSids = @((Get-Acl -LiteralPath (Join-Path $fd 'catalog\model-catalog.json')).Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique)
        Assert-Equal 'S-1-5-18|S-1-5-32-544|S-1-5-32-545' ($fileSids -join '|') 'a file inside inherits the folder''s permissions (icacls /grant (OI)(CI) /T left files with none)'
        $reqSids = @((Get-Acl -LiteralPath (Join-Path $fd 'requests')).Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
        Assert-True ($reqSids -contains $me) 'requests: the Arena account'
        Assert-True (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath (Join-Path $fd 'status')) -Extra @($me)) 'status: nobody standard writes'
        Set-Content -LiteralPath $py -Encoding ASCII -Value '@echo {"ok": false, "error": "the Gateway''s observe tool failed"}'
        $msg = ''; try { Install-LabFleet -Python $py -Catalog $cat -Helper $helper -FleetDir (Join-Path $w 'fleet2') -LibraryDir (Join-Path $w 'lib2') -ArenaSids @($me) -TrustedOwners @($me) | Out-Null } catch { $msg = $_.Exception.Message }
        Assert-Match 'could not publish the Gateway.s state: the Gateway.s observe tool failed' $msg
        $msg = ''; try { Install-LabFleet -Python $py -Catalog $cat -Helper $helper -FleetDir (Join-Path $w 'fleet3') -LibraryDir (Join-Path $w 'lib3') -ArenaSids @() -TrustedOwners @($me) | Out-Null } catch { $msg = $_.Exception.Message }
        Assert-Match 'no Arena account' $msg
    } finally { & icacls.exe $w /reset /T /C /Q 2>&1 | Out-Null }
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "Install-LabFleet -Run \`$run -Python \`$py -Catalog \(Join-Path \`$Release 'config\\model-catalog\.json'\)" $sp 'the spreadsheet catalog, not the 1.0.3 head profiles'
    Assert-Match 'INSTALL-ADMIN-HUB\.cmd' $sp 'the report says how Arena itself is updated'
}
Test-Case 'Gateway Models: Administration hub > 5 plans one step and the menu offers it' {
    $p = Get-TestPlan 'hub' $hubInsp @{ HubMode = 'models' }
    Assert-Equal 'models' $p.hub_mode; Assert-Equal 'g-models-console,g-fleet,g-hide-account' ((@($p.steps) | ForEach-Object id) -join ',')
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "'5' \{ return @\{ HubMode = 'models' \} \}" $sp; Assert-Match "if \(\`$mode -eq 'models'\)" $sp
    Assert-Match "Install-LabModelsConsole -Run \`$run -Source \(Join-Path \`$PSScriptRoot 'hub-tools\\gateway-models'\)" $sp
}
Test-Case 'the publisher keeps gateway-ca (the public CA the hub writes) as it keeps reports' {
    $pubFile = Join-Path $root 'build\Publish-LabUsb.ps1'
    if (-not (Test-Path -LiteralPath $pubFile)) { Add-TestResult -Name 'the publisher is not part of the USB copy: checked in the repository' -Passed $true -Detail ''; return }
    $pub = Get-Content -LiteralPath $pubFile -Raw
    # 1.5.0: also handoff, where heads 3 and 4 leave the QMS/ERP tokens for the Capstone laptop.
    Assert-Match "\`$kept = @\('reports', 'gateway-ca', 'handoff'\)" $pub
    Assert-Match 'Where-Object \{ \$kept -notcontains \$_\.Name \}\) \{ Remove-Item' $pub
}
Test-Case 'one-clicks use Windows syntax and keep their options when they ask for administrator rights (1.6.17)' {
    foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $root 'engine') -Recurse -Filter '*.cmd')) { Assert-True (-not ((Get-Content -LiteralPath $f.FullName -Raw) -match '/dev/null')) ('no /dev/null in ' + $f.Name) }
    $ho = Get-Content -LiteralPath (Join-Path $root 'engine\head-tools\head-ollama\HEAD-OLLAMA.cmd') -Raw
    Assert-Match "-ArgumentList '%\*' -Verb RunAs" $ho
}
Test-Case 'Ollama restarts end the boot task''s cmd.exe before Ollama, and head reports go to the stick root (1.6.18)' {
    # head 8, 2026-11-17: the running wrapper cmd.exe read on in the rewritten file and started 0.40.15 with the old settings.
    $uh = Get-Content -LiteralPath (Join-Path $root 'engine\head-tools\head-ollama\Update-HeadOllama.ps1') -Raw
    $rs = $uh.Substring($uh.IndexOf('function Restart-Server'))
    Assert-True ($rs.IndexOf('Stop-LabOllamaBootHost') -ge 0 -and $rs.IndexOf('Stop-LabOllamaBootHost') -lt $rs.IndexOf('Stop-Process')) 'HEAD-OLLAMA ends the boot host before Ollama'
    Assert-Match 'Read-ServerLog \$from' $uh
    $lh = Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Head.psm1') -Raw
    $rl = $lh.Substring($lh.IndexOf('function Restart-LabOllamaServer'))
    Assert-True ($rl.IndexOf('Stop-LabOllamaBootHost') -ge 0 -and $rl.IndexOf('Stop-LabOllamaBootHost') -lt $rl.IndexOf('Stop-Process')) 'the provisioner ends the boot host before Ollama'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\head-tools\head-parallel\Set-HeadOllamaParallel.ps1') -Raw
    Assert-True ($sp.IndexOf('Stop-ScheduledTask') -ge 0 -and $sp.IndexOf('Stop-ScheduledTask') -lt $sp.IndexOf('Stop-Process -Id $p.Id')) 'HEAD-PARALLEL ends the boot task before Ollama'
    Import-Module (Join-Path $root 'engine\modules\Lab.OllamaRuntime.psm1') -Force -DisableNameChecking
    Assert-True ([bool](Get-Command Stop-LabOllamaBootHost -ErrorAction SilentlyContinue)) 'Stop-LabOllamaBootHost is exported'
    foreach ($f in @('engine\head-tools\head-ollama\Update-HeadOllama.ps1', 'engine\hub-tools\model-capacity\Apply-ModelCapacity.ps1')) {
        $t = Get-Content -LiteralPath (Join-Path $root $f) -Raw
        Assert-Match 'if \(Test-Path -LiteralPath \(Join-Path \$d ''START-LAB-PROVISIONER\.cmd''\)\) \{ \$stick = \$d \}' $t
    }
}
Test-Case 'a Gateway folder that grants Arena access is repaired with approval, not blocked (1.6.15)' {
    $only = @('logs folder C:\ProgramData\InferenceGateway\logs grants Desktop-Redact10\ArenaSvcUser: Arena and broad groups must hold no access')
    Assert-True (Test-LabGatewayFolderGrantsOnly -Blockers $only) 'folder grant only'
    Assert-True (-not (Test-LabGatewayFolderGrantsOnly -Blockers ($only + 'the Arena account X is a member of Administrators'))) 'a membership blocker still blocks'
    Assert-True (-not (Test-LabGatewayFolderGrantsOnly -Blockers @())) 'no blockers'
    $flow = Get-Content -LiteralPath (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1') -Raw
    Assert-Match "-Word 'REMOVE-ACCESS'" $flow
}
Test-Case 'the publisher (1.6.11) takes a fixed USB SSD only by model, and refuses the protected sticks by serial' {
    $pubFile = Join-Path $root 'build\Publish-LabUsb.ps1'
    if (-not (Test-Path -LiteralPath $pubFile)) { Add-TestResult -Name 'the publisher is not part of the USB copy: checked in the repository' -Passed $true -Detail ''; return }
    $pub = Get-Content -LiteralPath $pubFile -Raw
    Assert-Match "\`$usbFixed = \(\`$vol\.DriveType -eq 'Fixed' -and \`$disk -and \`$disk\.BusType -eq 'USB' -and \`$modelOk\)" $pub
    Assert-Match "RefuseSerials = @\('REDACTED2', 'REDACTED3', 'REDACTED4'\)" $pub
    Assert-Match "\`$blankSerial = \(\`$serial -eq '' -or \`$serial -match '\^0\+\$'\)" $pub
    Assert-Match "@\('System Volume Information', '\`$RECYCLE\.BIN'\) -notcontains" $pub
}

# ---------------------------------------------------------------------------
Start-TestGroup 'field 2026-11-07: Capstone laptop, drive letter, head 10'
Test-Case 'Capstone rc.3 unpacks inside Windows'' path limit at the provisioner''s folder; the 1.4.4 folder did not fit' {
    $long = 'payload/application/docs/phase5_lab_evidence/runtime-data/experiments/EXP-91a83613c14d4bd2b7df8eca7716b40b/RUN-90887f85b9a34a198373eec29d37dc74/test-temp/test_task_transitions_dependen0/phase4.db'
    Assert-Equal 195 $long.Length 'the deepest entry of the rc.3 archive'
    $old = Test-LabZipFits -Destination 'C:\ProgramData\AgentArenaLab\staging\Capstone-6.0.0-rc.3-windows-x64' -Entries @($long, 'package-manifest.json')
    Assert-False $old.Ok ('1.4.4 would have needed {0} characters' -f $old.Longest)
    $new = Test-LabZipFits -Destination 'C:\ProgramData\AgentArenaLab\cs' -Entries @($long, 'package-manifest.json')
    Assert-True $new.Ok ('{0} characters' -f $new.Longest); Assert-True ($new.Room -ge 25) 'room to spare for a longer release'
    $apps = Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Apps.psm1') -Raw
    Assert-Match "Join-Path \`$Run\.Root 'cs'" $apps; Assert-Match 'Expand-LabZipChecked' $apps
    $zip = @(Get-ChildItem -LiteralPath (Join-Path (Split-Path -Parent $root) 'lab-provisioner\packages\capstone'), 'F:\AGENT-ARENA-LAB-1.4.4\packages\capstone' -Filter 'Capstone-6.0.0-rc.3-windows-x64.zip' -ErrorAction SilentlyContinue) | Select-Object -First 1
    if ($zip) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem; $z = [IO.Compression.ZipFile]::OpenRead($zip.FullName)
        try { $entries = @($z.Entries | ForEach-Object { $_.FullName }) } finally { $z.Dispose() }
        Assert-True (Test-LabZipFits -Destination 'C:\ProgramData\AgentArenaLab\cs' -Entries $entries).Ok 'every entry of the real archive'
        # Capstone's own installer copies payload\application into C:\Program Files\Capstone\releases\<version>\application
        $inst = @($entries | Where-Object { $_ -like 'payload/application/*' } | ForEach-Object { $_.Substring(20) })
        Assert-True (Test-LabZipFits -Destination 'C:\Program Files\Capstone\releases\6.0.0-rc.3\application' -Entries $inst).Ok 'and where Install-Capstone.ps1 copies it'
    }
}
Test-Case 'a too-deep folder is refused before anything is unpacked, with the numbers' {
    $d = Join-Path $Temp ('z' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $d | Out-Null
    $zipPath = Join-Path $d 'deep.zip'; Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::Open($zipPath, 'Create'); try { $e = $z.CreateEntry(('a/' * 110) + 'f.txt'); $w = New-Object IO.StreamWriter($e.Open()); $w.Write('x'); $w.Close() } finally { $z.Dispose() }
    $target = Join-Path $d 'out'
    $msg = ''; try { Expand-LabZipChecked -Zip $zipPath -Destination $target | Out-Null } catch { $msg = $_.Exception.Message }
    Assert-Match 'cannot be unpacked into .* characters .* Nothing was unpacked' $msg; Assert-False (Test-Path -LiteralPath $target) 'nothing written'
}
Test-Case 'the Capstone laptop gets its lab address from the provisioner (no Windows Settings step)' {
    $p = Get-TestPlan 'capstone' (& $fakeInspection) @{}
    $n = @($p.steps | Where-Object { $_.id -eq 'network' }) | Select-Object -First 1
    Assert-True ([bool]$n) 'a network step'; Assert-Match '192\.0\.2\.201/24, no gateway' $n.title; Assert-True $n.approval 'asks before changing the address'
    Assert-Equal 'network' (@($p.steps)[0].id) 'first, before Capstone is installed'
    $held = @((Get-TestPlan 'capstone' (& $fakeInspection 'capstone' @('192.0.2.201')) @{}).steps | Where-Object { $_.id -eq 'network' })[0]
    Assert-Match 'Keep 192\.0\.2\.201/24' $held.title; Assert-False $held.approval 'already set: nothing to approve'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "Invoke-LabStep -Id 'network' -Title 'Lab address' -Action \{\s+if \(\`$addrHeld\)" $sp
}
Test-Case 'no prompt or plan line names the stick F: (it is E: on some computers)' {
    $hits = @()
    foreach ($f in Get-ChildItem (Join-Path $root 'engine') -Recurse -Include *.ps1, *.psm1 | Where-Object { $_.FullName -notmatch '\\head-engine\\' }) {
        $tokErr = $null; $strs = @([Management.Automation.PSParser]::Tokenize((Get-Content -LiteralPath $f.FullName -Raw), [ref]$tokErr) | Where-Object { $_.Type -eq 'String' } | ForEach-Object { $_.Content })
        foreach ($s in $strs) { if ($s -match '(from|on|via|to|copied to|identical to|never) F:|F:\\gateway-ca|\(not F:\)') { $hits += ('{0}: {1}' -f $f.Name, $s.Substring(0, [Math]::Min(80, $s.Length))) } }
    }
    Assert-Equal 0 $hits.Count ($hits -join ' | ')
    Assert-Equal 'the provisioner stick (E:)' (Get-LabUsbLabel 'E:\'); Assert-Equal 'the provisioner stick (F:)' (Get-LabUsbLabel 'f:\'); Assert-Equal 'the provisioner stick' (Get-LabUsbLabel '')
    Assert-Match "Join-Path \`$UsbRoot 'gateway-ca\\inference-gateway-ca\.pem'" (Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw) 'the CA is found from where the provisioner runs'
}
Test-Case 'Capstone laptop (1.4.6): the clock and Smart App Control are planned like the address' {
    $ids = @((Get-TestPlan 'capstone' (& $fakeInspection 'capstone' @('192.0.2.201')) @{}).steps | ForEach-Object id)
    Assert-Equal 'network,time,appcontrol' (($ids | Select-Object -First 3) -join ',')
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "Invoke-LabStep -Id 'time' -Title 'Time zone and time source' -ContinueOnFailure -Action \{ Set-LabTimeSync" $sp
    Assert-Match 'Request-LabSmartAppControlOff -Run \$run -IncludeEvaluation' $sp
}
Test-Case 'Capstone health: waited for after its task starts; when it does not answer, its logs come back on the stick' {
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    $w = $sp.IndexOf('Wait-LabCapstoneHealth -Config $Config -Seconds 180'); $t = $sp.IndexOf("tools\Test-CapstoneHealth.ps1")
    Assert-True ($w -gt 0 -and $w -lt $t) 'waits (up to 3 minutes) before Capstone''s own 10-second check'
    Assert-Match 'Save-LabCapstoneDiagnostics -Run \$run -Config \$Config -UsbRoot \$UsbRoot' $sp
    Assert-Equal 'PermissionError: [Errno 13] Permission denied: ''C:\ProgramData\Capstone\data\x.db''' (Get-LabCapstoneFailureLine -Lines @('INFO:     Started server process [1]', 'Traceback (most recent call last):', '  File "run_app.py", line 3', 'PermissionError: [Errno 13] Permission denied: ''C:\ProgramData\Capstone\data\x.db'''))
    Assert-Equal 'ERROR: [Errno 10048] error while attempting to bind on address' (Get-LabCapstoneFailureLine -Lines @('INFO:     Waiting for application startup.', 'ERROR: [Errno 10048] error while attempting to bind on address', 'INFO:     Application shutdown complete.'))
    Assert-Equal '' (Get-LabCapstoneFailureLine -Lines @())
}
Test-Case 'Ticketing adapter: Python for the signed-in user comes from the stick; the codes say where to get them' {
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "Test-LabRuntimeInstaller -Path \`$inst -Spec \`$Config\.runtime\.python" $sp 'the installer is checked before it runs'
    Assert-Match "'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1'" $sp 'for this user only'
    Assert-Match 'Machines > Machines and codes\.\.\. > Addresses' $sp 'the claim code prompt names its place in the admin hub'
    Assert-Match 'Applications, the Ticketing \(capstone\) row > App code' $sp 'so does the app code'
    Assert-False ($sp -match 'hub code') 'one name for each code'
}
Test-Case 'the Gateway''s service account can be hidden from the sign-in screen (it keeps working)' {
    $key = 'HKCU:\Software\AgentArenaLabTest\UserList-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
    try {
        $acct = (Get-LocalUser | Select-Object -First 1).Name     # any local account (this laptop's sign-in may be a Microsoft account)
        Assert-Equal 'DONE' (Set-LabHiddenSignInAccount -Name $acct -Key $key).Outcome
        Assert-Equal 0 ((Get-ItemProperty -Path $key -Name $acct).$acct)
        Assert-Equal 'UNCHANGED' (Set-LabHiddenSignInAccount -Name $acct -Key $key).Outcome
        $msg = ''; try { Set-LabHiddenSignInAccount -Name 'no-such-account-xyz' -Key $key | Out-Null } catch { $msg = $_.Exception.Message }
        Assert-Match 'no local account' $msg
    } finally { Remove-Item -Path 'HKCU:\Software\AgentArenaLabTest' -Recurse -Force -ErrorAction SilentlyContinue }
}
Test-Case 'Smart App Control: offered on the viewer step and turned off with a typed TURN-OFF; a restart is said when needed' {
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    $vs = $sp.Substring($sp.IndexOf('function Invoke-LabViewerStep'), 1200)
    Assert-Match 'Request-LabSmartAppControlOff -Run \$Run' $vs 'before the viewer is touched'
    Assert-Match 'restart this computer, then run Existing machine > 8 again' $vs
    $arena = Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Arena.psm1') -Raw
    Assert-Match "-Word 'TURN-OFF'" $arena; Assert-Match 'VerifiedAndReputablePolicyState -Value 0' $arena
}
Test-Case 'Capstone laptop (1.4.7): the Visual C++ runtime its torch needs is checked and installed from the stick' {
    $d = Join-Path $Temp ('vc' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $d | Out-Null
    $r = Get-LabVcRuntimeState -SystemDir $d
    Assert-False $r.Ok; Assert-Equal 'vcruntime140.dll,vcruntime140_1.dll,vcruntime140_threads.dll,msvcp140.dll,msvcp140_atomic_wait.dll' ($r.Missing -join ',') 'the five DLLs torch imports (DESKTOP-REDACT11 had none)'
    foreach ($n in $r.Missing) { Set-Content -LiteralPath (Join-Path $d $n) -Value 'x' }
    Assert-True (Get-LabVcRuntimeState -SystemDir $d).Ok
    $spec = $Config.runtime.vcredist
    Assert-Equal 'vc_redist.x64.exe' $spec.file; Assert-Equal 'Microsoft Corporation' $spec.publisher; Assert-Match '^[0-9a-f]{64}$' $spec.sha256
    $ids = @((Get-TestPlan 'capstone' (& $fakeInspection 'capstone' @('192.0.2.201')) @{}).steps | ForEach-Object id)
    Assert-True ([array]::IndexOf($ids, 'vcredist') -ge 0 -and [array]::IndexOf($ids, 'vcredist') -lt [array]::IndexOf($ids, 'capstone-install')) 'before Capstone'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "Test-LabRuntimeInstaller -Path \`$inst -Spec \`$spec" $sp; Assert-Match "'/install', '/quiet', '/norestart'" $sp
    Assert-Match 'task had stopped \(last result' $sp 'a task that ended with an error is started again'
    Assert-False ((Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Arena.psm1') -Raw) -match 'CiTool.exe') 'no CiTool call (it stopped the run on DESKTOP-REDACT11)'
}
Test-Case 'Capstone laptop (1.4.8): an address claimed in an earlier run is not asked for again (DESKTOP-REDACT11''s real report)' {
    $d = Join-Path $Temp ('rp' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $d | Out-Null
    Assert-Equal '' (Get-LabPriorClaim -ReportsDir $d -Address '192.0.2.201') 'no record: the code is asked'
    Copy-Item (Join-Path $root 'tests\fixtures\device2-20261107\DESKTOP-REDACT11-20261107-180529-DESKTOP-REDACT11-capstone.json') $d
    $p = Get-LabPriorClaim -ReportsDir $d -Address '192.0.2.201'
    Assert-Match 'already claimed in run 20261107-180529-DESKTOP-REDACT11-capstone: claimed as capstone/main' $p
    Assert-Equal '' (Get-LabPriorClaim -ReportsDir $d -Address '192.0.2.202') 'another address is not claimed'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    $c = $sp.IndexOf('Get-LabPriorClaim -ReportsDir $Run.ReportsDir'); $q = $sp.IndexOf('Arena CLAIM CODE for')
    Assert-True ($c -gt 0 -and $c -lt $q) 'checked before the code is asked'
}
Test-Case 'Capstone laptop (1.4.8): a "Capstone" desktop icon opens it in the browser' {
    $d = Join-Path $Temp ('cs' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path (Join-Path $d 'config'), (Join-Path $d 'desk') -Force | Out-Null
    '{"listen_host":"192.0.2.201","listen_port":8000}' | Set-Content -LiteralPath (Join-Path $d 'config\appsettings.json') -Encoding ASCII
    $cfg = [pscustomobject]@{ capstone = [pscustomobject]@{ data_root = $d } }
    Assert-Equal 'DONE' (Set-LabCapstoneShortcut -Config $cfg -Desktop (Join-Path $d 'desk')).Outcome
    Assert-Match 'URL=http://192\.0\.2\.201:8000/' (Get-Content -LiteralPath (Join-Path $d 'desk\Capstone.url') -Raw)
    Assert-Match 'URL=http://192\.0\.2\.201:8000/testing-lab-login\?next=/pressure-testing' (Get-Content -LiteralPath (Join-Path $d 'desk\Capstone Testing Lab.url') -Raw)
    Assert-Equal 'UNCHANGED' (Set-LabCapstoneShortcut -Config $cfg -Desktop (Join-Path $d 'desk')).Outcome
    Assert-Match "Invoke-LabStep -Id 'capstone-shortcut'" (Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw)
    # Where Capstone's own installer made its icons (rc.4 fresh install), no second icon is added.
    $e = Join-Path $d 'desk2'; New-Item -ItemType Directory -Path $e -Force | Out-Null; foreach ($n in 'Capstone', 'Capstone Testing Lab') { Set-Content -LiteralPath (Join-Path $e "$n.lnk") -Value 'x' }
    Assert-Equal 'UNCHANGED' (Set-LabCapstoneShortcut -Config $cfg -Desktop $e).Outcome
    Assert-Equal 0 @(Get-ChildItem -LiteralPath $e -Filter '*.url').Count
    # rc.5's updater makes its own icons: the lab's earlier copies are removed, not shown twice; a foreign .url stays.
    $f = Join-Path $d 'desk3'; New-Item -ItemType Directory -Path $f -Force | Out-Null
    Assert-Equal 'DONE' (Set-LabCapstoneShortcut -Config $cfg -Desktop $f).Outcome
    Set-Content -LiteralPath (Join-Path $f 'Capstone.lnk') -Value 'x'; Set-Content -LiteralPath (Join-Path $f 'Capstone Testing Lab.lnk') -Value 'x'
    [IO.File]::WriteAllText((Join-Path $f 'Capstone Testing Lab.url'), "[InternetShortcut]`r`nURL=http://elsewhere/`r`n")
    $r = Set-LabCapstoneShortcut -Config $cfg -Desktop $f
    Assert-Equal 'DONE' $r.Outcome; Assert-Match 'duplicate removed' $r.Detail
    Assert-False (Test-Path -LiteralPath (Join-Path $f 'Capstone.url')) 'the lab duplicate is gone'
    Assert-True (Test-Path -LiteralPath (Join-Path $f 'Capstone Testing Lab.url')) 'a .url the lab did not write stays'
    Assert-Equal 'UNCHANGED' (Set-LabCapstoneShortcut -Config $cfg -Desktop $f).Outcome
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Capstone 6.0.0-rc.5: Research Lab on heads 2-10 (1.5.2)'
$labCfg = Get-LabConfig -Path (Join-Path $root 'config\lab-network.json')
Test-Case 'rc.5''s updater asks 127.0.0.1, where Capstone does not listen: its wait is pointed at Capstone''s own address' {
    $rc5 = "try{`r`n Start-ScheduledTask -TaskName `$TaskName`r`n `$deadline=(Get-Date).AddSeconds(240);`$healthy=`$false`r`n while((Get-Date) -lt `$deadline){`r`n  try{`$response=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 3;if(`$response.ok){`$healthy=`$true;break}}catch{}`r`n }`r`n}catch{`r`n & (Join-Path `$InstallRoot 'tools\Stop-Capstone.ps1') -TaskName `$TaskName`r`n `$rollback=1`r`n}"
    Assert-False (Test-LabCapstoneUpdaterFixed -Text $rc5) 'rc.5 as shipped is not fixed (127.0.0.1)'
    $p = ConvertTo-LabPatchedCapstoneUpdate -Text $rc5
    Assert-True ($p -notmatch '127\.0\.0\.1') 'no loopback left'
    Assert-Match "\`$labS=Get-Content -Raw -LiteralPath \(Join-Path \`$DataRoot 'config\\appsettings\.json'\)\|ConvertFrom-Json;\`$deadline=\(Get-Date\)\.AddSeconds\(240\)" $p
    Assert-Match "-Uri \('http://\{0\}:\{1\}/api/health' -f \`$labS\.listen_host,\`$labS\.listen_port\) -TimeoutSec 3" $p
    Assert-True (Test-LabCapstoneUpdaterFixed -Text $p) 'patched rc.5 counts as fixed'
    $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseInput($p, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'parses'
    $rc4 = "try{`r`n Start-ScheduledTask -TaskName `$TaskName;Start-Sleep -Seconds 5`r`n}catch{`r`n `$rollback=1`r`n}"
    Assert-False (Test-LabCapstoneUpdaterFixed -Text $rc4) 'rc.4 shape'
    $own = $rc5.Replace("-Uri 'http://127.0.0.1:8000/api/health'", "-Uri (""http://`$(`$s.listen_host):`$(`$s.listen_port)/api/health"")")
    Assert-False (Test-LabCapstoneUpdaterFixed -Text $own) 'asking its own address but trusting Stop-Capstone.ps1 is not enough'
    $own = $own.Replace("`$rollback=1", "Get-CimInstance Win32_Process|Where-Object{`$_.ExecutablePath -like ""`$InstallRoot*""}|Stop-Process -Force`r`n `$rollback=1")
    Assert-True (Test-LabCapstoneUpdaterFixed -Text $own) 'an updater that asks its own address and stops its processes is run as shipped'
    Assert-False (Test-LabCapstoneUpdaterFixed -Text ($own -replace 'AddSeconds\(240\)', 'AddSeconds(5)')) 'a shorter wait is not the fix'
    Assert-False (Test-LabCapstoneUpdaterFixed -Text ($own -replace "& \(Join-Path \`$InstallRoot 'tools\\Stop-Capstone\.ps1'\) -TaskName \`$TaskName", '')) 'no stop before rollback is not the fix'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match 'Test-LabCapstoneUpdaterFixed -Text \$updText' $sp; Assert-Match 'ConvertTo-LabPatchedCapstoneUpdate -Text \$updText' $sp
    # Every Stop-Capstone.ps1 call is followed by stopping what it leaves running; patching twice adds nothing.
    $two = $rc5.Replace("try{`r`n", "& (Join-Path `$InstallRoot 'tools\Stop-Capstone.ps1') -TaskName `$TaskName`r`ntry{`r`n")
    $p2 = ConvertTo-LabPatchedCapstoneUpdate -Text $two
    Assert-Equal 2 ([regex]::Matches($p2, "Stop-Capstone\.ps1'\) -TaskName \`$TaskName;\`$labRoot=")).Count 'both stops followed'
    Assert-Equal 2 ([regex]::Matches((ConvertTo-LabPatchedCapstoneUpdate -Text $p2), '\$labRoot=\(Resolve-Path')).Count 'idempotent'
    Assert-Match 'Stop-LabCapstoneProcesses -InstallRoot' $sp
    Assert-Match 'the Capstone that answers is the active release' $sp
    # The real rc.5 updater, when the package is next to the repo: not fixed as shipped, fixed and parseable once patched.
    $real = Join-Path $root '..\..\Capstone\deploy\windows\work\Capstone-6.0.0-rc.5\tools\Update-Capstone.ps1'
    if (Test-Path -LiteralPath $real) {
        $rt = [IO.File]::ReadAllText($real); Assert-False (Test-LabCapstoneUpdaterFixed -Text $rt) 'shipped rc.5 asks 127.0.0.1'
        $rp = ConvertTo-LabPatchedCapstoneUpdate -Text $rt; Assert-True (Test-LabCapstoneUpdaterFixed -Text $rp) 'patched rc.5'
        $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseInput($rp, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'patched rc.5 parses'
    } else { Write-Host '      (the rc.5 package folder is not next to the repo: real-file check skipped)' }
}
Test-Case 'rc.6 fixes both in its own scripts: its updater runs as shipped; a patch never lands between a call''s arguments' {
    # rc.6 calls Stop-Capstone.ps1 with -InstallRoot and -DataRoot, and the process sweep lives in Stop-Capstone.ps1.
    $rc6 = "try{`r`n Start-ScheduledTask -TaskName `$TaskName`r`n `$deadline=(Get-Date).AddSeconds(240);`$healthy=`$false`r`n while((Get-Date) -lt `$deadline){`r`n  try{`$response=Invoke-RestMethod -Uri ""http://`$(`$settings.listen_host):`$(`$settings.listen_port)/api/health"" -TimeoutSec 3;if(`$response.ok){`$healthy=`$true;break}}catch{}`r`n }`r`n}catch{`r`n & (Join-Path `$InstallRoot 'tools\Stop-Capstone.ps1') -TaskName `$TaskName -InstallRoot `$InstallRoot -DataRoot `$DataRoot`r`n `$rollback=1`r`n}"
    $stop6 = "`$owned=@(Get-CimInstance Win32_Process | Where-Object { `$_.ExecutablePath -and ([IO.Path]::GetFullPath([string]`$_.ExecutablePath)).StartsWith(`$installPrefix,[StringComparison]::OrdinalIgnoreCase) })`r`nforeach(`$process in `$owned){Stop-Process -Id ([int]`$process.ProcessId) -Force}"
    $stop5 = "Stop-ScheduledTask -TaskName `$TaskName`r`nGet-CimInstance Win32_Process | Where-Object {`$_.CommandLine -like '*Start-Capstone.ps1*'} | Stop-Process -Force"
    Assert-True (Test-LabCapstoneUpdaterFixed -Text $rc6 -StopText $stop6) 'rc.6 with its own sweep is run as shipped'
    Assert-False (Test-LabCapstoneUpdaterFixed -Text $rc6 -StopText $stop5) 'the same updater with rc.5''s Stop-Capstone.ps1 is not fixed'
    Assert-False (Test-LabCapstoneUpdaterFixed -Text $rc6) 'no Stop-Capstone.ps1 given: not fixed'
    # If it ever has to be patched, the snippet follows the whole call, which still parses; patching twice adds nothing.
    $p = ConvertTo-LabPatchedCapstoneUpdate -Text $rc6
    Assert-Match "-TaskName \`$TaskName -InstallRoot \`$InstallRoot -DataRoot \`$DataRoot;\`$labRoot=" $p
    $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseInput($p, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'patched rc.6 shape parses'
    Assert-Equal 1 ([regex]::Matches((ConvertTo-LabPatchedCapstoneUpdate -Text $p), '\$labRoot=\(Resolve-Path')).Count 'idempotent with arguments after -TaskName'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match 'Test-LabCapstoneUpdaterFixed -Text \$updText -StopText \$stopText' $sp
    # The real rc.6 scripts, when the package is next to the repo: fixed as shipped.
    $w6 = Join-Path $root '..\..\Capstone\deploy\windows\work\Capstone-6.0.0-rc.6\tools'
    if (Test-Path -LiteralPath (Join-Path $w6 'Update-Capstone.ps1')) {
        $u = [IO.File]::ReadAllText((Join-Path $w6 'Update-Capstone.ps1')); $s = [IO.File]::ReadAllText((Join-Path $w6 'Stop-Capstone.ps1'))
        Assert-True (Test-LabCapstoneUpdaterFixed -Text $u -StopText $s) 'shipped rc.6 is fixed'
        Assert-False (Test-LabCapstoneUpdaterFixed -Text $u) 'without its Stop-Capstone.ps1 the updater alone does not show the sweep'
        $rp = ConvertTo-LabPatchedCapstoneUpdate -Text $u
        $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseInput($rp, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'real rc.6 patched (never needed) still parses'
        $r5 = Join-Path $root '..\..\Capstone\deploy\windows\work\Capstone-6.0.0-rc.5\tools'
        if (Test-Path -LiteralPath (Join-Path $r5 'Stop-Capstone.ps1')) {
            Assert-False (Test-LabCapstoneUpdaterFixed -Text ([IO.File]::ReadAllText((Join-Path $r5 'Update-Capstone.ps1'))) -StopText ([IO.File]::ReadAllText((Join-Path $r5 'Stop-Capstone.ps1')))) 'real rc.5 with its own stop is still patched'
        }
    } else { Write-Host '      (the rc.6 package folder is not next to the repo: real-file check skipped)' }
    # rc.7 (approved in 1.5.6) ships rc.6's updater and stop byte for byte: run as shipped too.
    $w7 = Join-Path $root '..\..\Capstone\deploy\windows\work\Capstone-6.0.0-rc.7\tools'
    if (Test-Path -LiteralPath (Join-Path $w7 'Update-Capstone.ps1')) {
        Assert-True (Test-LabCapstoneUpdaterFixed -Text ([IO.File]::ReadAllText((Join-Path $w7 'Update-Capstone.ps1'))) -StopText ([IO.File]::ReadAllText((Join-Path $w7 'Stop-Capstone.ps1')))) 'shipped rc.7 is fixed'
    } else { Write-Host '      (the rc.7 package folder is not next to the repo: real-file check skipped)' }
}
Test-Case 'COLLECT-LAB-EVIDENCE: one double-click, every machine, no secret file copied, nothing changed (1.5.7)' {
    $c = Join-Path $root 'engine\Collect-LabEvidence.ps1'; $cmd = Join-Path $root 'COLLECT-LAB-EVIDENCE.cmd'
    Assert-True (Test-Path -LiteralPath $c) 'the collector'; Assert-True (Test-Path -LiteralPath $cmd) 'its double-click launcher'
    $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseFile($c, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'parses in Windows PowerShell 5.1'
    $text = [IO.File]::ReadAllText($c)
    Assert-Match "fltmc" ([IO.File]::ReadAllText($cmd)) 'asks for administrator rights itself'
    foreach ($w in "-eq `$hubIp) { 'hub' }", "-eq `$capIp) { 'capstone' }", 'config\lab-network.json', 'Copy-Database', 'X-Capstone-Developer-Token', '/api/lab/results', '/trace', 'InferenceGateway\state', 'AgentArenaLab\fleet', '/api/tags', '/api/ps',
                 'head-speed.json', 'Start-Job -ScriptBlock $SpeedTestBlock', 'keep_alive = 0', 'Get-Counter', 'Get-MpPreference', 'systemprofile\AppData\Local\Ollama', 'powercfg', 'Get-StorageReliabilityCounter',
                 'facts.json', 'agent-arena.lab-facts/1', 'Get-NetNeighbor', 'Get-NetAdapterStatistics', 'w32tm /stripchart', 'ActiveOverlayAcPowerScheme', 'ollama app', 'duplicate_blobs', "'ctx2048'", '/api/show') {
        Assert-True ($text.Contains($w)) "collects: $w"
    }
    # Secret files are never copied, and nothing on the machine is written to (no Set-*/Remove-* outside the stick).
    $secret = [regex]::Match($text, "\`$SecretName = '([^']+)'").Groups[1].Value
    foreach ($n in 'capstone.token', 'capstone-research.token', 'ca.key', 'developer.token', 'ticketing-producer.dpapi') { Assert-True ($n -match $secret) "$n is skipped" }
    foreach ($n in 'appsettings.json', 'capstone.log', 'fleet-state.json', 'worker.log') { Assert-False ($n -match $secret) "$n is copied" }
    Assert-False ($text -match '(?m)^\s*(Set-ItemProperty|Remove-Item -LiteralPath (?!\$dbCopy)|Stop-Service|Restart-Service|netsh advfirewall firewall (add|set|delete))') 'changes nothing on the machine'
}
Test-Case 'COLLECT-LAB-EVIDENCE 1.6.29: logs only unless SPEED is typed; never during Gateway work; TLS by the Gateway CA' {
    $text = [IO.File]::ReadAllText((Join-Path $root 'engine\Collect-LabEvidence.ps1'))
    $ast = [Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$null)
    # The switch and the speed scriptblock must not share a name (PowerShell names ignore case: the block would
    # overwrite the switch and every collection would time the heads).
    $assigned = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$SpeedTest' }, $true))
    Assert-Equal 0 $assigned.Count 'nothing assigns $SpeedTest (the switch)'
    Assert-Match '\[switch\]\$SpeedTest' $text 'a -SpeedTest switch'
    Assert-Match "\`$answer -ceq 'SPEED'" $text 'SPEED, exactly, asks for the speed test'
    Assert-Match "if \(-not \`$wantSpeed\) \{ Say 'not run: logs only" $text 'logs only by default on the hub'
    Assert-Match "if \(\`$script:inFlight -gt 0\) \{ Say \(""not run: the Gateway has" $text 'refused while the Gateway has calls in flight'
    Assert-Match "if \(\`$null -eq \`$script:inFlight\) \{ Say 'not run: the Gateway''s work in flight is unknown" $text 'refused when it cannot tell'
    Assert-Match 'if \(\$wantSpeed\) \{\s+\$speed = & \$SpeedTestBlock' $text 'the head''s own speed test only when asked'
    Assert-Match '\[LabTls\]::Get\(\$Url, \$gatewayCa' $text 'Gateway /health and /ready trust only the Gateway CA'
    Assert-False ($text -match 'Get-Json "https://') 'no Gateway check through Windows'' public CA list'
    Assert-False ($text -match '(?i)ServerCertificateValidationCallback\s*=\s*\{\s*\$true') 'TLS checking is never turned off'
    Assert-Match 'logs only' ([IO.File]::ReadAllText((Join-Path $root 'COLLECT-LAB-EVIDENCE.cmd'))) 'the launcher says what Enter does'
}
Test-Case 'the banner and reports name this release (1.6.29: 1.6.28 shipped saying 1.6.27)' {
    $want = ([IO.File]::ReadAllText((Join-Path $root 'VERSION'))).Trim()
    Assert-Equal $want (Get-LabVersion) 'Get-LabVersion reads the release''s VERSION file'
    Assert-False ([IO.File]::ReadAllText((Join-Path $root 'engine\modules\Lab.Core.psm1')) -match "(?m)^\`$script:Version = '\d") 'no version typed into Lab.Core'
}
Test-Case 'the viewer step says DONE when it updated the viewer (1.6.29: nine updated heads read UNCHANGED)' {
    $t = [IO.File]::ReadAllText((Join-Path $root 'engine\Start-LabProvisioner.ps1'))
    Assert-Match '\$updatedFrom = \$v\.Version' $t 'remembers the version it updated from'
    Assert-Match "if \(\`$updatedFrom\) \{ return \(New-LabOutcome 'DONE' \(""viewer updated" $t 'and reports the update as DONE'
}
Test-Case 'WATCH-LAB 1.6.29: one double-click per machine, read-only, the right window for the hub, the laptop and a head' {
    $w = Join-Path $root 'engine\watch'
    foreach ($n in 'Start-Watch.ps1', 'Watch-Gateway.ps1', 'Watch-Head.ps1', 'watch_capstone.py') { Assert-True (Test-Path -LiteralPath (Join-Path $w $n)) $n }
    $cmd = [IO.File]::ReadAllText((Join-Path $root 'WATCH-LAB.cmd'))
    Assert-Match 'fltmc' $cmd 'asks for administrator rights itself (service folders)'; Assert-Match 'engine\\watch\\Start-Watch\.ps1' $cmd 'runs the role router'
    Assert-False ($cmd -match '(?i)\b[D-Z]:\\') 'no drive letter'
    foreach ($n in 'Start-Watch.ps1', 'Watch-Gateway.ps1', 'Watch-Head.ps1') {
        $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $w $n), [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count "$n parses in Windows PowerShell 5.1"
        $text = [IO.File]::ReadAllText((Join-Path $w $n))
        Assert-False ($text -match '(?im)^\s*[^#]*\b(Remove-Item|Stop-Process|Stop-Service|Restart-Service|Set-ItemProperty|Set-Content|Clear-Content)\b') "$n deletes, stops or rewrites nothing"
        Assert-False ($text -match '(?i)Invoke-(RestMethod|WebRequest)[^\r\n]*-Method') "$n makes only plain GET requests"
    }
    $head = [IO.File]::ReadAllText((Join-Path $w 'Watch-Head.ps1'))
    Assert-Match "'Open', 'Read', 'ReadWrite, Delete'" $head 'opens Ollama''s log for shared reading (never locks it)'
    Assert-Match '/api/ps"' $head 'lists loaded models'; Assert-False ($head -match 'Invoke-RestMethod[^\r\n]*/api/(chat|generate|embed|pull)') 'never calls a model'
    $start = [IO.File]::ReadAllText((Join-Path $w 'Start-Watch.ps1'))
    Assert-Match "\`$answer\.Trim\(\) -ceq 'SHORTCUT'" $start 'the desktop icon only when SHORTCUT is typed'
    Assert-Match "C:\\ProgramData\\AgentArenaLab\\watch" $start 'and its copy stays in the lab''s own folder'
    Assert-Match 'CommonDesktopDirectory' $start 'on every account''s desktop'
    # Behaviour, on fixtures: the hub's window replays an observation, the head's window a server log.
    $tmp = Join-Path $env:TEMP ('watch-test-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $obs = @{ admission_open = $true; requests = @{ queued = 2; leased = 1 }
            leases = @(@{ request_id = 'REQ-aaaa1111bbbb'; app_id = 'capstone-research'; resource_id = 'head-192-0-2-7'; executing = 1 })
            events = @(@{ event_id = 5; app_id = 'capstone-research'; request_id = 'REQ-aaaa1111bbbb'; event_type = 'queued'; at = $now - 3 },
                       @{ event_id = 6; app_id = 'capstone-research'; request_id = 'REQ-aaaa1111bbbb'; event_type = 'admitted'; at = $now - 2 },
                       @{ event_id = 7; app_id = 'capstone-research'; request_id = 'REQ-cccc2222dddd'; event_type = 'invocation.failed'; at = $now - 1 })
            resources = @(@{ resource_id = 'head-192-0-2-7'; resource_type = 'shared_inference'; health = 'healthy'; qualified_models = @('qwen3.5:4b') },
                          @{ resource_id = 'head-192-0-2-10'; resource_type = 'shared_inference'; health = 'offline'; qualified_models = @() })
            last_hour = @{ invocation_failures = @(@{ resource_id = 'head-192-0-2-3'; model = 'gemma3:4b'; app_id = 'capstone-research'; class = 'request_fault'; count = 2; last_at = $now }) }
            suspensions = @(); alerts = @() }
        $of = Join-Path $tmp 'observation.json'; [IO.File]::WriteAllText($of, ($obs | ConvertTo-Json -Depth 6))
        $g = (& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $w 'Watch-Gateway.ps1') -Replay $of -Once 2>&1) -join "`n"
        Assert-Match 'QUEUED +capstone-research REQ-aaaa1111' $g 'a queued request'; Assert-Match 'ADMITTED +capstone-research REQ-aaaa1111 +placed on head 7' $g 'admitted, with its head'
        Assert-Match 'FAILED +capstone-research REQ-cccc2222' $g 'a failure'; Assert-Match 'LAST HOUR +2 request_fault failure\(s\) of gemma3:4b on head 3' $g 'last hour''s failures by model and head'
        Assert-Match 'head 7 +healthy +running +1 +leased +1' $g 'the head table'; Assert-Match 'head 10 +offline' $g 'an offline head'
        $log = @('[GIN] 2026/11/19 - 10:42:07 | 200 |   12.74s |     192.0.2.254 | POST     "/api/chat"',
                 '[GIN] 2026/11/19 - 10:42:08 | 200 |       0s |       127.0.0.1 | GET      "/api/version"',
                 '[GIN] 2026/11/19 - 10:42:09 | 500 |    1.20s |     192.0.2.254 | POST     "/api/chat"',
                 'time=2026-11-19T10:42:10.100-05:00 level=INFO source=llama_server.go:1098 msg="loading model via llama-server" model=C:\x',
                 'time=2026-11-19T10:42:11.100-05:00 level=WARN source=x.go:1 msg="model recommendations refresh failed"',
                 '[GIN] 2026/11/19 - 10:42:12 | 200 | 268.83ms |     192.0.2.254 | POST     "/api/embed"') -join "`n"
        $lf = Join-Path $tmp 'server.log'; [IO.File]::WriteAllText($lf, $log)
        $h = (& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $w 'Watch-Head.ps1') -Replay $lf -Once -Port 1 2>&1) -join "`n"
        Assert-Match '10:42:07 +model call \(chat\) +12\.74s +from the Gateway \(hub\)' $h 'a model call, its time and its caller'
        Assert-Match '10:42:09 +model call \(chat\) +1\.20s +from the Gateway \(hub\) +HTTP 500' $h 'a failed call'
        Assert-Match '10:42:10 +loading a model into memory' $h 'a model load'; Assert-Match 'embedding +268\.83ms' $h 'an embedding'
        Assert-False ($h -match '/api/version|recommendations') 'listings and Ollama''s own online warnings are left out'
    } finally { [IO.Directory]::Delete($tmp, $true) }
}
Test-Case 'FIX-LAB-HEAD: memory recorded first; Defender exclusions; it deletes nothing (head 7, 2026-11-10)' {
    $f = Join-Path $root 'engine\Fix-LabHead.ps1'; $cmd = Join-Path $root 'FIX-LAB-HEAD.cmd'
    Assert-True (Test-Path -LiteralPath $f) 'the fix'; Assert-True (Test-Path -LiteralPath $cmd) 'its double-click launcher'
    Assert-Match 'fltmc' ([IO.File]::ReadAllText($cmd)) 'asks for administrator rights itself'
    $t = $null; $e = $null; $ast = [Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'parses in Windows PowerShell 5.1'
    $text = [IO.File]::ReadAllText($f)
    Assert-True ($text.IndexOf("Snapshot 'memory-before.txt'") -lt $text.IndexOf('Add-MpPreference')) 'the memory is recorded before anything changes'
    Assert-Match 'PrivateMemorySize64' $text; Assert-Match 'PoolNonpagedBytes' $text
    Assert-Match "Add-MpPreference -ExclusionPath" $text; Assert-Match "Add-MpPreference -ExclusionProcess" $text
    # Head 7: 1.6.1 deleted the models Ollama serves from. Nothing in the fix deletes a file or a folder any more.
    $calls = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() } | Where-Object { $_ })
    foreach ($bad in 'Remove-Item', 'del', 'rd', 'rmdir', 'erase', 'ri') { Assert-False ($calls -contains $bad) "the fix never calls $bad" }
    Assert-False ($text -match "'DELETE'") 'nothing to confirm a deletion for'
    Assert-Match "'RESTART'" $text; Assert-Match '\$a -ceq \$word' $text
    Assert-Match 'Rename-Item -LiteralPath \$l.FullName' $text 'the tray start-up is renamed, not deleted'
    # The store listing returns the folders themselves, and the store in use, its subfolders and its parents are never
    # a "second copy". Rebuilt as head 7 had it: the store in use (system profile) and the user's copy.
    foreach ($name in 'Get-StoreFolders', 'Test-SamePath', 'Test-Within') {
        $fn = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true) | Select-Object -First 1
        Assert-True ($null -ne $fn) "the fix defines $name"; . ([scriptblock]::Create($fn.Extent.Text))
    }
    $d = Join-Path $Temp ('h7' + [guid]::NewGuid().ToString('N').Substring(0, 6)); $active = Join-Path $d 'systemprofile\.ollama\models'; $user = Join-Path $d 'Users\Davice_7\.ollama\models'
    foreach ($st in $active, $user) {
        foreach ($sub in 'blobs', 'manifests\registry.ollama.ai\library\qwen3', 'manifests-v2', 'metadata') { New-Item -ItemType Directory -Path (Join-Path $st $sub) -Force | Out-Null }
        Set-Content (Join-Path $st 'manifests\registry.ollama.ai\library\qwen3\4b') 'M' -NoNewline; Set-Content (Join-Path $st 'blobs\sha256-aa') 'blob' -NoNewline
    }
    $found = @(Get-StoreFolders -Paths @((Join-Path $d 'Users\*\.ollama\models'), $active))
    Assert-Equal 2 $found.Count 'two stores, not their subfolders'
    Assert-True (@($found | Where-Object { $_ -match '\\(blobs|manifests|manifests-v2|metadata)$' }).Count -eq 0) 'no blobs/manifests/metadata folder is listed as a store'
    Assert-True (Test-SamePath $found[1] $active) 'the store in use is recognised as itself'
    foreach ($sub in 'blobs', 'manifests', 'metadata') { Assert-True (Test-Within (Join-Path $active $sub) $active) "$sub is inside the store in use" }
    Assert-False (Test-Within $user $active) 'the user copy is another store'
    Assert-True (Test-Within $active (Join-Path $d 'systemprofile')) 'a parent of the store in use contains it'
    Assert-True (Test-Path -LiteralPath (Join-Path $active 'blobs\sha256-aa')) 'the scratch store is untouched'
    # The models section only reports; it warns when the store in use is empty (head 7 after 1.6.1).
    Assert-Match 'this head has no models to serve' $text
    Assert-Match 'Kept: Ollama does not use it' $text
    # A computer that is not a head is not changed.
    Assert-Match 'has no head address' $text; Assert-Match 'exit 1' $text
}
Test-Case 'kernel memory: pool tags and handles are read, the hourly log writes clean lines, MEMORY-TRACE measures four phases (1.6.3)' {
    Import-Module (Join-Path $root 'engine\modules\Lab.Memory.psm1') -Force -DisableNameChecking
    # Real reads on this machine: the tags add up to most of what Windows reports for its pools.
    $perf = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $all = Get-LabPoolTagTable; $paged = 0L; foreach ($v in $all.Values) { $paged += $v[0] }
    Assert-True ($all.Count -gt 100) 'hundreds of pool tags read'
    Assert-True ($paged -gt 0.4 * $perf.PoolPagedBytes -and $paged -lt 1.6 * $perf.PoolPagedBytes) ('tags sum {0:N0} MB against the paged pool {1:N0} MB' -f ($paged / 1MB), ($perf.PoolPagedBytes / 1MB))
    $top = @(Get-LabPoolTags -Top 5); Assert-Equal 10 $top.Count 'five per pool'
    Assert-Equal 'graphics kernel (dxgkrnl)' (Get-LabTagMeaning 'DxgK'); Assert-Equal 'graphics kernel (dxgkrnl)' (Get-LabTagMeaning 'Dxgk'); Assert-Equal '' (Get-LabTagMeaning 'DXGK') 'tags are case-sensitive'
    $h = Get-LabHandleHolders -Top 3; Assert-True ($h.total -gt 1000 -and @($h.top).Count -eq 3) 'handles counted'
    # The hourly log, run twice as its task would: a header and two lines of 11 fields, numbers without separators.
    $d = Join-Path $Temp ('mw' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path $d -Force | Out-Null
    $csv = Join-Path $d 'memory-log.csv'; $ws = Join-Path $d 'w.ps1'
    [IO.File]::WriteAllText($ws, (New-LabMemoryWatchScript -ModulePath (Join-Path $root 'engine\modules\Lab.Memory.psm1') -CsvPath $csv))
    1..2 | ForEach-Object { & "$env:windir\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File $ws }
    $rows = @(Get-Content -LiteralPath $csv); Assert-Equal 3 $rows.Count 'header and two hourly lines'
    foreach ($r in $rows) { Assert-Equal 11 @($r.Split(',')).Count "11 fields: $r" }
    Assert-Match '^\d{4}-\d\d-\d\dT[\d:]+,\d+(\.\d)?,\d+(\.\d)?,\d+,\d+,' $rows[1] 'plain numbers'
    # FIX-LAB-HEAD records the kernel's owners and starts the log; the collector brings both back.
    $fx = [IO.File]::ReadAllText((Join-Path $root 'engine\Fix-LabHead.ps1')); $co = [IO.File]::ReadAllText((Join-Path $root 'engine\Collect-LabEvidence.ps1'))
    Assert-Match 'Format-LabKernelMemory -Tags \$tags' $fx; Assert-Match "Register-ScheduledTask -TaskName 'Agent Arena Lab memory log'" $fx
    Assert-Match 'Get-LabPoolTags -Top 12 -FindDrivers' $co; Assert-Match "memory-watch" $co; Assert-Match "memory-trace-\*" $co
    # MEMORY-TRACE: as the signed-in user, four phases, Agent Arena put back as found, nothing deleted.
    $f = Join-Path $root 'engine\Trace-LabMemory.ps1'; Assert-True (Test-Path -LiteralPath (Join-Path $root 'MEMORY-TRACE.cmd')) 'its launcher'
    Assert-False ([IO.File]::ReadAllText((Join-Path $root 'MEMORY-TRACE.cmd')) -match 'RunAs') 'no administrator: Agent Arena opens as the signed-in user'
    $t = $null; $e = $null; $ast = [Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'parses in Windows PowerShell 5.1'
    $text = [IO.File]::ReadAllText($f)
    foreach ($p in "name = 'quiet'; arena = `$false; load = `$false", "name = 'Arena'; arena = `$true; load = `$false", "name = 'inference'; arena = `$false; load = `$true", "name = 'both'; arena = `$true; load = `$true") { Assert-True ($text.Contains($p)) "phase: $p" }
    Assert-Match 'if \(\$viewerWasOpen\) \{ \[void\]\(Start-Viewer\) \}' $text 'Agent Arena is left as it was'
    Assert-Match '127\.0\.0\.1:11434' $text 'inference on the head''s own Ollama, not through the Gateway'
    $calls = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() } | Where-Object { $_ })
    Assert-False ($calls -contains 'Remove-Item') 'it deletes nothing'
    Assert-Match 'Get-LabTraceVerdict -Phases \$results' $text 'the verdict is the tested function'
    # The verdict: an effect only when both comparisons agree; the laptop run of 2026-11-10 (+899 and +184 MB/h, Zoom
    # started) is no consistent effect; a 300 MB/h Agent Arena leak is named; a viewer that did not open is not measured.
    $mk = { param($n, $a, $l, $r, $progs = @()) [pscustomobject]@{ phase = $n; arena_open = $a; inference = $l; pool_mb_per_hour = $r; programs = $progs } }
    $v = (Get-LabTraceVerdict -Minutes 3 -Phases @((& $mk 1 $false $false -552), (& $mk 2 $true $false 347 @([pscustomobject]@{ program = 'Zoom'; processes = '0->1' })), (& $mk 3 $false $true -184), (& $mk 4 $true $true 0))) -join "`n"
    Assert-Match 'Agent Arena open:\s+no consistent effect \(phase 2-1 \+899, phase 4-3 \+184' $v; Assert-Match 'Zoom \(phase 2\)' $v
    $v = (Get-LabTraceVerdict -Minutes 30 -Phases @((& $mk 1 $false $false 10), (& $mk 2 $true $false 310), (& $mk 3 $false $true 20), (& $mk 4 $true $true 320))) -join "`n"
    Assert-Match 'Agent Arena open:\s+ADDS about 300 MB/h' $v; Assert-Match 'inference running:\s+no consistent effect' $v
    $v = (Get-LabTraceVerdict -Minutes 30 -Phases @((& $mk 1 $false $false 10), (& $mk 2 $false $false 15), (& $mk 3 $false $true 20), (& $mk 4 $false $true 25))) -join "`n"
    Assert-Match 'Agent Arena open:\s+NOT MEASURED' $v
    $v = (Get-LabTraceVerdict -Minutes 30 -Phases @((& $mk 1 $false $false 10), (& $mk 2 $true $false 300))) -join "`n"
    Assert-Match 'NOT MEASURED: a phase did not finish' $v 'an unfinished trace gives no verdict'
}
Test-Case 'RESTORE-HEAD-MODELS puts a head''s models back through the hub''s Fleet Commit, deleting nothing (1.6.2)' {
    $f = Join-Path $root 'engine\Restore-LabHeadModels.ps1'; $cmd = Join-Path $root 'RESTORE-HEAD-MODELS.cmd'
    Assert-True (Test-Path -LiteralPath $f) 'the restore'; Assert-True (Test-Path -LiteralPath $cmd) 'its double-click launcher'
    Assert-Match 'fltmc' ([IO.File]::ReadAllText($cmd)) 'asks for administrator rights itself'
    $t = $null; $e = $null; $ast = [Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'parses in Windows PowerShell 5.1'
    $text = [IO.File]::ReadAllText($f)
    $calls = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() } | Where-Object { $_ })
    Assert-False ($calls -contains 'Remove-Item') 'it deletes nothing'
    Assert-Match '\$mine -notcontains \$hub' $text 'it runs on the hub only'
    Assert-Match "schema = 'agent-arena.fleet-request/1'" $text; Assert-Match "op = 'install'" $text
    Assert-Match '& \$python -I \$helper --commit \$id' $text 'the hub''s own helper, which asks APPROVE'
    Assert-Match 'registry\.ollama\.ai' $text 'says so when the hub has no internet, before anything'
    Assert-Match '\$now -notcontains \$_' $text 'only what the head does not have now'
    $sp = $text.IndexOf('$net = $false'); $wr = $text.IndexOf('WriteAllText((Join-Path $requests')
    Assert-True ($sp -gt 0 -and $wr -gt $sp) 'the internet is checked before the request is written'
}
Test-Case 'Gateway 0.4.0: each head''s slots and memory_gb come from the lab head list (RAM - 4 GB; VRAM on the GPU head)' {
    $cfg = Get-Content -Raw (Join-Path $root 'config\lab-network.json') | ConvertFrom-Json
    $want = @{ '192.0.2.2' = 16; '192.0.2.3' = 11.4; '192.0.2.6' = 3.4; '192.0.2.7' = 27.4; '192.0.2.10' = 27.4 }
    foreach ($a in $want.Keys) { $c = Get-LabHeadCapacity -Config $cfg -Address $a; Assert-Equal 1 $c.slots "$a slots"; Assert-Equal $want[$a] $c.memory_gb "$a memory_gb" }
    Assert-True ($null -eq (Get-LabHeadCapacity -Config $cfg -Address '192.0.2.99')) 'an unlisted head gets no capacity (the Gateway keeps its own)'
    # SET-PARALLEL-PER-HEAD's choice (head-slots.json) is kept by a later hub run; memory still from the head list.
    $sf = Join-Path $Temp ('slots-' + [guid]::NewGuid().ToString('N').Substring(0, 6) + '.json')
    '{"schema": "agent-arena.head-slots/1", "slots": {"head-192-0-2-7": 6, "head-192-0-2-6": 3}}' | Set-Content -LiteralPath $sf -Encoding UTF8
    $c7 = Get-LabHeadCapacity -Config $cfg -Address '192.0.2.7' -SlotsFile $sf; Assert-Equal 6 $c7.slots 'chosen slots kept'; Assert-Equal 27.4 $c7.memory_gb
    Assert-Equal 1 (Get-LabHeadCapacity -Config $cfg -Address '192.0.2.3' -SlotsFile $sf).slots 'a head not in the file keeps the default'
    'not json' | Set-Content -LiteralPath $sf; Assert-Equal 1 (Get-LabHeadCapacity -Config $cfg -Address '192.0.2.7' -SlotsFile $sf).slots 'an unreadable file changes nothing'
    # The tools that set it ship with the engine; the pressure test tests one head per class by default.
    foreach ($n in 'SET-PARALLEL-PER-HEAD.cmd', 'Set-ParallelPerHead.ps1', 'set_parallel.py') { Assert-True (Test-Path -LiteralPath (Join-Path $root "engine\hub-tools\pressure-test\$n")) $n }
    Assert-Match "Ask 'Test heads, by number' '2,5,6,7'" ([IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\pressure-test\Start-PressureTest.ps1')))
    # The verified inventory carries it in the form Gateway 0.4.0's heads import reads (head_inventory.resources: head.capacity).
    $inv = New-LabHeadInventory -Probes @([pscustomobject]@{ Head = '192.0.2.8'; Open = $true; Version = '0.40.13'; ModelsRead = $true; Models = @('qwen3.5:4b'); CheckedAt = 't' }) -Config $cfg -From '192.0.2.254' -Computer 'DESKTOP-REDACT10'
    Assert-Equal 27.4 $inv.heads[0].capacity.memory_gb; Assert-Equal 1 $inv.heads[0].capacity.slots
    Assert-Equal '0.4.6' ([string]$cfg.gateway.expected_release) 'this stick carries Gateway 0.4.6 (unmeasured_model_limit since 0.4.5; policy-light reads, fleet, lab tools: 1.6.29)'
}
Test-Case 'Gateway update: passes only with the ledger kept, and lists the heads held at upgrade' {
    $ok = '{"ok":true,"verb":"update","from":"0.3.0","to":"0.4.0","backup":{"backup":"b1"},"ledger":{"before":"LEDGER-fa3e","after":"LEDGER-fa3e","kept":true},"held_at_upgrade":[{"resource_id":"head-192-0-2-3","failed_model":"gemma3:270m","failed_operation":"completion"},{"resource_id":"head-192-0-2-6","failed_model":"gemma3:270m","failed_operation":"completion"}]}' | ConvertFrom-Json
    $v = Get-LabGatewayUpdateVerdict -Json $ok
    Assert-True $v.Ok 'kept ledger passes'; Assert-Equal 2 $v.Held.Count; Assert-Match 'head-192-0-2-3 \(gemma3:270m completion\)' $v.Detail; Assert-Match 'ledger kept LEDGER-fa3e' $v.Detail
    $lost = '{"ok":true,"from":"0.3.0","to":"0.4.0","ledger":{"before":"LEDGER-a","after":"LEDGER-b","kept":false},"held_at_upgrade":[]}' | ConvertFrom-Json
    Assert-False (Get-LabGatewayUpdateVerdict -Json $lost).Ok 'a new ledger is a failure'
    Assert-False (Get-LabGatewayUpdateVerdict -Json $null).Ok 'no output is a failure'
    $flow = [IO.File]::ReadAllText((Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1'))
    $u = $flow.IndexOf("`$r = & `$deploy 'update'"); $fix = $flow.IndexOf('Repair-LabGatewayReleaseAcl -Run $Run; Write-LabLog (''  the update did not start')
    Assert-True ($u -gt 0 -and $fix -gt $u) 'a failed start of the new release is repaired (owner-only pip files) and the update runs once more'
    Assert-Match "Request-LabApproval -Prompt \(""Update the Inference Gateway" $flow 'the update is asked (UPDATE)'
    # DESKTOP-REDACT10 2026-11-12 14:00: the failed update left the service pointed at 0.4.0, so a retry that waited for "still on
    # the old release" never ran. It retries on any failure, and switches back when the new release still does not run.
    $seg = $flow.Substring($u, 700)
    Assert-Match '\$r = & \$deploy ''update''\s+if \(-not \$r\.Ok\) \{' $seg 'retry on any failed update'
    Assert-False ($seg -match 'Release -ne \$want\) \{') 'not only when the service still points at the old release'
    $sb = $flow.IndexOf('Invoke-LabGatewaySwitchBack -Python $py -PackageRoot $ctx.Root -Settings $ctx.Settings -To $have -From $want')
    Assert-True ($sb -gt $fix) 'a second failure switches back to the old release'
    Assert-Match "State -ne 'Running'\) \{" $flow.Substring($fix, $sb - $fix) 'only when the Gateway is not running'
    $m = [IO.File]::ReadAllText((Join-Path $root 'engine\modules\Lab.Gateway.psm1'))
    Assert-Match "inst\._switch\(to\)" $m; Assert-Match "_record\(action='update_failed_rolled_back', current=to, previous=frm\)" $m
}
Test-Case 'Gateway qualification: embedding models get the embedding test; a model no head can run fails only if a client has nothing left (hub 2026-11-12)' {
    foreach ($e in 'all-minilm:latest', 'nomic-embed-text:latest', 'granite-embedding:30m', 'embeddinggemma:latest', 'qwen3-embedding:0.6b', 'bge-m3:latest') { Assert-Equal 'embedding' (Get-LabQualifyPurpose -Model $e) $e }
    foreach ($c in 'gemma3:4b', 'qwen3:4b', 'smollm2:135m', 'hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M', 'granite4:3b', 'deepseek-r1:14b') { Assert-Equal 'completion' (Get-LabQualifyPurpose -Model $c) $c }
    $res = @([pscustomobject]@{ Resource = 'head-192-0-2-7'; Model = 'gemma3:4b'; Passed = $true }, [pscustomobject]@{ Resource = 'head-192-0-2-7'; Model = 'qwen3:4b'; Passed = $false },
             [pscustomobject]@{ Resource = 'head-192-0-2-3'; Model = 'all-minilm:latest'; Passed = $true })
    $cl = @([pscustomobject]@{ app_id = 'capstone'; models = @('gemma3:4b', 'qwen3:4b', 'all-minilm:latest') })
    $s = Get-LabQualifySummary -Results $res -Clients $cl
    Assert-Equal 'DONE' $s.Status 'a model the Gateway refuses on a head does not fail the run'; Assert-Equal 2 $s.Passed; Assert-Equal 1 $s.Failed
    $s2 = Get-LabQualifySummary -Results $res -Clients @($cl[0], [pscustomobject]@{ app_id = 'balanced'; models = @('qwen3:4b') })
    Assert-Equal 'FAILED' $s2.Status 'a client left with no usable model fails'; Assert-Match '^balanced \(qwen3:4b\)$' ($s2.Stranded -join '|')
    Assert-Equal 'PENDING' (Get-LabQualifySummary -Results @() -Clients $cl).Status
    $flow = [IO.File]::ReadAllText((Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1'))
    # 1.6.7: the pairs not usable now only, through qualify_pairs.py (the Gateway Models screen's run_head_tests: load
    # first, the right purpose, heads side by side); the plan is made first and shown in the approval.
    Assert-Match "hub-tools\\gateway-models\\qualify_pairs\.py" $flow
    Assert-True ($flow.IndexOf('--plan-only') -gt 0 -and $flow.IndexOf('--plan-only') -lt $flow.IndexOf("-Word 'QUALIFY'")) 'the plan comes before the approval'
    Assert-Match 'not usable now \(\{1\} usable now are kept\)' $flow
    Assert-Match "\(the Gateway''s ' \+ \`$q\.purpose \+ ' test: ' \+ \`$q\.why" $flow 'the line says the test and the reason'
    $qpy = [IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\gateway-models\qualify_pairs.py'))
    Assert-Match 'run_head_tests' $qpy; Assert-Match 'mode=ro' $qpy
    Assert-Match '\$sum = Get-LabQualifySummary -Results \$results -Clients \$clients' $flow
    # The pressure test (hub) and the head's parallel toggle ship with the engine; the head edit is one line, undone exactly.
    foreach ($n in 'hub-tools\pressure-test\PRESSURE-TEST.cmd', 'hub-tools\pressure-test\Start-PressureTest.ps1', 'hub-tools\pressure-test\pressure_test.py',
                   'head-tools\head-parallel\HEAD-PARALLEL.cmd', 'head-tools\head-parallel\Set-HeadOllamaParallel.ps1',
                   'hub-tools\gateway-concurrency-fix\FIX-GATEWAY-CONCURRENCY.cmd') { Assert-True (Test-Path -LiteralPath (Join-Path $root "engine\$n")) $n }
    . (Join-Path $root 'engine\head-tools\head-parallel\Set-HeadOllamaParallel.ps1') -Choice selftest
    $w = "@echo off`r`nset `"OLLAMA_HOST=192.0.2.7:11434`"`r`nset `"OLLAMA_MODELS=C:\m`"`r`n`"C:\Program Files\Ollama\ollama.exe`" serve`r`n"
    $on = Set-WrapperParallel $w 10
    Assert-Match 'OLLAMA_MODELS=C:\\m"\r\nset "OLLAMA_NUM_PARALLEL=10"\r\n"C:\\Program Files\\Ollama\\ollama.exe" serve' $on 'set just before serve'
    Assert-Equal $w (Set-WrapperParallel $on 0) 'off gives back the wrapper byte for byte'
    Assert-Equal $on (Set-WrapperParallel $on 10) 'on twice is the same'
    $threw = $false; try { Set-WrapperParallel "@echo off`r`necho hi`r`n" 4 | Out-Null } catch { $threw = $true }; Assert-True $threw 'a file that is not the provisioner wrapper is refused'
    Assert-Match 'Repair-HubGatewayConcurrency\.ps1" -Ask -SkipRetest' ([IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\gateway-concurrency-fix\FIX-GATEWAY-CONCURRENCY.cmd'))) 'the operator chooses the limits'
    $rc = [IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\gateway-concurrency-fix\Repair-HubGatewayConcurrency.ps1'))
    Assert-Match '1-1024 \(Enter = 1024\)' $rc; Assert-Match '1-10000 \(Enter = 10000\)' $rc
    Assert-Match '\[int\]\$n -gt 1024' $rc 'the Gateway''s own maximum'; Assert-Match '\[int\]\$Parallel -gt 64' ([IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-parallel\Set-HeadOllamaParallel.ps1')))
    # The hub fix of 2026-11-12 is shipped with the engine.
    foreach ($n in 'FIX-GATEWAY-QUALIFY.cmd', 'Repair-HubGatewayQualify.ps1', 'requalify_embedding.py') { Assert-True (Test-Path -LiteralPath (Join-Path $root "engine\hub-tools\gateway-qualify-fix\$n")) $n }
}
Test-Case 'FIX-LAB-HEAD counts the models in manifests-v2 too (head 7 after its restore, 2026-11-11: "holds 0 model(s)")' {
    # The real layout from the 2026-11-11 restore: blobs, an empty manifests folder and the models in manifests-v2.
    $st = Join-Path $env:TEMP ('lab-v2-' + [guid]::NewGuid().ToString('N')); $m = Join-Path $st 'manifests-v2\registry.ollama.ai\library'
    foreach ($n in 'qwen3.5', 'gemma3') { New-Item -ItemType Directory -Path (Join-Path $m $n) -Force | Out-Null; Set-Content -LiteralPath (Join-Path $m "$n\4b") 'x' }
    New-Item -ItemType Directory -Path (Join-Path $st 'manifests'), (Join-Path $st 'blobs') -Force | Out-Null
    try {
        $text = [IO.File]::ReadAllText((Join-Path $root 'engine\Fix-LabHead.ps1'))
        $line = ($text -split "`r?`n" | Where-Object { $_ -match '^\s*\$activeManifests = ' }) | Select-Object -First 1
        Assert-True ([bool]$line) 'the count line is there'
        $active = $st; $activeManifests = $null; Invoke-Expression $line
        Assert-Equal 2 $activeManifests 'both models in manifests-v2 are counted'
        Assert-Match "Test-Path -LiteralPath \(Join-Path \`$machineModels 'manifests-v2'\)" $text 'a store with only manifests-v2 is still the store in use'
    } finally { Remove-Item -LiteralPath $st -Recurse -Force -ErrorAction SilentlyContinue }
}
Test-Case 'FIX-LAB-HEAD repairs a lost lab cable by itself and updates Agent Arena keeping its credential (1.6.1)' {
    $text = [IO.File]::ReadAllText((Join-Path $root 'engine\Fix-LabHead.ps1'))
    # Head 5: known by its computer name when it has no lab address; the repair runs only then, after the memory snapshot.
    Assert-Match '\$cfg\.head_inventory \| Where-Object \{ \[string\]\$_\.computer -eq \$env:COMPUTERNAME \}' $text
    $snap = $text.IndexOf("Snapshot 'memory-before.txt'"); $net = $text.IndexOf('if (-not $lab) {')
    Assert-True ($snap -gt 0 -and $net -gt $snap) 'the network repair comes after the memory is recorded'
    foreach ($p in 'pnputil.exe /scan-devices', 'Enable-PnpDevice', 'pnputil.exe /restart-device', 'Enable-NetAdapter -Name', 'power-before.json', 'Set-NetAdapterAdvancedProperty -Name $nic.Name') { Assert-True ($text.Contains($p)) "the repair uses $p" }
    Assert-False ($text -match 'Set-NetAdapterAdvancedProperty[^\r\n]*-NoRestart') 'the old settings are applied with a driver restart this time'
    # The address is set only on one wired adapter with a link, and never when another computer already answers on it.
    $set = $text.IndexOf('New-NetIPAddress -InterfaceIndex'); $dup = $text.LastIndexOf('Test-Connection -ComputerName $target', $set); $many = $text.LastIndexOf('$up.Count -gt 1', $set)
    Assert-True ($set -gt 0 -and $dup -gt 0 -and $many -gt 0 -and $dup -lt $set -and $many -lt $set) 'duplicate and ambiguity checks come first'
    Assert-Match 'Update-LabViewerQuiet -Toolset \$tool' $text 'Agent Arena is updated'
    # The viewer toolset is found by version, and the update command keeps the credential (no --code) and logs.
    $d = Join-Path $Temp ('vt' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    foreach ($n in 'AGENT-ARENA-2.1.2', 'AGENT-ARENA-2.1.10', 'AGENT-ARENA-HUB', 'AGENT-ARENA-2.1.3') { New-Item -ItemType Directory -Path (Join-Path $d $n) -Force | Out-Null }
    Assert-Equal '2.1.10' (Get-LabViewerToolset -Payload $d).Version 'newest by version, not by name'
    Assert-True ($null -eq (Get-LabViewerToolset -Payload (Join-Path $d 'none'))) 'no toolset: none'
    $c = ConvertTo-LabViewerUpdateCmd -Exe 'C:\U\app\AgentArena.exe' -Log 'C:\U\update.log' -HubHost '192.0.2.254' -Version '2.1.3'
    Assert-Match '"C:\\U\\app\\AgentArena\.exe" --install-quiet >> "C:\\U\\update\.log" 2>&1' $c
    Assert-False ($c -match '--code') 'no enrollment code: the credential is kept'
    Assert-Match 'ping -n 1 -w 1000 192\.0\.2\.254 \| find "TTL="' $c 'waits for the hub'
    Assert-Match 'current\.txt' $c; Assert-Match '--background' $c; Assert-Match '(?m)^echo done ' $c
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-False ($sp -match "AGENT-ARENA-2\.1\.2'") 'the provisioner takes the viewer version from the stick'
    Assert-Match 'Update-LabViewerQuiet -Toolset \$tool' $sp
}
Test-Case 'a failed Research Lab run brings its cause back: head, error and worker.log, per head, onto the stick (1.5.4)' {
    Assert-Equal "PermissionError: Testing Lab requires the capstone-research client" (Get-LabLogFailureLine -Lines @('starting', '  File "x.py", line 3, in run', 'PermissionError: Testing Lab requires the capstone-research client', '')) 'the exception line'
    Assert-Equal 'last words' (Get-LabLogFailureLine -Lines @('a', 'last words')) 'else the last line'
    Assert-Equal '' (Get-LabLogFailureLine -Lines @()) 'empty'
    $d = Join-Path $Temp ('rl' + [guid]::NewGuid().ToString('N').Substring(0, 6)); $usb = Join-Path $d 'usb'; $runDir = Join-Path $d 'run'
    New-Item -ItemType Directory -Path $usb, $runDir -Force | Out-Null
    foreach ($h in 'head-192-0-2-3', 'head-192-0-2-4') {
        $rd = Join-Path $d ("data\data\experiments\EXP-$h\RUN-1"); New-Item -ItemType Directory -Path $rd -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $rd 'manifest.json'), (@{ gateway_head_id = $h; profile = @{ routes = @{ a = @{ model = 'qwen3.5:4b' } } } } | ConvertTo-Json -Depth 5))
        [IO.File]::WriteAllText((Join-Path $rd 'worker.log'), "Traceback (most recent call last):`r`n  File ""w.py"", line 1`r`nRuntimeError: Gateway 400: unsupported option on $h`r`n")
    }
    $cfg = [pscustomobject]@{ capstone = [pscustomobject]@{ data_root = (Join-Path $d 'data') } }
    $r = Save-LabResearchLabDiagnostics -Run ([pscustomobject]@{ Folder = $runDir; Id = 'RUNX' }) -Config $cfg -UsbRoot $usb -Since (Get-Date).AddMinutes(-5)
    Assert-Equal 2 $r.Experiments
    Assert-Equal 'RuntimeError: Gateway 400: unsupported option on head-192-0-2-3' $r.ByHead['head-192-0-2-3']
    Assert-True (Test-Path -LiteralPath (Join-Path $usb ('reports\' + $env:COMPUTERNAME + '\RUNX-research-lab-diagnostics.txt'))) 'copied to the stick under reports\<computer>'
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match 'Save-LabResearchLabDiagnostics -Run \$run -Config \$Config -UsbRoot \$UsbRoot -Since \$lhSince' $sp
    Assert-Match "if \(\`$Run\.Role -eq 'hub'\)" $sp; Assert-Match 'Save-LabHubDiagnostics -Run \$Run -UsbRoot \$UsbRoot' $sp
    # The hub collector runs anywhere (absent folders are named, not fatal) and writes its file.
    $h = Save-LabHubDiagnostics -Run ([pscustomobject]@{ Folder = $runDir; Id = 'RUNH' }) -UsbRoot $usb
    Assert-True (Test-Path -LiteralPath (Join-Path $runDir 'hub-diagnostics.txt')) 'hub file written'
    Assert-Match 'lines naming HTTP 400, a certificate' ([IO.File]::ReadAllText((Join-Path $runDir 'hub-diagnostics.txt')))
}
Test-Case 'a Python process left running from the Capstone folder (Stop-Capstone.ps1 leaves it) is stopped' {
    # Proven on the lab laptop: Stop-ScheduledTask ends the launcher, not its Python child. A long-running program
    # copied into a scratch "install folder" stands in for that child.
    $d = Join-Path $Temp ('cp' + [guid]::NewGuid().ToString('N').Substring(0, 6)); $rt = Join-Path $d 'runtime'; New-Item -ItemType Directory -Path $rt -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\PING.EXE') -Destination (Join-Path $rt 'stand-in.exe')
    $p = Start-Process -FilePath (Join-Path $rt 'stand-in.exe') -ArgumentList '-n', '120', '127.0.0.1' -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    Assert-False $p.HasExited 'the stand-in runs'
    $n = Stop-LabCapstoneProcesses -InstallRoot $d
    Start-Sleep -Milliseconds 500
    Assert-True ($n -ge 1) "found it ($n)"; Assert-True $p.HasExited 'stopped'
    Assert-Equal 0 (Stop-LabCapstoneProcesses -InstallRoot $d) 'nothing left'
    Assert-Equal 0 (Stop-LabCapstoneProcesses -InstallRoot (Join-Path $d 'missing')) 'no folder, nothing to do'
}
Test-Case 'lab-heads.json: schema agent-arena.lab-heads/1, heads 2-10 in order, head 2 GPU, QMS on 3, ERP on 4' {
    $t = New-LabCapstoneLabHeadsText -Config $labCfg -GeneratedAt '2026-11-08T12:00:00Z'
    $o = $t | ConvertFrom-Json
    Assert-Equal 'agent-arena.lab-heads/1' $o.schema; Assert-Equal 'https://192.0.2.254:8443' $o.gateway_url; Assert-Equal '2026-11-08T12:00:00Z' $o.generated_at
    Assert-Equal '2,3,4,5,6,7,8,9,10' (($o.heads | ForEach-Object { $_.number }) -join ',')
    $h2 = $o.heads[0]; Assert-Equal 'head-192-0-2-2' $h2.resource_id; Assert-Equal 'RTX 4060 Ti 16 GB' $h2.gpu; Assert-Equal 63.9 $h2.ram_gb; Assert-Equal 0 @($h2.services).Count
    Assert-Equal 'qms' (@($o.heads[1].services) -join ','); Assert-Equal 'erp' (@($o.heads[2].services) -join ',')
    Assert-True ($null -eq $o.heads[3].gpu) 'a CPU head has gpu null'
    Assert-Equal 'head-192-0-2-10' $o.heads[-1].resource_id; Assert-Equal '192.0.2.10' $o.heads[-1].address
    # The same shape as Capstone's own rc.5 fixture, when it is next to the repo.
    $fx = Join-Path $root '..\..\Capstone\deploy\windows\work\Capstone-6.0.0-rc.5\payload\application\tests\fixtures\lab-heads-2026-11-08.json'
    if (Test-Path -LiteralPath $fx) { $want = (Get-Content -LiteralPath $fx -Raw | ConvertFrom-Json); Assert-Equal ($want.heads | ConvertTo-Json -Depth 5 -Compress) ($o.heads | ConvertTo-Json -Depth 5 -Compress) 'identical to Capstone''s fixture' }
    # A head without inventory still appears, by address.
    $c2 = $labCfg.PSObject.Copy(); $c2 | Add-Member -NotePropertyName known_heads -NotePropertyValue @('192.0.2.11') -Force; $c2 | Add-Member -NotePropertyName head_inventory -NotePropertyValue @() -Force
    $o2 = (New-LabCapstoneLabHeadsText -Config $c2) | ConvertFrom-Json; Assert-Equal 'head-192-0-2-11' $o2.heads[0].resource_id; Assert-True ($null -eq $o2.heads[0].computer) 'no name'
}
Test-Case 'lab-heads.json is written without a byte-order mark, once, and left alone when the heads are the same' {
    $d = Join-Path $Temp ('lh' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path (Join-Path $d 'config') -Force | Out-Null
    $c2 = $labCfg.PSObject.Copy(); $c2 | Add-Member -NotePropertyName capstone -NotePropertyValue ([pscustomobject]@{ data_root = $d }) -Force
    $r = Set-LabCapstoneLabHeads -Config $c2; Assert-Equal 'DONE' $r.Outcome
    $b = [IO.File]::ReadAllBytes((Join-Path $d 'config\lab-heads.json')); Assert-True ($b[0] -eq 0x7B) 'starts with {, no BOM'
    Assert-Equal 'UNCHANGED' (Set-LabCapstoneLabHeads -Config $c2).Outcome
    $c3 = $labCfg.PSObject.Copy(); $c3 | Add-Member -NotePropertyName capstone -NotePropertyValue ([pscustomobject]@{ data_root = (Join-Path $d 'none') }) -Force
    Assert-Equal 'PENDING' (Set-LabCapstoneLabHeads -Config $c3).Outcome
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "Invoke-LabStep -Id 'capstone-lab-heads'" $sp
}
Test-Case 'Test-CapstoneLabHeads output reads as one line per head, with the reason when a head fails' {
    $out = "noise`r`n" + '{"resource_id":"head-192-0-2-2","model":"qwen3.5:4b","status":"completed","gateway_request_id":"r1","queue_ms":12,"duration_ms":41000,"arena_rows_written":14}' + "`r`n" +
           '{"resource_id":"head-192-0-2-10","model":null,"status":"failed","reason":"no usable chat model","arena_rows_written":0}' + "`r`n" +
           '{"resource_id":"head-192-0-2-7","model":"qwen3.5:4b","status":"completed","duration_ms":90000,"arena_rows_written":0}' + "`r`n" + '{"passed":false,"targets":3}'
    $r = ConvertFrom-LabLabHeadsOutput -Text $out
    Assert-True $r.Read; Assert-False $r.Passed; Assert-Equal 3 $r.Targets; Assert-Equal 3 $r.Heads.Count
    Assert-True $r.Heads[0].Ok; Assert-Equal 'head 2: qwen3.5:4b completed in 41 s, 14 Arena rows' $r.Heads[0].Text
    Assert-False $r.Heads[1].Ok; Assert-Equal 'head 10: (no model) failed - no usable chat model' $r.Heads[1].Text
    Assert-False $r.Heads[2].Ok 'completed but nothing reached the Arena is a failure'
    # rc.8 fields: no model call, or a call that did not reach the pinned head, is not a pass.
    $r8 = ConvertFrom-LabLabHeadsOutput -Text ('{"resource_id":"head-192-0-2-3","model":"qwen3.5:4b","status":"completed","model_calls":3,"pinned_head_observed":true,"duration_ms":50000,"arena_rows_written":9}' + "`r`n" +
           '{"resource_id":"head-192-0-2-4","model":"qwen3.5:4b","status":"completed","model_calls":0,"pinned_head_observed":false,"duration_ms":18000,"arena_rows_written":4}' + "`r`n" +
           '{"resource_id":"head-192-0-2-7","model":"qwen3.5:4b","status":"completed","model_calls":2,"pinned_head_observed":false,"duration_ms":40000,"arena_rows_written":5}' + "`r`n" + '{"passed":false,"targets":3}')
    Assert-True $r8.Heads[0].Ok 'rc.8: model calls on the pinned head'; Assert-False $r8.Heads[1].Ok 'rc.8: no model call'; Assert-False $r8.Heads[2].Ok 'rc.8: answered by another head'
    # 1.6.7: paused for a person's review after model calls on the pinned head = served; without proof = paused (PENDING).
    $ph = ConvertFrom-LabLabHeadsOutput -Text ('{"resource_id":"head-192-0-2-7","model":"qwen3.5:4b","status":"paused","error":null,"model_calls":10,"pinned_head_observed":true}' + "`r`n" +
           '{"resource_id":"head-192-0-2-2","model":"qwen3.5:4b","status":"paused","error":null}' + "`r`n" +
           '{"resource_id":"head-192-0-2-6","model":"qwen3.5:2b","status":"paused","error":null,"model_calls":0,"pinned_head_observed":false}' + "`r`n" +
           '{"resource_id":"head-192-0-2-5","model":"qwen3.5:4b","status":"failed","error":"read timeout"}' + "`r`n" + '{"passed":false,"targets":4}')
    Assert-True $ph.Heads[0].Held 'held after 10 calls'; Assert-Match 'served: 10 model call\(s\) on this head' $ph.Heads[0].Text
    Assert-True ($ph.Heads[1].Paused -and -not $ph.Heads[1].Held) 'paused, no proof'; Assert-Match 'does not say whether' $ph.Heads[1].Text
    Assert-False $ph.Heads[2].Held 'no model call is not served'; Assert-False ($ph.Heads[3].Paused -or $ph.Heads[3].Held) 'a failure stays a failure'
    Assert-Equal 1 $ph.Served; Assert-False $ph.AllServed
    Assert-True (ConvertFrom-LabLabHeadsOutput -Text ('{"resource_id":"head-192-0-2-7","model":"m","status":"paused","model_calls":3}' + "`r`n" + '{"passed":false,"targets":1}')).AllServed
    $p = ConvertFrom-LabLabHeadsOutput -Text '{"passed":true,"targets":0}'; Assert-True $p.Passed; Assert-Equal 0 $p.Heads.Count
    Assert-False (ConvertFrom-LabLabHeadsOutput -Text 'Invoke-RestMethod : 401').Read
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match 'Test-CapstoneLabHeads\.ps1' $sp; Assert-Match 'ConvertFrom-LabLabHeadsOutput -Text \$r\.Output' $sp
    # 2026-11-14: rc.13 found 0 heads and this read "0 of 0 served" as PASS. No head is a FAIL.
    Assert-Match '\[int\]\$lr\.Targets -gt 0 -and @\(\$lr\.Heads\)\.Count -gt 0' $sp 'zero heads never passes'
    $z = ConvertFrom-LabLabHeadsOutput -Text '{"passed":true,"targets":0}'; Assert-Equal 0 $z.Targets; Assert-Equal 0 @($z.Heads).Count
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Capstone 6.0.0-rc.4: QMS on head 3, ERP on head 4 (1.5.0)'
$labCfg = Get-LabConfig -Path (Join-Path $root 'config\lab-network.json')
Test-Case 'the configuration names Capstone rc.29 and the two approved (rc.4) service packages' {
    Assert-Equal '6.0.0-rc.29' $labCfg.capstone.approved_release.version
    Assert-Equal '1b2fe0b422e786a8fbc9b7d2582c12224cafe37814701a250f7cd461c05fb2c4' $labCfg.capstone.approved_release.sha256
    Assert-Equal '1.0.0-rc.4' $labCfg.business_services.qms.approved_release.version; Assert-Equal '1.0.0-rc.4' $labCfg.business_services.erp.approved_release.version
    $q = Get-LabServiceSpecByName -Config $labCfg -Name 'qms'; $e = Get-LabServiceSpecByName -Config $labCfg -Name 'erp'
    Assert-Equal 'CapstoneQMS' $q.Product; Assert-Equal '192.0.2.3' $q.Head; Assert-Equal 8611 $q.Port; Assert-Equal 'head-192-0-2-3' $q.Node; Assert-Equal '3' $q.HeadNumber
    Assert-Equal 'CapstoneERP' $e.Product; Assert-Equal '192.0.2.4' $e.Head; Assert-Equal 8612 $e.Port; Assert-Equal 'head-192-0-2-4' $e.Node
    foreach ($s in $q, $e) { Assert-Equal '192.0.2.201' $s.Client; Assert-Match '^[0-9a-f]{64}$' ([string]$s.Approved.sha256); Assert-Match '^C:\\ProgramData\\Capstone\\services\\' $s.LaptopToken }
    Assert-Equal 'CapstoneQMS' (Get-LabBusinessServiceSpec -Config $labCfg -HeadIp '192.0.2.3').Product
    Assert-Equal 'CapstoneERP' (Get-LabBusinessServiceSpec -Config $labCfg -HeadIp '192.0.2.4').Product
    Assert-True ($null -eq (Get-LabBusinessServiceSpec -Config $labCfg -HeadIp '192.0.2.5')) 'no service on head 5'
    Assert-True ($null -eq (Get-LabBusinessServiceSpec -Config $labCfg -HeadIp '')) 'no address, no service'
}
Test-Case 'a service package must be the one for this head: format, service, version, address, no secrets' {
    $q = Get-LabServiceSpecByName -Config $labCfg -Name 'qms'
    $m = [pscustomobject]@{ format = 'capstone-business-service-windows-release-v1'; service = 'qms'; version = '1.0.0-rc.4'; listen = '192.0.2.3:8611'; secrets_included = $false }
    Assert-True (Test-LabServiceManifest -Manifest $m -Spec $q).Ok
    $bad = $m.PSObject.Copy(); $bad.service = 'erp'; $bad.listen = '192.0.2.4:8612'
    $r = Test-LabServiceManifest -Manifest $bad -Spec $q; Assert-False $r.Ok; Assert-Match 'service erp' ($r.Problems -join ';'); Assert-Match 'listens on 192.0.2.4:8612' ($r.Problems -join ';')
    $bad = $m.PSObject.Copy(); $bad.secrets_included = $true; Assert-False (Test-LabServiceManifest -Manifest $bad -Spec $q).Ok
    Assert-False (Test-LabServiceManifest -Manifest $null -Spec $q).Ok
}
Test-Case 'the firewall rule must admit exactly the Capstone laptop, on the service port and the head address' {
    $q = Get-LabServiceSpecByName -Config $labCfg -Name 'qms'
    $ok = [pscustomobject]@{ Enabled = $true; Action = 'Allow'; Direction = 'Inbound'; Remote = @('192.0.2.201'); Local = @('192.0.2.3'); Port = '8611'; Protocol = 'TCP' }
    Assert-True (Test-LabServiceRule -Rule $ok -Spec $q).Ok
    foreach ($change in @(@{ Remote = @('Any') }, @{ Remote = @('192.0.2.201', '192.0.2.1') }, @{ Local = @('Any') }, @{ Port = '8612' }, @{ Enabled = $false }, @{ Action = 'Block' })) {
        $r = $ok.PSObject.Copy(); foreach ($k in $change.Keys) { $r.$k = $change[$k] }
        Assert-False (Test-LabServiceRule -Rule $r -Spec $q).Ok ("accepted " + ($change.Keys -join ','))
    }
    Assert-False (Test-LabServiceRule -Rule $null -Spec $q).Ok
}
Test-Case 'tokens: 64 random hex characters; the fingerprint is short, stable and not the token' {
    $a = New-LabServiceToken; $b = New-LabServiceToken
    Assert-Match '^[0-9a-f]{64}$' $a; Assert-True ($a -ne $b) 'two tokens differ'
    $f = Get-LabTokenFingerprint -Token $a
    Assert-Match '^[0-9a-f]{12}$' $f; Assert-Equal $f (Get-LabTokenFingerprint -Token ($a + "`r`n")); Assert-False ($a.Contains($f) -and $a.StartsWith($f)) 'not a prefix of the token'
}
Test-Case 'the token travels on the stick only in handoff, and the laptop deletes it there' {
    $h = Get-LabServiceHandoff -UsbRoot 'C:\stick' -Name 'qms'
    Assert-Equal 'C:\stick\handoff\qms.token' $h.Token; Assert-Equal 'C:\stick\handoff\qms-capstone-client.json' $h.Client
    $svc = Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Services.psm1') -Raw
    Assert-Match 'Remove-Item -LiteralPath \$ho\.Token, \$ho\.Client -Force' $svc
    Assert-Match 'could not be deleted from the stick' $svc
    Assert-True ($svc -notmatch 'Write-LabLog[^\r\n]*\$token') 'a token is never logged'
}
Test-Case 'the laptop compares its own QMS and ERP data with the service package (nothing to move when the same)' {
    $d = Join-Path $Temp ('sv' + [guid]::NewGuid().ToString('N').Substring(0, 6)); New-Item -ItemType Directory -Path (Join-Path $d 'pkg'), (Join-Path $d 'cs\data\erp') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $d 'cs\data\erp\orders.json'), '[1]')
    $h = (Get-FileHash -LiteralPath (Join-Path $d 'cs\data\erp\orders.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    $m = @{ format = 'capstone-business-service-windows-release-v1'; files = @(@{ path = 'payload/data/erp/orders.json'; sha256 = $h; size = 3 }) } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText((Join-Path $d 'pkg\package-manifest.json'), $m)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = Join-Path $d 'svc.zip'; [IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $d 'pkg'), $zip)
    $e = Get-LabServiceSpecByName -Config $labCfg -Name 'erp'
    $r = Compare-LabServiceData -Spec $e -Zip $zip -CapstoneData (Join-Path $d 'cs'); Assert-True $r.Same; Assert-Equal 1 $r.Files
    [IO.File]::WriteAllText((Join-Path $d 'cs\data\erp\orders.json'), '[2]')
    $r = Compare-LabServiceData -Spec $e -Zip $zip -CapstoneData (Join-Path $d 'cs'); Assert-False $r.Same; Assert-Equal 'orders.json' ($r.Different -join ',')
}
Test-Case 'Capstone''s end-to-end result reads as one line: each call, or the error' {
    $ok = '{"format":"capstone-end-to-end-v1","passed":true,"calls":[{"application_id":"capstone","operation":"/api/generate","model":"qwen3.5:4b","resource_id":"head-192-0-2-5","duration_ms":812.4},{"application_id":"capstone","operation":"/api/embed","model":"all-minilm:latest","resource_id":"head-192-0-2-3","duration_ms":40}]}'
    Assert-Equal 'passed: capstone /api/generate qwen3.5:4b on head-192-0-2-5 (812 ms); capstone /api/embed all-minilm:latest on head-192-0-2-3 (40 ms)' (ConvertTo-LabEndToEndSummary -Text ("noise`r`n" + $ok))
    $bad = '{"format":"capstone-end-to-end-v1","passed":false,"calls":[],"error_class":"PermissionError","error":"no embedding model is allowed for capstone in the Gateway; the administrator allows one in the admin hub"}'
    Assert-Match '^failed: PermissionError: no embedding model is allowed for capstone' (ConvertTo-LabEndToEndSummary -Text $bad)
    Assert-Match '^no result' (ConvertTo-LabEndToEndSummary -Text 'Traceback: boom')
}
Test-Case 'DESKTOP-REDACT11 (1.5.0): Capstone''s updater gets a 4-minute start wait and a stop before rollback; anything else is refused' {
    # The two lines of Capstone 6.0.0-rc.4's Update-Capstone.ps1 that failed on DESKTOP-REDACT11: health 5 s after the start, and a
    # rollback that "starts" a task that is still running.
    $orig = "try{`r`n Start-ScheduledTask -TaskName `$TaskName;Start-Sleep -Seconds 5`r`n & (Join-Path `$InstallRoot 'tools\Test-CapstoneHealth.ps1') -DataRoot `$DataRoot|Out-Null`r`n}catch{`r`n `$rollback=[ordered]@{version=`$old.version}|ConvertTo-Json`r`n Start-ScheduledTask -TaskName `$TaskName`r`n}"
    $p = ConvertTo-LabPatchedCapstoneUpdate -Text $orig
    Assert-True ($p -notmatch 'Start-Sleep -Seconds 5\r?\n') 'the 5-second wait is gone'
    Assert-Match 'AddSeconds\(240\)' $p; Assert-Match "Test-CapstoneHealth\.ps1'\) -DataRoot \`$DataRoot" $p
    Assert-Match "\}catch\{\r\n & \(Join-Path \`$InstallRoot 'tools\\Stop-Capstone\.ps1'\) -TaskName \`$TaskName;\`$labRoot=[^\r\n]+\r\n \`$rollback=" $p
    $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseInput($p, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count 'parses'
    $msg = ''; try { ConvertTo-LabPatchedCapstoneUpdate -Text 'Start-ScheduledTask -TaskName $TaskName' | Out-Null } catch { $msg = $_.Exception.Message }
    Assert-Match 'was not patched' $msg
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match 'Update-Capstone\.lab\.ps1' $sp; Assert-Match 'Test-LabCapstoneDatabase' $sp
    # The config's "_comment" is not a service (the report showed "_COMMENT on head").
    Assert-Match "Where-Object \{ -not \`$_\.StartsWith\('_'\) \}" $sp
    Assert-True ($null -eq (Get-LabBusinessServiceSpec -Config $labCfg -HeadIp '')) 'no service for no address'
}
Test-Case 'the provisioner wires it in: head menu 10 on heads 3 and 4 only, the laptop step, Configure gets the services' {
    $sp = Get-Content -LiteralPath (Join-Path $root 'engine\Start-LabProvisioner.ps1') -Raw
    Assert-Match "\`$menu\['10'\] = 'business-service'" $sp
    Assert-Match "Invoke-LabStep -Id 'business-clients'" $sp
    Assert-Match "'-QmsServiceConfig', \`$sw\.qms" $sp; Assert-Match "'-ErpServiceConfig', \`$sw\.erp" $sp
    Assert-Match 'Stop-Capstone\.ps1' $sp
    Assert-Match 'Test-CapstoneEndToEnd\.ps1' $sp
    Assert-True ([regex]::Matches($sp, "Invoke-LabVcRedistStep -Who").Count -ge 2) 'the Visual C++ runtime step serves Capstone and the services'
    Assert-Match 'Compare-LabTicketingProgram' $sp
    $plan = New-LabPlan -Role 'existing' -Inspection ([pscustomobject]@{ detected = [pscustomobject]@{ Role = 'head' }; network = [pscustomobject]@{ Lab = @([pscustomobject]@{ Address = '192.0.2.4'; Adapter = 'Ethernet' }) } }) -Config $labCfg -Choices @{ Actions = @('business-service') }
    $st = @($plan.steps | Where-Object { $_.id -eq 'business-service' }); Assert-Equal 1 $st.Count; Assert-Match 'CapstoneERP' $st[0].title; Assert-Match 'TCP 8612 from 192\.0\.2\.201 only' $st[0].title
}
Test-Case 'no icacls call grants folder permissions with /T (files inside would get an empty access list)' {
    $hits = @()
    foreach ($f in Get-ChildItem (Join-Path $root 'engine') -Recurse -Include *.ps1, *.psm1) {
        $n = 0; foreach ($line in Get-Content -LiteralPath $f.FullName) { $n++; if ($line -match 'icacls' -and $line -match '/grant' -and $line -match '(/T\b|\$deep)') { $hits += "$($f.Name):$n" } }
    }
    Assert-Equal 0 $hits.Count ($hits -join ', ')
}
Test-Case 'head 10: Windows App Control blocking the viewer is named, with the fix' {
    Assert-True (Test-LabAppControlMessage 'This command cannot be run due to the error: An Application Control policy has blocked this file.') 'head 10''s exact words'
    Assert-False (Test-LabAppControlMessage 'The system cannot find the file specified.')
    Assert-Equal 'on (blocks unsigned programs)' (ConvertTo-LabSmartAppControl 1); Assert-Equal 'off' (ConvertTo-LabSmartAppControl 0); Assert-Match 'evaluation' (ConvertTo-LabSmartAppControl 2); Assert-Equal 'unknown' (ConvertTo-LabSmartAppControl $null)
    $arena = Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Arena.psm1') -Raw
    Assert-Match 'Smart App Control settings > Off' $arena; Assert-Match 'Test-LabAppControlMessage \$_\.Exception\.Message' $arena
    Assert-Match "Add-LabFact security 'Smart App Control'" (Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Inspect.psm1') -Raw)
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Ollama 0.40.15 and verified parallel inference (1.6.14)'
Import-Module (Join-Path $root 'engine\head-tools\head-ollama\HeadOllama.Qualify.psm1') -Force -Global -DisableNameChecking
Test-Case 'the runtime is pinned (version, file, SHA-256, source), never "latest", and ships on the stick' {
    $rt = $Config.ollama.runtime
    Assert-Equal '0.40.15' ([string]$rt.version); Assert-Equal 'ollama-windows-amd64.zip' ([string]$rt.file)
    Assert-Equal '9ca74a7ab66ec86870b1eecac7df160df08dc4e3a291ab5d2d59bcf9310221c6' ([string]$rt.sha256)
    Assert-Equal 'https://github.com/ollama/ollama/releases/download/v0.40.15/ollama-windows-amd64.zip' ([string]$rt.source)
    Assert-False ((@($rt.version, $rt.file, $rt.source, $rt.release) -join ' ') -match '(?i)latest') 'no "latest" in the pin'
    Assert-Equal 'C:\ProgramData\AgentArenaLab\ollama\runtime' ([string]$rt.install_root)
    $b = [IO.File]::ReadAllText((Join-Path $root 'build\build_lab_release.py'))
    Assert-Match 'def check_ollama' $b; Assert-Match 'packages / "ollama" / path\.name' $b 'the build copies it to packages\ollama'
    Assert-Match 'out_dir / "HEAD-OLLAMA\.cmd"' $b 'one click from the USB root on a head'; Assert-Match 'out_dir / "APPLY-MODEL-CAPACITY\.cmd"' $b 'and on the hub'
    $cache = Join-Path (Split-Path -Parent $root) ('dist\_runtime-cache\ollama-0.40.15\' + $rt.file)
    if (Test-Path -LiteralPath $cache) { Assert-Equal ([string]$rt.sha256) (Get-FileHash -LiteralPath $cache -Algorithm SHA256).Hash.ToLowerInvariant() 'the downloaded archive is the pinned one' }
    $ss = $Config.ollama.server_settings
    Assert-Equal 8192 ([int]$ss.context_length); Assert-Equal '2,3,4' (@($ss.parallel_ladder) -join ','); Assert-Equal 16 ([int]$ss.max_queue); Assert-Equal 4096 ([int]$ss.reviewer_num_predict)
    Assert-Equal '' ([string]$ss.flash_attention) 'flash attention stays at the runtime default'; Assert-Equal '' ([string]$ss.kv_cache_type) 'KV cache stays at the runtime default'
}
Test-Case 'the wrapper: managed keys before serve, HOST and MODELS kept, output to the log; old wrapper text unchanged without settings' {
    $w = "@echo off`r`nrem x`r`nset `"OLLAMA_HOST=0.0.0.0:11434`"`r`nset `"OLLAMA_MODELS=C:\ProgramData\Ollama\models`"`r`nset `"OLLAMA_NUM_PARALLEL=10`"`r`n`"C:\Users\a\AppData\Local\Programs\Ollama\ollama.exe`" serve`r`n"
    $s = New-LabServerSettings -Spec $Config.ollama.server_settings -Parallel 3 -MaxLoaded 2
    $new = Set-LabWrapperContent -Text $w -Settings $s -Executable 'C:\ProgramData\AgentArenaLab\ollama\runtime\0.40.15\ollama.exe' -LogFile 'C:\ProgramData\AgentArenaLab\ollama\logs\ollama-server.log'
    $f = Get-LabWrapperFacts -Text $new
    Assert-Equal '0.0.0.0:11434' $f.Env['OLLAMA_HOST']; Assert-Equal 'C:\ProgramData\Ollama\models' $f.Env['OLLAMA_MODELS']
    Assert-Equal '3' $f.Env['OLLAMA_NUM_PARALLEL'] 'the old HEAD-PARALLEL value is replaced'; Assert-Equal '8192' $f.Env['OLLAMA_CONTEXT_LENGTH']; Assert-Equal '2' $f.Env['OLLAMA_MAX_LOADED_MODELS']
    Assert-Equal '16' $f.Env['OLLAMA_MAX_QUEUE']; Assert-Equal '30m' $f.Env['OLLAMA_KEEP_ALIVE']
    Assert-False ($f.Env.Contains('OLLAMA_FLASH_ATTENTION')) 'an empty setting is not written (runtime default)'
    Assert-Equal 'C:\ProgramData\AgentArenaLab\ollama\runtime\0.40.15\ollama.exe' $f.Executable; Assert-Equal 'C:\ProgramData\AgentArenaLab\ollama\logs\ollama-server.log' $f.LogFile
    Assert-Match 'set "OLLAMA_KEEP_ALIVE=30m"\r\nif not exist "C:\\ProgramData\\AgentArenaLab\\ollama\\logs" mkdir "C:\\ProgramData\\AgentArenaLab\\ollama\\logs"\r\n"C:\\ProgramData\\AgentArenaLab\\ollama\\runtime\\0\.40\.15\\ollama\.exe" serve >> "C:\\ProgramData\\AgentArenaLab\\ollama\\logs\\ollama-server\.log" 2>&1\r\n$' $new
    Assert-Equal $new (Set-LabWrapperContent -Text $new -Settings $s) 'applying twice is the same'
    $back = Set-LabWrapperContent -Text $new -Settings ([ordered]@{ OLLAMA_NUM_PARALLEL = '1' }) -LogFile ''
    Assert-Match '"\s+serve\r\n$' $back 'the log redirection can be taken out'; Assert-Equal '8192' (Get-LabWrapperFacts -Text $back).Env['OLLAMA_CONTEXT_LENGTH'] 'keys not named are kept'
    $threw = $false; try { Set-LabWrapperContent -Text "@echo off`r`necho hi`r`n" -Settings $s | Out-Null } catch { $threw = $true }; Assert-True $threw 'not the provisioner wrapper: refused'
    $threw = $false; try { Set-LabWrapperContent -Text $w -Settings @{ OLLAMA_HOST = '127.0.0.1' } | Out-Null } catch { $threw = $true }; Assert-True $threw 'OLLAMA_HOST is never managed here'
    $plain = New-LabOllamaWrapperText -Executable 'C:\o\ollama.exe' -Bind '0.0.0.0:11434' -ModelDirectory 'C:\m'
    Assert-Match '^@echo off\r\n(rem .*\r\n){3}set "OLLAMA_HOST=0\.0\.0\.0:11434"\r\nset "OLLAMA_MODELS=C:\\m"\r\n"C:\\o\\ollama\.exe" serve\r\n$' $plain 'without settings: the wrapper as before 1.6.14'
    $managed = New-LabOllamaWrapperText -Executable 'C:\o\ollama.exe' -Bind '0.0.0.0:11434' -ModelDirectory 'C:\m' -Settings $s -LogFile 'C:\l\s.log'
    Assert-Equal '3' (Get-LabWrapperFacts -Text $managed).Env['OLLAMA_NUM_PARALLEL'] 'reprovisioning re-applies the qualified settings'
    . (Join-Path $root 'engine\head-tools\head-parallel\Set-HeadOllamaParallel.ps1') -Choice selftest
    Assert-Match 'OLLAMA_NUM_PARALLEL=5"\r\n"C:' (Set-WrapperParallel $new 5) 'HEAD-PARALLEL still finds a serve line with the log redirection'
    Assert-Match 'managed by HEAD-OLLAMA' ([IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-parallel\Set-HeadOllamaParallel.ps1'))) 'HEAD-PARALLEL leaves a HEAD-OLLAMA head alone'
}
Test-Case 'the settings record survives reprovisioning; a rolled-back record is ignored' {
    $r = Join-Path $Temp ('oll-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); $p = Get-LabOllamaPaths -Root $r
    New-Item -ItemType Directory -Path $p.Dir -Force | Out-Null; $exe = Join-Path $r 'ollama.exe'; 'x' | Set-Content -LiteralPath $exe
    ([ordered]@{ schema = 'agent-arena.ollama-settings/1'; state = 'active'; executable = $exe; env = [ordered]@{ OLLAMA_NUM_PARALLEL = '3' }; log_file = 'C:\l.log' } | ConvertTo-Json) | Set-Content -LiteralPath $p.Settings
    Assert-Equal $exe (Get-LabServingOllamaExecutable -Root $r) 'the qualified runtime is what reprovisioning serves'
    Assert-Equal '3' (ConvertTo-LabSettingsMap (Read-LabOllamaSettings -Path $p.Settings).env)['OLLAMA_NUM_PARALLEL']
    ((Get-Content -LiteralPath $p.Settings -Raw) -replace '"active"', '"rolled_back"') | Set-Content -LiteralPath $p.Settings
    Assert-True ($null -eq (Read-LabOllamaSettings -Path $p.Settings)) 'rolled back: not applied again'
    "@echo off`r`n`"$exe`" serve`r`n" | Set-Content -LiteralPath $p.Wrapper
    Assert-Equal $exe (Get-LabServingOllamaExecutable -Root $r) 'otherwise the wrapper''s serve line'
    Assert-Match '\$active = Read-LabOllamaSettings' ([IO.File]::ReadAllText((Join-Path $root 'engine\modules\Lab.Head.psm1'))) 'Set-LabOllamaService reads the record'
    $sp = [IO.File]::ReadAllText((Join-Path $root 'engine\Start-LabProvisioner.ps1'))
    Assert-False ($sp -match 'Set-LabOllamaService -Run \$run -Executable \(Get-CapstoneOllamaExecutable\)') 'the boot task is not pointed back at the per-user install'
    Assert-Match 'Install-LabOllamaRuntime -Archive \$pinned' $sp 'a new head gets the pinned runtime from the stick'
    Assert-Equal 1 (Get-LabMaxLoadedModels -RamGb 7.4 -Rule $Config.ollama.server_settings) 'head 6 (8 GB): one resident model'
    Assert-Equal 2 (Get-LabMaxLoadedModels -RamGb 15.4 -Rule $Config.ollama.server_settings); Assert-Equal 2 (Get-LabMaxLoadedModels -RamGb 63.9 -Rule $Config.ollama.server_settings)
}
Test-Case 'a head with one qualified model still writes entries as a JSON array (1.6.22)' {
    $uh = [IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-ollama\Update-HeadOllama.ps1'))
    Assert-Match 'entries = @\(if \(\$q\) \{ \$q\.entries \}\)' $uh
    Assert-False ($uh -match 'entries = \$\(if \(\$q\)') 'the unrolling form is gone'
    $q = [ordered]@{ entries = @([ordered]@{ model = 'qwen3.5:4b' }) }
    Assert-Match '"entries":\[\{' ([ordered]@{ entries = @(if ($q) { $q.entries }) } | ConvertTo-Json -Compress) 'one entry: an array'
    Assert-Match '"entries":\[\]' ([ordered]@{ entries = @(if ($null) { 1 }) } | ConvertTo-Json -Compress) 'none: an empty array'
    Assert-Match 'inventory = @\(if \(\$i\) \{ \$i\.facts \}\)' ([IO.File]::ReadAllText((Join-Path $root 'engine\modules\Lab.Report.psm1'))) 'the install report has the same fix'
}
Test-Case 'the publication ACL check runs a copy that skips files SQLite removed meanwhile (1.6.21)' {
    $src = Join-Path $Temp ('acl-' + [guid]::NewGuid().ToString('N').Substring(0, 6) + '.ps1')
    "`$x = 1`r`n    foreach (`$item in @()) {`r`n        foreach (`$rule in (Get-Acl -LiteralPath `$item.FullName).Access) {`r`n        }`r`n    }`r`n" | Set-Content -LiteralPath $src
    $out = Get-LabFixedPublicationAclScript -Source $src -Folder $Temp
    Assert-True ($out -ne $src) 'a 0.4.4 script gets the fixed copy'
    $t = [IO.File]::ReadAllText($out)
    Assert-Match 'catch \{ if \(Test-Path -LiteralPath \$item\.FullName\) \{ throw \} else \{ continue \} \}' $t 'only a file that no longer exists is skipped'
    Assert-False ($t.Contains('(Get-Acl -LiteralPath $item.FullName).Access')) 'the failing line is gone'
    Assert-Equal $out (Get-LabFixedPublicationAclScript -Source $out -Folder $Temp) 'an already fixed script runs as it is'
    Assert-Match 'Get-LabFixedPublicationAclScript -Source \$script' ([IO.File]::ReadAllText((Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1'))) 'the hub flow uses it'
}
Test-Case 'each head gets its machine class: GPU, 32 GB, 16 GB, 8 GB (1.6.19, decided with the operator 2026-11-17)' {
    $mc = $Config.ollama.server_settings.machine_classes
    $g = Get-LabMachineClass -RamGb 63.9 -VramGb 16 -Classes $mc
    Assert-Equal 'GPU' $g.name; Assert-Equal 3 ([int]$g.max_loaded_models); Assert-Equal 10 ([int]$g.gateway_slots); Assert-Equal 16 ([double]$g.gateway_memory_gb)
    Assert-Equal '32 GB' (Get-LabMachineClass -RamGb 63.9 -VramGb $null -Classes $mc).name 'no NVIDIA card found: by RAM'
    $c32 = Get-LabMachineClass -RamGb 31.4 -Classes $mc
    Assert-Equal '32 GB' $c32.name; Assert-Equal '2,3,4' (@($c32.parallel_ladder) -join ','); Assert-Equal 3 ([int]$c32.max_loaded_models); Assert-Equal 'qwen3.5:4b' $c32.worker_model; Assert-Equal 23 ([double]$c32.gateway_memory_gb)
    $c16 = Get-LabMachineClass -RamGb 15.4 -Classes $mc
    Assert-Equal '16 GB' $c16.name; Assert-Equal 2 ([int]$c16.max_loaded_models); Assert-Equal 'qwen3.5:4b' (@($c16.qualify_models) -join ',') 'no reviewer on a 16 GB head'; Assert-Equal 8.5 ([double]$c16.gateway_memory_gb)
    $c8 = Get-LabMachineClass -RamGb 7.4 -Classes $mc
    Assert-Equal '8 GB' $c8.name; Assert-Equal 'qwen3.5:2b' $c8.worker_model; Assert-Equal '2' (@($c8.parallel_ladder) -join ','); Assert-Equal 4 ([int]$c8.gateway_slots)
    $uh = [IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-ollama\Update-HeadOllama.ps1'))
    Assert-Match 'if \(\$worker -and \$adopt\.ContainsKey\(\$worker\)\) \{ \$adopt\[\$worker\] \}' $uh 'the worker sets OLLAMA_NUM_PARALLEL, not the lowest model'
    Assert-False ($uh -match '\$ok = \(-not \$bm\.failed -and -not \$bm\.invalid') 'a rejected answer does not unload models in the mixed test'
    Assert-Match 'machine_class = ' $uh 'the report carries the class for APPLY-MODEL-CAPACITY'
    Assert-Equal 4096 ([int]$Config.ollama.server_settings.reviewer_num_predict) 'the thinking reviewer has room to finish'
    $sf = Join-Path $Temp ('slots-' + [guid]::NewGuid().ToString('N').Substring(0, 6) + '.json')
    '{"schema":"agent-arena.head-slots/1","slots":{"head-192-0-2-8":10},"memory_gb":{"head-192-0-2-8":23.0}}' | Set-Content -LiteralPath $sf
    $hc = Get-LabHeadCapacity -Config $Config -Address '192.0.2.8' -SlotsFile $sf
    if ($hc) { Assert-Equal 23 ([double]$hc.memory_gb) 'the class memory_gb survives later hub runs'; Assert-Equal 10 ([int]$hc.slots) }
}
Test-Case 'the server''s own log proves the effective settings and the backend' {
    $log = @('time=2026-11-17T08:00:00 level=INFO source=routes.go msg="server config" env="map[CUDA_VISIBLE_DEVICES: GPU_DEVICE_ORDINAL: HTTPS_PROXY: OLLAMA_CONTEXT_LENGTH:8192 OLLAMA_DEBUG:INFO OLLAMA_FLASH_ATTENTION:false OLLAMA_HOST:http://0.0.0.0:11434 OLLAMA_KEEP_ALIVE:30m0s OLLAMA_KV_CACHE_TYPE: OLLAMA_MAX_LOADED_MODELS:2 OLLAMA_MAX_QUEUE:16 OLLAMA_MODELS:C:\\ProgramData\\Ollama\\models OLLAMA_NUM_PARALLEL:3 OLLAMA_ORIGINS:[http://localhost https://localhost] ROCR_VISIBLE_DEVICES:]"',
             'time=2026-11-17T08:00:00 level=INFO source=routes.go msg="Listening on [::]:11434 (version 0.40.15)"',
             'time=2026-11-17T08:00:01 level=INFO source=types.go msg="inference compute" id=cpu library=cpu compute="" name=cpu') -join "`n"
    $c = Get-LabServerConfigFromLog -Text $log
    Assert-Equal '3' $c.Env['OLLAMA_NUM_PARALLEL']; Assert-Equal '8192' $c.Env['OLLAMA_CONTEXT_LENGTH']; Assert-Equal '' $c.Env['OLLAMA_KV_CACHE_TYPE']; Assert-Equal 'false' $c.Env['OLLAMA_FLASH_ATTENTION']
    Assert-Equal 'cpu' $c.Backend; Assert-Equal '0.40.15' $c.Version
    $s = New-LabServerSettings -Spec $Config.ollama.server_settings -Parallel 3 -MaxLoaded 2
    Assert-Equal 0 @(Test-LabServerSettingsApplied -Wanted $s -Seen $c.Env).Count 'all applied (30m = 30m0s)'
    $s4 = New-LabServerSettings -Spec $Config.ollama.server_settings -Parallel 4 -MaxLoaded 2
    Assert-Match 'OLLAMA_NUM_PARALLEL=3 \(wanted 4\)' (@(Test-LabServerSettingsApplied -Wanted $s4 -Seen $c.Env) -join ';') 'a setting that did not reach the process is caught'
    Assert-True ($null -eq (Get-LabServerConfigFromLog -Text 'nothing here')) 'no config line: not proven'
    Assert-True (Test-LabRuntimeVersion '0.40.15' '0.40.15'); Assert-True (Test-LabRuntimeVersion '0.41.0' '0.40.15'); Assert-False (Test-LabRuntimeVersion '0.40.13' '0.40.15')
}
Test-Case 'overlap is counted from compute windows, not from simultaneous HTTP requests' {
    $r = { param($end, $pe, $ev) [pscustomobject]@{ Ok = $true; Valid = $true; Sent = 0; Received = 30
             Reply = [pscustomobject]@{ created_at = $end; prompt_eval_duration = $pe; eval_duration = $ev; eval_count = 100; total_duration = 5e9; load_duration = 1e6 } } }
    $w = Get-LabComputeWindow -Reply (& $r '2026-11-17T08:00:10.123456789Z' 2e9 3e9).Reply
    Assert-Equal 5 ([math]::Round($w.End - $w.Start, 3)) 'created_at - (prompt_eval + eval), nanoseconds'
    # Four requests sent at the same instant that the server ran one after another: overlap 1 (a queue, not parallel).
    $queued = @((& $r '2026-11-17T08:00:05Z' 1e9 4e9), (& $r '2026-11-17T08:00:10Z' 1e9 4e9), (& $r '2026-11-17T08:00:15Z' 1e9 4e9), (& $r '2026-11-17T08:00:20Z' 1e9 4e9))
    $m = Get-LabBatchMetrics -Results $queued
    Assert-Equal 1 $m.measured_overlap 'simultaneous HTTP requests are not proof'
    Assert-False (Test-LabLevelPassed -Level 4 -Metrics $m -Isolated $true -MinFreeGb 5 -Floor 1).Passed 'level 4 fails on a queue'
    $par = @((& $r '2026-11-17T08:00:08Z' 1e9 6e9), (& $r '2026-11-17T08:00:08.5Z' 1e9 6e9), (& $r '2026-11-17T08:00:09Z' 1e9 6e9))
    $pm = Get-LabBatchMetrics -Results $par
    Assert-Equal 3 $pm.measured_overlap; Assert-True (Test-LabLevelPassed -Level 3 -Metrics $pm -Isolated $true -MinFreeGb 5 -Floor 1).Passed
    Assert-Match 'free memory' ((Test-LabLevelPassed -Level 3 -Metrics $pm -Isolated $true -MinFreeGb 0.6 -Floor 1).Reasons -join ';') 'the memory floor fails a level'
    Assert-Equal 1 (Get-LabMaxOverlap -Windows @([pscustomobject]@{ Start = 0; End = 5 }, [pscustomobject]@{ Start = 5; End = 9 })) 'touching windows do not overlap'
    Assert-Equal 2 (Select-LabParallelLevel -Levels @([pscustomobject]@{ Level = 2; Passed = $true; TokensPerS = 15 }, [pscustomobject]@{ Level = 3; Passed = $true; TokensPerS = 15.5 }, [pscustomobject]@{ Level = 4; Passed = $true; TokensPerS = 30 }) -SerialTokensPerS 10 -Gain 0.1) 'stops where a level adds less than 10 %'
    Assert-Equal 1 (Select-LabParallelLevel -Levels @([pscustomobject]@{ Level = 2; Passed = $false; TokensPerS = 30 }) -SerialTokensPerS 10) 'a failed first level keeps 1'
    Assert-Equal 4 (Select-LabParallelLevel -Levels @([pscustomobject]@{ Level = 2; Passed = $true; TokensPerS = 14 }, [pscustomobject]@{ Level = 3; Passed = $true; TokensPerS = 18 }, [pscustomobject]@{ Level = 4; Passed = $true; TokensPerS = 21 }) -SerialTokensPerS 10)
}
Test-Case 'conversations: unique markers, num_ctx 8192, reviewer 1024 tokens; answers judged, isolation checked, compared with serial' {
    $w = @(1..4 | ForEach-Object { New-LabQualifyConversation -Model 'qwen3.5:4b' -Index $_ -RunTag 'AB12' })
    $v = @(1..4 | ForEach-Object { New-LabQualifyConversation -Model 'qwen3:4b' -Index $_ -RunTag 'AB12' -ReviewerModel 'qwen3:4b' })
    Assert-Equal 4 @($w.Marker | Select-Object -Unique).Count; Assert-Equal 'structured,tool,structured,tool' ($w.Kind -join ','); Assert-Equal 'review,structured,review,structured' ($v.Kind -join ',')
    foreach ($c in $w + $v) { Assert-Equal 8192 $c.Body.options.num_ctx "$($c.Marker) num_ctx"; Assert-False $c.Body.think 'thinking off: bounded, comparable output' }
    Assert-Equal 1024 $v[0].Body.options.num_predict 'the reviewer output budget'; Assert-Equal 256 $w[0].Body.options.num_predict
    Assert-True ([bool]$w[0].Body.format) 'structured output by JSON schema'; Assert-Equal 'lookup_order' $w[1].Body.tools[0].function.name
    $ok = [pscustomobject]@{ message = [pscustomobject]@{ content = ('{"ticket_code":"' + $w[0].Marker + '","category":"billing","priority":"high","summary":"Refund one charge."}') } }
    $a = Test-LabQualifyAnswer -Conversation $w[0] -Reply $ok; Assert-True $a.Valid $a.Why; Assert-Equal 'billing/high' $a.Key
    $wrong = [pscustomobject]@{ message = [pscustomobject]@{ content = ('{"ticket_code":"' + $w[2].Marker + '","category":"billing","priority":"high","summary":"x"}') } }
    Assert-False (Test-LabQualifyAnswer -Conversation $w[0] -Reply $wrong).Valid 'another conversation''s ticket code is invalid'
    $tool = [pscustomobject]@{ message = [pscustomobject]@{ content = ''; tool_calls = @([pscustomobject]@{ function = [pscustomobject]@{ name = 'lookup_order'; arguments = [pscustomobject]@{ order_id = $w[1].Order; ticket_code = $w[1].Marker } } }) } }
    $t = Test-LabQualifyAnswer -Conversation $w[1] -Reply $tool; Assert-True $t.Valid $t.Why; Assert-Match ([regex]::Escape($w[1].Marker)) $t.Text 'the tool arguments are checked for isolation'
    $rev = [pscustomobject]@{ message = [pscustomobject]@{ content = "Review $($v[0].Marker)`nFindings: ...`nRisks: ...`nVerdict: REVISE" } }
    $rv = Test-LabQualifyAnswer -Conversation $v[0] -Reply $rev; Assert-True $rv.Valid $rv.Why; Assert-Equal 'REVISE' $rv.Key
    Assert-False (Test-LabQualifyAnswer -Conversation $v[0] -Reply $null).Valid 'no reply'
    $iso = Test-LabConversationIsolation -Results @([pscustomobject]@{ Marker = 'TKT-A-01'; Text = 'TKT-A-01 ok' }, [pscustomobject]@{ Marker = 'TKT-A-02'; Text = 'TKT-A-02 and TKT-A-01' })
    Assert-False $iso.Passed; Assert-Match 'TKT-A-02: carries TKT-A-01' ($iso.Failures -join ';')
    Assert-Equal 0.5 (Get-LabAgreement -Results @([pscustomobject]@{ Marker = 'a'; Key = 'x' }, [pscustomobject]@{ Marker = 'b'; Key = 'y' }) -Serial @([pscustomobject]@{ Marker = 'a'; Key = 'x'; Valid = $true }, [pscustomobject]@{ Marker = 'b'; Key = 'z'; Valid = $true }))
    Assert-Match 'DefaultConnectionLimit' ([IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-ollama\HeadOllama.Qualify.psm1'))) '.NET Framework''s 2 connections per host would queue requests on the client'
}
Test-Case 'HEAD-OLLAMA: one click, from the stick, proves the running server, keeps rollback, deletes nothing, downloads nothing' {
    foreach ($n in 'HEAD-OLLAMA.cmd', 'Update-HeadOllama.ps1', 'HeadOllama.Qualify.psm1') { Assert-True (Test-Path -LiteralPath (Join-Path $root "engine\head-tools\head-ollama\$n")) $n }
    $u = [IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-ollama\Update-HeadOllama.ps1'))
    $all = $u + [IO.File]::ReadAllText((Join-Path $root 'engine\modules\Lab.OllamaRuntime.psm1')) + [IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-ollama\HeadOllama.Qualify.psm1'))
    Assert-False ($all -match 'Remove-Item|\bdel\b|rmdir|Clear-Content') 'deletes nothing'
    Assert-False ($all -match 'Invoke-WebRequest|DownloadFile|Start-BitsTransfer|/api/pull|ollama\.com/download') 'downloads nothing, pulls no model'
    Assert-Match "Get-Api '/api/version'" $u 'the running server''s version, after each restart'; Assert-Match 'ListenerPath -ieq \$Exe' $u 'and the program that listens'
    Assert-Match 'Find-LabOllamaArchive' $u; Assert-Match 'rollback-\$stamp\.json' $u; Assert-Match 'ollama-serve\.before-1\.6\.14-\$stamp\.cmd' $u
    Assert-Match 'ollama-qualify-\$stamp\.json' $u; Assert-Match 'firewall rules unchanged' $u; Assert-Match 'models unchanged' $u
    Assert-Match 'no internet is needed' $u 'the screen says the update needs no internet'
    foreach ($k in 'resource_id', 'address', 'model', 'digest', 'runtime_version', 'backend', 'num_ctx', 'num_parallel_setting', 'measured_parallel', 'max_loaded_models', 'max_queue', 'keep_alive', 'flash_attention', 'kv_cache_type', 'memory', 'throughput', 'latency', 'isolation_passed', 'passed', 'evidence') {
        Assert-Match ("(?m)(^\s+|; )$k = ") $u "entry field $k" }
    $cmd = [IO.File]::ReadAllText((Join-Path $root 'engine\head-tools\head-ollama\HEAD-OLLAMA.cmd'))
    Assert-Match 'Update-HeadOllama\.ps1' $cmd; Assert-False ($cmd -match '(?i)\b[D-Z]:\\') 'no drive letter'
    $c = [IO.File]::ReadAllText((Join-Path $root 'engine\Collect-LabEvidence.ps1'))
    Assert-Match 'the running Ollama server is the pinned' $c; Assert-Match "Copy-Evidence -Path \(Join-Path \`$od 'logs'\)" $c
}
Test-Case 'APPLY-MODEL-CAPACITY (hub) ships with the engine and applies through the Gateway''s bootstrap' {
    foreach ($n in 'APPLY-MODEL-CAPACITY.cmd', 'Apply-ModelCapacity.ps1', 'model_capacity.py') { Assert-True (Test-Path -LiteralPath (Join-Path $root "engine\hub-tools\model-capacity\$n")) $n }
    $py = [IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\model-capacity\model_capacity.py'))
    Assert-Match 'agent-arena\.model-capacity/1' $py; Assert-Match 'hub\.bootstrap\(planned' $py; Assert-Match 'GATEWAY_MIN = \(0, 4, 4\)' $py
    Assert-Match 'model-capacity\.json' ([IO.File]::ReadAllText((Join-Path $root 'engine\hub-tools\model-capacity\Apply-ModelCapacity.ps1')))
}
Start-TestGroup 'static checks'
Test-Case 'no native program in the engine is given an argument with a double quote (Windows PowerShell 5.1 strips it)' {
    $hits = @()
    foreach ($f in Get-ChildItem (Join-Path $root 'engine') -Recurse -Include *.ps1, *.psm1 | Where-Object { $_.FullName -notmatch '\\head-engine\\' }) {
        $ast = [Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$null)
        foreach ($c in $ast.FindAll({ param($a) $a -is [Management.Automation.Language.CommandAst] }, $true)) {
            $n = [string]$c.GetCommandName()
            if (-not ($c.InvocationOperator -in 'Ampersand', 'Dot' -or $n -match '\.(exe|cmd)$' -or $n -in 'netsh', 'icacls', 'w32tm', 'winget', 'py', 'python', 'powercfg', 'schtasks', 'cmd', 'reg', 'sc')) { continue }
            foreach ($e in @($c.CommandElements | Select-Object -Skip 1)) { if (($e -is [Management.Automation.Language.StringConstantExpressionAst] -or $e -is [Management.Automation.Language.ExpandableStringExpressionAst]) -and $e.Value -match '"') { $hits += ('{0}:{1}' -f $f.Name, $e.Extent.StartLineNumber) } }
        }
    }
    Assert-Equal 0 $hits.Count ($hits -join ', ')
}
# ---------------------------------------------------------------------------
Start-TestGroup 'Coder laptop (192.0.2.202): its own Gateway files, Remote Desktop over Wi-Fi only, Ethernet and Wi-Fi apart'
$coderLayout = Get-LabCoderLayout -Config $Config
Test-Case 'the client configuration is byte for byte Coder''s own template' {
    # Coder's own project sits beside AgentArena on the development laptop; on the stick there is nothing above the drive root.
    $up = Split-Path -Parent (Split-Path -Parent $root); $tpl = $(if ($up) { Join-Path $up 'Coder\config\gateway-client.template.json' } else { '' })
    $text = New-LabCoderClientConfigText -Layout $coderLayout
    if ($tpl -and (Test-Path -LiteralPath $tpl)) { Assert-Equal ([IO.File]::ReadAllText($tpl)) $text 'Coder''s config/gateway-client.template.json' }
    $j = $text | ConvertFrom-Json
    Assert-Equal 'configuration_version,contract,service_id,url,application_id,token_file,ca_file' ((@($j.PSObject.Properties.Name)) -join ',') 'exactly the seven file-based fields'
    Assert-Equal 'https://192.0.2.254:8443|shared-inference|coder|shared.inference/1' ('{0}|{1}|{2}|{3}' -f $j.url, $j.service_id, $j.application_id, $j.contract)
    Assert-False ($text -match 'token_environment|\r') 'no environment-token fallback; LF only'
}
Test-Case 'the CA pin is the hub''s own CA (its 2026-11-06 install report)' {
    $rep = Join-Path $here 'fixtures\device1-20261106\DESKTOP-REDACT10-20261106-082448-DESKTOP-REDACT10-hub.txt'
    Assert-Match '^[0-9a-f]{64}$' $coderLayout.CaSha256
    if (Test-Path -LiteralPath $rep) { Assert-True ((Get-Content -LiteralPath $rep -Raw) -match ('public CA copied to .+ \(sha256 ' + $coderLayout.CaSha256.Substring(0, 12) + '\)')) 'the hub''s report records the CA it copied to the stick (first 12 digits)' }
}
Test-Case 'Remote Desktop only from the control workstation''s Wi-Fi address' {
    Assert-True (Test-LabCoderRdpSource -Source '198.51.100.149' -WifiAddress '198.51.100.37' -WifiPrefix 24 -LabPrefix $Config.prefix).Ok
    foreach ($bad in @(@('192.0.2.1', 'on the lab cable'), @('203.0.113.5', 'not on this laptop'), @('198.51.100.37', 'own Wi-Fi'), @('198.51.100.300', 'not an IPv4'), @('::1', 'not an IPv4'), @('198.51.100.0/24', 'not an IPv4'))) {
        $r = Test-LabCoderRdpSource -Source $bad[0] -WifiAddress '198.51.100.37' -WifiPrefix 24 -LabPrefix $Config.prefix
        Assert-False $r.Ok $bad[0]; Assert-Match $bad[1] $r.Reason
    }
    Assert-True (Test-LabCoderRdpSource -Source '172.16.9.200' -WifiAddress '172.16.0.10' -WifiPrefix 20 -LabPrefix $Config.prefix).Ok '/20 network'
    Assert-False (Test-LabCoderRdpSource -Source '172.16.16.1' -WifiAddress '172.16.0.10' -WifiPrefix 20 -LabPrefix $Config.prefix).Ok 'outside the /20'
    Assert-Match 'no Wi-Fi address' (Test-LabCoderRdpSource -Source '198.51.100.149' -WifiAddress '' -WifiPrefix 0 -LabPrefix $Config.prefix).Reason
}
Test-Case 'Ethernet and Wi-Fi apart: forwarding, sharing, a bridge, a gateway or DNS on the lab adapter are findings' {
    Assert-Equal 0 @(Get-LabCoderSeparationFindings -Forwarding @([pscustomobject]@{ InterfaceAlias = 'Ethernet'; AddressFamily = 'IPv4'; Forwarding = 'Disabled' }) -LabAdapter ([pscustomobject]@{ Name = 'Ethernet'; DefaultGateways = @(); DnsServers = @() })).Count
    $f = @(Get-LabCoderSeparationFindings -Forwarding @([pscustomobject]@{ InterfaceAlias = 'Wi-Fi'; AddressFamily = 'IPv4'; Forwarding = 'Enabled' }) -Adapters @([pscustomobject]@{ Name = 'Bridge'; Description = 'Microsoft Network Adapter Multiplexor / MAC Bridge Miniport' }) -Sharing @([pscustomobject]@{ Name = 'Wi-Fi'; Enabled = $true }) -LabAdapter ([pscustomobject]@{ Name = 'Ethernet'; DefaultGateways = @('192.0.2.254'); DnsServers = @('192.0.2.254') }))
    Assert-Equal 5 $f.Count ($f -join ' | ')
    Assert-Match 'not one' ($f -join ' ') 'the Gateway is not a router'
}
Test-Case 'Coder''s files: SYSTEM and Administrators full, the runtime account read only, nobody else' {
    $rt = 'S-1-5-21-1-2-3-1001'
    $good = @([pscustomobject]@{ Sid = 'S-1-5-18'; Rights = 'FullControl'; Type = 'Allow' }, [pscustomobject]@{ Sid = 'S-1-5-32-544'; Rights = 'FullControl'; Type = 'Allow' }, [pscustomobject]@{ Sid = $rt; Rights = 'ReadAndExecute, Synchronize'; Type = 'Allow' })
    Assert-Equal 0 @(Test-LabCoderAclEntries -Entries $good -Protected $true -RuntimeSid $rt).Count
    Assert-Equal 0 @(Test-LabCoderAclEntries -Entries $good -Protected $false -RuntimeSid $rt -AllowInherited).Count 'a file inherits from the protected folder'
    Assert-Match 'inheritance' (@(Test-LabCoderAclEntries -Entries $good -Protected $false -RuntimeSid $rt) -join ' ')
    Assert-Match 'Users has access' (@(Test-LabCoderAclEntries -Entries ($good + [pscustomobject]@{ Sid = 'S-1-5-32-545'; Rights = 'ReadAndExecute'; Type = 'Allow' }) -Protected $true -RuntimeSid $rt) -join ' ')
    Assert-Match 'Everyone' (@(Test-LabCoderAclEntries -Entries ($good + [pscustomobject]@{ Sid = 'S-1-1-0'; Rights = 'Read'; Type = 'Allow' }) -Protected $true -RuntimeSid $rt) -join ' ')
    Assert-Match 'may change' (@(Test-LabCoderAclEntries -Entries @($good[0], $good[1], [pscustomobject]@{ Sid = $rt; Rights = 'Modify, Synchronize'; Type = 'Allow' }) -Protected $true -RuntimeSid $rt) -join ' ')
    Assert-Match 'cannot read' (@(Test-LabCoderAclEntries -Entries @($good[0], $good[1]) -Protected $true -RuntimeSid $rt) -join ' ')
    Assert-Match 'S-1-5-21-9-9-9-1002 has access' (@(Test-LabCoderAclEntries -Entries ($good + [pscustomobject]@{ Sid = 'S-1-5-21-9-9-9-1002'; Rights = 'Read'; Type = 'Allow' }) -Protected $true -RuntimeSid $rt) -join ' ') 'another person''s account'
}
Test-Case 'the token comes from a transfer stick, never from the provisioner stick' {
    $have = @('E:\coder\coder.token', 'G:\coder\coder.token')
    $exists = { param($p) $have -contains $p }
    $d = @([pscustomobject]@{ Root = 'E:\' }, [pscustomobject]@{ Root = 'G:\' })
    Assert-Equal 'G:\coder\coder.token' ((Find-LabCoderTokenSource -Drives $d -ProvisionerRoot 'E:\AGENT-ARENA-LAB-1.6.6' -TransferFolder $coderLayout.TransferFolder -Exists $exists) -join ',')
    Assert-Equal 0 @(Find-LabCoderTokenSource -Drives @($d[0]) -ProvisionerRoot 'E:\' -TransferFolder $coderLayout.TransferFolder -Exists $exists).Count
}
Test-Case 'the runtime account: a local, enabled account that is not the built-in Administrator' {
    Assert-True (Get-LabCoderRuntimeVerdict -Account ([pscustomobject]@{ Name = 'Coder_1'; Sid = 'S-1-5-21-1-2-3-1001'; Enabled = $true }) -Membership 'standard').Ok
    Assert-False (Get-LabCoderRuntimeVerdict -Account ([pscustomobject]@{ Name = 'Administrator'; Sid = 'S-1-5-21-1-2-3-500'; Enabled = $true }) -Membership 'privileged').Ok
    Assert-False (Get-LabCoderRuntimeVerdict -Account ([pscustomobject]@{ Name = 'devuser'; Sid = 'S-1-12-1-5-6-7'; Enabled = $true }) -Membership 'standard').Ok 'not a local account (Entra/domain)'
    Assert-False (Get-LabCoderRuntimeVerdict -Account ([pscustomobject]@{ Name = 'Old'; Sid = 'S-1-5-21-1-2-3-1003'; Enabled = $false }) -Membership 'standard').Ok
    Assert-Match 'standard account is safer' ((Get-LabCoderRuntimeVerdict -Account ([pscustomobject]@{ Name = 'Owner'; Sid = 'S-1-5-21-1-2-3-1001'; Enabled = $true }) -Membership 'privileged').Notes -join ' ')
}
Test-Case 'the Coder module changes no clock and no Smart App Control' {
    $src = Get-Content -LiteralPath (Join-Path $root 'engine\modules\Lab.Coder.psm1') -Raw
    Assert-False ($src -match 'w32tm(\.exe)?\s+/config|Set-TimeZone|Set-LabTimeSync|Request-LabSmartAppControlOff|VerifiedAndReputablePolicyState')
}

$code = Get-ChildItem (Join-Path $root 'engine') -Recurse -Include *.ps1, *.psm1 | Where-Object { $_.FullName -notmatch '\\head-engine\\' }
Test-Case 'every file parses in Windows PowerShell 5.1' {
    foreach ($f in $code + @(Get-ChildItem (Join-Path $root 'engine\head-engine') -Recurse -Include *.psm1)) { $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$t, [ref]$e); Assert-Equal 0 @($e).Count ("$($f.Name): " + (@($e) | ForEach-Object Message) -join '; ') }
}
function Get-CodeStrings { param($File) $e = $null; @([Management.Automation.PSParser]::Tokenize((Get-Content -LiteralPath $File -Raw), [ref]$e) | Where-Object { $_.Type -eq 'String' } | ForEach-Object { $_.Content }) }
Test-Case 'no client address is hard-coded in the engine code (they come from config\lab-network.json)' {
    foreach ($f in $code) { $hits = @(Get-CodeStrings $f.FullName | Where-Object { $_ -match '\b192\.0\.2\.(1|254|201|202)\b' }); Assert-Equal 0 $hits.Count ("$($f.Name): " + ($hits -join ' | ')) }
}
Test-Case 'nothing outside the 1.0.3 model queue calls Ollama''s pull' { foreach ($f in $code) { Assert-Equal 0 @(Get-CodeStrings $f.FullName | Where-Object { $_ -match 'api/pull' }).Count $f.Name } }
Test-Case 'the 1.0.3 originals are byte-identical to commit 5574901 (not modified here)' {
    $orig = Join-Path (Split-Path -Parent $root) 'provisioner\engine\modules'
    if (-not (Test-Path $orig)) { return }
    $diff = & git -C (Split-Path -Parent $root) status --porcelain -- provisioner/engine provisioner/tests provisioner/coordinator 2>$null
    Assert-Equal '' (($diff | Where-Object { $_ }) -join ',')
}

Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
$summary = Write-TestSummary -Title 'LAB PROVISIONER TESTS'
if ($ResultPath) {
    $payload = [pscustomobject]@{ suite = 'lab-provisioner'; generated_at = (Get-Date).ToString('o'); machine = $env:COMPUTERNAME; powershell = $PSVersionTable.PSVersion.ToString(); total = $summary.Total; passed = $summary.Passed; failed = $summary.Failed
                                  results = @($summary.Results | ForEach-Object { [pscustomobject]@{ group = $_.Group; name = $_.Name; passed = $_.Passed; detail = $_.Detail } }) }
    [IO.File]::WriteAllText($ResultPath, ($payload | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
}
if ($summary.Failed -gt 0) { exit 1 }
exit 0
