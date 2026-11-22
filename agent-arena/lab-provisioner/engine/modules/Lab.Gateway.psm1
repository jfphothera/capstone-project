<#
    Lab.Gateway - the independent Inference Gateway release, seen from the provisioner.

    F: never contains Gateway logic. It:
      * takes the Gateway's release ZIP unchanged (packages\gateway\), checks the archive against its
        published .sha256, every file against SHA256SUMS and the deployment manifest, and the
        manifest's declarations against the lab architecture (Test-LabGatewayPackage);
      * writes the installer's settings file from the Gateway's own template, filling only the one
        value the contract leaves to Arena: arena_accounts (New-LabGatewaySettings);
      * runs the Gateway's own commands exactly as the manifest states them, reads their one JSON
        object, and keeps their semantics: exit 0 success, 1 failed check or refused step, 2 bad input
        (Invoke-LabGatewayCommand). A password is handed over on stdin only and never logged;
      * creates the one inbound rule the manifest names, at the stage it names, and a separate
        approved cutover rule for 192.0.2.1 (Set-LabGatewayListenerRule);
      * checks /health and /ready over TLS against the Gateway's CA only (Test-LabGatewayHealth).
#>

$script:Runner = $null

if (-not ('LabTls' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class LabTls {
    // GET over HTTPS trusting ONLY the given CA file; the host name (an IP SAN here) must match.
    public static string Get(string url, string caFile, int timeoutMs, out int status, out string error, out string serverCert) {
        status = 0; error = ""; serverCert = "";
        var ca = new X509Certificate2(File.ReadAllBytes(caFile));
        string seen = "", why = "";
        var req = (HttpWebRequest)WebRequest.Create(url);
        req.Proxy = null; req.Timeout = timeoutMs; req.ReadWriteTimeout = timeoutMs; req.KeepAlive = false;
        req.ServerCertificateValidationCallback = (sender, cert, chain, errors) => {
            var leaf = new X509Certificate2(cert); seen = leaf.Subject + " until " + leaf.NotAfter.ToString("s");
            if ((errors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0) { why = "certificate does not match the address (SAN)"; return false; }
            if ((errors & SslPolicyErrors.RemoteCertificateNotAvailable) != 0) { why = "no certificate"; return false; }
            var c = new X509Chain(); c.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
            c.ChainPolicy.VerificationFlags = X509VerificationFlags.AllowUnknownCertificateAuthority; c.ChainPolicy.ExtraStore.Add(ca);
            if (!c.Build(leaf)) { foreach (var s in c.ChainStatus) if (s.Status != X509ChainStatusFlags.UntrustedRoot) { why = "chain: " + s.StatusInformation.Trim(); return false; } }
            var root = c.ChainElements[c.ChainElements.Count - 1].Certificate;
            if (root.Thumbprint != ca.Thumbprint) { why = "not issued by the Gateway CA"; return false; }
            return true; };
        try {
            using (var resp = (HttpWebResponse)req.GetResponse()) { status = (int)resp.StatusCode; serverCert = seen; using (var r = new StreamReader(resp.GetResponseStream())) return r.ReadToEnd(); }
        } catch (WebException ex) {
            serverCert = seen;
            var resp = ex.Response as HttpWebResponse;
            if (resp != null) { status = (int)resp.StatusCode; using (var r = new StreamReader(resp.GetResponseStream())) { var body = r.ReadToEnd(); resp.Close(); return body; } }
            error = why != "" ? why : ex.Status + ": " + ex.Message; return "";
        }
    }
}
'@
}

# ---------------------------------------------------------------------------
# Intake: the release ZIP, its hashes and its manifest (read-only)
# ---------------------------------------------------------------------------

function Find-LabGatewayPackage {
    param([Parameter(Mandatory)][string]$Folder)
    return (@(Get-ChildItem -LiteralPath $Folder -Filter 'inference-gateway-*.zip' -File -ErrorAction SilentlyContinue | Sort-Object Name -Descending) | Select-Object -First 1)
}

function Expand-LabGatewayPackage {
    <#  The archive against its published .sha256, then unpacked to a local staging folder.  #>
    param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$Staging)
    $sumFile = $Zip + '.sha256'
    if (-not (Test-Path -LiteralPath $sumFile)) { throw "no $([IO.Path]::GetFileName($sumFile)) next to the release" }
    $line = ([IO.File]::ReadAllText($sumFile)).Trim()
    if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$' -or $Matches[2].Trim() -ne [IO.Path]::GetFileName($Zip)) { throw "$sumFile does not name $([IO.Path]::GetFileName($Zip))" }
    $want = $Matches[1].ToLowerInvariant(); $have = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($want -ne $have) { throw "the release archive does not match its published SHA-256 ($have, expected $want)" }
    $null = Expand-LabZipChecked -Zip $Zip -Destination $Staging
    $root = @(Get-ChildItem -LiteralPath $Staging -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'deployment-manifest.json') }) | Select-Object -First 1
    if (-not $root) { throw 'the archive holds no folder with deployment-manifest.json' }
    return [pscustomobject]@{ Root = $root.FullName; ArchiveSha256 = $have }
}

function Test-LabGatewayPackage {
    <#  Every check the provisioner needs before it may run anything from the release.  #>
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Config)
    $findings = @(); $warnings = @(); $sums = @{}
    $sumPath = Join-Path $Root 'SHA256SUMS'
    if (-not (Test-Path -LiteralPath $sumPath)) { return [pscustomobject]@{ Ok = $false; Findings = @('SHA256SUMS is missing'); Warnings = @(); Manifest = $null; Files = 0 } }
    foreach ($l in [IO.File]::ReadAllLines($sumPath)) {
        if (-not $l.Trim()) { continue }
        if ($l -notmatch '^([0-9a-f]{64})  (.+)$') { $findings += "SHA256SUMS line not understood: $l"; continue }
        $rel = $Matches[2]; $sums[$rel] = $Matches[1]
        $full = [IO.Path]::GetFullPath((Join-Path $Root $rel))
        if (-not $full.StartsWith([IO.Path]::GetFullPath($Root), [StringComparison]::OrdinalIgnoreCase)) { $findings += "path escapes the package: $rel"; continue }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { $findings += "missing: $rel"; continue }
        if ((Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Matches[1]) { $findings += "SHA-256 mismatch: $rel" }
    }
    $extra = @(Get-ChildItem -LiteralPath $Root -Recurse -File | ForEach-Object { $_.FullName.Substring($Root.TrimEnd('\').Length + 1) -replace '\\', '/' } | Where-Object { $_ -ne 'SHA256SUMS' -and -not $sums.ContainsKey($_) })
    foreach ($x in $extra) { $findings += "file not listed in SHA256SUMS: $x" }
    $m = $null; try { $m = Get-Content -LiteralPath (Join-Path $Root 'deployment-manifest.json') -Raw | ConvertFrom-Json } catch { $findings += 'deployment-manifest.json is unreadable' }
    if ($m) {
        $g = $Config.gateway
        if ($m.schema -ne [string]$g.manifest_schema) { $findings += "manifest schema $($m.schema), expected $($g.manifest_schema)" }
        if ($m.release -ne [string]$g.expected_release) { $findings += "release $($m.release), expected $($g.expected_release)" }
        if ($m.host -ne $Config.hub) { $findings += "manifest host $($m.host) is not the hub $($Config.hub)" }
        foreach ($p in $m.files.PSObject.Properties) { if (-not $sums.ContainsKey($p.Name)) { $findings += "manifest file not in SHA256SUMS: $($p.Name)" } elseif ($sums[$p.Name] -ne [string]$p.Value) { $findings += "manifest and SHA256SUMS disagree on $($p.Name)" } }
        $l = $m.listener; $fr = $l.firewall_rule
        if ($l.bind -ne $Config.hub) { $findings += "listener binds $($l.bind), not the hub" }
        if (-not ([int]$l.port -gt 0)) { $findings += 'no listener port' }
        foreach ($c in @($l.allowed_clients)) { $n = Get-LabHostNumber $c; if ($n -lt [int]$Config.ranges.app[0] -or $n -gt [int]$Config.ranges.app[1]) { $findings += "allowed client $c is outside the application range" } }
        if (@($l.allowed_clients) -contains $Config.dev_workstation) { $findings += 'the development workstation must not be in the default client list' }
        if ($fr.remote_addresses -ne 'allowed_clients' -or [string]$fr.never -ne 'Any' -or $fr.local_address -ne $Config.hub -or [int]$fr.local_port -ne [int]$l.port -or $fr.protocol -ne 'TCP' -or $fr.direction -ne 'inbound') { $findings += 'the listener firewall rule is not the restricted rule F: implements' }
        if (@($m.service_account.rights) -join ',' -ne 'SeServiceLogonRight') { $findings += "service account rights: $(@($m.service_account.rights) -join ',')" }
        if ($m.runtime.machine_wide -ne $true) { $findings += 'runtime is not machine-wide' }
        foreach ($v in 'plan', 'certificate', 'install', 'verify', 'update', 'rollback', 'repair', 'uninstall', 'publication_acl', 'accept_ledger', 'status') { if (-not ($m.commands.PSObject.Properties.Name -contains $v)) { $findings += "manifest has no '$v' command" } }
        if ($m.head_inventory_input.schema -ne 'agent-arena.verified-head-inventory/1') { $findings += "head inventory schema $($m.head_inventory_input.schema)" }
        if ($m.observation.file -ne $g.observation_file -or $m.observation.contract -ne $g.observation_contract -or $m.observation.gateway_id_for_arena -ne $g.gateway_id) { $findings += 'the observation declaration differs from Arena''s configuration' }
        if (-not (Test-Path -LiteralPath (Join-Path $Root 'bin\gateway-deploy.py'))) { $findings += 'bin\gateway-deploy.py missing' }
        if ($m.contract -and -not (Test-Path -LiteralPath (Join-Path $Root $m.contract))) { $warnings += ("the manifest names its contract as '{0}', which is the source-tree path; in the package it is docs\DEPLOYMENT-CONTRACT.md" -f $m.contract) }
        foreach ($o in @($m.open_items)) { $warnings += ('open item {0} ({1}): {2}' -f $o.id, $o.owner, $o.item) }
    }
    return [pscustomobject]@{ Ok = ($findings.Count -eq 0); Findings = $findings; Warnings = $warnings; Manifest = $m; Files = $sums.Count; Release = $(if ($m) { $m.release } else { '' }) }
}

# ---------------------------------------------------------------------------
# Settings, from the Gateway's own template
# ---------------------------------------------------------------------------

function New-LabGatewaySettingsText {
    <#  Pure. The template with arena_accounts filled in; every other value must already equal the manifest.  #>
    param([Parameter(Mandatory)][string]$TemplateJson, [Parameter(Mandatory)]$Manifest, [Parameter(Mandatory)][string[]]$ArenaAccounts, [string]$Computer = $env:COMPUTERNAME)
    $t = $TemplateJson | ConvertFrom-Json
    $problems = @()
    if ($t.python -ne $Manifest.runtime.path) { $problems += "template python $($t.python) is not the manifest runtime $($Manifest.runtime.path)" }
    if ($t.service_account -ne $Manifest.service_account.qualified) { $problems += 'template service account differs from the manifest' }
    if ($t.listen.host -ne $Manifest.listener.bind -or [int]$t.listen.port -ne [int]$Manifest.listener.port) { $problems += 'template listener differs from the manifest' }
    # Every account is written as <computer>\name. The Gateway resolves these names with
    # NTAccount.Translate, which cannot resolve ".\name" (checked on Windows 11). With the template's
    # ".\svc-inference-gw", its plan refuses service_account. The service manager accepts both forms.
    $accounts = @($ArenaAccounts | Where-Object { $_ } | ForEach-Object { if ($_ -match '\\') { ConvertTo-LabResolvableAccount -Account $_ -Computer $Computer } else { '{0}\{1}' -f $Computer, $_ } })
    if (-not $accounts.Count) { $problems += 'no Arena standard account given' }
    if ($accounts | Where-Object { ($_ -split '\\')[-1] -ieq ($Manifest.service_account.name) }) { $problems += 'an Arena account cannot be the Gateway service account' }
    if ($problems.Count) { throw ('settings not written: ' + ($problems -join '; ')) }
    $t.service_account = ConvertTo-LabResolvableAccount -Account ([string]$t.service_account) -Computer $Computer
    $t.arena_accounts = $accounts
    return ($t | ConvertTo-Json -Depth 8)
}

function New-LabGatewaySettings {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Manifest, [Parameter(Mandatory)][string[]]$ArenaAccounts)
    $text = New-LabGatewaySettingsText -TemplateJson ([IO.File]::ReadAllText((Join-Path $Root 'config\deployment-settings.template.json'))) -Manifest $Manifest -ArenaAccounts $ArenaAccounts
    $dir = Join-Path $Run.Root 'gateway'; $path = Join-Path $dir 'deployment-settings.json'
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    & icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
    $before = $null; if (Test-Path -LiteralPath $path) { $before = Join-Path $Run.Backups 'deployment-settings.before.json'; Copy-Item -LiteralPath $path -Destination $before -Force }
    if ($before -and ([IO.File]::ReadAllText($path) -eq $text)) { return (New-LabOutcome 'UNCHANGED' $path $path) }
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
    Register-LabChange -Kind 'gateway-settings' -Description ("Gateway installer settings $path (arena_accounts " + ($ArenaAccounts -join ', ') + ')') -Undo ([pscustomobject]@{ type = 'file-restore'; path = $path; backup = $before }) | Out-Null
    return (New-LabOutcome 'DONE' $path $path)
}

# ---------------------------------------------------------------------------
# Running the Gateway's own commands
# ---------------------------------------------------------------------------

function ConvertTo-LabWindowsArgument {
    <#  Pure - unit tested. One argument quoted for the Windows command-line rules that Python, pwsh and
        the C runtime use: backslashes before a quote are doubled, an inner quote becomes \", and a
        trailing backslash is doubled before the closing quote. Empty becomes "".  #>
    param([AllowEmptyString()][string]$Value)
    if ($Value -ne '' -and $Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object Text.StringBuilder; [void]$sb.Append('"'); $slashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') { [void]$sb.Append('\', 2 * $slashes + 1); [void]$sb.Append('"') } else { if ($slashes) { [void]$sb.Append('\', $slashes) }; [void]$sb.Append($ch) }
        $slashes = 0
    }
    if ($slashes) { [void]$sb.Append('\', 2 * $slashes) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Get-LabDefaultGatewayRunner {
    return {
        param([string]$FileName, [string[]]$Arguments, [string]$Stdin, [string]$WorkingDirectory)
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $FileName
        $psi.Arguments = (@($Arguments | ForEach-Object { ConvertTo-LabWindowsArgument $_ }) -join ' ')
        $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
        if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
        # .NET Framework opens the child's stdin with the console's input encoding and flushes at once: on a
        # console set to UTF-8 (code page 65001, or Windows' "Use Unicode UTF-8" option) that writes a byte-order
        # mark first, and a password would reach the Gateway installer with three extra characters in front.
        $savedIn = $null
        try { if ([Console]::InputEncoding.GetPreamble().Length -gt 0) { $savedIn = [Console]::InputEncoding; [Console]::InputEncoding = New-Object Text.UTF8Encoding $false } } catch { $savedIn = $null }
        try { $p = [Diagnostics.Process]::Start($psi) } finally { if ($savedIn) { try { [Console]::InputEncoding = $savedIn } catch { } } }
        # Bytes in the ANSI code page: Python reads a piped stdin in the locale (ANSI) encoding, also under -I,
        # while Windows PowerShell's StandardInput writer uses the console's OEM page. With WriteLine, a password
        # with a character like '£' or 'é' reached the Gateway installer as a different password.
        if ($Stdin) { $bytes = [Text.Encoding]::Default.GetBytes($Stdin + "`r`n"); $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length); $p.StandardInput.BaseStream.Flush(); [Array]::Clear($bytes, 0, $bytes.Length) }
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEndAsync()
        $p.WaitForExit()
        return [pscustomobject]@{ ExitCode = $p.ExitCode; Stdout = $out.Result; Stderr = $err.Result }
    }
}

function Set-LabGatewayRunner { param([scriptblock]$Runner) $script:Runner = $Runner }

function ConvertFrom-LabGatewayOutput {
    <#  Pure. The one JSON object a Gateway command prints (possibly over several lines).  #>
    param([AllowEmptyString()][string]$Text)
    $t = ([string]$Text).Trim()
    if (-not $t) { return $null }
    try { return ($t | ConvertFrom-Json) } catch { }
    $i = $t.IndexOf('{'); $j = $t.LastIndexOf('}')
    if ($i -ge 0 -and $j -gt $i) { try { return ($t.Substring($i, $j - $i + 1) | ConvertFrom-Json) } catch { } }
    return $null
}

function Get-LabFixedPublicationAclScript {
    <#  1.6.21: the Gateway's Set-ObservationPublicationAcl.ps1 with a vanished file skipped (a SQLite -shm/-wal file
        removed between the folder listing and the ACL read), written to -Folder; the source itself when it already
        has the fix or does not have the expected line (then it runs as shipped).  #>
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Folder)
    $old = 'foreach ($rule in (Get-Acl -LiteralPath $item.FullName).Access) {'
    $new = 'try { $acl = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop } catch { if (Test-Path -LiteralPath $item.FullName) { throw } else { continue } }; foreach ($rule in $acl.Access) {'
    if (-not (Test-Path -LiteralPath $Source)) { return $Source }
    $t = [IO.File]::ReadAllText($Source)
    if (-not $t.Contains($old)) { return $Source }
    $dest = Join-Path $Folder 'Set-ObservationPublicationAcl.lab-fixed.ps1'
    [IO.File]::WriteAllText($dest, $t.Replace($old, $new), (New-Object Text.UTF8Encoding $true))
    Write-LabLog "  publication ACL: running a copy of $Source that skips files SQLite removed meanwhile ($dest)" -Level INFO
    return $dest
}

function Invoke-LabGatewayCommand {
    <#  One Gateway command. -Password (SecureString) goes to stdin only, for install/repair.
        Returns Ok only for exit 0 with "ok": true; Meaning names the exit-code semantics.  #>
    param([Parameter(Mandatory)][string]$FileName, [Parameter(Mandatory)][string[]]$Arguments, [Parameter(Mandatory)][string]$Label,
          [Security.SecureString]$Password = $null, [string]$WorkingDirectory = '', $Run = $null)
    $runner = $script:Runner; if (-not $runner) { $runner = Get-LabDefaultGatewayRunner }
    $stdin = $null
    if ($Password) { $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password); try { $stdin = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) } }
    Write-LabLog ('  gateway: {0} {1}{2}' -f [IO.Path]::GetFileName($FileName), ($Arguments -join ' '), $(if ($Password) { '  (password on stdin)' } else { '' })) -Level INFO
    try { $r = & $runner $FileName $Arguments $stdin $WorkingDirectory } finally { $stdin = $null }
    $json = ConvertFrom-LabGatewayOutput -Text $r.Stdout
    $meaning = @{ 0 = 'success'; 1 = 'failed check or refused step'; 2 = 'bad input' }[[int]$r.ExitCode]; if (-not $meaning) { $meaning = 'unexpected exit code' }
    # Exit 0 with one JSON object is success unless that object says "ok" other than true. The deploy verbs
    # and credentials print "ok"; bootstrap, lifecycle status, publisher status and accept-ledger print
    # their result without it (Gateway 0.3.0, rehearsed). 1.3.4 required "ok": true and so would have
    # stopped a successful bootstrap.
    $hasOk = ($json -and @($json.PSObject.Properties.Name) -contains 'ok')
    $ok = ([int]$r.ExitCode -eq 0 -and $json -and (-not $hasOk -or $json.ok -eq $true))
    if ([int]$r.ExitCode -eq 0 -and -not $json) { $meaning = 'exit 0 without a JSON result (treated as a failure)' }
    elseif ([int]$r.ExitCode -eq 0 -and -not $ok) { $meaning = 'exit 0 with "ok" not true (treated as a failure)' }
    $result = [pscustomobject][ordered]@{ Label = $Label; ExitCode = [int]$r.ExitCode; Ok = $ok; Meaning = $meaning; Json = $json; Stderr = (Protect-LabText ([string]$r.Stderr)).Trim() }
    if ($Run) { Save-LabJson -InputObject (Protect-LabObject ([pscustomobject]@{ label = $Label; exit_code = $result.ExitCode; ok = $ok; meaning = $meaning; output = $json; stderr = $result.Stderr })) -Path (Join-Path $Run.Folder ('gateway-' + ($Label -replace '[^\w\-]', '_') + '.json')) }
    return $result
}

function Get-LabGatewayDeployArgs {
    <#  Pure. "<python>" -I "<package>\bin\gateway-deploy.py" <verb> --settings "<settings>" [extra]  #>
    param([Parameter(Mandatory)][string]$Package, [Parameter(Mandatory)][string]$Verb, [Parameter(Mandatory)][string]$Settings, [string[]]$Extra = @())
    # [IO.Path]::Combine, not Join-Path: Windows PowerShell 5.1's Join-Path throws for a drive letter that is absent.
    return @('-I', [IO.Path]::Combine($Package, 'bin\gateway-deploy.py'), $Verb, '--settings', $Settings) + @($Extra)
}

function Get-LabGatewayLauncherArgs {
    <#  Pure. The installed release's service_launcher.py, as the manifest's commands use it.  #>
    param([Parameter(Mandatory)]$Manifest, [string[]]$Arguments = @(), [string]$InstallRoot = 'C:\Program Files\InferenceGateway')
    return @('-I', (Join-Path $InstallRoot ('releases\{0}\inference_gateway\service_launcher.py' -f $Manifest.release))) + @($Arguments)
}

# ---------------------------------------------------------------------------
# Service, listener rule, health
# ---------------------------------------------------------------------------

function Test-LabUsersCanRead {
    <#  True when BUILTIN\Users (S-1-5-32-545) is allowed to read the file or folder.  #>
    param([Parameter(Mandatory)][string]$Path)
    try { $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop } catch { return $false }
    foreach ($a in $acl.Access) {
        $sid = ''; try { $sid = $a.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { }
        if ($sid -eq 'S-1-5-32-545' -and [string]$a.AccessControlType -eq 'Allow' -and ($a.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadData)) { return $true }
    }
    return $false
}

function Repair-LabGatewayReleaseAcl {
    <#  The Gateway installer builds each release with "pip --target". Under Python 3.12.4 and later, pip's
        temporary folders are created owner-only (CPython's fix for CVE-2024-4030). The files moved out of
        them keep that DACL: SYSTEM, Administrators, OWNER RIGHTS. The service account then cannot read
        service_launcher.py and the service never connects to Windows (DESKTOP-REDACT10, 1.3.5: event 7009, no
        Gateway log written). The manifest declares Users:RX on C:\Program Files\InferenceGateway; each
        release is reset to inherit it. Nothing else changes, and the files themselves are untouched.  #>
    param([string]$InstallRoot = 'C:\Program Files\InferenceGateway', $Run = $null)
    $rel = Join-Path $InstallRoot 'releases'
    if (-not (Test-Path -LiteralPath $rel)) { return (New-LabOutcome 'UNCHANGED' "no release folder yet ($rel)") }
    $notes = @()
    foreach ($d in @(Get-ChildItem -LiteralPath $rel -Directory -ErrorAction Stop | Where-Object { $_.Name -notlike '*.staging' })) {
        $files = @(Get-ChildItem -LiteralPath $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue)
        $blocked = @($files | Where-Object { -not (Test-LabUsersCanRead $_.FullName) })
        if (-not $blocked.Count) { continue }
        & icacls.exe $d.FullName /reset /T /C /Q | Out-Null
        $still = @($files | Where-Object { -not (Test-LabUsersCanRead $_.FullName) })
        if ($still.Count) { throw ("release {0}: {1} file(s) still not readable by Users after the reset, for example {2}" -f $d.Name, $still.Count, $still[0].FullName) }
        $notes += ('release {0}: {1} of {2} files were owner-only (pip temporary folder); now inherit Users read from {3}' -f $d.Name, $blocked.Count, $files.Count, $InstallRoot)
        if ($Run) { Register-LabChange -Kind 'acl' -Description ("reset the ACL of $($d.FullName) to inherit (Users read, as the Gateway manifest declares)") -Undo ([pscustomobject]@{ type = 'manual'; instructions = 'none needed: the release now has the ACL the Gateway manifest declares' }) | Out-Null }
    }
    if (-not $notes.Count) { return (New-LabOutcome 'UNCHANGED' 'every release file is readable by Users') }
    return (New-LabOutcome 'DONE' ($notes -join '; '))
}

function Start-LabGatewayService {
    <#  Starts the installed Gateway service and waits until it runs. On DESKTOP-REDACT10 (1.3.4) the installer's
        own first start ended with 1053: Windows gives a service 30 s to connect, and the first start under a
        new account also builds that account's profile and starts a cold Python. Waiting here also covers
        the service's own recovery restart (30 s after a failure).  #>
    param([string]$Name = 'InferenceGateway', [int]$WaitSeconds = 150)
    $s = Get-Service -Name $Name -ErrorAction Stop
    if ([string]$s.Status -eq 'Running') { return [pscustomobject]@{ Ok = $true; Status = 'Running'; Error = ''; Seconds = 0 } }
    $t0 = Get-Date; $err = ''
    try { if ([string]$s.Status -ne 'StartPending') { $s.Start() } } catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; $err = $e.Message }
    do { Start-Sleep -Seconds 2; $s.Refresh() } while ([string]$s.Status -ne 'Running' -and ((Get-Date) - $t0).TotalSeconds -lt $WaitSeconds)
    return [pscustomobject]@{ Ok = ([string]$s.Status -eq 'Running'); Status = [string]$s.Status; Error = $err; Seconds = [int]((Get-Date) - $t0).TotalSeconds }
}

function Get-LabServiceDiagnostics {
    <#  Read-only evidence for a service that does not start: its configuration and status, the Service
        Control Manager's events about it, Application crash events for its program, the Gateway's own
        logs, the account's profile, and the permissions on what the service opens before it connects.
        Returns text lines (secrets redacted).  #>
    param([string]$Name = 'InferenceGateway', [string]$DataRoot = 'C:\ProgramData\InferenceGateway', [string]$InstallRoot = 'C:\Program Files\InferenceGateway', [string]$Python = '', [int]$Hours = 6)
    $out = New-Object Collections.ArrayList
    $add = { param($t) [void]$out.Add([string]$t) }
    & $add ("Service diagnostics for $Name on $env:COMPUTERNAME, " + (Get-Date).ToString('s'))
    & $add '--- sc.exe queryex / qc / qfailure'
    foreach ($v in 'queryex', 'qc', 'qfailure') { try { & sc.exe $v $Name 2>&1 | Where-Object { "$_".Trim() } | ForEach-Object { & $add ('  ' + "$_".Trim()) } } catch { & $add "  sc $v failed: $($_.Exception.Message)" } }
    & $add '--- Service Control Manager events about it (System log)'
    try {
        $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Service Control Manager'; StartTime = (Get-Date).AddHours(-$Hours) } -ErrorAction Stop | Where-Object { $_.Message -match [regex]::Escape($Name) -or $_.Message -match 'Inference Gateway' } | Select-Object -First 40)
        if (-not $ev.Count) { & $add '  none' }
        foreach ($e in $ev) { & $add ('  {0:s} id {1}: {2}' -f $e.TimeCreated, $e.Id, ($e.Message -replace '\s+', ' ')) }
    } catch { & $add "  none or unreadable: $($_.Exception.Message)" }
    & $add '--- Application errors for python.exe (Application log)'
    try {
        $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; StartTime = (Get-Date).AddHours(-$Hours); Level = 1, 2 } -ErrorAction Stop | Where-Object { $_.Message -match 'python' } | Select-Object -First 20)
        if (-not $ev.Count) { & $add '  none' }
        foreach ($e in $ev) { & $add ('  {0:s} {1} id {2}: {3}' -f $e.TimeCreated, $e.ProviderName, $e.Id, (($e.Message -replace '\s+', ' ') -replace '^(.{600}).*$', '$1 ...')) }
    } catch { & $add "  none or unreadable: $($_.Exception.Message)" }
    foreach ($f in 'logs\gateway-fault.log', 'logs\gateway-service.log') {
        $p = Join-Path $DataRoot $f; & $add "--- $p (last 60 lines)"
        if (Test-Path -LiteralPath $p) { Get-Content -LiteralPath $p -Tail 60 -ErrorAction SilentlyContinue | ForEach-Object { & $add ('  ' + $_) } } else { & $add '  absent' }
    }
    $svc = Get-CimInstance Win32_Service -Filter ("Name='{0}'" -f $Name) -ErrorAction SilentlyContinue
    $sid = $(if ($svc -and $svc.StartName) { Resolve-LabSid ([string]$svc.StartName) } else { '' })
    & $add ("--- account {0} ({1}): profile" -f $(if ($svc) { $svc.StartName } else { '?' }), $sid)
    $prof = $(if ($sid) { Get-CimInstance Win32_UserProfile -Filter ("SID='{0}'" -f $sid) -ErrorAction SilentlyContinue } else { $null })
    & $add $(if ($prof) { '  {0}, loaded {1}, last used {2}' -f $prof.LocalPath, $prof.Loaded, $prof.LastUseTime } else { '  no profile yet (the account has never started)' })
    & $add '--- permissions on what the service opens before it connects'
    $rel = @(Get-ChildItem -LiteralPath (Join-Path $InstallRoot 'releases') -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike '*.staging' } | Sort-Object Name -Descending | Select-Object -First 1)
    $targets = @($Python, $(if ($rel) { Join-Path $rel[0].FullName 'inference_gateway\service_launcher.py' }), (Join-Path $DataRoot 'config\gateway-service.json'), (Join-Path $DataRoot 'state\capacity-ledger.db'), (Join-Path $DataRoot 'tls\server.pem'), (Join-Path $DataRoot 'tls\server.key'), (Join-Path $DataRoot 'logs')) | Where-Object { $_ }
    foreach ($t in $targets) { & $add "  $t"; if (Test-Path -LiteralPath $t) { & icacls.exe $t 2>&1 | Where-Object { "$_".Trim() -and "$_" -notmatch '^Successfully processed' } | ForEach-Object { & $add ('    ' + "$_".Trim()) } } else { & $add '    absent' } }
    return @($out | ForEach-Object { Protect-LabText $_ })
}

function Save-LabGatewayDiagnostics {
    <#  Writes Get-LabServiceDiagnostics into the run folder and onto the USB; returns where.  #>
    param([Parameter(Mandatory)]$Run, [string]$Name = 'InferenceGateway', [string]$Python = '')
    $lines = Get-LabServiceDiagnostics -Name $Name -Python $Python
    $file = Join-Path $Run.Folder 'gateway-service-diagnostics.txt'
    [IO.File]::WriteAllLines($file, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
    $where = $file
    if ($Run.UsbRoot) { try { $d = Join-Path $Run.UsbRoot ('reports\' + $env:COMPUTERNAME); New-Item -ItemType Directory -Path $d -Force | Out-Null; $u = Join-Path $d ($Run.Id + '-gateway-service-diagnostics.txt'); Copy-Item -LiteralPath $file -Destination $u -Force; $where = "$file (copied to $u)" } catch { } }
    $scm = @($lines | Where-Object { $_ -match '^\s+\S+ id (7000|7009|7011|7023|7024|7031|7034|7038|7041):' } | Select-Object -First 3)
    return [pscustomobject]@{ Where = $where; Summary = ($scm -join ' | ') }
}

function Get-LabGatewayService {
    param([string]$Name = 'InferenceGateway')
    $s = Get-CimInstance Win32_Service -Filter ("Name='{0}'" -f $Name) -ErrorAction SilentlyContinue
    if (-not $s) { return [pscustomobject]@{ Present = $false; State = ''; StartMode = ''; Delayed = $null; Account = ''; Release = '' } }
    # The release the service runs: its command line names <install_root>\releases\<version>\... (0.3.0 on DESKTOP-REDACT10).
    $rel = ''; $m = [regex]::Match([string]$s.PathName, '\\releases\\(\d+\.\d+\.\d+)\\'); if ($m.Success) { $rel = $m.Groups[1].Value }
    return [pscustomobject]@{ Present = $true; State = [string]$s.State; StartMode = [string]$s.StartMode; Delayed = [bool]$s.DelayedAutoStart; Account = [string]$s.StartName; Release = $rel }
}

function Get-LabGatewayUpdateVerdict {
    <#  Pure - unit tested. Gateway 0.4.0's "update" output (DeploymentManager.update): the ledger must be kept (same
        ledger_id before and after); the heads it holds at upgrade are listed for the operator.  #>
    param($Json)
    if (-not $Json) { return [pscustomobject]@{ Ok = $false; Kept = $false; Held = @(); Detail = 'update printed no result' } }
    $kept = [bool]($Json.ledger -and $Json.ledger.kept -eq $true)
    $held = @(@($Json.held_at_upgrade) | Where-Object { $_ } | ForEach-Object { '{0} ({1} {2})' -f $_.resource_id, $_.failed_model, $_.failed_operation })
    $d = 'Gateway {0} -> {1}; ledger {2} {3}; backup {4}; held at upgrade: {5}' -f $Json.from, $Json.to, $(if ($kept) { 'kept' } else { 'NOT KEPT' }), $Json.ledger.after, $(if ($Json.backup) { $Json.backup.backup } else { '?' }), $(if ($held.Count) { $held -join ', ' } else { 'none' })
    return [pscustomobject]@{ Ok = ([bool]$Json.ok -and $kept); Kept = $kept; Held = $held; Detail = $d }
}

function New-LabGatewayListenerRuleSpec {
    <#  Pure. The rule exactly as manifest listener.firewall_rule declares it, or the separate cutover rule.  #>
    param([Parameter(Mandatory)]$Manifest, [switch]$Cutover, [string]$Until = '')
    $l = $Manifest.listener; $fr = $l.firewall_rule
    $remote = @($l.allowed_clients); $name = [string]$fr.name; $display = ('Inference Gateway HTTPS {0} from {1}' -f $l.port, ($remote -join ', '))
    if ($Cutover) { $remote = @($l.cutover_clients); $name = $name + '-cutover'; $display = ('Inference Gateway HTTPS {0} from {1} (CUTOVER until {2})' -f $l.port, ($remote -join ', '), $Until) }
    if ($remote -contains 'Any' -or -not $remote.Count) { throw 'refused: the listener rule must name its clients' }
    return [pscustomobject][ordered]@{ Name = $name; DisplayName = $display; Group = 'Inference Gateway (Agent Arena Lab)'; Direction = 'Inbound'; Action = 'Allow'; Protocol = [string]$fr.protocol
        LocalPort = [string]$fr.local_port; LocalAddress = [string]$fr.local_address; RemoteAddress = $remote; Profile = [string]$fr.profile; Enabled = 'True'
        Description = $(if ($Cutover) { "Temporary: approved Capstone migration access until $Until (Gateway open item O2). Remove when the cutover ends." } else { 'Inference Gateway client listener: Capstone (.201) and Coder (.202) only (Gateway deployment-manifest.json).' }) }
}

function Set-LabGatewayListenerRule {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Spec)
    $b = Get-LabFirewallBackend
    $snap = Get-LabFirewallSnapshot
    $have = @($snap.Rules | Where-Object { $_.Name -eq $Spec.Name }) | Select-Object -First 1
    if ($have) {
        $same = (((@($have.RemoteAddresses) | Sort-Object) -join ',') -eq ((@($Spec.RemoteAddress) | Sort-Object) -join ',')) -and ((@($have.LocalPorts) -join ',') -eq $Spec.LocalPort) -and ((@($have.LocalAddresses) -join ',') -eq $Spec.LocalAddress) -and $have.Enabled -and ((@($have.Profiles) -join ',') -eq $Spec.Profile)
        if ($same) { return (New-LabOutcome 'UNCHANGED' ("$($Spec.Name) already admits only " + ($Spec.RemoteAddress -join ', '))) }
        throw ("a rule named $($Spec.Name) exists with different settings; review it (it was not changed)")
    }
    $p = @{}; foreach ($k in 'Name', 'DisplayName', 'Group', 'Direction', 'Action', 'Protocol', 'LocalPort', 'LocalAddress', 'RemoteAddress', 'Profile', 'Enabled', 'Description') { $p[$k] = $Spec.$k }
    & $b.NewRule $p
    Register-LabChange -Kind 'firewall' -Description ("added '{0}' (TCP {1} on {2} from {3}, {4} profile)" -f $Spec.DisplayName, $Spec.LocalPort, $Spec.LocalAddress, ($Spec.RemoteAddress -join ', '), $Spec.Profile) -Undo ([pscustomobject]@{ type = 'firewall'; action = 'remove'; name = $Spec.Name }) | Out-Null
    return (New-LabOutcome 'DONE' ("$($Spec.Name): TCP $($Spec.LocalPort) from " + ($Spec.RemoteAddress -join ', ')))
}

function Get-LabListenerExposure {
    <#  Who the hub's firewall admits to the Gateway listener (rule analysis).  #>
    param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)]$Manifest, [string]$InterfaceAlias = '', $Config = $null)
    $sources = @($Manifest.listener.allowed_clients) + @($Manifest.listener.cutover_clients) + @('192.0.2.50', '192.0.2.3')
    return (Get-LabOllamaExposure -Snapshot $Snapshot -HeadIp $Manifest.listener.bind -Sources $sources -InterfaceAlias $InterfaceAlias -ServerPaths @() -Port ([int]$Manifest.listener.port) -Config $Config)
}

function Test-LabGatewayHealth {
    <#  /health and /ready over TLS, trusting only the Gateway CA. Service identity from /health.  #>
    param([Parameter(Mandatory)][string]$BaseUrl, [Parameter(Mandatory)][string]$CaFile, [string]$ExpectedServiceId = 'shared-inference', [int]$TimeoutMs = 8000)
    if (-not (Test-Path -LiteralPath $CaFile)) { return [pscustomobject]@{ Ok = $false; Health = $null; Ready = $null; Detail = "no CA file at $CaFile" } }
    $out = @{}
    foreach ($path in '/health', '/ready') {
        $status = 0; $err = ''; $cert = ''
        $body = $null
        try { $body = [LabTls]::Get($BaseUrl.TrimEnd('/') + $path, $CaFile, $TimeoutMs, [ref]$status, [ref]$err, [ref]$cert) }
        catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; $err = 'the CA file cannot be read as a certificate: ' + $e.Message }
        $j = $null; try { if ($body) { $j = $body | ConvertFrom-Json } } catch { }
        $out[$path] = [pscustomobject]@{ Status = $status; Error = $err; Certificate = $cert; Json = $j }
    }
    $h = $out['/health']; $r = $out['/ready']
    $sid = ''; if ($h.Json -and $h.Json.PSObject.Properties.Name -contains 'service_id') { $sid = [string]$h.Json.service_id }
    $ok = ($h.Status -eq 200 -and $sid -eq $ExpectedServiceId -and $r.Status -eq 200)
    $detail = $(if ($h.Error) { 'TLS refused: ' + $h.Error } else { '/health {0} service {1}; /ready {2}; certificate {3}' -f $h.Status, $sid, $r.Status, $h.Certificate })
    return [pscustomobject]@{ Ok = $ok; Health = $h; Ready = $r; ServiceId = $sid; Detail = $detail }
}

# ---------------------------------------------------------------------------
# Policy identities (the reviewed policy is a public file: no credentials in it)
# ---------------------------------------------------------------------------

function Get-LabModelChoices {
    <#  Pure - unit tested. The chat/generation models installed on the verified heads, each with the heads
        that have it, sorted by name. Embedding-only models are left out: the Gateway qualifies policy
        models for completion. -Known lists models that cannot pass on these heads, with the reason.  #>
    param([Parameter(Mandatory)]$Inventory, [hashtable]$Known = @{ 'qwen3:4b' = 'cannot pass the Gateway''s check here: it reasons before answering' })
    $by = [ordered]@{}
    foreach ($h in @($Inventory.heads)) {
        $n = ([string]$h.resource_id -replace '^head-192-0-2-', '.')
        foreach ($m in @($h.verified.models)) {
            $name = [string]$m.name
            if (-not $name -or $name -match '(?i)embed|minilm') { continue }
            if (-not $by.Contains($name)) { $by[$name] = New-Object Collections.ArrayList }
            [void]$by[$name].Add($n)
        }
    }
    return @($by.Keys | Sort-Object | ForEach-Object { [pscustomobject][ordered]@{ Name = $_; Heads = @($by[$_]); Note = $(if ($Known.ContainsKey($_)) { $Known[$_] } else { '' }) } })
}

function ConvertFrom-LabModelSelection {
    <#  Pure - unit tested. "1,3, 5" or "1 3 5" against the numbered choices -> model names, in menu order.
        Returns Ok $false with a reason for anything else.  #>
    param([string]$Answer, [Parameter(Mandatory)][AllowEmptyCollection()][array]$Choices)
    $parts = @(([string]$Answer) -split '[,;\s]+' | Where-Object { $_ })
    if (-not $Choices.Count) { return [pscustomobject]@{ Ok = $false; Models = @(); Reason = 'no chat model is installed on the verified heads' } }
    if (-not $parts.Count) { return [pscustomobject]@{ Ok = $false; Models = @(); Reason = 'no number given' } }
    $picked = @()
    foreach ($p in $parts) {
        if ($p -notmatch '^\d+$' -or [int]$p -lt 1 -or [int]$p -gt $Choices.Count) { return [pscustomobject]@{ Ok = $false; Models = @(); Reason = ("'{0}' is not a number from 1 to {1}" -f $p, $Choices.Count) } }
        $picked += [int]$p
    }
    $models = @($picked | Sort-Object -Unique | ForEach-Object { [string]$Choices[$_ - 1].Name })
    return [pscustomobject]@{ Ok = $true; Models = $models; Reason = '' }
}

function New-LabPolicyWithModels {
    <#  Pure - unit tested. The approved policy with these models for capstone, and for capstone-research
        the ones it already had that are still chosen (else the first chosen). Every other setting
        (concurrency, queues, priorities, credentials) is kept. With no current policy, the clients come
        from the Gateway release's template, without coder (open item O5).  #>
    param($Policy = $null, [Parameter(Mandatory)][string[]]$Models, $Template = $null)
    $base = $(if ($Policy) { $Policy } elseif ($Template) { $Template } else { throw 'no policy and no template' })
    $p = $base | ConvertTo-Json -Depth 10 | ConvertFrom-Json
    $p.clients = @(@($p.clients) | Where-Object { [string]$_.app_id -in 'capstone', 'capstone-research' })
    foreach ($c in @($p.clients)) {
        # [object[]] keeps a one-model list a JSON array: "$(if ...)" unrolled it to a string, and the Gateway
        # stored capstone-research's models as "qwen3.5:4b" (rehearsal, 2026-11-06).
        if ($c.app_id -eq 'capstone') { $keep = @($Models) }
        else { $keep = @(@($c.models) | Where-Object { $Models -contains $_ }); if (-not $keep.Count) { $keep = @($Models[0]) } }
        $c.models = [object[]]$keep
    }
    if (-not @($p.clients).Count) { throw 'the policy has neither capstone nor capstone-research' }
    return $p
}

function Test-LabGatewayPolicyIdentities {
    <#  Pure. capstone and capstone-research are separate clients; research never inherits operational
        privilege: lower priority, max_concurrency 1, its own provisioned credential.  #>
    param([Parameter(Mandatory)]$Policy)
    $clients = @{}; foreach ($c in @($Policy.clients)) { $clients[[string]$c.app_id] = $c }
    $findings = @(); $notes = @()
    foreach ($id in 'capstone', 'capstone-research') { if (-not $clients.ContainsKey($id)) { $findings += "no client '$id' in the policy" } }
    if ($clients.ContainsKey('capstone') -and $clients.ContainsKey('capstone-research')) {
        $op = $clients['capstone']; $rs = $clients['capstone-research']
        if ([int]$rs.priority -ge [int]$op.priority) { $findings += ('capstone-research priority {0} is not below capstone''s {1}' -f $rs.priority, $op.priority) }
        # The Gateway's contract says research "should" stay at 1 and that the operator sets it: running work is never
        # preempted, so research holding N heads leaves operational work the rest. The operator's call (2026-11-12).
        if ([int]$rs.max_concurrency -gt 1) { $notes += "capstone-research may hold $($rs.max_concurrency) heads at once (the Gateway's contract suggests 1: running research is never preempted by tickets)" }
        foreach ($c in $op, $rs) { if ([string]$c.credential -ne 'provisioned') { $findings += ("client {0} credential is '{1}', not 'provisioned'" -f $c.app_id, $c.credential) } }
        $extraModels = @(@($rs.models) | Where-Object { @($op.models) -notcontains $_ })
        if ($extraModels.Count) { $notes += ('capstone-research has models capstone does not: ' + ($extraModels -join ', ') + ' (allowed, but review)') }
        if (@($op.models) -contains 'REPLACE_WITH_APPROVED_MODEL' -or @($rs.models) -contains 'REPLACE_WITH_APPROVED_MODEL') { $findings += 'the policy still holds template placeholders (REPLACE_WITH_APPROVED_MODEL)' }
    }
    if ($clients.ContainsKey('coder')) { $notes += 'coder is in the policy; its credential is issued only when Coder is ready (open item O5)' }
    return [pscustomobject]@{ Ok = ($findings.Count -eq 0); Findings = $findings; Notes = $notes; Clients = @($clients.Keys | Sort-Object) }
}

function Export-LabGatewayCa {
    <#  The Gateway CA certificate is public (contract section 2): copied to the USB for the clients, with its hash.  #>
    param([Parameter(Mandatory)][string]$CaFile, [Parameter(Mandatory)][string]$UsbRoot)
    $dest = Join-Path $UsbRoot 'gateway-ca'
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    $target = Join-Path $dest ([IO.Path]::GetFileName($CaFile))
    Copy-Item -LiteralPath $CaFile -Destination $target -Force
    $h = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($target + '.sha256', ("{0}  {1}`r`n" -f $h, [IO.Path]::GetFileName($target)), (New-Object Text.ASCIIEncoding))
    return [pscustomobject]@{ Path = $target; Sha256 = $h }
}

# ---------------------------------------------------------------------------
# Machine-wide runtimes (manifest "runtime"): Python 3.12.10 and PowerShell 7.4+
# ---------------------------------------------------------------------------

function Test-LabRuntimeInstaller {
    <#  The installer file against its publisher's published hash and its Authenticode signer.  #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Spec)
    $problems = @()
    if (-not (Test-Path -LiteralPath $Path)) { return [pscustomobject]@{ Ok = $false; Problems = @("missing: $Path") } }
    if ($Spec.PSObject.Properties.Name -contains 'md5_published' -and (Get-FileHash -LiteralPath $Path -Algorithm MD5).Hash.ToLowerInvariant() -ne $Spec.md5_published) { $problems += "MD5 differs from the value published at $($Spec.md5_source)" }
    $want = $(if ($Spec.PSObject.Properties.Name -contains 'sha256_published') { $Spec.sha256_published } else { $Spec.sha256 })
    if ($want -and (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $want) { $problems += 'SHA-256 differs from the recorded value' }
    $sig = Get-AuthenticodeSignature -FilePath $Path
    if ($sig.Status -ne 'Valid') { $problems += "signature $($sig.Status)" } elseif ([string]$sig.SignerCertificate.Subject -notmatch [regex]::Escape($Spec.publisher)) { $problems += "signed by $($sig.SignerCertificate.Subject), not $($Spec.publisher)" }
    return [pscustomobject]@{ Ok = ($problems.Count -eq 0); Problems = $problems }
}

function Get-LabRuntimeState {
    param([Parameter(Mandatory)]$Config)
    $py = $Config.runtime.python; $ps = $Config.runtime.pwsh
    $pyv = ''; $base = $false
    # No quote characters in the probe: Windows PowerShell 5.1 strips inner double quotes from a native
    # argument (1.3.1 printed a syntax error here, so an installed 3.12.10 read as missing).
    if (Test-Path -LiteralPath $py.path) { try { $o = [string](& $py.path -I -c 'import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2], int(sys.prefix == sys.base_prefix))' 2>$null | Select-Object -First 1); if ($o -match '^(\d+) (\d+) (\d+) ([01])$') { $pyv = '{0}.{1}.{2}' -f $Matches[1], $Matches[2], $Matches[3]; $base = ($Matches[4] -eq '1') } } catch { } }
    $psv = ''
    if (Test-Path -LiteralPath $ps.path) { try { $psv = [string](& $ps.path -NoProfile -NonInteractive -Command '$PSVersionTable.PSVersion.ToString()' 2>$null) } catch { } }
    $psOk = $false; if ($psv -match '^(\d+)\.(\d+)') { $psOk = ([version]("{0}.{1}" -f $Matches[1], $Matches[2]) -ge [version]$ps.minimum) }
    return [pscustomobject]@{ Python = [pscustomobject]@{ Path = $py.path; Version = $pyv; Base = $base; Ok = ($pyv -eq $py.version -and $base) }
                              Pwsh = [pscustomobject]@{ Path = $ps.path; Version = $psv; Ok = $psOk } }
}

function Install-LabRuntime {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Config, [Parameter(Mandatory)][ValidateSet('python', 'pwsh')][string]$Kind, [Parameter(Mandatory)][string]$PackageDir)
    $spec = $Config.runtime.$Kind
    $state = Get-LabRuntimeState -Config $Config
    if (($Kind -eq 'python' -and $state.Python.Ok) -or ($Kind -eq 'pwsh' -and $state.Pwsh.Ok)) { return (New-LabOutcome 'UNCHANGED' ('{0} {1} at {2}' -f $Kind, $(if ($Kind -eq 'python') { $state.Python.Version } else { $state.Pwsh.Version }), $spec.path)) }
    $file = Join-Path $PackageDir $spec.file
    $check = Test-LabRuntimeInstaller -Path $file -Spec $spec
    if (-not $check.Ok) { throw ("{0} installer refused: {1}" -f $spec.file, ($check.Problems -join '; ')) }
    # Every installer writes its own log into the run folder; on failure the report quotes its error lines
    # and the logs are copied to the USB (1.3.2 reported only "exited 1603").
    $logDir = Join-Path $Run.Folder 'installer-logs'
    if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    $logBase = [IO.Path]::GetFileNameWithoutExtension($spec.file) + '-install'
    $log = Join-Path $logDir ($logBase + '.log')
    $note = ''
    if ($Kind -eq 'python') {
        # Runs as SYSTEM (DESKTOP-REDACT10, 1.3.2: exit 1603). The installer run as the operator saw the operator's
        # own per-user Python 3.12.10 (LabOperator1 has one) and tried to modify it instead of installing for
        # all users. SYSTEM has no per-user Python, and other accounts' per-user copies (LabOperator1's,
        # ArenaSvcUser's) are left as they are. The installer is copied off the USB first and checked again.
        $stage = Join-Path $Run.Folder 'staging\runtime'
        if (-not (Test-Path -LiteralPath $stage)) { New-Item -ItemType Directory -Path $stage -Force | Out-Null }
        $local = Join-Path $stage $spec.file
        Copy-Item -LiteralPath $file -Destination $local -Force
        $check = Test-LabRuntimeInstaller -Path $local -Spec $spec
        if (-not $check.Ok) { throw ("{0} installer refused after copying it to {1}: {2}" -f $spec.file, $stage, ($check.Problems -join '; ')) }
        $mine = @(Get-LabPerUserPython -Series ((([string]$spec.version) -split '\.')[0..1] -join '.'))
        if ($mine.Count) { $note = ('; this account''s own Python left as it is: ' + ($mine -join ', ')); Write-LabLog ('  this account has its own Python ({0}); the machine-wide install runs as SYSTEM so it cannot collide with it' -f ($mine -join ', ')) }
        Write-LabLog ('  running {0} as SYSTEM: {1}' -f $spec.file, $spec.arguments)
        $code = Invoke-LabProcessAsSystem -FilePath $local -Arguments ('{0} /log "{1}"' -f $spec.arguments, $log)
    }
    else {
        $code = (Start-Process -FilePath (Join-Path $env:WINDIR 'System32\msiexec.exe') -ArgumentList @('/i', ('"{0}"' -f $file), '/qn', '/norestart', '/l*v', ('"{0}"' -f $log)) -Wait -PassThru).ExitCode
    }
    if ($code -notin 0, 3010) {
        $logs = @(Get-ChildItem -LiteralPath $logDir -Filter ($logBase + '*.log') -ErrorAction SilentlyContinue)
        $lines = @(foreach ($l in $logs) { Get-Content -LiteralPath $l.FullName -ErrorAction SilentlyContinue })
        $why = @(Get-LabInstallerLogSummary -Lines $lines)
        $where = $logDir
        if ($Run.UsbRoot -and $logs.Count) {
            try { $usb = Join-Path $Run.UsbRoot ('reports\{0}\{1}-installer-logs' -f $env:COMPUTERNAME, $Run.Id); New-Item -ItemType Directory -Path $usb -Force | Out-Null; $logs | Copy-Item -Destination $usb -Force; $where = "$logDir (copied to $usb)" } catch { }
        }
        $meaning = Get-LabInstallerExitMeaning -Code $code
        throw ('{0} installer exited {1}{2}{3}. Installer log: {4}{5}' -f $spec.file, $code, $(if ($meaning) { " ($meaning)" } else { '' }), $(if ($why.Count) { '. Its log says: ' + ($why -join ' | ') } else { '' }), $where, $note)
    }
    $state = Get-LabRuntimeState -Config $Config
    $ok = $(if ($Kind -eq 'python') { $state.Python.Ok } else { $state.Pwsh.Ok })
    if (-not $ok) { throw ("{0} did not verify after install: {1}" -f $Kind, $(if ($Kind -eq 'python') { "version '$($state.Python.Version)' base $($state.Python.Base)" } else { "version '$($state.Pwsh.Version)'" })) }
    $how = $(if ($Kind -eq 'python') { "Apps > Python 3.12.10 > Uninstall, or `"$($spec.file)`" /uninstall /quiet" } else { "msiexec /x `"$($spec.file)`" /qn" })
    Register-LabChange -Kind 'runtime' -Description ("installed {0} {1} machine-wide at {2}" -f $Kind, $spec.version, $spec.path) -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("only after the Gateway is uninstalled: " + $how) }) | Out-Null
    return (New-LabOutcome 'DONE' ("{0} {1} installed at {2} (exit {3}{4}){5}" -f $Kind, $spec.version, $spec.path, $code, $(if ($Kind -eq 'python') { ', as SYSTEM' } else { '' }), $note))
}

function Get-LabPerUserPython {
    <#  This account's own (per-user) Python installs of one series, from its registry, as "3.12 at <path>".
        Reported only: the machine-wide install runs as SYSTEM so that it cannot collide with them.  #>
    param([string]$Series = '3.12')
    if (-not $Series) { $Series = '3.12' }
    $out = @()
    foreach ($tag in @($Series, ($Series + '-32'))) {
        $k = "HKCU:\Software\Python\PythonCore\$tag\InstallPath"
        if (Test-Path -LiteralPath $k) { $p = [string](Get-Item -LiteralPath $k).GetValue(''); if ($p) { $out += ('{0} at {1}' -f $tag, $p.TrimEnd('\')) } }
    }
    return $out
}

function Get-LabInstallerExitMeaning {
    <#  Pure - unit tested. The Windows Installer exit codes an operator meets, in words.  #>
    param([long]$Code)
    switch ($Code) {
        1602 { return 'cancelled' }
        1603 { return 'fatal error during installation' }
        1618 { return 'another installation is already running: wait for Windows Update or the other installer to finish, then run again' }
        1638 { return 'another version of this product is already installed' }
        1641 { return 'a restart was started' }
        3010 { return 'success, a restart is required' }
        default { return '' }
    }
}

function Get-LabInstallerLogSummary {
    <#  Pure - unit tested. The lines of a Burn bundle log or an msiexec /l*v log that say why an install
        failed, without their [process:thread][time] prefixes, de-duplicated, at most $Max.  #>
    param([string[]]$Lines = @(), [int]$Max = 6)
    $pat = '(?i)(error 0x[0-9a-f]{8}|\bError \d{4}\b|Return value 3|already installed|another version|0x80070643|Failed to (install|configure|apply|execute))'
    $hits = @($Lines | Where-Object { $_ -and $_ -match $pat } | ForEach-Object { ((($_ -replace '^(\[[^\]]*\])+\s*', '') -replace '^\w\d{3}:\s*', '') -replace '^MSI \([sc]\) \([0-9A-F:]+\) \[[0-9:]+\]:\s*', '').Trim() } | Where-Object { $_ } | Select-Object -Unique)
    return @($hits | Select-Object -First $Max)
}

function Invoke-LabProcessAsSystem {
    <#  Runs one program as LOCAL SYSTEM through a one-time scheduled task, waits, removes the task and
        returns the program's exit code. -AsCurrentUser runs it as the caller instead (tests).  #>
    param([Parameter(Mandatory)][string]$FilePath, [string]$Arguments = '', [int]$TimeoutSeconds = 1200, [switch]$AsCurrentUser)
    $name = 'AgentArenaLab-oneshot-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $actArgs = @{ Execute = $FilePath; WorkingDirectory = (Split-Path -Parent $FilePath) }
    if ($Arguments) { $actArgs.Argument = $Arguments }
    $action = New-ScheduledTaskAction @actArgs
    $principal = $(if ($AsCurrentUser) { New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited } else { New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest })
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds $TimeoutSeconds) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -TaskPath '\' -Action $action -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
    try {
        Start-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction Stop
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds + 60)
        do {
            Start-Sleep -Milliseconds 1500
            $t = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction Stop
            $r = [long](Get-ScheduledTaskInfo -TaskName $name -TaskPath '\' -ErrorAction Stop).LastTaskResult
            # 267009 = running, 267011 = not run yet (Task Scheduler's own codes, not the program's)
            if ([string]$t.State -ne 'Running' -and $r -ne 267009 -and $r -ne 267011) { return $r }
        } while ((Get-Date) -lt $deadline)
        throw ('{0} did not finish within {1} s (one-time task {2})' -f [IO.Path]::GetFileName($FilePath), $TimeoutSeconds, $name)
    }
    finally { Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# Gateway Models screen (engine\hub-tools\gateway-models): installed on the hub by Administration hub > 5.
# Its folder holds the hub's copy of the approved policy and the Do not use list, which hub > 2 respects.
# ---------------------------------------------------------------------------

$script:ModelsConsoleData = 'C:\ProgramData\AgentArenaLab\gateway\models-console'

function Get-LabDoNotUse {
    <#  The Gateway Models screen's Do not use list as "resource_id|model" strings (empty if none).  #>
    param([string]$Folder = $script:ModelsConsoleData)
    $f = Join-Path $Folder 'do-not-use.json'
    if (-not (Test-Path -LiteralPath $f)) { return @() }
    try { return @(@((Get-Content -LiteralPath $f -Raw | ConvertFrom-Json).entries) | ForEach-Object { '{0}|{1}' -f $_.resource_id, $_.model }) }
    catch { throw "the Gateway Models screen's Do not use list is unreadable ($f): fix or remove it before qualifying" }
}

function Invoke-LabGatewaySwitchBack {
    <#  Back to release $To after a failed update to $From, with the Gateway's own Installer._switch (drains, expires
        leftover leases, points the service at $To, starts it and waits) and the record its update writes when it
        switches back itself. Used because Gateway 0.4.0's update lets StartServiceW's error escape its switch-back.
        Returns Ok, Detail. Writes switch-back.py and its output to $Folder.  #>
    param([string]$Python, [string]$PackageRoot, [string]$Settings, [string]$To, [string]$From, [string]$Folder)
    $code = @'
import json, os, sys
root, settings, to, frm = sys.argv[1:5]
sys.path.insert(0, [os.path.join(root, 'wheel', n) for n in os.listdir(os.path.join(root, 'wheel')) if n.endswith('.whl')][0])
os.environ['INFERENCE_GATEWAY_PACKAGE'] = root
from inference_gateway import deployment as d
inst = d.Installer(d.load_settings(settings), d.WindowsSystem(), package=d.package_root())
ok = inst._switch(to)
if ok: inst._record(action='update_failed_rolled_back', current=to, previous=frm)
print(json.dumps({'ok': bool(ok), 'running': to if ok else None}))
'@
    $f = Join-Path $Folder 'switch-back.py'; [IO.File]::WriteAllText($f, $code, (New-Object Text.UTF8Encoding $false))
    $out = (& $Python -I $f $PackageRoot $Settings $To $From 2>&1 | Out-String).Trim()
    try { [IO.File]::WriteAllText((Join-Path $Folder 'switch-back.txt'), $out, (New-Object Text.UTF8Encoding $false)) } catch { }
    [pscustomobject]@{ Ok = ($out -match '"ok": true'); Detail = $(if ($out.Length -gt 300) { $out.Substring($out.Length - 300) } else { $out }) }
}

function Get-LabQualifyPurpose {
    <#  The Gateway test a model needs: "embedding" (a real /api/embed) for an embedding model, else "completion".
        The same rule as the Gateway Models screen (gateway_models.NOT_CHAT). An embedding model given the text test
        always fails it, and a failed test takes the model off the head (hub, 2026-11-12: all-minilm off all nine).  #>
    param([string]$Model)
    if ($Model -match '(?i)embed|minilm|(^|[/:-])bge|(^|[/:-])gte') { 'embedding' } else { 'completion' }
}

function Get-LabQualifySummary {
    <#  Outcome of a qualification pass. $Results: objects with Resource, Model, Passed. $Clients: the reviewed policy's
        clients (app_id/client_id, models). FAILED only when a client is left with no model passing on any head: a model
        that fails the Gateway's test is listed as not usable on that head, which is the Gateway's verdict, not a fault
        of the run. Pure.  #>
    param([object[]]$Results, [object[]]$Clients)
    $passed = @($Results | Where-Object { $_.Passed } | ForEach-Object { [string]$_.Model } | Select-Object -Unique)
    $stranded = @(foreach ($c in @($Clients)) {
        $id = [string]$(if ($c.PSObject.Properties.Name -contains 'app_id') { $c.app_id } elseif ($c.PSObject.Properties.Name -contains 'client_id') { $c.client_id } else { $c.id })
        $want = @(@($c.models) | Where-Object { $_ })
        if ($want.Count -and -not @($want | Where-Object { $passed -contains $_ }).Count) { '{0} ({1})' -f $id, ($want -join ', ') }
    })
    $failed = @($Results | Where-Object { -not $_.Passed })
    [pscustomobject]@{
        Status   = $(if (-not @($Results).Count) { 'PENDING' } elseif ($stranded.Count) { 'FAILED' } else { 'DONE' })
        Stranded = $stranded
        Passed   = @($Results).Count - $failed.Count
        Failed   = $failed.Count
    }
}

function Get-LabHubApprovedPolicy {
    <#  The approved policy the Gateway Models screen last applied on this hub, or $null. It is newer than the USB
        copy whenever it exists, because the screen writes it only after the Gateway accepted the change.  #>
    param([string]$Folder = $script:ModelsConsoleData)
    $f = Join-Path $Folder 'gateway-policy-approved.json'
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    return [pscustomobject]@{ Path = $f; Policy = (Get-Content -LiteralPath $f -Raw | ConvertFrom-Json); Written = (Get-Item -LiteralPath $f).LastWriteTime }
}

function Test-LabNoUserWrite {
    <#  Pure on an ACL - unit tested. $true when no standard principal (Users, Authenticated Users, Everyone,
        INTERACTIVE, or one of -Extra) may write, modify, delete or change permissions. The screen runs elevated,
        so a standard account that could change its files could run code as administrator.  #>
    param([Parameter(Mandatory)]$Acl, [string[]]$Extra = @())
    $standard = @('S-1-5-32-545', 'S-1-5-11', 'S-1-1-0', 'S-1-5-4') + @($Extra)
    # Write-only bits (Modify would also match read): WriteData 2, AppendData 4, WriteExtendedAttributes 16,
    # DeleteSubdirectoriesAndFiles 64, WriteAttributes 256, Delete 65536, ChangePermissions 262144,
    # TakeOwnership 524288, GENERIC_ALL 0x10000000, GENERIC_WRITE 0x40000000.
    $write = 2 + 4 + 16 + 64 + 256 + 65536 + 262144 + 524288 + 0x10000000 + 0x40000000
    foreach ($r in @($Acl.Access)) {
        if ([string]$r.AccessControlType -ne 'Allow') { continue }
        if ([string]$r.PropagationFlags -match 'InheritOnly') { continue }     # applies to children only; they are checked themselves
        $sid = $r.IdentityReference; try { $sid = $r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $sid = [string]$r.IdentityReference }
        if ($standard -notcontains $sid) { continue }
        if (([int]$r.FileSystemRights -band $write) -ne 0) { return $false }
    }
    return $true
}

function Install-LabModelsConsole {
    <#  Copies the screen from this USB to Program Files (administrators write; users read), checks every file
        against the USB copy, removes files an older version left, restricts its settings folder to SYSTEM and
        Administrators, puts a "Gateway Models" shortcut on this administrator's desktop, then runs the screen's
        own self-check, which reads the real Gateway once (observe and credentials list, read-only).  #>
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Python, $Run = $null,
          [string]$Target = 'C:\Program Files\AgentArenaLab\GatewayModels', [string]$DataDir = $script:ModelsConsoleData,
          [string]$Desktop = [Environment]::GetFolderPath('Desktop'), [string[]]$ArenaSids = @(), [string[]]$TrustedOwners = @('S-1-5-32-544', 'S-1-5-18'))
    $src = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
    $files = @(Get-ChildItem -LiteralPath $src -Recurse -File | Where-Object { $_.FullName -notmatch '\\__pycache__\\' })
    if (-not @($files | Where-Object Name -eq 'gateway_models.py').Count) { throw "no gateway_models.py in $src" }
    # The Fleet Commit helper (fleet_commit.py, in this folder) also needs the model library, kept beside it so it
    # loads only from this administrators-only folder.
    $lib = Join-Path (Split-Path -Parent $src) 'model-library\model_library.py'
    $relOf = @{}
    if (Test-Path -LiteralPath $lib) { $files += Get-Item -LiteralPath $lib; $relOf[(Get-Item -LiteralPath $lib).FullName] = 'model_library.py' }
    # Lab Traffic publishes to Arena with Arena's own client (standard library): carried from the stick's hub payload,
    # or from the repository in tests, into arena_service\ beside the tool.
    $releaseRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $src))
    $arenaDir = @((Join-Path $releaseRoot 'payload\AGENT-ARENA-HUB\app\arena_service'), (Join-Path (Split-Path -Parent $releaseRoot) 'arena_service')) | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'client.py') } | Select-Object -First 1
    if ($arenaDir) { foreach ($n in '__init__.py', 'client.py', 'crypto.py', 'secretfile.py') { $it = Get-Item -LiteralPath (Join-Path $arenaDir $n); $files += $it; $relOf[$it.FullName] = 'arena_service\' + $n } }
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    # First, so a refusal changes nothing. Under ProgramData any account may create folders: one made beforehand by a standard account is refused,
    # because its owner could open its permissions again after they are restricted.
    $owner = (Get-Acl -LiteralPath $DataDir).Owner
    try { $owner = ([Security.Principal.NTAccount]$owner).Translate([Security.Principal.SecurityIdentifier]).Value } catch { }
    if ($TrustedOwners -notcontains $owner) { throw ("{0} belongs to {1}, not to Administrators: delete that folder (as administrator) and run this again" -f $DataDir, (Get-Acl -LiteralPath $DataDir).Owner) }
    $fresh = -not (Test-Path -LiteralPath $Target)
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    $copied = 0; $want = @{}
    foreach ($f in $files) {
        $rel = $(if ($relOf.ContainsKey($f.FullName)) { $relOf[$f.FullName] } elseif ($f.FullName.StartsWith($src + '\', [StringComparison]::OrdinalIgnoreCase)) { $f.FullName.Substring($src.Length + 1) } else { $f.Name }); $want[$rel.ToLowerInvariant()] = $true
        $dst = Join-Path $Target $rel
        $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
        if (-not (Test-Path -LiteralPath $dst) -or (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash -ne $h) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
            Copy-Item -LiteralPath $f.FullName -Destination $dst -Force; $copied++
        }
        if ((Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash -ne $h) { throw "copy check failed: $rel" }
    }
    $removed = 0
    foreach ($old in @(Get-ChildItem -LiteralPath $Target -Recurse -File)) {
        $rel = $old.FullName.Substring($Target.TrimEnd('\').Length + 1)
        if (-not $want.ContainsKey($rel.ToLowerInvariant())) { Remove-Item -LiteralPath $old.FullName -Force; $removed++ }
    }
    foreach ($p in @($Target) + @(Get-ChildItem -LiteralPath $Target -Recurse | ForEach-Object FullName)) {
        if (-not (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $p) -Extra $ArenaSids)) { throw "a standard account may change $p; the screen runs as administrator, so it is refused (check the Program Files permissions)" }
    }
    # SYSTEM and Administrators only, inheritance removed. Applied when the folder is not exactly that yet; when it
    # is, whatever is inside is checked instead (an older version's files keep the folder's inherited ACL).
    $dacl = Get-Acl -LiteralPath $DataDir
    $have = @($dacl.Access | ForEach-Object { try { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { [string]$_.IdentityReference } } | Sort-Object -Unique)
    if (-not ($dacl.AreAccessRulesProtected -and ($have -join '|') -eq 'S-1-5-18|S-1-5-32-544')) {
        Set-LabFolderAcl -Path $DataDir -Grants @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
    }
    else {
        $inside = @(); try { $inside = @(Get-ChildItem -LiteralPath $DataDir -Recurse -Force -ErrorAction Stop | ForEach-Object FullName) } catch { }   # unreadable without elevation: nothing to judge
        foreach ($p in $inside) { if (-not (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $p) -Extra $ArenaSids)) { throw "a standard account may change $p in the screen's settings folder; reset it with: icacls `"$DataDir`" /reset /T, then run this again" } }
    }
    $lnk = Join-Path $Desktop 'Gateway Models.lnk'
    $sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = (Join-Path $Target 'Gateway Models.cmd'); $s.WorkingDirectory = $Target
    $s.Description = 'Gateway Models: which models each orchestrator may use (asks for administrator rights)'
    $s.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"; $s.Save()
    if (Test-Path -LiteralPath (Join-Path $Target 'Lab Traffic.cmd')) {
        $t = $sh.CreateShortcut((Join-Path $Desktop 'Lab Traffic.lnk'))
        $t.TargetPath = (Join-Path $Target 'Lab Traffic.cmd'); $t.WorkingDirectory = $Target
        $t.Description = 'Lab Traffic: TEST work through the Inference Gateway to the heads, shown in Agent Arena (asks for administrator rights)'
        $t.IconLocation = "$env:SystemRoot\System32\imageres.dll,166"; $t.Save()
    }
    $out = & $Python -I (Join-Path $Target 'gateway_models.py') --self-check 2>&1
    $check = $null; try { $check = (($out | Where-Object { $_ -match '^\{' }) | Select-Object -Last 1) | ConvertFrom-Json } catch { }
    if ($Run) {
        Register-LabChange -Kind 'app' -Description ("Gateway Models screen {0} in {1} ({2} file(s) copied, {3} old removed); shortcut {4}" -f $(if ($fresh) { 'installed' } else { 'updated' }), $Target, $copied, $removed, $lnk) `
            -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("delete {0} and {1}; its settings folder {2} (Do not use list, policy copy, change log) is kept on purpose" -f $Target, $lnk, $DataDir) }) | Out-Null
    }
    if (-not $check -or -not $check.ok) { throw ('the screen is installed, but its self-check failed: ' + $(if ($check) { $check.error } else { ($out | Select-Object -Last 3) -join ' ' })) }
    $orch = (@($check.orchestrators.PSObject.Properties) | ForEach-Object { '{0}: {1}' -f $_.Name, (@($_.Value) -join ', ') }) -join '; '
    return [pscustomobject]@{ Outcome = $(if ($copied -or $removed -or $fresh) { 'DONE' } else { 'UNCHANGED' }); Shortcut = $lnk; Target = $Target; Check = $check
        Detail = ('{0} ({1} copied, {2} removed, every file checked); settings {3} (SYSTEM and Administrators only); shortcut {4}; self-check read Gateway {5}, ledger {6}: {7}; heads {8}' -f $Target, $copied, $removed, $DataDir, $lnk, $check.release, $check.ledger_id, $orch, (@($check.heads) -join ' ')) }
}

function Set-LabHiddenSignInAccount {
    <#  Hides a service account from the Windows sign-in and Switch user screens (Winlogon SpecialAccounts\UserList).
        The account keeps working: the Inference Gateway runs as svc-inference-gw, which may not sign in anyway.  #>
    param([Parameter(Mandatory)][string]$Name, $Run = $null, [string]$Key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList')
    if (-not (Get-LocalUser -Name $Name -ErrorAction SilentlyContinue)) { throw "there is no local account $Name on this computer" }
    $now = $null; try { $now = (Get-ItemProperty -Path $Key -Name $Name -ErrorAction Stop).$Name } catch { }
    if ("$now" -eq '0') { return (New-LabOutcome 'UNCHANGED' "$Name is already hidden from the sign-in screen") }
    New-Item -Path $Key -Force | Out-Null
    New-ItemProperty -Path $Key -Name $Name -Value 0 -PropertyType DWord -Force | Out-Null
    if ($Run) { Register-LabChange -Kind 'account' -Description "$Name hidden from the sign-in screen (it still runs the Gateway)" -Undo ([pscustomobject]@{ type = 'manual'; instructions = "delete the value $Name under $Key" }) | Out-Null }
    return (New-LabOutcome 'DONE' "$Name no longer shows on the sign-in or Switch user screen; the Gateway service still runs as it")
}

function Set-LabFolderAcl {
    <#  A folder's own permissions (inheritance removed, these grants), and everything already inside reset to
        inherit them. Never a folder grant with the recursive switch: that tool then gives each FILE inside an empty access list (the
        folder flags do not apply to files), so nobody can read it (found in rehearsal, 2026-11-07).  #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string[]]$Grants)
    $o = & icacls.exe $Path /inheritance:r /grant:r @($Grants) /C /Q 2>&1
    if ($LASTEXITCODE) { throw ("could not set the permissions of {0}: {1}" -f $Path, ($o -join ' ')) }
    if (@(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue).Count) {
        $o = & icacls.exe (Join-Path $Path '*') /reset /T /C /Q 2>&1
        if ($LASTEXITCODE) { throw ("could not reset the permissions inside {0}: {1}" -f $Path, ($o -join ' ')) }
    }
}

function Test-LabTrustedOwner {
    <#  A folder under ProgramData that a standard account created first (any account may create one there) is
        refused: its owner could open its permissions again after they are restricted.  #>
    param([Parameter(Mandatory)][string]$Path, [string[]]$TrustedOwners = @('S-1-5-32-544', 'S-1-5-18'))
    $owner = (Get-Acl -LiteralPath $Path).Owner
    try { $owner = ([Security.Principal.NTAccount]$owner).Translate([Security.Principal.SecurityIdentifier]).Value } catch { }
    if ($TrustedOwners -notcontains $owner) { throw ("{0} belongs to {1}, not to Administrators: delete that folder (as administrator) and run this again" -f $Path, (Get-Acl -LiteralPath $Path).Owner) }
}

function Get-LabFleetAclPlan {
    <#  Pure - unit tested. The icacls grants for the Fleet folders: the admin hub (Arena's standard accounts) reads
        the catalog, the published state and the Commit status, and may write only its Commit requests; the model
        library is for SYSTEM and Administrators only (the Commit helper serves it to heads while it runs).  #>
    param([Parameter(Mandatory)][string]$FleetDir, [Parameter(Mandatory)][string]$LibraryDir, [string[]]$ArenaSids = @())
    $base = @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
    $req = $base + '*S-1-5-32-545:(OI)(CI)RX' + @($ArenaSids | ForEach-Object { "*${_}:(OI)(CI)M" })
    return @(
        [pscustomobject]@{ Path = $FleetDir; Grants = $base + '*S-1-5-32-545:(OI)(CI)RX'; Note = 'Users read' }
        [pscustomobject]@{ Path = (Join-Path $FleetDir 'requests'); Grants = $req; Note = 'Arena writes its Commit requests' }
        [pscustomobject]@{ Path = $LibraryDir; Grants = $base; Note = 'SYSTEM and Administrators only' })
}

function New-LabHeadInventoryText {
    <#  Pure - unit tested. The Fleet window's head list (schema agent-arena.head-inventory/1) from the provisioner's
        head_inventory: address, computer, CPU, RAM, and the GPU with its VRAM read from its name ("RTX 4060 Ti 16 GB").  #>
    param([object[]]$Inventory = @())
    $heads = foreach ($h in @($Inventory)) {
        $gpu = [string]$h.gpu; $vram = $null
        if ($gpu -match '(\d+(?:\.\d+)?)\s*GB') { $vram = [double]$Matches[1] }
        [ordered]@{ address = [string]$h.address; computer = [string]$h.computer; cpu = [string]$h.cpu; ram_gb = [double]$h.ram_gb; gpu = $(if ($gpu) { $gpu } else { $null }); vram_gb = $vram }
    }
    return ([ordered]@{ schema = 'agent-arena.head-inventory/1'; heads = @($heads) } | ConvertTo-Json -Depth 4)
}

function Install-LabFleet {
    <#  The admin hub's Fleet window and its Commit helper, on the hub: the fleet folder (catalog, published state,
        Commit requests and status) with its permissions, the model library folder, the catalog from this USB, and
        the first published state, written by the helper itself (it reads the Gateway; it changes nothing).  #>
    param([Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$Catalog, $Run = $null,
          [string]$Helper = 'C:\Program Files\AgentArenaLab\GatewayModels\fleet_commit.py',
          [string]$FleetDir = 'C:\ProgramData\AgentArenaLab\fleet', [string]$LibraryDir = 'C:\ProgramData\AgentArenaLab\model-library',
          [string[]]$ArenaSids = @(), [string[]]$HelperArgs = @(), [string[]]$TrustedOwners = @('S-1-5-32-544', 'S-1-5-18'),
          [object[]]$Inventory = @())
    if (-not (Test-Path -LiteralPath $Helper)) { throw "the Fleet Commit helper is not at $Helper (the Gateway Models screen step installs it)" }
    if (-not @($ArenaSids).Count) { throw 'no Arena account found on this hub: the admin hub could not write its Commit requests' }
    foreach ($d in $FleetDir, $LibraryDir) { New-Item -ItemType Directory -Path $d -Force | Out-Null; Test-LabTrustedOwner -Path $d -TrustedOwners $TrustedOwners }
    foreach ($sub in 'catalog', 'published', 'requests', 'status') { New-Item -ItemType Directory -Path (Join-Path $FleetDir $sub) -Force | Out-Null }
    # The catalog before the permissions, so they cover it too.
    $dst = Join-Path $FleetDir 'catalog\model-catalog.json'
    Copy-Item -LiteralPath $Catalog -Destination $dst -Force
    if ((Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $Catalog -Algorithm SHA256).Hash) { throw 'catalog copy check failed' }
    # The lab's head list (hardware), for heads that send Arena no hardware report (heads 2 and 10 have no viewer):
    # without it Fleet knows no tier for them, so it plans no model and shows no "Add all ... planned".
    if (@($Inventory).Count) {
        [IO.File]::WriteAllText((Join-Path $FleetDir 'catalog\head-inventory.json'), (New-LabHeadInventoryText -Inventory $Inventory), (New-Object Text.UTF8Encoding $false))
    }
    foreach ($a in Get-LabFleetAclPlan -FleetDir $FleetDir -LibraryDir $LibraryDir -ArenaSids $ArenaSids) {
        # Already exactly these accounts, inheritance removed: left as it is (a re-run changes nothing).
        $acl = Get-Acl -LiteralPath $a.Path
        $have = @($acl.Access | ForEach-Object { try { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { [string]$_.IdentityReference } } | Sort-Object -Unique)
        $wantSids = @($a.Grants | ForEach-Object { $_.TrimStart('*').Split(':')[0] } | Sort-Object -Unique)
        if ($acl.AreAccessRulesProtected -and ($have -join '|') -eq ($wantSids -join '|') -and $a.Path -ne $FleetDir) { continue }
        Set-LabFolderAcl -Path $a.Path -Grants $a.Grants
    }
    foreach ($p in @($FleetDir, (Join-Path $FleetDir 'catalog'), (Join-Path $FleetDir 'published'), (Join-Path $FleetDir 'status'), $LibraryDir)) {
        if (-not (Test-LabNoUserWrite -Acl (Get-Acl -LiteralPath $p) -Extra $ArenaSids)) { throw "a standard account may change $p; refused" }
    }
    $out = & $Python -I $Helper --publish @HelperArgs 2>&1
    $check = $null; try { $check = (($out | Where-Object { $_ -match '^\{' }) | Select-Object -Last 1) | ConvertFrom-Json } catch { }
    if ($Run) {
        Register-LabChange -Kind 'app' -Description ("Fleet folders {0} (Users read; requests writable by the Arena account) and {1} (administrators only); catalog copied" -f $FleetDir, $LibraryDir) `
            -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("delete {0}; keep {1} unless its downloaded models are no longer wanted" -f $FleetDir, $LibraryDir) }) | Out-Null
    }
    if (-not $check -or -not $check.ok) { throw ('the Fleet folders are ready, but the helper could not publish the Gateway''s state: ' + $(if ($check) { $check.error } else { ($out | Select-Object -Last 3) -join ' ' })) }
    return [pscustomobject]@{ Outcome = 'DONE'; Check = $check
        Detail = ('fleet {0}: catalog ({1:N0} KB), published state for {2} head(s) ({3}); requests writable by {4} Arena account(s); library {5} (administrators only)' -f $FleetDir, ((Get-Item -LiteralPath $dst).Length / 1KB), @($check.heads).Count, (@($check.heads) -join ' '), @($ArenaSids).Count, $LibraryDir) }
}

Export-ModuleMember -Function @(
    'Set-LabHiddenSignInAccount', 'Set-LabFolderAcl', 'Test-LabTrustedOwner', 'Get-LabFleetAclPlan', 'Install-LabFleet', 'New-LabHeadInventoryText',
    'Get-LabDoNotUse', 'Invoke-LabGatewaySwitchBack', 'Get-LabQualifyPurpose', 'Get-LabQualifySummary', 'Get-LabHubApprovedPolicy', 'Test-LabNoUserWrite', 'Install-LabModelsConsole',
    'Test-LabRuntimeInstaller', 'Get-LabRuntimeState', 'Install-LabRuntime', 'Get-LabPerUserPython', 'Get-LabInstallerExitMeaning', 'Get-LabInstallerLogSummary', 'Invoke-LabProcessAsSystem',
    'Get-LabGatewayUpdateVerdict', 'Find-LabGatewayPackage', 'Expand-LabGatewayPackage', 'Test-LabGatewayPackage', 'New-LabGatewaySettingsText', 'New-LabGatewaySettings',
    'ConvertTo-LabWindowsArgument', 'Get-LabDefaultGatewayRunner', 'Set-LabGatewayRunner', 'ConvertFrom-LabGatewayOutput', 'Get-LabFixedPublicationAclScript', 'Invoke-LabGatewayCommand', 'Get-LabGatewayDeployArgs', 'Get-LabGatewayLauncherArgs',
    'Get-LabGatewayService', 'Start-LabGatewayService', 'Test-LabUsersCanRead', 'Repair-LabGatewayReleaseAcl', 'Get-LabServiceDiagnostics', 'Save-LabGatewayDiagnostics','New-LabGatewayListenerRuleSpec', 'Set-LabGatewayListenerRule', 'Get-LabListenerExposure', 'Test-LabGatewayHealth',
    'Get-LabModelChoices', 'ConvertFrom-LabModelSelection', 'New-LabPolicyWithModels', 'Test-LabGatewayPolicyIdentities', 'Export-LabGatewayCa'
)
