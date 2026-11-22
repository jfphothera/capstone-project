# Runs the real Invoke-LabGatewayFlow from intake to the end. Real: release intake from F:, the head
# probe and inventory, every Gateway launcher command (heads, credentials, bootstrap, qualify, lifecycle,
# publisher, accept-ledger) against a scratch copy of Gateway 0.3.0 and a fresh ledger, the real heads.
# Stood in: what would change this laptop (account, rights, plan/certificate/install/verify of the
# Windows service, the ACL script, the firewall rule) and the final verification.
# -ConsoleData <folder>: the Gateway Models screen's settings folder to use instead of the hub's (its policy copy and
# Do not use list); -KeepModels: press Enter at the model menu instead of choosing models.
# -Work <folder>: where releases\0.3.0 (the scratch Gateway), flowdata, fakeusb and the run go (default: here).
param([string]$ConsoleData = '', [switch]$KeepModels, [string]$Work = '')
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path; $S = $(if ($Work) { $Work } else { $PSScriptRoot })
foreach ($m in 'Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Preflight') { Import-Module (Join-Path $root "engine\head-engine\modules\$m.psm1") -Force -Global -DisableNameChecking -WarningAction SilentlyContinue }
foreach ($m in 'Lab.Core', 'Lab.Firewall', 'Lab.Accounts', 'Lab.OllamaRuntime', 'Lab.Head', 'Lab.Gateway', 'Lab.Hub', 'Lab.Models', 'Lab.Arena', 'Lab.Apps', 'Lab.Inspect', 'Lab.Plan', 'Lab.Report') { Import-Module (Join-Path $root "engine\modules\$m.psm1") -Force -Global -DisableNameChecking -WarningAction SilentlyContinue }
$Config = Get-LabConfig -Path (Join-Path $root 'config\lab-network.json')
$Config.runtime.python.path = (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python312\python.exe')
$Packages = (@(Get-ChildItem 'F:\' -Directory -Filter 'AGENT-ARENA-LAB-*' | Sort-Object Name) | Select-Object -Last 1).FullName + '\packages'; $UsbRoot = (Join-Path $S 'fakeusb')
. (Join-Path $root 'engine\Invoke-LabGatewayFlow.ps1')

$script:fr = 'flowroot-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$data = Join-Path $S 'flowdata'; if (Test-Path $data) { Remove-Item $data -Recurse -Force }
foreach ($d in 'state', 'config', 'observation', 'observation-state', 'logs', 'backups', 'tls') { New-Item -ItemType Directory (Join-Path $data $d) -Force | Out-Null }
& $Config.runtime.python.path -I -c ("import sys; sys.path.insert(0, r'{0}'); from inference_gateway.capacity_gateway import Gateway; Gateway(r'{1}')" -f (Join-Path $S 'releases\0.3.0'), (Join-Path $data 'state\capacity-ledger.db'))

function Get-LabGatewayPaths { param($Manifest) [pscustomobject]@{ Data = $data; Ledger = "$data\state\capacity-ledger.db"; PublisherState = "$data\observation-state\publisher-state.json"; Observation = "$data\observation\shared-inference-observation.json"; Config = "$data\config\gateway-service.json"; Review = "$data\config\policy-review.json"; Ca = "$data\tls\inference-gateway-ca.pem"; CaKey = "$data\ca\ca.key"; Backups = "$data\backups" } }
function Get-LabGatewayLauncherArgs { param($Manifest, [string[]]$Arguments = @(), [string]$InstallRoot = '') @('-I', (Join-Path $S 'releases\0.3.0\inference_gateway\service_launcher.py')) + @($Arguments) }
function Get-LabRuntimeState { param($Config) [pscustomobject]@{ Python = [pscustomobject]@{ Ok = $true }; Pwsh = [pscustomobject]@{ Ok = $true } } }
function Get-LabLocalAccount { param($Name) [pscustomobject]@{ Exists = $true; Name = $Name; Sid = 'S-1-5-21-9-9-9-3000'; Enabled = $true; InUsers = $true } }
function Get-LabPrivilegedMembership { param($Sids, $Lookup) @($Sids | ForEach-Object { [pscustomobject]@{ Sid = $_; State = 'standard'; Groups = @(); Unreadable = @() } }) }
function Get-LabUserRights { param($Sid) @('SeServiceLogonRight', 'SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight') }
function Grant-LabUserRight { param($Sid, $Right, $Account) $false }
function Get-LabHubGatewayReadiness { param($Config, $LedgerAccepted, $MembershipLookup) [pscustomobject]@{ Accounts = [pscustomobject]@{ State = 'OK'; Blockers = @(); Unknown = @() }; ArenaSids = @('S-1-5-21-9-9-9-1001') } }
function New-LabGatewaySettings { param($Run, $Root, $Manifest, $ArenaAccounts) New-LabOutcome 'UNCHANGED' 'settings (stand-in)' "$data\settings.json" }
function Get-LabGatewayService { param($Name) [pscustomobject]@{ Present = $true; State = 'Stopped'; Account = 'DESKTOP-REDACT10\svc-inference-gw'; StartMode = 'Auto'; Delayed = $true } }
function Repair-LabGatewayReleaseAcl { param($InstallRoot, $Run) New-LabOutcome 'DONE' 'release files reset (stand-in)' }
function Start-LabGatewayService { param($Name, $WaitSeconds) [pscustomobject]@{ Ok = $true; Status = 'Running'; Error = ''; Seconds = 4 } }
function Set-LabGatewayListenerRule { param($Run, $Spec) New-LabOutcome 'DONE' ("$($Spec.Name) (stand-in): TCP $($Spec.LocalPort) from " + ($Spec.RemoteAddress -join ', ')) }
function Get-NetIPAddress { [CmdletBinding()] param($IPAddress) [pscustomobject]@{ InterfaceAlias = 'Ethernet' } }
function Get-NetConnectionProfile { [CmdletBinding()] param($InterfaceAlias) [pscustomobject]@{ NetworkCategory = 'Private' } }
function Export-LabGatewayCa { param($CaFile, $UsbRoot) [pscustomobject]@{ Path = 'stand-in'; Sha256 = ('0' * 64) } }
function Add-LabGatewayVerification { param($Run, $Manifest, $Paths, $Stage) }
function Resolve-LabAccount { param($Sid) 'DESKTOP-REDACT10\ArenaSvcUser' }
if ($ConsoleData) {
    function Get-LabDoNotUse { Lab.Gateway\Get-LabDoNotUse -Folder $ConsoleData }
    function Get-LabHubApprovedPolicy { Lab.Gateway\Get-LabHubApprovedPolicy -Folder $ConsoleData }
}
$real = Get-LabDefaultGatewayRunner
Set-LabGatewayRunner {
    param($f, $a, $in, $wd)
    $joined = $a -join ' '
    if ($joined -match 'gateway-deploy\.py (\w+)') {
        $v = $Matches[1]
        if ($v -eq 'plan') { return [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true, "checks": {"python": {"ok": true}, "elevated": {"ok": true}, "service_account": {"ok": true}, "arena_accounts": {"ok": true}, "service": {"ok": false, "state": "stopped"}, "package": {"ok": true}, "tls": {"ok": true}, "ledger": {"ok": true}}}'; Stderr = '' } }
        return [pscustomobject]@{ ExitCode = 0; Stdout = ('{"ok": true, "verb": "' + $v + '"}'); Stderr = '' }
    }
    if ($f -match 'pwsh') { return [pscustomobject]@{ ExitCode = 0; Stdout = '{"ok": true, "applied": true}'; Stderr = '' } }
    & $real $f $a $in $wd
}.GetNewClosure()
$script:answers = New-Object Collections.ArrayList
Set-LabApprovalProvider { param($p, $w, $k) [void]$script:answers.Add("APPROVAL [$w] $p"); $w }
Set-LabInputProvider { param($p)
    if ($p -match 'numbers to allow' -and -not ($KeepModels -and $p -match 'Press Enter to keep')) {
        $inv = Get-ChildItem (Join-Path $S $script:fr) -Recurse -Filter 'verified-head-inventory-*.json' | Select-Object -Last 1
        $c = @(Get-LabModelChoices -Inventory (Get-Content $inv.FullName -Raw | ConvertFrom-Json))
        $want = 'qwen3.5:4b', 'gemma3:4b', 'llama3.2:3b'
        $ans = (@(for ($i = 0; $i -lt $c.Count; $i++) { if ($want -contains $c[$i].Name) { $i + 1 } }) -join ',')
        [void]$script:answers.Add("INPUT $p -> $ans"); return $ans }
    [void]$script:answers.Add("INPUT $p -> (Enter)"); '' }

if (Test-Path (Join-Path $S $script:fr)) { Remove-Item (Join-Path $S $script:fr) -Recurse -Force }
$run = New-LabRun -Role 'hub' -Root (Join-Path $S $script:fr) -UsbRoot ''
# The outbox as an elevated administrator would have it (this shell is not elevated): pre-created with this account.
$ob = Join-Path $run.Root 'gateway\outbox'; New-Item -ItemType Directory $ob -Force | Out-Null
$run.Plan = [pscustomobject]@{ blocked = @(); steps = @() }
Invoke-LabGatewayFlow -Run $run
Set-LabGatewayRunner $null; Set-LabApprovalProvider $null; Set-LabInputProvider $null
'=== what the operator is asked'; $script:answers
'=== steps'; $run.Steps | ForEach-Object { '{0,-10} {1,-16} {2}' -f $_.status, $_.id, ([string]$_.detail).Substring(0, [Math]::Min(230, ([string]$_.detail).Length)) }
'=== pending'; $run.Pending | ForEach-Object { '  ' + ($_ | Out-String).Trim() }
