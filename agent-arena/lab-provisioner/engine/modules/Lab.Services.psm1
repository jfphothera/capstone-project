<#
    Lab.Services - Capstone's business services on their own heads (Capstone 6.0.0-rc.4): CapstoneQMS on head 3
    (http://192.0.2.3:8611) and CapstoneERP on head 4 (http://192.0.2.4:8612), and their client side on the Capstone
    laptop.

    As for Capstone itself, the provisioner runs the services' own scripts unchanged (Install, Configure, Register task,
    Update, Health) and adds only what they leave to the operator:
      * the Visual C++ runtime their installer requires (same installer and step as on the Capstone laptop);
      * the bearer token: 32 random bytes made on the head, given to the service's Configure script, and carried to the
        Capstone laptop on the provisioner stick (handoff\<service>.token), which takes it and deletes it from the
        stick. The services speak plain HTTP on the lab cable, so the token guards against other lab machines, not
        against someone on the cable;
      * one inbound firewall rule per service: its port, on the head's lab address, from 192.0.2.201 only.
    Tokens are never logged, shown or written into reports.
#>

$script:HandoffFolder = 'handoff'

function Get-LabBusinessServiceSpec {
    <#  Pure - unit tested. The business service this head runs (by its lab address), or $null.  #>
    param([Parameter(Mandatory)]$Config, [string]$HeadIp = '')
    if (-not $HeadIp -or @($Config.PSObject.Properties.Name) -notcontains 'business_services') { return $null }
    foreach ($p in $Config.business_services.PSObject.Properties) {
        if ($p.Name.StartsWith('_')) { continue }
        if ([string]$p.Value.head -eq $HeadIp) { return (Get-LabServiceSpecByName -Config $Config -Name $p.Name) }
    }
    return $null
}

function Get-LabServiceSpecByName {
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string]$Name)
    $v = $Config.business_services.$Name
    if (-not $v) { return $null }
    $headNo = ([string]$v.head).Split('.')[-1]
    return [pscustomobject][ordered]@{
        Name = $Name; Product = [string]$v.product; Label = [string]$v.label; Head = [string]$v.head; HeadNumber = $headNo; Port = [int]$v.port
        Node = 'head-' + ([string]$v.head).Replace('.', '-'); Probe = [string]$v.probe; Client = [string]$v.client
        InstallRoot = [string]$v.install_root; DataRoot = [string]$v.data_root; TaskName = [string]$v.task_name
        Approved = $v.approved_release; RuleName = ('AgentArenaLab-{0}-{1}' -f $v.product, $v.port)
        RuleDisplay = ('Agent Arena Lab: {0} {1} from {2}' -f $v.product, $v.port, $v.client)
        LaptopToken = (Join-Path ([string]$Config.capstone.data_root) ('services\{0}.token' -f $Name))
        LaptopClient = (Join-Path ([string]$Config.capstone.data_root) ('services\{0}-capstone-client.json' -f $Name))
    }
}

function Get-LabServiceHandoff {
    <#  Pure - unit tested. Where the token and client file wait on the provisioner stick.  #>
    param([Parameter(Mandatory)][string]$UsbRoot, [Parameter(Mandatory)][string]$Name)
    $d = Join-Path $UsbRoot $script:HandoffFolder
    return [pscustomobject]@{ Folder = $d; Token = (Join-Path $d "$Name.token"); Client = (Join-Path $d "$Name-capstone-client.json") }
}

function Find-LabServicePackage {
    param([Parameter(Mandatory)][string]$Folder, [Parameter(Mandatory)]$Spec)
    if (-not (Test-Path -LiteralPath $Folder)) { return $null }
    $want = [string]$Spec.Approved.file
    $f = Join-Path $Folder $want
    if ($want -and (Test-Path -LiteralPath $f)) { return (Get-Item -LiteralPath $f) }
    return (@(Get-ChildItem -LiteralPath $Folder -Filter ('{0}-*-windows-x64.zip' -f $Spec.Product) -File -ErrorAction SilentlyContinue | Sort-Object Name -Descending) | Select-Object -First 1)
}

function Test-LabServiceManifest {
    <#  Pure - unit tested. A business-service package manifest is the one this head expects.  #>
    param($Manifest, [Parameter(Mandatory)]$Spec)
    $p = @()
    if (-not $Manifest) { return [pscustomobject]@{ Ok = $false; Problems = @('no package-manifest.json') } }
    if ([string]$Manifest.format -ne 'capstone-business-service-windows-release-v1') { $p += "format $($Manifest.format)" }
    if ([string]$Manifest.service -ne $Spec.Name) { $p += "service $($Manifest.service), expected $($Spec.Name)" }
    if ($Spec.Approved -and [string]$Manifest.version -ne [string]$Spec.Approved.version) { $p += "version $($Manifest.version), expected $($Spec.Approved.version)" }
    if ([string]$Manifest.listen -ne ('{0}:{1}' -f $Spec.Head, $Spec.Port)) { $p += "listens on $($Manifest.listen), expected $($Spec.Head):$($Spec.Port)" }
    if ($Manifest.secrets_included -ne $false) { $p += 'does not declare secrets_included:false' }
    return [pscustomobject]@{ Ok = ($p.Count -eq 0); Problems = $p }
}

function Expand-LabServicePackage {
    <#  The approved archive (SHA-256), extracted to a short local folder and checked file by file against its own
        manifest (its installer checks again).  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)]$Spec)
    if ($Spec.Approved -and $Spec.Approved.sha256) {
        $h = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($h -ne ([string]$Spec.Approved.sha256).ToLowerInvariant()) { throw ("the {0} archive's SHA-256 {1} is not the approved {2}" -f $Spec.Product, $h, $Spec.Approved.sha256) }
    }
    $dest = Join-Path $Run.Root ('sv-' + $Spec.Name)
    $null = Expand-LabZipChecked -Zip $Zip -Destination $dest
    $m = Read-LabJson (Join-Path $dest 'package-manifest.json')
    $chk = Test-LabServiceManifest -Manifest $m -Spec $Spec
    if (-not $chk.Ok) { throw ('{0} package: {1}' -f $Spec.Product, ($chk.Problems -join '; ')) }
    $bad = @()
    foreach ($f in @($m.files)) {
        $p = Join-Path $dest $f.path
        if (-not (Test-Path -LiteralPath $p) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant() -ne $f.sha256) { $bad += $f.path; if ($bad.Count -ge 5) { break } }
    }
    if ($bad.Count) { throw ('{0} package files do not match its manifest: {1}' -f $Spec.Product, ($bad -join ', ')) }
    return [pscustomobject]@{ Root = $dest; Version = [string]$m.version; Files = @($m.files).Count }
}

function Get-LabServiceState {
    <#  The service on this head, read-only: installed release, configuration, token file present (never read),
        startup task, its own health answer and the firewall rule.  #>
    param([Parameter(Mandatory)]$Spec)
    $s = [ordered]@{ Installed = $false; Version = ''; Configured = $false; ListenHost = ''; ListenPort = 0; TokenFile = $false
                     Task = [pscustomobject]@{ Present = $false; State = ''; User = ''; LastResult = $null }; Health = $null; Rule = $null }
    $cur = Read-LabJson (Join-Path $Spec.InstallRoot 'current.json')
    if ($cur) { $s.Installed = $true; $s.Version = [string]$cur.version }
    $cfg = Read-LabJson (Join-Path $Spec.DataRoot 'config\service.json')
    if ($cfg) { $s.Configured = $true; $s.ListenHost = [string]$cfg.listen_host; $s.ListenPort = [int]$cfg.listen_port; $s.TokenFile = [bool]($cfg.token_file -and (Test-Path -LiteralPath ([string]$cfg.token_file))) }
    try {
        $t = Get-ScheduledTask -TaskName $Spec.TaskName -ErrorAction Stop; $i = $t | Get-ScheduledTaskInfo
        $s.Task = [pscustomobject]@{ Present = $true; State = [string]$t.State; User = [string]$t.Principal.UserId; LastResult = [int64]$i.LastTaskResult }
    } catch { }
    $h = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/health' -f $Spec.Head, $Spec.Port) -TimeoutSec 5
    $s.Health = [pscustomobject]@{ Answered = [bool]$h; Ok = [bool]($h -and $h.ok -and [string]$h.service -eq $Spec.Name); Format = $(if ($h) { [string]$h.format } else { '' }) }
    $r = Get-NetFirewallRule -Name $Spec.RuleName -ErrorAction SilentlyContinue
    if ($r) {
        $af = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $r; $pf = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $r
        $s.Rule = [pscustomobject]@{ Enabled = ([string]$r.Enabled -eq 'True'); Action = [string]$r.Action; Direction = [string]$r.Direction; Remote = @($af.RemoteAddress | ForEach-Object { [string]$_ }); Local = @($af.LocalAddress | ForEach-Object { [string]$_ }); Port = [string]$pf.LocalPort; Protocol = [string]$pf.Protocol }
    }
    return [pscustomobject]$s
}

function Test-LabServiceRule {
    <#  Pure - unit tested. The managed rule admits exactly the Capstone laptop, on this service's port and address.  #>
    param($Rule, [Parameter(Mandatory)]$Spec)
    if (-not $Rule) { return [pscustomobject]@{ Ok = $false; Detail = 'no rule' } }
    $p = @()
    if (-not $Rule.Enabled) { $p += 'disabled' }
    if ($Rule.Action -ne 'Allow' -or $Rule.Direction -ne 'Inbound') { $p += "$($Rule.Direction) $($Rule.Action)" }
    if ((@($Rule.Remote) -join ',') -ne $Spec.Client) { $p += ('from ' + (@($Rule.Remote) -join ',')) }
    if ((@($Rule.Local) -join ',') -ne $Spec.Head) { $p += ('to ' + (@($Rule.Local) -join ',')) }
    if ($Rule.Port -ne [string]$Spec.Port -or $Rule.Protocol -ne 'TCP') { $p += "$($Rule.Protocol) $($Rule.Port)" }
    return [pscustomobject]@{ Ok = ($p.Count -eq 0); Detail = $(if ($p.Count) { $p -join '; ' } else { "TCP $($Spec.Port) on $($Spec.Head) from $($Spec.Client) only" }) }
}

function Get-LabOtherPortRules {
    <#  Other enabled inbound Allow rules that open this port (for the report: they would widen who can reach it).  #>
    param([Parameter(Mandatory)]$Spec)
    $out = @()
    foreach ($pf in @(Get-NetFirewallPortFilter -Protocol TCP -ErrorAction SilentlyContinue | Where-Object { @($_.LocalPort) -contains [string]$Spec.Port })) {
        $r = $pf | Get-NetFirewallRule -ErrorAction SilentlyContinue
        if ($r -and $r.Name -ne $Spec.RuleName -and [string]$r.Enabled -eq 'True' -and [string]$r.Direction -eq 'Inbound' -and [string]$r.Action -eq 'Allow') { $out += $r.DisplayName }
    }
    return $out
}

function Write-LabJsonWithoutBom {
    <#  Copies a JSON file without a UTF-8 byte-order mark. The services' Configure scripts write their Capstone client
        file with Windows PowerShell 5.1's "Set-Content -Encoding utf8", which puts one first; Capstone's Python reads
        the file with json.loads(read_text('utf-8')) and refuses it ("Unexpected UTF-8 BOM"), so every QMS or ERP call
        would fail (found in rehearsal, 2026-11-08; reported to the Capstone project).  #>
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination)
    $text = [IO.File]::ReadAllText($Source, [Text.Encoding]::UTF8)          # strips a leading byte-order mark
    if ($text.Length -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    $null = $text | ConvertFrom-Json                                         # still JSON
    [IO.File]::WriteAllText($Destination, $text, (New-Object Text.UTF8Encoding $false))
}

function New-LabServiceToken {
    <#  32 random bytes from the system's cryptographic generator, as 64 hex characters.  #>
    $b = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return (-join ($b | ForEach-Object { $_.ToString('x2') }))
}

function Get-LabTokenFingerprint {
    <#  Pure - unit tested. A short, non-secret way to tell two token files apart in the report.  #>
    param([Parameter(Mandatory)][string]$Token)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $h = $sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($Token.Trim())) } finally { $sha.Dispose() }
    return (-join ($h[0..5] | ForEach-Object { $_.ToString('x2') }))
}

function Set-LabServiceFirewall {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Spec)
    $st = Get-LabServiceState -Spec $Spec
    $chk = Test-LabServiceRule -Rule $st.Rule -Spec $Spec
    if ($chk.Ok) { return (New-LabOutcome 'UNCHANGED' $chk.Detail) }
    if ($st.Rule) { Remove-NetFirewallRule -Name $Spec.RuleName -ErrorAction Stop }
    New-NetFirewallRule -Name $Spec.RuleName -DisplayName $Spec.RuleDisplay -Group 'Agent Arena Lab' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Spec.Port -LocalAddress $Spec.Head -RemoteAddress $Spec.Client -Profile Any -Enabled True -ErrorAction Stop | Out-Null
    Register-LabChange -Kind 'firewall' -Description ("added '{0}'" -f $Spec.RuleDisplay) -Undo ([pscustomobject]@{ type = 'firewall'; action = 'remove'; name = $Spec.RuleName }) | Out-Null
    $after = Test-LabServiceRule -Rule (Get-LabServiceState -Spec $Spec).Rule -Spec $Spec
    if (-not $after.Ok) { throw ('the rule was added but reads back as: ' + $after.Detail) }
    return (New-LabOutcome 'DONE' $after.Detail)
}

function Set-LabServiceToken {
    <#  A new token for the service: made here, written to a SYSTEM/Administrators folder, given to the service's own
        Configure script (which copies it for LOCAL SERVICE), then put on the provisioner stick for the Capstone laptop
        with the service's non-secret client file. The local copy is deleted.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][string]$UsbRoot, [Parameter(Mandatory)][string]$LogFile)
    $work = Join-Path $Run.Root 'services'
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    Set-LabFolderAcl -Path $work -Grants @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
    $tmp = Join-Path $work ($Spec.Name + '.token.new')
    $token = New-LabServiceToken
    try {
        [IO.File]::WriteAllText($tmp, $token, (New-Object Text.ASCIIEncoding))
        $r = Invoke-LabExternalScript -Script (Join-Path $Spec.InstallRoot ('tools\Configure-{0}.ps1' -f $Spec.Product)) -LogFile $LogFile -Arguments @(
            '-TokenSourceFile', $tmp, '-InstallRoot', $Spec.InstallRoot, '-DataRoot', $Spec.DataRoot, '-ListenHost', $Spec.Head, '-ListenPort', [string]$Spec.Port, '-CapstoneTokenFile', $Spec.LaptopToken)
        if ($r.ExitCode -ne 0 -or $r.Output -notmatch '"configured"\s*:\s*true') { throw ('Configure-{0}.ps1 refused: {1}' -f $Spec.Product, (($r.Output -split "`r?`n" | Select-Object -Last 3) -join ' ')) }
        $client = Join-Path $Spec.DataRoot ('config\{0}-capstone-client.json' -f $Spec.Name)
        $c = Read-LabJson $client
        if (-not $c -or [string]$c.execution_node -ne $Spec.Node -or [string]$c.url -ne ('http://{0}:{1}' -f $Spec.Head, $Spec.Port) -or [string]$c.token_file -ne $Spec.LaptopToken) { throw ("the service's client file $client does not name $($Spec.Node), http://$($Spec.Head):$($Spec.Port) and $($Spec.LaptopToken)") }
        $ho = Get-LabServiceHandoff -UsbRoot $UsbRoot -Name $Spec.Name
        New-Item -ItemType Directory -Path $ho.Folder -Force | Out-Null
        [IO.File]::WriteAllText($ho.Token, $token, (New-Object Text.ASCIIEncoding))
        Write-LabJsonWithoutBom -Source $client -Destination $ho.Client
        $fp = Get-LabTokenFingerprint -Token $token
    } finally {
        $token = $null
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
    Register-LabChange -Kind 'secret-file' -Description ("{0} configured with a new token (fingerprint {1}); token and client file put on the provisioner stick ({2}) for the Capstone laptop" -f $Spec.Product, $fp, $ho.Folder) -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("delete {0} and {1} if the Capstone laptop has not taken them; choose this head's service again to issue another" -f $ho.Token, $ho.Client) }) | Out-Null
    return [pscustomobject]@{ Fingerprint = $fp; Handoff = $ho }
}

function Restart-LabServiceTask {
    param([Parameter(Mandatory)]$Spec)
    Stop-ScheduledTask -TaskName $Spec.TaskName -ErrorAction SilentlyContinue
    # The task's PowerShell starts python.exe; stopping the task may leave that child running on the port.
    $rel = Join-Path $Spec.InstallRoot 'releases'
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($rel, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    Start-ScheduledTask -TaskName $Spec.TaskName
}

function Wait-LabServiceHealth {
    param([Parameter(Mandatory)]$Spec, [int]$Seconds = 45)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $h = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/health' -f $Spec.Head, $Spec.Port) -TimeoutSec 4
        if ($h -and $h.ok -and [string]$h.service -eq $Spec.Name) { return $true }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-LabServiceLogTail {
    param([Parameter(Mandatory)]$Spec)
    $f = Join-Path $Spec.DataRoot 'logs\service-error.log'
    if (-not (Test-Path -LiteralPath $f)) { return 'its error log is empty' }
    $l = @(Get-Content -LiteralPath $f -Tail 5 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    if (-not $l.Count) { return 'its error log is empty' }
    return ('its error log ends with: ' + (Protect-LabText $l[-1]))
}

# ---------------------------------------------------------------------------
# The Capstone laptop's side
# ---------------------------------------------------------------------------

function Get-LabServiceClientState {
    <#  The laptop's copy of one service's token and client file, and what the service answers with it. The token is
        read only to send it to the service; it is never logged or returned.  #>
    param([Parameter(Mandatory)]$Spec, [string]$UsbRoot = '')
    $s = [ordered]@{ Name = $Spec.Name; TokenFile = (Test-Path -LiteralPath $Spec.LaptopToken); ClientFile = (Test-Path -LiteralPath $Spec.LaptopClient)
                     OnStick = $false; Health = $false; Authorized = $null; Detail = ''; Fingerprint = '' }
    if ($UsbRoot) { $ho = Get-LabServiceHandoff -UsbRoot $UsbRoot -Name $Spec.Name; $s.OnStick = (Test-Path -LiteralPath $ho.Token) }
    $h = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/health' -f $Spec.Head, $Spec.Port) -TimeoutSec 5
    $s.Health = [bool]($h -and $h.ok -and [string]$h.service -eq $Spec.Name)
    if ($s.TokenFile -and $s.Health) {
        $token = ''
        try {
            $token = ([IO.File]::ReadAllText($Spec.LaptopToken)).Trim()
            $s.Fingerprint = Get-LabTokenFingerprint -Token $token
            $code = 0
            try {
                $req = [Net.HttpWebRequest]::Create(('http://{0}:{1}{2}' -f $Spec.Head, $Spec.Port, $Spec.Probe))
                $req.Proxy = $null; $req.Timeout = 8000; $req.Headers.Add('Authorization', 'Bearer ' + $token)
                $resp = $req.GetResponse(); $code = [int]$resp.StatusCode; $resp.Close()
            } catch [Net.WebException] { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } else { $s.Detail = $_.Exception.Message } }
            $s.Authorized = ($code -eq 200)
            if (-not $s.Detail) { $s.Detail = "GET $($Spec.Probe) with the token: HTTP $code" }
        } finally { $token = $null }
    } elseif (-not $s.Health) { $s.Detail = ("no health answer from http://{0}:{1}" -f $Spec.Head, $Spec.Port) }
    return [pscustomobject]$s
}

function Receive-LabServiceHandoff {
    <#  Takes one service's token and client file from the provisioner stick into C:\ProgramData\Capstone\services
        (SYSTEM and Administrators; Capstone's Configure script adds LOCAL SERVICE read to the token), then deletes
        them from the stick and proves they are gone.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][string]$UsbRoot)
    $ho = Get-LabServiceHandoff -UsbRoot $UsbRoot -Name $Spec.Name
    if (-not (Test-Path -LiteralPath $ho.Token) -or -not (Test-Path -LiteralPath $ho.Client)) { throw "the stick does not hold both $($ho.Token) and $($ho.Client)" }
    $c = Read-LabJson $ho.Client
    if (-not $c -or [string]$c.execution_node -ne $Spec.Node -or [string]$c.url -ne ('http://{0}:{1}' -f $Spec.Head, $Spec.Port) -or [string]$c.token_file -ne $Spec.LaptopToken) { throw "the client file on the stick does not name $($Spec.Node), http://$($Spec.Head):$($Spec.Port) and $($Spec.LaptopToken)" }
    $dir = Split-Path -Parent $Spec.LaptopToken
    if (-not (Test-Path -LiteralPath $dir)) {
        # Set once, when made: resetting it later would take back the read access Capstone's Configure script gives
        # LOCAL SERVICE on the token files inside. LOCAL SERVICE may list this folder only.
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Set-LabFolderAcl -Path $dir -Grants @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-19:(RX)')
    }
    foreach ($f in @($Spec.LaptopToken, $Spec.LaptopClient)) { if (Test-Path -LiteralPath $f) { Copy-Item -LiteralPath $f -Destination (Join-Path $Run.Backups ((Split-Path -Leaf $f) + '.before')) -Force } }
    $token = ([IO.File]::ReadAllText($ho.Token)).Trim()
    try {
        if ($token.Length -lt 32 -or $token -notmatch '^[0-9a-f]+$') { throw "the token on the stick is not one this provisioner made (expected 64 hex characters)" }
        [IO.File]::WriteAllText($Spec.LaptopToken, $token, (New-Object Text.ASCIIEncoding))
        $fp = Get-LabTokenFingerprint -Token $token
    } finally { $token = $null }
    Write-LabJsonWithoutBom -Source $ho.Client -Destination $Spec.LaptopClient
    Remove-Item -LiteralPath $ho.Token, $ho.Client -Force
    if ((Test-Path -LiteralPath $ho.Token) -or (Test-Path -LiteralPath $ho.Client)) { throw "the token was copied but could not be deleted from the stick: delete $($ho.Token) by hand" }
    if (-not @(Get-ChildItem -LiteralPath $ho.Folder -Force -ErrorAction SilentlyContinue).Count) { Remove-Item -LiteralPath $ho.Folder -Force -ErrorAction SilentlyContinue }
    Register-LabChange -Kind 'secret-file' -Description ("{0} token (fingerprint {1}) and client file taken from the provisioner stick into {2}; deleted from the stick" -f $Spec.Product, $fp, $dir) -Undo ([pscustomobject]@{ type = 'manual'; instructions = "delete $($Spec.LaptopToken) and $($Spec.LaptopClient) (earlier copies, if any, are in the run's backups)" }) | Out-Null
    return $fp
}

function Compare-LabServiceData {
    <#  Is the laptop's own copy of the service's data the same as what the service package ships (by SHA-256)? When it
        is, nothing has to move to the head (the services start with the same seed data).  #>
    param([Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$CapstoneData)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $e = $z.Entries | Where-Object { $_.FullName -match '^package-manifest\.json$' } | Select-Object -First 1
        $sr = New-Object IO.StreamReader($e.Open()); $m = $sr.ReadToEnd() | ConvertFrom-Json; $sr.Dispose()
    } finally { $z.Dispose() }
    $prefix = 'payload/data/{0}/' -f $Spec.Name
    $diff = @(); $n = 0
    foreach ($f in @($m.files | Where-Object { $_.path -like "$prefix*" })) {
        $rel = $f.path.Substring($prefix.Length)
        $local = $(if ($Spec.Name -eq 'qms' -and $rel -eq 'qms_compatibility.json') { Join-Path $CapstoneData 'data\qms_compatibility.json' } else { Join-Path $CapstoneData ('data\{0}\{1}' -f $Spec.Name, $rel.Replace('/', '\')) })
        $n++
        if (-not (Test-Path -LiteralPath $local) -or (Get-FileHash -LiteralPath $local -Algorithm SHA256).Hash.ToLowerInvariant() -ne $f.sha256) { $diff += $rel }
    }
    $extra = @()
    $dir = Join-Path $CapstoneData ('data\' + $Spec.Name)
    if (Test-Path -LiteralPath $dir) { $extra = @(Get-ChildItem -LiteralPath $dir -File -Recurse | Where-Object { $r = $_.FullName.Substring($dir.Length + 1).Replace('\', '/'); -not @($m.files | Where-Object { $_.path -eq ($prefix + $r) }).Count } | ForEach-Object { $_.Name }) }
    return [pscustomobject]@{ Same = ($diff.Count -eq 0 -and $extra.Count -eq 0); Files = $n; Different = $diff; Extra = $extra }
}

Export-ModuleMember -Function @(
    'Get-LabBusinessServiceSpec', 'Get-LabServiceSpecByName', 'Get-LabServiceHandoff', 'Find-LabServicePackage', 'Test-LabServiceManifest', 'Expand-LabServicePackage',
    'Get-LabServiceState', 'Test-LabServiceRule', 'Get-LabOtherPortRules', 'Write-LabJsonWithoutBom', 'New-LabServiceToken', 'Get-LabTokenFingerprint', 'Set-LabServiceFirewall', 'Set-LabServiceToken',
    'Restart-LabServiceTask', 'Wait-LabServiceHealth', 'Get-LabServiceLogTail', 'Get-LabServiceClientState', 'Receive-LabServiceHandoff', 'Compare-LabServiceData'
)
