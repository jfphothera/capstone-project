<#
    Lab.Accounts - local accounts, user rights and group membership, for the Gateway's declared
    machine prerequisites (deployment-manifest.json "service_account", "arena_accounts").

      * New-LabStandardAccount     a local standard user (Users only), operator-typed password that
                                   lives only in a SecureString and is never logged or stored
      * Grant/Revoke-LabUserRight  LSA account rights (SeServiceLogonRight, and the deny rights
                                   SeDenyInteractiveLogonRight / SeDenyRemoteInteractiveLogonRight)
      * Get-LabPrivilegedMembership  TRANSITIVE local membership of Administrators (S-1-5-32-544) and
                                   Backup Operators (S-1-5-32-551), expanding nested local groups;
                                   unreadable membership is reported, never treated as "not a member"
    Every change is recorded with its undo. Nothing here runs unless the operator approves it.
#>

$script:Privileged = [ordered]@{ 'S-1-5-32-544' = 'Administrators'; 'S-1-5-32-551' = 'Backup Operators' }

if (-not ('LabLsa' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Collections.Generic;
public static class LabLsa {
    [StructLayout(LayoutKind.Sequential)] struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)] struct LSA_OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public int Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
    [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref LSA_OBJECT_ATTRIBUTES attr, int access, out IntPtr handle);
    [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr policy, byte[] sid, LSA_UNICODE_STRING[] rights, int count);
    [DllImport("advapi32.dll")] static extern uint LsaRemoveAccountRights(IntPtr policy, byte[] sid, bool all, LSA_UNICODE_STRING[] rights, int count);
    [DllImport("advapi32.dll")] static extern uint LsaEnumerateAccountRights(IntPtr policy, byte[] sid, out IntPtr rights, out int count);
    [DllImport("advapi32.dll")] static extern uint LsaFreeMemory(IntPtr buffer);
    [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr handle);
    [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint status);
    const int POLICY_ALL = 0x00F0FFF; const uint NOT_FOUND = 0xC0000034;
    static IntPtr Open() { var a = new LSA_OBJECT_ATTRIBUTES(); IntPtr h; uint s = LsaOpenPolicy(IntPtr.Zero, ref a, POLICY_ALL, out h); if (s != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(s)); return h; }
    static byte[] Sid(string sid) { var s = new SecurityIdentifier(sid); var b = new byte[s.BinaryLength]; s.GetBinaryForm(b, 0); return b; }
    static LSA_UNICODE_STRING Str(string v) { var u = new LSA_UNICODE_STRING(); u.Buffer = Marshal.StringToHGlobalUni(v); u.Length = (ushort)(v.Length * 2); u.MaximumLength = (ushort)(u.Length + 2); return u; }
    public static void Add(string sid, string right) { IntPtr h = Open(); try { var r = new[] { Str(right) }; uint s = LsaAddAccountRights(h, Sid(sid), r, 1); Marshal.FreeHGlobal(r[0].Buffer); if (s != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(s)); } finally { LsaClose(h); } }
    public static void Remove(string sid, string right) { IntPtr h = Open(); try { var r = new[] { Str(right) }; uint s = LsaRemoveAccountRights(h, Sid(sid), false, r, 1); Marshal.FreeHGlobal(r[0].Buffer); if (s != 0 && s != NOT_FOUND) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(s)); } finally { LsaClose(h); } }
    public static string[] List(string sid) {
        IntPtr h = Open(); var list = new List<string>();
        try { IntPtr p; int n; uint s = LsaEnumerateAccountRights(h, Sid(sid), out p, out n);
              if (s == NOT_FOUND) return list.ToArray(); if (s != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(s));
              int size = Marshal.SizeOf(typeof(LSA_UNICODE_STRING));
              for (int i = 0; i < n; i++) { var u = (LSA_UNICODE_STRING)Marshal.PtrToStructure(new IntPtr(p.ToInt64() + i * size), typeof(LSA_UNICODE_STRING)); list.Add(Marshal.PtrToStringUni(u.Buffer, u.Length / 2)); }
              LsaFreeMemory(p); }
        finally { LsaClose(h); }
        return list.ToArray();
    }
}
'@
}

function Get-LabUserRights { param([Parameter(Mandatory)][string]$Sid) try { return @([LabLsa]::List($Sid)) } catch { return @('inaccessible') } }

function Grant-LabUserRight {
    param([Parameter(Mandatory)][string]$Sid, [Parameter(Mandatory)][string]$Right, [string]$Account = '')
    if ((Get-LabUserRights -Sid $Sid) -contains $Right) { return $false }
    [LabLsa]::Add($Sid, $Right)
    Register-LabChange -Kind 'user-right' -Description ("granted {0} to {1}" -f $Right, $(if ($Account) { $Account } else { $Sid })) -Undo ([pscustomobject]@{ type = 'user-right'; action = 'remove'; sid = $Sid; right = $Right }) | Out-Null
    return $true
}

function Undo-LabUserRight {
    param([Parameter(Mandatory)]$Undo)
    if ($Undo.action -eq 'remove') { [LabLsa]::Remove($Undo.sid, $Undo.right); return ('removed {0} from {1}' -f $Undo.right, $Undo.sid) }
    [LabLsa]::Add($Undo.sid, $Undo.right); return ('granted {0} to {1} again' -f $Undo.right, $Undo.sid)
}

function Get-LabGroupMemberSids {
    <#  Direct members of a local group (by SID), with their class, through the WinNT provider.  #>
    param([Parameter(Mandatory)][string]$GroupSid)
    $name = ((New-Object Security.Principal.SecurityIdentifier($GroupSid)).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1]
    $group = [ADSI]("WinNT://{0}/{1},group" -f $env:COMPUTERNAME, $name)
    $out = @()
    foreach ($m in @($group.psbase.Invoke('Members'))) {
        $bytes = $m.GetType().InvokeMember('objectSid', 'GetProperty', $null, $m, $null)
        $class = [string]$m.GetType().InvokeMember('Class', 'GetProperty', $null, $m, $null)
        if ($bytes) { $out += [pscustomobject]@{ Sid = (New-Object Security.Principal.SecurityIdentifier([byte[]]$bytes, 0)).Value; Class = $class } }
    }
    return $out
}

function Resolve-LabTransitiveMembers {
    <#  Pure over a member lookup: every SID reached from a group through nested groups.
        $Lookup is a scriptblock (group SID -> members with Sid/Class); it may throw.  #>
    param([Parameter(Mandatory)][string]$GroupSid, [Parameter(Mandatory)][scriptblock]$Lookup)
    $seen = @{}; $members = @{}; $queue = New-Object System.Collections.Queue; $queue.Enqueue($GroupSid); $unexpanded = @()
    while ($queue.Count) {
        $g = $queue.Dequeue(); if ($seen[$g]) { continue }; $seen[$g] = $true
        $list = $null
        try { $list = @(& $Lookup $g) } catch { $unexpanded += $g; continue }
        foreach ($m in $list) { $members[$m.Sid] = $true; if ($m.Class -eq 'Group') { $queue.Enqueue($m.Sid) } }
    }
    return [pscustomobject]@{ Members = @($members.Keys); Unexpanded = $unexpanded }
}

function Get-LabPrivilegedMembership {
    <#  For each account SID: is it, transitively through local groups, in Administrators or Backup
        Operators? State per account: privileged / standard / unknown (fail closed).  #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Sids, [scriptblock]$Lookup = { param($g) Get-LabGroupMemberSids -GroupSid $g })
    $groups = @{}
    foreach ($g in $script:Privileged.Keys) { $groups[$g] = Resolve-LabTransitiveMembers -GroupSid $g -Lookup $Lookup }
    $out = @()
    foreach ($s in $Sids) {
        $in = @($script:Privileged.Keys | Where-Object { $groups[$_].Members -contains $s } | ForEach-Object { $script:Privileged[$_] })
        $unknown = @($script:Privileged.Keys | Where-Object { @($groups[$_].Unexpanded).Count } | ForEach-Object { $script:Privileged[$_] })
        $state = $(if ($in.Count) { 'privileged' } elseif ($unknown.Count) { 'unknown' } else { 'standard' })
        $out += [pscustomobject][ordered]@{ Sid = $s; State = $state; Groups = $in; Unreadable = $unknown }
    }
    return $out
}

function ConvertTo-LabLocalAccountName {
    <#  Pure - unit tested. "ArenaSvcUser", ".\ArenaSvcUser" and "DESKTOP-REDACT10\ArenaSvcUser" (this computer,
        any letter case) name the same local account; another prefix is a domain or another computer.  #>
    param([Parameter(Mandatory)][string]$Name, [string]$Computer = $env:COMPUTERNAME)
    $n = $Name.Trim()
    if ($n -match '^([^\\]+)\\([^\\]+)$') {
        if ($Matches[1] -ne '.' -and $Matches[1] -ine $Computer) { return [pscustomobject]@{ Local = $false; Name = $Matches[2]; Qualified = $n } }
        $n = $Matches[2]
    }
    return [pscustomobject]@{ Local = ($n -notmatch '[\\@]'); Name = $n; Qualified = ('{0}\{1}' -f $Computer, $n) }
}

function Get-LabLocalAccount {
    param([Parameter(Mandatory)][string]$Name)
    $id = ConvertTo-LabLocalAccountName -Name $Name
    if (-not $id.Local) { return [pscustomobject]@{ Exists = $false; NotLocal = $true; Name = $Name; Qualified = $Name; Sid = ''; Enabled = $false; InUsers = $false } }
    $u = Get-LocalUser -Name $id.Name -ErrorAction SilentlyContinue
    if (-not $u) { return [pscustomobject]@{ Exists = $false; NotLocal = $false; Name = $id.Name; Qualified = $id.Qualified; Sid = ''; Enabled = $false; InUsers = $false } }
    $users = @(); try { $users = @(Get-LabGroupMemberSids -GroupSid 'S-1-5-32-545' | ForEach-Object Sid) } catch { }
    return [pscustomobject]@{ Exists = $true; NotLocal = $false; Name = $u.Name; Qualified = $id.Qualified; Sid = $u.SID.Value; Enabled = [bool]$u.Enabled; InUsers = ($users -contains $u.SID.Value); PasswordNeverExpires = ($null -eq $u.PasswordExpires) }
}

function New-LabStandardAccount {
    <#  Creates a local standard user. The password arrives as a SecureString typed by the operator.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][Security.SecureString]$Password, [string]$Description = '')
    # Windows' own limits (New-LocalUser): name 20 characters, description 48. 1.3.2 sent a 55-character
    # description and DESKTOP-REDACT10 refused the account after the password had been typed.
    if ($Name.Length -gt 20) { throw "local account name '$Name' is longer than Windows' 20 characters" }
    if ($Description.Length -gt 48) { $Description = $Description.Substring(0, 48) }
    $existing = Get-LabLocalAccount -Name $Name
    if ($existing.Exists) { return (New-LabOutcome 'UNCHANGED' ("local account $Name exists (" + $existing.Sid + ')') $existing) }
    New-LocalUser -Name $Name -Password $Password -Description $Description -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -ErrorAction Stop | Out-Null
    $acct = Get-LabLocalAccount -Name $Name
    Register-LabChange -Kind 'local-account' -Description ("created local standard account $Name") -Undo ([pscustomobject]@{ type = 'local-account'; name = $Name; sid = $acct.Sid }) | Out-Null
    if (-not $acct.InUsers) { Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $acct.Sid -ErrorAction Stop; $acct = Get-LabLocalAccount -Name $Name }
    return (New-LabOutcome 'DONE' ("created $Name (" + $acct.Sid + '), member of Users only') $acct)
}

function Undo-LabLocalAccount {
    param([Parameter(Mandatory)]$Undo)
    $u = Get-LocalUser -Name $Undo.name -ErrorAction SilentlyContinue
    if ($u -and $u.SID.Value -eq $Undo.sid) { Remove-LocalUser -SID $Undo.sid -ErrorAction Stop; return ('removed local account ' + $Undo.name) }
    return ('local account {0} not removed: it is not the one this run created' -f $Undo.name)
}

Export-ModuleMember -Function @('Get-LabUserRights', 'Grant-LabUserRight', 'Undo-LabUserRight', 'Get-LabGroupMemberSids', 'Resolve-LabTransitiveMembers',
    'Get-LabPrivilegedMembership', 'ConvertTo-LabLocalAccountName', 'Get-LabLocalAccount', 'New-LabStandardAccount', 'Undo-LabLocalAccount')
