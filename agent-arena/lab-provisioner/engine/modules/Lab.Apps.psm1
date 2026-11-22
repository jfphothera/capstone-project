<#
    Lab.Apps - the independent projects, seen from the provisioner.

    The USB coordinates and verifies; it does not build, rewrite or replace other projects:

      * Capstone   uses its own Windows release (format capstone-windows-release-v1; 6.0.0-rc.3 is the
                   approved one, by archive SHA-256) and its own scripts in their documented order:
                   Install-Capstone.ps1 or Update-Capstone.ps1, Configure-Capstone.ps1 (two Gateway
                   identities, capstone and capstone-research, each with its own token file),
                   Register-CapstoneTask.ps1, then Test-CapstoneHealth.ps1 / Test-CapstoneGateway.ps1.
                   Its mode is read from its appsettings.json.
      * Gateway    0.3.0 is installed on the hub by Invoke-LabGatewayFlow.ps1 from its manifest; here
                   only the client side (folder, CA) and its observation file are handled.
      * Coder      has no Windows package, Gateway client or Arena publisher. Its stage stays PENDING.

    Credentials (Gateway tokens, developer tokens, CA keys) are never read: only whether the
    file or variable that should hold them exists.
#>

# ---------------------------------------------------------------------------
# Capstone
# ---------------------------------------------------------------------------

function Get-LabCapstoneState {
    param([Parameter(Mandatory)]$Config)
    $root = [string]$Config.capstone.install_root; $data = [string]$Config.capstone.data_root
    $state = [ordered]@{ Installed = $false; Version = ''; InferenceMode = ''; ModeSource = ''; ListenHost = ''; ListenPort = 0; Settings = 'missing'
                         Gateway = $null; ResearchGateway = $null; DeveloperTokenFile = 'n/a'; DeveloperTokenPath = ''; ArenaRegistration = ''; Task = $null; Health = $null; MachineModeVariable = '' ; Notes = @(); Services = @{} }
    $pointer = Read-LabJson (Join-Path $root 'current.json')
    if ($pointer) { $state.Installed = $true; $state.Version = [string]$pointer.version }
    $settings = Read-LabJson (Join-Path $data 'config\appsettings.json')
    if ($settings) {
        $state.Settings = 'present'; $state.InferenceMode = [string]$settings.inference_mode; $state.ModeSource = 'appsettings.json inference_mode'
        $state.ListenHost = [string]$settings.listen_host; $state.ListenPort = [int]$settings.listen_port
        $state.DeveloperTokenPath = [string]$settings.developer_token_file
        $state.DeveloperTokenFile = $(if ($settings.developer_token_file -and (Test-Path -LiteralPath $settings.developer_token_file)) { 'present' } else { 'missing' })
        if ($settings.arena_adapter) { $state.ArenaRegistration = [string]$settings.arena_adapter.registration_status }
        if ($settings.gateway_client_config) { $state.Gateway = Get-LabGatewayBootstrapState -Path ([string]$settings.gateway_client_config) }
        if ($settings.PSObject.Properties.Name -contains 'research_gateway_client_config' -and $settings.research_gateway_client_config) { $state.ResearchGateway = Get-LabGatewayBootstrapState -Path ([string]$settings.research_gateway_client_config) }
        # Capstone 6.0.0-rc.4: QMS and ERP served by their own heads when these are set (else in-process).
        foreach ($n in 'qms', 'erp') { $k = $n + '_service_config'; if ($settings.PSObject.Properties.Name -contains $k -and $settings.$k) { $state.Services[$n] = [string]$settings.$k } }
    }
    $mv = [Environment]::GetEnvironmentVariable('CAPSTONE_INFERENCE_MODE', 'Machine')
    if ($mv) { $state.MachineModeVariable = $mv; if ($mv -ne 'gateway') { $state.Notes += "A machine-wide CAPSTONE_INFERENCE_MODE=$mv exists" } }
    try { $t = Get-ScheduledTask -TaskName $Config.capstone.task_name -ErrorAction Stop; $state.Task = [pscustomobject]@{ Present = $true; State = [string]$t.State; User = [string]$t.Principal.UserId } }
    catch { $state.Task = [pscustomobject]@{ Present = $false; State = ''; User = '' } }
    if ($state.ListenHost -and $state.ListenPort) {
        $h = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/health' -f $state.ListenHost, $state.ListenPort) -TimeoutSec 8
        $state.Health = [pscustomobject]@{ Answered = [bool]$h; Ok = $(if ($h) { [bool]$h.ok } else { $false }); Version = $(if ($h) { [string]$h.version } else { '' }) }
    }
    return [pscustomobject]$state
}

function Get-LabGatewayBootstrapState {
    <#  One Gateway client bootstrap (Capstone RC.2 keeps two: capstone and capstone-research), without
        reading any credential. Gateway 0.3 deployments use token_file only: a bootstrap that embeds a
        token or names a token environment variable is refused, as Capstone's Configure-Capstone does.  #>
    param([Parameter(Mandatory)][string]$Path)
    $b = Read-LabJson $Path
    if (-not $b) { return [pscustomobject]@{ Present = $false; Path = $Path } }
    $uri = $null; try { $uri = [Uri]$b.url } catch { }
    $tokenFile = ''; if ($b.PSObject.Properties.Name -contains 'token_file') { $tokenFile = [string]$b.token_file }
    $acl = $null
    if ($tokenFile -and (Test-Path -LiteralPath $tokenFile)) { $sids = Get-LabAclAllowSids -Path $tokenFile; $acl = $(if ($sids -is [string]) { [pscustomobject]@{ Ok = $false; Problems = @('ACL unreadable') } } else { Test-LabTokenFileAcl -AllowSids $sids }) }
    return [pscustomobject][ordered]@{
        Present = $true; Path = $Path; Url = [string]$b.url; Scheme = $(if ($uri) { $uri.Scheme } else { '' }); Host = $(if ($uri) { $uri.Host } else { '' }); Port = $(if ($uri) { $uri.Port } else { 0 })
        Placeholder = ([string]$b.url -match 'REPLACE_' -or $tokenFile -match 'REPLACE_'); ApplicationId = [string]$b.application_id; Contract = [string]$b.contract; ServiceId = [string]$b.service_id
        CaPath = [string]$b.ca_file; CaFile = $(if ($b.ca_file -and (Test-Path -LiteralPath $b.ca_file)) { 'present' } else { 'missing' })
        TokenPath = $tokenFile; TokenFile = $(if ($tokenFile) { if (Test-Path -LiteralPath $tokenFile) { 'present' } else { 'missing' } } else { 'not configured' }); TokenAcl = $acl
        NamesTokenEnvironment = ($b.PSObject.Properties.Name -contains 'token_environment')
        EmbedsCredential = ($b.PSObject.Properties.Name -contains 'token' -or [string]$b.url -match '@')
    }
}

function Test-LabCapstoneBootstraps {
    <#  Pure over two bootstrap states: both Gateway 0.3 identities complete and independent.  #>
    param($Operational, $Research, [Parameter(Mandatory)][string]$Url, [string]$CaPath = '')
    $p = @()
    foreach ($pair in @(@($Operational, 'capstone'), @($Research, 'capstone-research'))) {
        $b = $pair[0]; $id = $pair[1]
        if (-not $b -or -not $b.Present) { $p += "$id bootstrap missing"; continue }
        if ($b.ApplicationId -ne $id) { $p += "$id bootstrap is scoped to '$($b.ApplicationId)'" }
        if ($b.Url -ne $Url) { $p += "$id url $($b.Url), expected $Url" }
        if ($b.Contract -ne 'shared.inference/1' -or $b.ServiceId -ne 'shared-inference') { $p += "$id contract/service identity" }
        if ($b.EmbedsCredential) { $p += "$id bootstrap embeds a credential" }
        if ($b.NamesTokenEnvironment) { $p += "$id bootstrap names a token environment variable (retired: Gateway 0.3 uses token_file only)" }
        if ($b.TokenFile -ne 'present') { $p += "$id token file $($b.TokenFile): $($b.TokenPath)" }
        elseif ($b.TokenAcl -and -not $b.TokenAcl.Ok) { $p += ("$id token file ACL: " + ($b.TokenAcl.Problems -join '; ')) }
        if ($b.CaFile -ne 'present') { $p += "$id CA file missing: $($b.CaPath)" } elseif ($CaPath -and $b.CaPath -ne $CaPath) { $p += "$id CA $($b.CaPath), expected $CaPath" }
    }
    if ($Operational -and $Research -and $Operational.Present -and $Research.Present -and $Operational.TokenPath -and $Operational.TokenPath -eq $Research.TokenPath) { $p += 'capstone and capstone-research share one token file' }
    return [pscustomobject]@{ Ok = ($p.Count -eq 0); Problems = $p }
}
function Find-LabCapstonePackage {
    param([Parameter(Mandatory)][string]$Folder)
    if (-not (Test-Path -LiteralPath $Folder)) { return $null }
    return (@(Get-ChildItem -LiteralPath $Folder -Filter 'Capstone-*-windows-x64.zip' -File -ErrorAction SilentlyContinue | Sort-Object Name -Descending) | Select-Object -First 1)
}

function Expand-LabCapstonePackage {
    <#  Extracts the release locally and checks it against its own package-manifest.json.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Zip, [string]$ExpectedFormat = 'capstone-windows-release-v1', [string]$ExpectedSha256 = '')
    if ($ExpectedSha256) { $h = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant(); if ($h -ne $ExpectedSha256.ToLowerInvariant()) { throw ("the Capstone archive's SHA-256 {0} is not the released {1}" -f $h, $ExpectedSha256) } }
    # A short folder: Capstone's deepest file is 195 characters inside the archive, so the run folder
    # (C:\ProgramData\AgentArenaLab\staging\Capstone-6.0.0-rc.3-windows-x64\...) was too deep (DESKTOP-REDACT11).
    $dest = Join-Path $Run.Root 'cs'
    $null = Expand-LabZipChecked -Zip $Zip -Destination $dest
    $root = $dest
    if (-not (Test-Path -LiteralPath (Join-Path $root 'package-manifest.json'))) { $sub = @(Get-ChildItem -LiteralPath $dest -Directory) | Select-Object -First 1; if ($sub) { $root = $sub.FullName } }
    $m = Read-LabJson (Join-Path $root 'package-manifest.json')
    if (-not $m) { throw 'the Capstone package has no package-manifest.json' }
    if ($m.format -ne $ExpectedFormat) { throw ('the Capstone package format is {0}, not {1}' -f $m.format, $ExpectedFormat) }
    if ($m.secrets_included -ne $false) { throw 'the Capstone package does not declare secrets_included:false' }
    $bad = @()
    foreach ($f in @($m.files)) {
        $p = Join-Path $root $f.path
        if (-not (Test-Path -LiteralPath $p) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant() -ne $f.sha256) { $bad += $f.path; if ($bad.Count -ge 5) { break } }
    }
    if ($bad.Count) { throw ('Capstone package files do not match its manifest: ' + ($bad -join ', ')) }
    return [pscustomobject]@{ Root = $root; Version = [string]$m.version; Files = @($m.files).Count; Compatibility = $m.compatibility }
}

function Invoke-LabExternalScript {
    <#  Runs another project's PowerShell script unchanged, in Windows PowerShell 5.1, and
        returns its exit code and output (credentials are never passed on the command line).  #>
    param([Parameter(Mandatory)][string]$Script, [string[]]$Arguments = @(), [Parameter(Mandatory)][string]$LogFile)
    $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # The child's stderr must stay text: under "Stop", Windows PowerShell 5.1 turns it into a terminating
    # error, and the exit code and log of a refusal would be lost.
    $ErrorActionPreference = 'Continue'
    # Windows PowerShell 5.1 passes native arguments without escaping: an inner double quote or a trailing
    # backslash would reach the script changed, and an empty argument would vanish. Refuse instead.
    $bad = @($Arguments | Where-Object { $_ -eq '' -or $_ -match '"' -or ($_ -match '\s' -and $_ -match '\\$') })
    if ($bad.Count) { throw ('an argument for {0} cannot be passed safely (empty, a double quote, or a trailing backslash): {1}' -f (Split-Path -Leaf $Script), ($bad -join ' | ')) }
    $out = & $ps -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments 2>&1 | ForEach-Object { [string]$_ }
    $code = $LASTEXITCODE
    $text = Protect-LabText ($out -join "`r`n")
    Add-Content -LiteralPath $LogFile -Value ("==== {0} {1}`r`n{2}`r`nexit {3}`r`n" -f $Script, ($Arguments -join ' '), $text, $code) -Encoding UTF8
    return [pscustomobject]@{ ExitCode = $code; Output = $text }
}

# ---------------------------------------------------------------------------
# Gateway and Coder (read-only)
# ---------------------------------------------------------------------------

function Get-LabGatewayState {
    param([Parameter(Mandatory)]$Config)
    $g = $Config.gateway
    $file = [string]$g.observation_file
    $obs = [ordered]@{ Present = $false; AgeSeconds = $null; Contract = ''; Fresh = $false; Readable = $true }
    if (Test-Path -LiteralPath $file) {
        $obs.Present = $true
        $obs.AgeSeconds = [int]((Get-Date) - (Get-Item -LiteralPath $file).LastWriteTime).TotalSeconds
        $obs.Fresh = ($obs.AgeSeconds -le [int]$g.stale_seconds)
        try { $j = Get-Content -LiteralPath $file -Raw -ErrorAction Stop | ConvertFrom-Json; $obs.Contract = [string]$j.contract } catch { $obs.Readable = $false }
    }
    $task = $null
    try { $t = Get-ScheduledTask -TaskName $g.publisher_task -ErrorAction Stop; $task = [pscustomobject]@{ Present = $true; State = [string]$t.State } } catch { $task = [pscustomobject]@{ Present = $false; State = '' } }
    $portNote = 'no Gateway port is assigned yet (Gateway project)'
    $listening = $null
    if ($g.port) { $portNote = "port $($g.port)"; $listening = Test-LabTcp -HostName $Config.hub -Port ([int]$g.port) }
    return [pscustomobject][ordered]@{ Observation = [pscustomobject]$obs; PublisherTask = $task; Port = $portNote; Listening = $listening
                                       DataFolder = (Test-Path -LiteralPath 'C:\ProgramData\InferenceGateway') }
}

function Get-LabSuppliedPackage {
    <#  What another project has put on the USB under packages\<project>, and its manifest, if any.  #>
    param([Parameter(Mandatory)][string]$Folder)
    if (-not (Test-Path -LiteralPath $Folder)) { return [pscustomobject]@{ Supplied = $false; Files = @(); Manifest = '' } }
    $files = @(Get-ChildItem -LiteralPath $Folder -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'README.txt' })
    $manifest = @($files | Where-Object { $_.Name -match 'manifest' }) | Select-Object -First 1
    return [pscustomobject]@{ Supplied = ($files.Count -gt 0); Files = @($files | ForEach-Object Name); Manifest = $(if ($manifest) { $manifest.Name } else { '' }) }
}

# ---------------------------------------------------------------------------
# Gateway-only inference: application laptops must NOT reach any head directly
# ---------------------------------------------------------------------------

function Test-LabDirectOllama {
    <#  TCP to port 11434 on each head, from this machine: real LAN evidence.  #>
    param([Parameter(Mandatory)][string[]]$Heads, [int]$Port = 11434, [int]$TimeoutMs = 2000)
    $out = @()
    foreach ($h in $Heads) {
        $open = Test-LabTcp -HostName $h -Port $Port -TimeoutMs $TimeoutMs
        $isOllama = $false; $reach = 'no route or no answer'
        if ($open) { $v = Get-LabJsonFromUrl -Url ('http://{0}:{1}/api/version' -f $h, $Port) -TimeoutSec 5; $isOllama = [bool]($v -and $v.version); $reach = 'connected' }
        else { if (Test-Connection -ComputerName $h -Count 1 -Quiet -ErrorAction SilentlyContinue) { $reach = 'host answers ping; port closed or filtered' } }
        $out += [pscustomobject][ordered]@{ Head = $h; Open = $open; Ollama = $isOllama; Detail = $reach }
    }
    return $out
}

# ---------------------------------------------------------------------------
# Gateway client readiness on an application laptop (Gateway 0.3.0 CLIENT_CONTRACT and manifest
# "tokens"). F: prepares the folder, the public CA and the client configuration; the token file is
# carried by the operator from the hub and only its presence and ACL are checked - never its content.
# ---------------------------------------------------------------------------

function Read-LabGatewayManifestFromZip {
    param([Parameter(Mandatory)][string]$Zip)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $e = @($z.Entries | Where-Object { $_.FullName -match '(^|/)deployment-manifest\.json$' }) | Select-Object -First 1
        $t = @($z.Entries | Where-Object { $_.FullName -match '(^|/)config/client\.template\.json$' }) | Select-Object -First 1
        if (-not $e) { return $null }
        $read = { param($x) $r = New-Object IO.StreamReader($x.Open()); try { $r.ReadToEnd() } finally { $r.Close() } }
        return [pscustomobject]@{ Manifest = ((& $read $e) | ConvertFrom-Json); ClientTemplate = $(if ($t) { & $read $t } else { '' }) }
    } finally { $z.Dispose() }
}

function Get-LabGatewayClientLayout {
    <#  Pure. The client files for one application, from the manifest (Capstone's paths are declared there).  #>
    param([Parameter(Mandatory)]$Manifest, [Parameter(Mandatory)][string]$AppId)
    $l = $Manifest.listener
    $url = 'https://{0}:{1}' -f $l.bind, $l.port
    if ($AppId -eq 'capstone') { $c = $Manifest.tokens.capstone; return [pscustomobject]@{ AppId = $AppId; Folder = (Split-Path -Parent $c.client_config); Config = $c.client_config; Token = $c.token_file; Ca = $c.ca_file; Reader = $c.reader; Url = $url; Declared = $true } }
    return [pscustomobject]@{ AppId = $AppId; Folder = ''; Config = ''; Token = ''; Ca = ''; Reader = ''; Url = $url; Declared = $false }
}

function New-LabGatewayClientConfigText {
    <#  Pure. The Gateway's client.template.json, filled from the manifest and the layout.  #>
    param([Parameter(Mandatory)][string]$TemplateJson, [Parameter(Mandatory)]$Layout, [Parameter(Mandatory)]$Manifest)
    $t = $TemplateJson | ConvertFrom-Json
    $t.url = $Layout.Url; $t.application_id = $Layout.AppId; $t.token_file = $Layout.Token; $t.ca_file = $Layout.Ca
    if ($t.PSObject.Properties.Name -contains 'service_id') { $t.service_id = [string]$Manifest.observation.service_id }
    if (($t | ConvertTo-Json -Depth 5) -match 'REPLACE_') { throw 'the client configuration still holds a placeholder' }
    return ($t | ConvertTo-Json -Depth 5)
}

function Test-LabTokenFileAcl {
    <#  Pure over ACL entries: readable by the application's service account, by nobody broad.  #>
    param([AllowEmptyCollection()][array]$AllowSids = @(), [string]$ReaderSid = 'S-1-5-19')
    $broad = @('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11', 'S-1-5-4', 'S-1-5-32-546')
    $bad = @($AllowSids | Where-Object { $broad -contains $_ })
    $problems = @(); if ($bad.Count) { $problems += ('broad access: ' + (($bad | ForEach-Object { Resolve-LabAccount $_ }) -join ', ')) }
    if ($AllowSids -notcontains $ReaderSid) { $problems += ('the service account {0} cannot read it' -f (Resolve-LabAccount $ReaderSid)) }
    return [pscustomobject]@{ Ok = ($problems.Count -eq 0); Problems = $problems }
}

function Get-LabGatewayClientState {
    param([Parameter(Mandatory)]$Layout, [string]$UsbCa = '')
    $s = [ordered]@{ AppId = $Layout.AppId; Declared = $Layout.Declared; Folder = $Layout.Folder; ConfigPresent = $false; ConfigOk = $false; ConfigProblems = @(); TokenPresent = $false; TokenAcl = $null; CaPresent = $false; CaMatchesUsb = $null }
    if (-not $Layout.Declared) { return [pscustomobject]$s }
    $cfg = Read-LabJson $Layout.Config
    if ($cfg) {
        $s.ConfigPresent = $true
        if ($cfg.url -ne $Layout.Url) { $s.ConfigProblems += "url $($cfg.url)" }; if ($cfg.application_id -ne $Layout.AppId) { $s.ConfigProblems += "application_id $($cfg.application_id)" }
        if ($cfg.token_file -ne $Layout.Token) { $s.ConfigProblems += "token_file $($cfg.token_file)" }; if ($cfg.ca_file -ne $Layout.Ca) { $s.ConfigProblems += "ca_file $($cfg.ca_file)" }
        if ($cfg.PSObject.Properties.Name -contains 'token') { $s.ConfigProblems += 'the configuration embeds a token' }
        $s.ConfigOk = ($s.ConfigProblems.Count -eq 0)
    }
    if (Test-Path -LiteralPath $Layout.Token) {
        $s.TokenPresent = ((Get-Item -LiteralPath $Layout.Token).Length -gt 0)        # size only; the content is never read
        $sids = Get-LabAclAllowSids -Path $Layout.Token
        $readerSid = Resolve-LabSid $Layout.Reader; if (-not $readerSid) { $readerSid = 'S-1-5-19' }
        $s.TokenAcl = $(if ($sids -is [string]) { [pscustomobject]@{ Ok = $false; Problems = @('ACL unreadable') } } else { Test-LabTokenFileAcl -AllowSids $sids -ReaderSid $readerSid })
    }
    if (Test-Path -LiteralPath $Layout.Ca) {
        $s.CaPresent = $true
        if ($UsbCa -and (Test-Path -LiteralPath $UsbCa)) { $s.CaMatchesUsb = ((Get-FileHash -LiteralPath $UsbCa).Hash -eq (Get-FileHash -LiteralPath $Layout.Ca).Hash) }
    }
    return [pscustomobject]$s
}

function Set-LabGatewayClientFiles {
    <#  Folder (SYSTEM/Administrators full, the service account read), the public CA from the USB,
        and the client configuration. Never a token.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Layout, [string]$TemplateJson = '', [Parameter(Mandatory)]$Manifest, [Parameter(Mandatory)][string]$UsbCa,
          [switch]$NoClientConfig)   # Capstone RC.2 supplies its own two bootstraps: only the folder and the CA are prepared
    if (-not $Layout.Declared) { return (New-LabOutcome 'PENDING' ("the Gateway contract declares no client file layout for $($Layout.AppId) yet")) }
    if (-not (Test-Path -LiteralPath $UsbCa)) { return (New-LabOutcome 'PENDING' ("no Gateway CA on the USB ($UsbCa): it is exported by the hub's certificate step")) }
    $sumFile = $UsbCa + '.sha256'
    if (Test-Path -LiteralPath $sumFile) { $want = (([IO.File]::ReadAllText($sumFile)).Trim() -split '\s+')[0]; if ((Get-FileHash -LiteralPath $UsbCa).Hash.ToLowerInvariant() -ne $want) { throw 'the CA on the USB does not match its recorded SHA-256' } }
    $notes = @()
    if (-not (Test-Path -LiteralPath $Layout.Folder)) {
        New-Item -ItemType Directory -Path $Layout.Folder -Force | Out-Null
        Register-LabChange -Kind 'folder' -Description ("created {0}" -f $Layout.Folder) -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("remove {0} once it is empty" -f $Layout.Folder) }) | Out-Null
        $notes += 'folder created'
    }
    $items = @(,@($Layout.Ca, $null)); if (-not $NoClientConfig) { $items += ,@($Layout.Config, (New-LabGatewayClientConfigText -TemplateJson $TemplateJson -Layout $Layout -Manifest $Manifest)) }
    foreach ($pair in $items) {
        $path = $pair[0]; $text = $pair[1]
        $backup = $null; if (Test-Path -LiteralPath $path) { $backup = Join-Path $Run.Backups ([IO.Path]::GetFileName($path) + '.before'); Copy-Item -LiteralPath $path -Destination $backup -Force }
        if ($null -eq $text) { if ($backup -and (Get-FileHash -LiteralPath $path).Hash -eq (Get-FileHash -LiteralPath $UsbCa).Hash) { continue }; Copy-Item -LiteralPath $UsbCa -Destination $path -Force }
        else { if ($backup -and [IO.File]::ReadAllText($path) -eq $text) { continue }; [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false)) }
        Register-LabChange -Kind 'gateway-client' -Description ("wrote {0}" -f $path) -Undo ([pscustomobject]@{ type = 'file-restore'; path = $path; backup = $backup }) | Out-Null
        $notes += ('wrote ' + [IO.Path]::GetFileName($path))
    }
    # Restricted after the public files are in place (and again on every run); the token the operator
    # copies in later inherits this ACL: SYSTEM and Administrators full, the service account read.
    $reader = Resolve-LabSid $Layout.Reader; if (-not $reader) { $reader = 'S-1-5-19' }
    & icacls.exe $Layout.Folder /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*{0}:(OI)(CI)RX' -f $reader) /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not restrict $($Layout.Folder)" }
    $notes += ('ACL: SYSTEM and Administrators full, {0} read' -f $Layout.Reader)
    return (New-LabOutcome 'DONE' ($notes -join '; '))
}

function Get-LabCapstoneBootstrapTemplates {
    <#  Capstone's own two Gateway client bootstraps (RC.2: config\inference-gateway.client.template.json
        and ...research-client.template.json in its release), checked against the Gateway manifest:
        the advertised URL, the CA path, distinct token files in the protected folder, token_file only.  #>
    param([Parameter(Mandatory)][string]$ConfigDir, [Parameter(Mandatory)]$Layout)
    $files = [ordered]@{ capstone = (Join-Path $ConfigDir 'inference-gateway.client.template.json'); 'capstone-research' = (Join-Path $ConfigDir 'inference-gateway.research-client.template.json') }
    $problems = @(); $tokens = @{}
    foreach ($id in $files.Keys) {
        $j = Read-LabJson $files[$id]
        if (-not $j) { $problems += "no $id template at $($files[$id])"; continue }
        if ($j.application_id -ne $id) { $problems += "$id template is scoped to $($j.application_id)" }
        if ($j.url -ne $Layout.Url) { $problems += "$id template url $($j.url) is not the Gateway listener $($Layout.Url)" }
        if ($j.ca_file -ne $Layout.Ca) { $problems += "$id template ca_file $($j.ca_file) is not the Gateway contract's $($Layout.Ca)" }
        if ((Split-Path -Parent ([string]$j.token_file)) -ne $Layout.Folder) { $problems += "$id token_file $($j.token_file) is outside $($Layout.Folder)" }
        if ($j.PSObject.Properties.Name -contains 'token' -or $j.PSObject.Properties.Name -contains 'token_environment') { $problems += "$id template embeds or names a credential" }
        $tokens[$id] = [string]$j.token_file
    }
    if ($tokens['capstone'] -and $tokens['capstone'] -eq $tokens['capstone-research']) { $problems += 'the two identities share one token file' }
    return [pscustomobject]@{ Ok = ($problems.Count -eq 0); Problems = $problems; Operational = $files['capstone']; Research = $files['capstone-research']; Tokens = $tokens }
}

function ConvertFrom-LabCapstoneHealthOutput {
    <#  Pure. The JSON object Capstone's Test-CapstoneHealth.ps1 prints last (after any warnings).  #>
    param([string]$Text)
    $lines = @(([string]$Text) -split "`r?`n")
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        if ($lines[$i] -match '^\{\s*$') {
            try { return (($lines[$i..($lines.Count - 1)] -join "`n") | ConvertFrom-Json) } catch { return $null }
        }
    }
    return $null
}

$script:RetiredTokenVariables = @('CAPSTONE_INFERENCE_TOKEN', 'CAPSTONE_INFRASTRUCTURE_TOKEN', 'APPLICATION_INFERENCE_TOKEN')
function Get-LabRetiredTokenVariables {
    <#  Which retired Gateway token variables are still set at machine scope (names only; values are never
        returned or logged). Gateway 0.3 deployments use token_file only.  #>
    return @($script:RetiredTokenVariables | Where-Object { [Environment]::GetEnvironmentVariable($_, 'Machine') })
}

$script:VcRuntimeDlls = @('vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll', 'msvcp140.dll', 'msvcp140_atomic_wait.dll')

function Get-LabVcRuntimeState {
    <#  The Microsoft Visual C++ 2015-2022 runtime (x64) that Capstone's torch imports (c10.dll, torch_cpu.dll,
        torch_python.dll): which of its DLLs are missing from System32, and the installed version. A clean Windows has
        none of them; Capstone then stops at start with WinError 126 (DESKTOP-REDACT11, 2026-11-07).  #>
    param([string]$SystemDir = (Join-Path $env:WINDIR 'System32'))
    $missing = @($script:VcRuntimeDlls | Where-Object { -not (Test-Path -LiteralPath (Join-Path $SystemDir $_)) })
    $ver = ''; try { $ver = [string](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64' -ErrorAction Stop).Version } catch { }
    return [pscustomobject]@{ Ok = ($missing.Count -eq 0); Missing = $missing; Version = $ver }
}

function Set-LabCapstoneShortcut {
    <#  A "Capstone" icon on the desktop of every user of this laptop, opening Capstone in the browser. Capstone has no
        window of its own: it runs as a startup task and is used at http://<listen_host>:<listen_port>/ (the operator
        could not find it on DESKTOP-REDACT11, 2026-11-07).  #>
    param([Parameter(Mandatory)]$Config, $Run = $null, [string]$Desktop = (Join-Path $env:PUBLIC 'Desktop'))
    $s = Read-LabJson (Join-Path ([string]$Config.capstone.data_root) 'config\appsettings.json')
    if (-not $s -or -not $s.listen_host -or -not $s.listen_port) { return (New-LabOutcome 'PENDING' 'Capstone is not configured yet (no listen address)') }
    $base = 'http://{0}:{1}' -f $s.listen_host, $s.listen_port
    # Capstone 6.0.0-rc.4's installer makes "Capstone" and "Capstone Testing Lab" itself, but its updater does not
    # (DESKTOP-REDACT11 is updated from rc.3). Where Capstone's own icon exists, no second one is added.
    $notes = @(); $made = $false
    foreach ($i in @(@('Capstone', "$base/"), @('Capstone Testing Lab', "$base/testing-lab-login?next=/pressure-testing"))) {
        $name = $i[0]; $url = $i[1]
        $file = Join-Path $Desktop "$name.url"
        $text = "[InternetShortcut]`r`nURL=$url`r`n"
        if (Test-Path -LiteralPath (Join-Path $Desktop "$name.lnk")) {
            # rc.5's updater makes its own icons again: the lab's copy (1.5.1, rc.4) would show a second icon of the same name.
            if ((Test-Path -LiteralPath $file) -and [IO.File]::ReadAllText($file) -eq $text) {
                Remove-Item -LiteralPath $file -Force
                if ($Run) { Register-LabChange -Kind 'app' -Description "desktop icon $file removed (Capstone's own $name icon is there)" -Undo ([pscustomobject]@{ type = 'manual'; instructions = 'none needed' }) | Out-Null }
                $notes += "$name (Capstone's own icon; the lab's duplicate removed)"; $made = $true
            } else { $notes += "$name (Capstone's own icon)" }
            continue
        }
        if ((Test-Path -LiteralPath $file) -and [IO.File]::ReadAllText($file) -eq $text) { $notes += "$name opens $url"; continue }
        New-Item -ItemType Directory -Path $Desktop -Force | Out-Null
        [IO.File]::WriteAllText($file, $text, (New-Object Text.ASCIIEncoding))
        if ($Run) { Register-LabChange -Kind 'app' -Description "desktop icon $name ($file) opens $url" -Undo ([pscustomobject]@{ type = 'manual'; instructions = "delete $file" }) | Out-Null }
        $notes += "$name opens $url (new)"; $made = $true
    }
    return (New-LabOutcome $(if ($made) { 'DONE' } else { 'UNCHANGED' }) ("desktop icons on every user's desktop: " + ($notes -join '; ') + ' (Capstone runs in the background and starts with Windows)'))
}

# Capstone's Stop-Capstone.ps1 stops its task, then kills processes whose command line names Start-Capstone.ps1. Stopping
# a scheduled task ends only its first process (the PowerShell launcher, proven on the lab laptop 2026-11-08), so the
# Python web process (run_app.py) keeps serving the port, and nothing names Start-Capstone.ps1 any more. A new release
# then cannot start ("port already in use") while the old one passes every health check. This stops, and waits for,
# every process running from Capstone's install folder (its runtime and base Python; nothing else lives there).
$script:LabCapstoneStopSnippet = '$labRoot=(Resolve-Path -LiteralPath $InstallRoot).Path.TrimEnd(''\'')+''\'';$labT=(Get-Date).AddSeconds(30);do{$labP=@(Get-CimInstance Win32_Process|Where-Object{$_.ExecutablePath -and $_.ExecutablePath.StartsWith($labRoot,[StringComparison]::OrdinalIgnoreCase)});foreach($labX in $labP){Stop-Process -Id $labX.ProcessId -Force -ErrorAction SilentlyContinue};if($labP.Count){Start-Sleep -Seconds 1}}while($labP.Count -and (Get-Date) -lt $labT)'

function Stop-LabCapstoneProcesses {
    <#  After Capstone's own Stop-Capstone.ps1: stops what it leaves running (see $script:LabCapstoneStopSnippet) and
        returns how many processes from the install folder were still running after it.  #>
    param([Parameter(Mandatory)][string]$InstallRoot)
    if (-not (Test-Path -LiteralPath $InstallRoot)) { return 0 }
    $root = (Resolve-Path -LiteralPath $InstallRoot).Path.TrimEnd('\') + '\'
    $found = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) }).Count
    $ErrorActionPreference = 'Continue'
    & ([scriptblock]::Create($script:LabCapstoneStopSnippet))
    return $found
}

function ConvertTo-LabPatchedCapstoneUpdate {
    <#  Pure - unit tested. Exact edits to a copy of Capstone's Update-Capstone.ps1, else refuse. The copy runs from the
        run folder with -PackageRoot pointing at the untouched, verified package.
          rc.4: it checked health 5 s after starting the new release (Capstone needs tens of seconds), and its rollback
                started a task that was still running (DESKTOP-REDACT11, 2026-11-08): a 240 s wait at Capstone's own address,
                and a stop before rollback.
          rc.5: it has both, but its wait asks http://127.0.0.1:8000, where Capstone does not listen (it binds only
                its lab address, 192.0.2.201): the wait asks appsettings.json's listen_host:listen_port instead.
          both: after each of its Stop-Capstone.ps1 calls, every process running from the install folder is stopped
                too (Stop-Capstone.ps1 leaves the Python web process running; see $script:LabCapstoneStopSnippet).  #>
    param([Parameter(Mandatory)][string]$Text)
    $edits = @()
    $loop = [regex]::new("-Uri 'http://127\.0\.0\.1:8000/api/health'")
    if ($Text -match '\.AddSeconds\(240\)' -and $loop.Matches($Text).Count -eq 1) {
        $d = [regex]::new('\$deadline=\(Get-Date\)\.AddSeconds\(240\)')
        if ($d.Matches($Text).Count -ne 1) { throw 'Update-Capstone.ps1 does not have the expected 240-second wait; it was not patched' }
        $out = $d.Replace($Text, '$$labS=Get-Content -Raw -LiteralPath (Join-Path $$DataRoot ''config\appsettings.json'')|ConvertFrom-Json;$$deadline=(Get-Date).AddSeconds(240)', 1)
        $out = $loop.Replace($out, '-Uri (''http://{0}:{1}/api/health'' -f $$labS.listen_host,$$labS.listen_port)', 1)
        $edits += "the health wait asks Capstone's own address (appsettings.json listen_host), not the loopback address"
    } elseif ($Text -match 'Start-ScheduledTask -TaskName \$TaskName;Start-Sleep -Seconds 5') {
        $wait = 'Start-ScheduledTask -TaskName $TaskName;$labDeadline=(Get-Date).AddSeconds(240);do{Start-Sleep -Seconds 5;$labOk=$false;try{$labS=Get-Content -Raw -LiteralPath (Join-Path $DataRoot ''config\appsettings.json'')|ConvertFrom-Json;$labH=Invoke-RestMethod -TimeoutSec 5 -Uri (''http://{0}:{1}/api/health'' -f $labS.listen_host,$labS.listen_port);$labOk=[bool]$labH.ok}catch{}}until($labOk -or (Get-Date) -gt $labDeadline)'
        $p1 = [regex]::new('Start-ScheduledTask -TaskName \$TaskName;Start-Sleep -Seconds 5')
        if ($p1.Matches($Text).Count -ne 1) { throw 'Update-Capstone.ps1 does not have the expected 5-second health wait; it was not patched' }
        $out = $p1.Replace($Text, $wait.Replace('$', '$$'), 1)
        $p2 = [regex]::new('\}catch\{(\r?\n)( \$rollback=)')
        if ($p2.Matches($out).Count -ne 1) { throw 'Update-Capstone.ps1 does not have the expected rollback block; it was not patched' }
        $out = $p2.Replace($out, '}catch{$1 & (Join-Path $$InstallRoot ''tools\Stop-Capstone.ps1'') -TaskName $$TaskName$1$2', 1)
        $edits += 'a 240 s start wait at Capstone''s own address', 'a stop before rollback'
    } elseif ($Text -match '\.AddSeconds\(240\)' -and $Text -match '/api/health') {
        $out = $Text
    } else { throw 'Update-Capstone.ps1 does not have the expected health wait; it was not patched' }
    # The whole call, with any further arguments (rc.6 adds -InstallRoot and -DataRoot): the snippet goes after it, never
    # between its arguments. Atomic, so an already patched call is not matched again on a shorter prefix.
    $stop = [regex]::new("& \(Join-Path \`$InstallRoot 'tools\\Stop-Capstone\.ps1'\) -TaskName \`$TaskName(?>[^\r\n;]*)(?!;\`$labRoot=)")
    $n = $stop.Matches($out).Count
    if ($n -lt 1 -and $out -notmatch '\$labRoot=') { throw 'Update-Capstone.ps1 does not call Stop-Capstone.ps1 as expected; it was not patched' }
    if ($n) {
        $snippet = $script:LabCapstoneStopSnippet
        $out = $stop.Replace($out, [Text.RegularExpressions.MatchEvaluator] { param($m) $m.Value + ';' + $snippet })
        $edits += ('after each of its {0} Stop-Capstone.ps1 calls, the processes it leaves running are stopped' -f $n)
    }
    return ("# Patched by the Agent Arena lab provisioner (see ConvertTo-LabPatchedCapstoneUpdate): " + ($edits -join '; ') + ".`r`n" + $out)
}

function Test-LabCapstoneUpdaterFixed {
    <#  Pure - unit tested. An Update-Capstone.ps1 that needs no lab edit: it waits up to 240 s for /api/health at
        Capstone's own address, stops Capstone before it rolls back, and stops the processes Stop-Capstone.ps1 leaves
        running (by their path under the install folder). rc.5's has the wait and the stop, but asks 127.0.0.1 and
        trusts Stop-Capstone.ps1, so it is not fixed yet (ConvertTo-LabPatchedCapstoneUpdate).
        rc.6 does the process sweep in Stop-Capstone.ps1 itself (-StopText: the package's Stop-Capstone.ps1), and
        waits until those processes are gone.  #>
    param([Parameter(Mandatory)][string]$Text, [string]$StopText = '')
    $wait = $Text -match '\.AddSeconds\(240\)' -and $Text -match '/api/health'
    $stop = $Text -match "\}catch\{\r?\n\s*&\s*\(Join-Path \`$InstallRoot 'tools\\Stop-Capstone\.ps1'\)"
    $loopback = $Text -match "https?://(127\.0\.0\.1|localhost)[:/][^'""]*api/health"
    $sweep = $StopText -match 'ExecutablePath' -and $StopText -match 'StartsWith\(' -and $StopText -match 'Stop-Process'
    $orphans = ($Text -match 'ExecutablePath') -or $sweep
    return [bool]($wait -and $stop -and -not $loopback -and $orphans)
}

function New-LabCapstoneLabHeadsText {
    <#  Pure - unit tested. Capstone 6.0.0-rc.5's lab inventory (schema agent-arena.lab-heads/1): the lab's heads with
        their names and hardware, and the business service each one runs. Capstone merges it with the Gateway's
        models(); the Gateway, not this file, decides what a head serves. Read-only for Capstone.  #>
    param([Parameter(Mandatory)]$Config, [string]$GeneratedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $svc = @{}
    if (@($Config.PSObject.Properties.Name) -contains 'business_services') {
        foreach ($p in $Config.business_services.PSObject.Properties) { if (-not $p.Name.StartsWith('_')) { $svc[[string]$p.Value.head] = @($svc[[string]$p.Value.head]) + $p.Name | Where-Object { $_ } } }
    }
    $inv = @{}; if (@($Config.PSObject.Properties.Name) -contains 'head_inventory') { foreach ($h in @($Config.head_inventory)) { $inv[[string]$h.address] = $h } }
    $heads = foreach ($a in @($Config.known_heads | Sort-Object { [int](([string]$_) -split '\.')[-1] })) {
        $n = [int](([string]$a) -split '\.')[-1]; $i = $inv[[string]$a]
        [ordered]@{ number = $n; address = [string]$a; resource_id = ('head-' + ([string]$a).Replace('.', '-'))
            computer = $(if ($i) { [string]$i.computer } else { $null }); cpu = $(if ($i) { [string]$i.cpu } else { $null })
            ram_gb = $(if ($i -and $null -ne $i.ram_gb) { [double]$i.ram_gb } else { $null }); gpu = $(if ($i -and $i.gpu) { [string]$i.gpu } else { $null })
            services = @($svc[[string]$a] | Where-Object { $_ } | Sort-Object) }
    }
    $doc = [ordered]@{ schema = 'agent-arena.lab-heads/1'; generated_at = $GeneratedAt; gateway_url = ('https://{0}:{1}' -f $Config.hub, $Config.gateway.port); heads = @($heads) }
    return ($doc | ConvertTo-Json -Depth 5)
}

function Set-LabCapstoneLabHeads {
    <#  Writes C:\ProgramData\Capstone\config\lab-heads.json (UTF-8, no byte-order mark) when its heads differ from the
        lab's, and makes sure LOCAL SERVICE (Capstone) can read it. Capstone reads it on every Heads refresh.  #>
    param([Parameter(Mandatory)]$Config, $Run = $null)
    $file = Join-Path ([string]$Config.capstone.data_root) 'config\lab-heads.json'
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $file))) { return (New-LabOutcome 'PENDING' 'Capstone is not configured yet (no config folder)') }
    $text = New-LabCapstoneLabHeadsText -Config $Config
    $want = ($text | ConvertFrom-Json).heads | ConvertTo-Json -Depth 5 -Compress
    $have = $null; if (Test-Path -LiteralPath $file) { try { $have = ([IO.File]::ReadAllText($file) | ConvertFrom-Json).heads | ConvertTo-Json -Depth 5 -Compress } catch { } }
    $count = @(($text | ConvertFrom-Json).heads).Count
    $changed = $false
    if ($have -ne $want) {
        [IO.File]::WriteAllText($file, $text, (New-Object Text.UTF8Encoding $false))
        if ($Run) { Register-LabChange -Kind 'file' -Description "Capstone lab inventory $file ($count heads)" -Undo ([pscustomobject]@{ type = 'manual'; instructions = "delete $file (Capstone then shows the Gateway's heads by id)" }) | Out-Null }
        $changed = $true
    }
    $readers = @((Get-Acl -LiteralPath $file).Access | Where-Object { $_.AccessControlType -eq 'Allow' } | ForEach-Object { try { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { '' } })
    $acl = ''
    if (-not ($readers | Where-Object { $_ -in 'S-1-5-19', 'S-1-5-32-545', 'S-1-5-11', 'S-1-1-0' })) {
        & icacls.exe $file /grant '*S-1-5-19:(R)' /Q | Out-Null
        if ($LASTEXITCODE) { throw "could not give LOCAL SERVICE read access to $file" }
        $acl = '; LOCAL SERVICE given read access'; $changed = $true
    }
    return (New-LabOutcome $(if ($changed) { 'DONE' } else { 'UNCHANGED' }) ('{0}: {1} heads (2-10), names and hardware; the Gateway decides what each serves{2}' -f $file, $count, $acl))
}

function ConvertFrom-LabLabHeadsOutput {
    <#  Pure - unit tested. Capstone 6.0.0-rc.5's Test-CapstoneLabHeads.ps1 prints one JSON line per serving head
        (resource_id, model, status, gateway_request_id, queue_ms, duration_ms, arena_rows_written, reason or error),
        then {"passed":..,"targets":..}. No credential is in it.  #>
    param([string]$Text = '')
    $heads = New-Object Collections.ArrayList; $summary = $null
    foreach ($l in ($Text -split "`r?`n")) {
        $t = $l.Trim(); if (-not $t.StartsWith('{')) { continue }
        try { $o = $t | ConvertFrom-Json } catch { continue }
        if (@($o.PSObject.Properties.Name) -contains 'resource_id') { [void]$heads.Add($o) }
        elseif (@($o.PSObject.Properties.Name) -contains 'passed') { $summary = $o }
    }
    $lines = foreach ($h in $heads) {
        $n = ([string]$h.resource_id) -replace '^head-192-0-2-', 'head '
        # rc.8 also reports model_calls and pinned_head_observed: a run that made no model call, or whose call went
        # to another head, is not a pass (rc.7's paused "offline deterministic" runs made none).
        $names = @($h.PSObject.Properties.Name)
        $ok = ($h.status -eq 'completed' -and [int]$h.arena_rows_written -gt 0 -and
               ($names -notcontains 'model_calls' -or [int]$h.model_calls -ge 1) -and
               ($names -notcontains 'pinned_head_observed' -or [bool]$h.pinned_head_observed))
        $why = $(if ($h.reason) { [string]$h.reason } elseif ($h.error) { [string]$h.error } else { '' })
        # "paused" with no error = Capstone's own reviewer held the draft for a person (2026-11-11: every such run had made
        # its model calls on its pinned head). The head served it: not a fault. Proven only when the line says so.
        $paused = (-not $ok -and $h.status -eq 'paused' -and -not $h.error)
        $calls = $(if ($names -contains 'model_calls') { [int]$h.model_calls } else { -1 })
        $held = ($paused -and $calls -ge 1 -and ($names -notcontains 'pinned_head_observed' -or [bool]$h.pinned_head_observed))
        [pscustomobject]@{ Head = $n; ResourceId = [string]$h.resource_id; Ok = $ok; Held = $held; Paused = $paused
            Text = $(if ($ok) { '{0}: {1} completed in {2:N0} s, {3} Arena rows' -f $n, $h.model, ([double]$h.duration_ms / 1000), $h.arena_rows_written }
                     elseif ($held) { '{0}: {1} served: {2} model call(s) on this head, then Capstone''s reviewer held the draft for a person (not a fault)' -f $n, $h.model, $calls }
                     elseif ($paused) { '{0}: {1} paused: Capstone''s reviewer held the draft for a person; its output does not say whether the model was called on this head' -f $n, $h.model }
                     else { '{0}: {1} {2}{3}' -f $n, $(if ($h.model) { $h.model } else { '(no model)' }), $h.status, $(if ($why) { ' - ' + $why } else { '' }) }) }
    }
    $served = @($lines | Where-Object { $_.Ok -or $_.Held }).Count
    return [pscustomobject]@{ Passed = [bool]($summary -and $summary.passed); Targets = $(if ($summary) { [int]$summary.targets } else { $heads.Count }); Heads = @($lines); Read = [bool]$summary
                              Served = $served; AllServed = (@($lines).Count -gt 0 -and $served -eq @($lines).Count) }   # @(): PS 5.1 gives one object no .Count
}

function Test-LabCapstoneDatabase {
    <#  SQLite's own integrity check of operations.db, read-only, with Capstone's own Python. The failed rc.4 update copied
        the database back while Capstone was running (DESKTOP-REDACT11, 2026-11-08), so it is checked before the next update.  #>
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string]$WorkDir)
    $py = Join-Path ([string]$Config.capstone.install_root) 'runtime\Scripts\python.exe'
    $db = [string]$Config.capstone.operations_database
    if (-not (Test-Path -LiteralPath $py) -or -not (Test-Path -LiteralPath $db)) { return [pscustomobject]@{ Ok = $true; Detail = 'not checked (no Capstone runtime or database yet)' } }
    $script = Join-Path $WorkDir 'check-db.py'
    [IO.File]::WriteAllText($script, "import sqlite3,sys,pathlib`nu=pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro'`nc=sqlite3.connect(u,uri=True,timeout=10)`nprint(';'.join(r[0] for r in c.execute('PRAGMA integrity_check').fetchall()[:5]))`n", (New-Object Text.UTF8Encoding $false))
    $ErrorActionPreference = 'Continue'
    $out = (& $py -I $script $db 2>&1 | ForEach-Object { [string]$_ }) -join ' '
    return [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0 -and $out.Trim() -eq 'ok'); Detail = ('integrity_check: ' + $out.Trim()) }
}

function ConvertTo-LabEndToEndSummary {
    <#  Pure - unit tested. One line from the JSON of Capstone's Test-CapstoneEndToEnd.ps1 (capstone-end-to-end-v1):
        each call's identity, operation, model and head, or the error. It holds no credential.  #>
    param([string]$Text = '')
    $i = $Text.IndexOf('{'); $j = $Text.LastIndexOf('}')
    if ($i -lt 0 -or $j -le $i) { return ('no result: ' + (($Text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' ')) }
    try { $o = $Text.Substring($i, $j - $i + 1) | ConvertFrom-Json } catch { return 'unreadable result' }
    $calls = @($o.calls | ForEach-Object { '{0} {1} {2} on {3} ({4} ms)' -f $_.application_id, $_.operation, $_.model, $_.resource_id, $(if ($null -ne $_.duration_ms) { [math]::Round([double]$_.duration_ms) } else { '?' }) })
    if ($o.passed) { return ('passed: ' + ($calls -join '; ')) }
    return ('failed: {0}{1}' -f $(if ($o.error) { "$($o.error_class): $($o.error)" } else { 'no error given' }), $(if ($calls.Count) { ' (completed first: ' + ($calls -join '; ') + ')' } else { '' }))
}

function Get-LabCapstoneFailureLine {
    <#  Pure - unit tested. The most telling line of Capstone's logs: the last Python error, else the last line that
        says error/exception/denied, else the last line. Uvicorn's INFO lines go to the error log too.  #>
    param([string[]]$Lines = @())
    $l = @($Lines | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
    if (-not $l.Count) { return '' }
    $py = @($l | Where-Object { $_ -match '^[A-Za-z_.]+(Error|Exception)\b[:]?|^Traceback' -and $_ -notmatch '^Traceback' }) | Select-Object -Last 1
    if ($py) { return $py }
    $bad = @($l | Where-Object { $_ -match '(?i)error|exception|denied|refused|cannot|failed' -and $_ -notmatch '^INFO:' }) | Select-Object -Last 1
    if ($bad) { return $bad }
    return $l[-1]
}

function Wait-LabCapstoneHealth {
    <#  Waits for Capstone's /api/health after its startup task has started it, up to -Seconds, and stops early when
        the task has already ended with an error. Read-only.  #>
    param([Parameter(Mandatory)]$Config, [int]$Seconds = 180)
    $s = Read-LabJson (Join-Path ([string]$Config.capstone.data_root) 'config\appsettings.json')
    if (-not $s -or -not $s.listen_host -or -not $s.listen_port) { return [pscustomobject]@{ Ok = $false; Detail = 'no listen address in appsettings.json' } }
    $url = 'http://{0}:{1}/api/health' -f $s.listen_host, $s.listen_port
    $start = Get-Date; $deadline = $start.AddSeconds($Seconds); $shown = 0
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-LabHttp -Url $url -TimeoutSec 5
        if ($r.status -eq 200 -and $r.text -match '"ok"\s*:\s*true') { return [pscustomobject]@{ Ok = $true; Detail = ('answers at {0} after {1:N0} s' -f $url, ((Get-Date) - $start).TotalSeconds) } }
        $t = $null; try { $t = Get-ScheduledTask -TaskName ([string]$Config.capstone.task_name) -ErrorAction Stop } catch { }
        $elapsed = ((Get-Date) - $start).TotalSeconds
        if ($t -and [string]$t.State -ne 'Running' -and $elapsed -gt 20) {
            $info = $null; try { $info = $t | Get-ScheduledTaskInfo } catch { }
            return [pscustomobject]@{ Ok = $false; Detail = ('its startup task is {0} (last result 0x{1:X}) and {2} does not answer' -f $t.State, $(if ($info) { [int64]$info.LastTaskResult } else { 0 }), $url) }
        }
        if ($elapsed -ge $shown) { Write-LabLog ('  waiting for Capstone to start ({0:N0} s of up to {1})' -f $elapsed, $Seconds); $shown += 30 }
        Start-Sleep -Seconds 3
    }
    return [pscustomobject]@{ Ok = $false; Detail = "no answer at $url after $Seconds s" }
}

function Save-LabCapstoneDiagnostics {
    <#  When Capstone does not answer: its startup task's state and last result, and the end of its own logs
        (credentials redacted), into the run folder and onto the provisioner stick, so the cause comes back with
        the stick. Returns one line for the report.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Config, [string]$UsbRoot = '')
    $logs = Join-Path ([string]$Config.capstone.data_root) 'logs'
    $out = New-Object Collections.ArrayList
    $task = 'not registered'; $result = ''
    try {
        $t = Get-ScheduledTask -TaskName ([string]$Config.capstone.task_name) -ErrorAction Stop; $i = $t | Get-ScheduledTaskInfo
        $result = ('0x{0:X}' -f [int64]$i.LastTaskResult)
        $task = ('{0}, last run {1}, last result {2}, user {3}' -f $t.State, $i.LastRunTime, $result, $t.Principal.UserId)
    } catch { }
    [void]$out.Add("startup task: $task")
    $all = @()
    foreach ($f in 'capstone.error.log', 'capstone.log') {
        $p = Join-Path $logs $f
        $tail = @(); if (Test-Path -LiteralPath $p) { $tail = @(Get-Content -LiteralPath $p -Tail 60 -ErrorAction SilentlyContinue) }
        [void]$out.Add(''); [void]$out.Add("===== $p (last $($tail.Count) lines)"); foreach ($x in $tail) { [void]$out.Add([string]$x) }
        if ($f -eq 'capstone.error.log') { $all = $tail }
    }
    $text = Protect-LabText ($out -join "`r`n")
    $file = Join-Path $Run.Folder 'capstone-diagnostics.txt'
    [IO.File]::WriteAllText($file, $text, (New-Object Text.UTF8Encoding $false))
    $where = $file
    if ($UsbRoot) { try { $d = Join-Path $UsbRoot ('reports\' + $env:COMPUTERNAME); New-Item -ItemType Directory -Path $d -Force | Out-Null; $u = Join-Path $d ($Run.Id + '-capstone-diagnostics.txt'); Copy-Item -LiteralPath $file -Destination $u -Force; $where = "$file (copied to $u)" } catch { } }
    $line = Get-LabCapstoneFailureLine -Lines $all
    return ('startup task {0}; {1}; logs: {2}' -f $(if ($result) { "last result $result" } else { $task }), $(if ($line) { 'Capstone''s error log ends with: ' + (Protect-LabText $line) } else { 'its error log is empty' }), $where)
}

function Get-LabLogFailureLine {
    <#  Pure - unit tested. The line of a log that says what went wrong: the last exception or refusal line, else the
        last non-empty line.  #>
    param([string[]]$Lines)
    $l = @($Lines | Where-Object { $_ -and $_.Trim() })
    if (-not $l.Count) { return '' }
    $hit = @($l | Where-Object { $_ -match '\b\w*(Error|Exception|Refused|Denied)\b|refused|not permitted|denied|HTTP \d{3}|Gateway \d{3}|certificate' -and $_ -notmatch '^\s*File "' })
    return ([string]$(if ($hit.Count) { $hit[-1] } else { $l[-1] })).Trim()
}

function Save-LabResearchLabDiagnostics {
    <#  After Capstone's Test-CapstoneLabHeads.ps1: for every Research Lab run made since -Since, the head it was pinned
        to, its status and error, and the end of its worker.log (Capstone's experiment worker writes the real cause
        there, which the test's JSON does not carry). Credentials redacted. Into the run folder and onto the stick.
        Returns @{ File; ByHead = @{ resource_id = 'failure line' } }.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)]$Config, [string]$UsbRoot = '', [datetime]$Since = (Get-Date).AddHours(-1))
    $root = [string]$Config.capstone.data_root
    $out = New-Object Collections.ArrayList; $byHead = @{}
    $dirs = @(foreach ($d in @((Join-Path $root 'data\experiments'), (Join-Path $root 'experiments'))) { if (Test-Path -LiteralPath $d) { Get-ChildItem -LiteralPath $d -Directory -Filter 'EXP-*' -ErrorAction SilentlyContinue | Where-Object { $_.CreationTime -ge $Since } } })
    [void]$out.Add(('Research Lab runs since {0:yyyy-MM-dd HH:mm:ss}: {1} experiment(s)' -f $Since, $dirs.Count))
    foreach ($e in ($dirs | Sort-Object CreationTime)) {
        foreach ($rd in @(Get-ChildItem -LiteralPath $e.FullName -Directory -Filter 'RUN-*' -ErrorAction SilentlyContinue | Sort-Object CreationTime)) {
            $head = ''; $model = ''; $status = ''; $err = ''
            try { $m = Get-Content -LiteralPath (Join-Path $rd.FullName 'manifest.json') -Raw | ConvertFrom-Json; $head = [string]$m.gateway_head_id; $model = @($m.profile.routes.PSObject.Properties | ForEach-Object { $_.Value.model } | Select-Object -Unique) -join ',' } catch { }
            try { $res = Get-Content -LiteralPath (Join-Path $rd.FullName 'result.json') -Raw | ConvertFrom-Json; $status = [string]$res.status; $err = [string]$res.error } catch { $status = 'no result.json' }
            $wl = Join-Path $rd.FullName 'worker.log'; $tail = @(); if (Test-Path -LiteralPath $wl) { $tail = @(Get-Content -LiteralPath $wl -Tail 80 -ErrorAction SilentlyContinue) }
            $line = if ($err) { $err } else { Get-LabLogFailureLine -Lines $tail }
            if ($head -and $status -ne 'completed' -and -not $byHead.ContainsKey($head)) { $byHead[$head] = (Protect-LabText $line) }
            [void]$out.Add(''); [void]$out.Add(('===== {0}\{1}: head {2}, model {3}, status {4}' -f $e.Name, $rd.Name, $(if ($head) { $head } else { 'any' }), $model, $status))
            if ($err) { [void]$out.Add('error: ' + $err) }
            [void]$out.Add("--- worker.log (last $($tail.Count) lines)"); foreach ($x in $tail) { [void]$out.Add([string]$x) }
        }
    }
    foreach ($f in 'capstone.error.log', 'capstone.log') {
        $p = Join-Path $root ('logs\' + $f); $tail = @(); if (Test-Path -LiteralPath $p) { $tail = @(Get-Content -LiteralPath $p -Tail 80 -ErrorAction SilentlyContinue) }
        [void]$out.Add(''); [void]$out.Add("===== $p (last $($tail.Count) lines)"); foreach ($x in $tail) { [void]$out.Add([string]$x) }
    }
    $file = Join-Path $Run.Folder 'research-lab-diagnostics.txt'
    [IO.File]::WriteAllText($file, (Protect-LabText ($out -join "`r`n")), (New-Object Text.UTF8Encoding $false))
    $where = $file
    if ($UsbRoot) { try { $d = Join-Path $UsbRoot ('reports\' + $env:COMPUTERNAME); New-Item -ItemType Directory -Path $d -Force | Out-Null; $u = Join-Path $d ($Run.Id + '-research-lab-diagnostics.txt'); Copy-Item -LiteralPath $file -Destination $u -Force; $where = $u } catch { } }
    return [pscustomobject]@{ File = $where; ByHead = $byHead; Experiments = $dirs.Count }
}

function Save-LabHubDiagnostics {
    <#  On every hub run: the end of the Inference Gateway's logs, the Fleet Commit status and requests, the Gateway
        Models screen's change log, and Arena's event-service log, plus every line among them that names an HTTP 400,
        a certificate, SSL or TLS. Credentials redacted. Into the run folder and onto the stick. Returns @{ File;
        Suspect (count); Last (the newest such line) }.  #>
    param([Parameter(Mandatory)]$Run, [string]$UsbRoot = '')
    $out = New-Object Collections.ArrayList; $files = New-Object Collections.ArrayList
    $sources = @(
        @{ Dir = 'C:\ProgramData\InferenceGateway\logs'; Filter = '*'; Newest = 4; Tail = 120 },
        @{ Dir = 'C:\ProgramData\AgentArenaLab\fleet\status'; Filter = '*.json'; Newest = 4; Tail = 200 },
        @{ Dir = 'C:\ProgramData\AgentArenaLab\fleet\requests'; Filter = '*.json'; Newest = 3; Tail = 80 },
        @{ Dir = 'C:\ProgramData\AgentArenaLab\gateway\models-console'; Filter = '*.log'; Newest = 3; Tail = 120 },
        @{ Dir = 'C:\ProgramData\AgentArenaLab\gateway\models-console\commits'; Filter = '*.json'; Newest = 3; Tail = 120 }
    )
    foreach ($u in @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue)) {
        $a = Join-Path $u.FullName 'AppData\Local\AgentArenaService'
        if (Test-Path -LiteralPath $a) { $sources += @{ Dir = $a; Filter = '*.log*'; Newest = 3; Tail = 120 } }
    }
    foreach ($s in $sources) {
        if (-not (Test-Path -LiteralPath $s.Dir)) { [void]$out.Add("===== $($s.Dir): not present"); continue }
        foreach ($f in @(Get-ChildItem -LiteralPath $s.Dir -File -Filter $s.Filter -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First $s.Newest)) {
            $tail = @(); try { $tail = @(Get-Content -LiteralPath $f.FullName -Tail $s.Tail -ErrorAction Stop) } catch { $tail = @('(not readable: ' + $_.Exception.Message + ')') }
            [void]$files.Add([pscustomobject]@{ Name = $f.FullName; Lines = $tail; At = $f.LastWriteTime })
            [void]$out.Add(''); [void]$out.Add(('===== {0} ({1:yyyy-MM-dd HH:mm:ss}, last {2} lines)' -f $f.FullName, $f.LastWriteTime, $tail.Count)); foreach ($x in $tail) { [void]$out.Add([string]$x) }
        }
    }
    # Smart App Control / App Control blocks of unsigned programs (DESKTOP-REDACT10 has it on): Windows' own record, last 12 h.
    $ci = @(); try { $ci = @(Get-WinEvent -LogName 'Microsoft-Windows-CodeIntegrity/Operational' -MaxEvents 400 -ErrorAction Stop | Where-Object { $_.TimeCreated -gt (Get-Date).AddHours(-12) -and $_.Id -in 3033, 3034, 3077, 3089 }) } catch { }
    $ciLines = @($ci | Select-Object -First 40 | ForEach-Object { '{0:yyyy-MM-dd HH:mm:ss} event {1}: {2}' -f $_.TimeCreated, $_.Id, ((([string]$_.Message) -split "`r?`n")[0]) })
    [void]$files.Add([pscustomobject]@{ Name = 'CodeIntegrity (Smart App Control blocks)'; Lines = @($ciLines | ForEach-Object { 'certificate/signature block: ' + $_ }); At = $(if ($ci.Count) { ($ci | Sort-Object TimeCreated | Select-Object -Last 1).TimeCreated } else { $null }) })
    [void]$out.Add(''); [void]$out.Add("===== Windows CodeIntegrity log, blocks in the last 12 h: $($ci.Count)"); foreach ($x in $ciLines) { [void]$out.Add($x) }
    $susFiles = @(foreach ($f in $files) { if (@($f.Lines | Where-Object { [string]$_ -match '(?<![,.\d])\b400\b(?![,.]\d)|(?i)certif|\bssl\b|\btls\b|bad request' }).Count) { $f } })
    $recent = @($susFiles | Where-Object { $_.At -and $_.At -gt (Get-Date).AddMinutes(-60) }).Count
    $newestAt = @($susFiles | Where-Object { $_.At } | Sort-Object At | Select-Object -Last 1 | ForEach-Object { $_.At })
    $sus = @(foreach ($f in $files) { foreach ($x in $f.Lines) { if ([string]$x -match '(?<![,.\d])\b400\b(?![,.]\d)|(?i)certif|\bssl\b|\btls\b|bad request') { '{0}: {1}' -f (Split-Path -Leaf $f.Name), ([string]$x).Trim() } } })
    $head = @("Hub diagnostics $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') on $env:COMPUTERNAME", '', "===== lines naming HTTP 400, a certificate, SSL or TLS: $($sus.Count) (newest last)") + @($sus | Select-Object -Last 60)
    $file = Join-Path $Run.Folder 'hub-diagnostics.txt'
    [IO.File]::WriteAllText($file, (Protect-LabText ((@($head) + @('') + @($out)) -join "`r`n")), (New-Object Text.UTF8Encoding $false))
    $where = $file
    if ($UsbRoot) { try { $d = Join-Path $UsbRoot ('reports\' + $env:COMPUTERNAME); New-Item -ItemType Directory -Path $d -Force | Out-Null; $u = Join-Path $d ($Run.Id + '-hub-diagnostics.txt'); Copy-Item -LiteralPath $file -Destination $u -Force; $where = $u } catch { } }
    return [pscustomobject]@{ File = $where; Suspect = $sus.Count; Recent = $recent; NewestAt = $(if ($newestAt) { $newestAt[0] } else { $null }); Last = $(if ($sus.Count) { Protect-LabText $sus[-1] } else { '' }) }
}

Export-ModuleMember -Function @(
    'Get-LabLogFailureLine', 'Save-LabResearchLabDiagnostics', 'Save-LabHubDiagnostics',
    'Set-LabCapstoneShortcut', 'ConvertTo-LabPatchedCapstoneUpdate', 'Test-LabCapstoneUpdaterFixed', 'Stop-LabCapstoneProcesses', 'New-LabCapstoneLabHeadsText', 'Set-LabCapstoneLabHeads', 'ConvertFrom-LabLabHeadsOutput', 'Test-LabCapstoneDatabase', 'ConvertTo-LabEndToEndSummary', 'Get-LabVcRuntimeState', 'Get-LabCapstoneFailureLine', 'Wait-LabCapstoneHealth', 'Save-LabCapstoneDiagnostics',
    'ConvertFrom-LabCapstoneHealthOutput', 'Get-LabRetiredTokenVariables', 'Get-LabCapstoneBootstrapTemplates', 'Test-LabCapstoneBootstraps',    'Read-LabGatewayManifestFromZip', 'Get-LabGatewayClientLayout', 'New-LabGatewayClientConfigText', 'Test-LabTokenFileAcl', 'Get-LabGatewayClientState', 'Set-LabGatewayClientFiles',
    'Get-LabCapstoneState', 'Get-LabGatewayBootstrapState', 'Find-LabCapstonePackage', 'Expand-LabCapstonePackage', 'Invoke-LabExternalScript',
    'Get-LabGatewayState', 'Get-LabSuppliedPackage', 'Test-LabDirectOllama'
)
