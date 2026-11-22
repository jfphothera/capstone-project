<#
Applies and verifies the Arena observation publication ACLs on the gateway host.

Publication folder: SYSTEM/Administrators full, the gateway account modify (the
only writer), each Arena account read on files only (no folder listing, no
write). Publisher state and log folders: no Arena access. The ledger folder is
verified, never changed: Arena and broad local principals must hold no ACE.

Account rule. -ArenaAccount is every Windows account that runs an Arena process
reading the publication (admin window, admin console, event service): the
process owner, whether or not that process is elevated. Such an account must not
be a member, directly or through nested local groups, of Administrators or
Backup Operators: either can take ownership or read with backup semantics, so no
ACL could keep the ledger private from it. UAC filtering does not change this,
because the account can elevate itself. Membership that cannot be read is a
finding (fail closed).

Changes no network, firewall, service or ledger setting. -PlanOnly changes nothing.
Prints one JSON object. Exit 0 ok, 1 findings, 2 bad input.
#>
param(
    [Parameter(Mandatory=$true)][string]$PublishDirectory,
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [Parameter(Mandatory=$true)][string]$LogDirectory,
    [Parameter(Mandatory=$true)][string]$LedgerDirectory,
    [Parameter(Mandatory=$true)][string]$GatewayAccount,
    [Parameter(Mandatory=$true)][string[]]$ArenaAccount,
    [switch]$PlanOnly
)
$ErrorActionPreference = 'Stop'
function Out-Result($Object, [int]$Code) { $Object | ConvertTo-Json -Depth 6; exit $Code }
function Resolve-Sid([string]$Account) {
    try { (New-Object System.Security.Principal.NTAccount($Account)).Translate([System.Security.Principal.SecurityIdentifier]).Value }
    catch { throw "Account '$Account' does not resolve on this computer." }
}
function Get-FullPath([string]$Path) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw 'Use absolute folder paths.' }
    [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}
try {
    $publish = Get-FullPath $PublishDirectory; $state = Get-FullPath $StateDirectory
    $logs = Get-FullPath $LogDirectory; $ledger = Get-FullPath $LedgerDirectory
    if ($publish -in @($state, $logs, $ledger)) { throw 'The publication folder must be dedicated: separate from ledger, state and logs.' }
    $gatewaySid = Resolve-Sid $GatewayAccount
    # -File passes "a,b" as one string; accept both forms.
    $arenaSids = @($ArenaAccount | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { Resolve-Sid $_.Trim() } | Select-Object -Unique)
    if (-not $arenaSids.Count) { throw 'At least one Arena account is required.' }
    if ($gatewaySid -in $arenaSids) { throw 'Arena and the gateway must use different Windows accounts.' }
} catch { Out-Result ([ordered]@{ ok = $false; error = "$_" }) 2 }
# SYSTEM, Administrators. Broad principals that would expose the ledger to Arena:
# Everyone, Users, Authenticated Users, Interactive, Guests.
$system = 'S-1-5-18'; $admins = 'S-1-5-32-544'; $backupOperators = 'S-1-5-32-551'
$broad = @('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11', 'S-1-5-4', 'S-1-5-32-546')
$private = @("*${system}:(OI)(CI)F", "*${admins}:(OI)(CI)F", "*${gatewaySid}:(OI)(CI)M")
$plan = @(
    [ordered]@{ path = $publish; grants = $private + @($arenaSids | ForEach-Object { "*${_}:(OI)(IO)R" }) },
    [ordered]@{ path = $state; grants = $private },
    [ordered]@{ path = $logs; grants = $private }
)
if (-not $PlanOnly) {
    foreach ($item in $plan) {
        New-Item -ItemType Directory -Force -Path $item.path | Out-Null
        # Start from inherited defaults, then drop them, so only the listed ACEs remain.
        & icacls $item.path /reset /T /Q | Out-Null
        if ($LASTEXITCODE) { Out-Result ([ordered]@{ ok = $false; error = "icacls reset failed for $($item.path)" }) 1 }
        & icacls $item.path /inheritance:r /grant:r @($item.grants) /Q | Out-Null
        if ($LASTEXITCODE) { Out-Result ([ordered]@{ ok = $false; error = "icacls grant failed for $($item.path)" }) 1 }
    }
}
$findings = @(); $warnings = @()
function Test-Path-Acl([string]$Path, [string[]]$Forbidden, [string]$Role) {
    if (-not (Test-Path -LiteralPath $Path)) {
        if ($PlanOnly -and $Role -ne 'ledger') { return @() } # created when applied
        return @("${Role}: $Path does not exist")
    }
    $found = @()
    foreach ($item in @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction SilentlyContinue)) {
        # SQLite's -shm/-wal files can vanish between the listing and this read (lab hub, 2026-11-17): a file that is
        # gone grants nothing, so it is skipped; any other read failure still stops the check.
        try { $acl = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop } catch { if (Test-Path -LiteralPath $item.FullName) { throw } else { continue } }
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne 'Allow') { continue }
            try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $sid = "$($rule.IdentityReference)" }
            if ($sid -in $Forbidden) { $found += "${Role}: $($item.FullName) grants $($rule.IdentityReference) $($rule.FileSystemRights)" }
        }
    }
    $found
}
function Get-GroupMemberSids([string]$GroupSid, [hashtable]$Seen) {
    <# Transitive members of a local group as SIDs; throws when membership cannot be read. #>
    if ($Seen.ContainsKey($GroupSid)) { return @() }; $Seen[$GroupSid] = $true
    $members = @()
    try { $members = @(Get-LocalGroupMember -SID $GroupSid -ErrorAction Stop | ForEach-Object { [pscustomobject]@{ Sid = $_.SID.Value; Group = ($_.ObjectClass -eq 'Group') } }) }
    catch {
        # Get-LocalGroupMember fails on unresolvable members; the WinNT provider still lists SIDs.
        $name = ((New-Object System.Security.Principal.SecurityIdentifier($GroupSid)).Translate([System.Security.Principal.NTAccount]).Value -split '\\')[-1]
        $group = [ADSI]("WinNT://{0}/{1},group" -f $env:COMPUTERNAME, $name)
        foreach ($m in @($group.psbase.Invoke('Members'))) {
            $bytes = $m.GetType().InvokeMember('objectSid', 'GetProperty', $null, $m, $null)
            $class = $m.GetType().InvokeMember('Class', 'GetProperty', $null, $m, $null)
            if ($bytes) { $members += [pscustomobject]@{ Sid = (New-Object System.Security.Principal.SecurityIdentifier([byte[]]$bytes, 0)).Value; Group = ($class -eq 'Group') } }
        }
    }
    $all = @()
    foreach ($member in $members) {
        $all += $member.Sid
        # Nested local groups are expanded; a domain group cannot be expanded offline and is reported.
        if ($member.Group) {
            if ($member.Sid -like 'S-1-5-32-*' -or $member.Sid -like ((New-Object System.Security.Principal.SecurityIdentifier($admins)).AccountDomainSid.Value + '*')) { $all += Get-GroupMemberSids $member.Sid $Seen }
            else { $all += "unexpandable:$($member.Sid)" }
        }
    }
    $all
}
$findings += Test-Path-Acl $ledger (@($arenaSids) + $broad) 'ledger'
$findings += Test-Path-Acl $state (@($arenaSids) + $broad) 'state'
$findings += Test-Path-Acl $logs (@($arenaSids) + $broad) 'logs'
$findings += Test-Path-Acl $publish $broad 'publication'
$membership = [ordered]@{}
foreach ($privileged in @(@{ sid = $admins; name = 'Administrators' }, @{ sid = $backupOperators; name = 'Backup Operators' })) {
    try {
        $members = @(Get-GroupMemberSids $privileged.sid @{})
        $membership[$privileged.name] = 'checked (transitive local membership)'
        foreach ($sid in $arenaSids) { if ($sid -in $members) { $findings += "Arena account $sid is a member of $($privileged.name); no ACL can keep the ledger private from it" } }
        if ($gatewaySid -in $members) { $warnings += "Gateway account is a member of $($privileged.name); the installer's verify requires a standard account" }
        foreach ($item in @($members | Where-Object { $_ -like 'unexpandable:*' })) { $findings += "$($privileged.name) contains a group that cannot be expanded here ($($item.Substring(13))); membership of the Arena account cannot be proven" }
    } catch {
        $membership[$privileged.name] = 'unreadable'
        $findings += "$($privileged.name) membership could not be read; the Arena account separation cannot be proven"
    }
}
Out-Result ([ordered]@{
    applied = -not $PlanOnly; plan = $plan; ledger_directory = $ledger
    gateway_sid = $gatewaySid; arena_sid = $arenaSids[0]; arena_sids = $arenaSids; privileged_membership = $membership
    findings = @($findings); warnings = @($warnings); ok = ($findings.Count -eq 0)
}) $(if ($findings.Count) { 1 } else { 0 })
