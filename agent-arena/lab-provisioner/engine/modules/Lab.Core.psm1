<#
    Lab.Core - shared primitives of the Agent Arena Lab Provisioner.

    Windows PowerShell 5.1 only: no ternary, no null-coalescing, no -AsHashtable.

    A run is one pass through the six stages (Inspect, Plan, Configure, Register,
    Verify, Report) for one machine role. Everything a run does is written under
    C:\ProgramData\AgentArenaLab\runs\<run id>\ so a failed run can be diagnosed
    and every change it made can be undone from its own record (changes.json).
#>

# 1.6.29: the version comes from the release's own VERSION file (engine\modules -> release root). 1.6.28 shipped with
# '1.6.27' typed here, so its banner and reports named the wrong release.
$script:Version = try { (Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'VERSION') -ErrorAction Stop).Trim() } catch { 'unknown' }
$script:Run = $null
$script:ApprovalProvider = $null      # tests replace the console prompt with a scriptblock
$script:InputProvider = $null

function Get-LabVersion { return $script:Version }

function Get-LabRoot {
    param([string]$Override = '')
    if ($Override) { return $Override }
    if ($env:AGENT_ARENA_LAB_ROOT) { return $env:AGENT_ARENA_LAB_ROOT }
    $base = $env:ProgramData
    if (-not $base) { $base = 'C:\ProgramData' }
    return (Join-Path $base 'AgentArenaLab')
}

function Get-LabConfig {
    param([Parameter(Mandatory)][string]$Path)
    $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.schema -ne 'agent-arena.lab-network/1') { throw "Unsupported lab configuration schema in $Path" }
    return $config
}

function Test-LabElevated {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

function Save-LabJson {
    param([Parameter(Mandatory)]$InputObject, [Parameter(Mandatory)][string]$Path, [int]$Depth = 12)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $json = $InputObject | ConvertTo-Json -Depth $Depth
    $tmp = $Path + '.tmp'
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding $false))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Read-LabJson {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

# ---------------------------------------------------------------------------
# Redaction. Applied to every report and log line. Credentials are never
# put into run objects in the first place; this is the second line of defence.
# ---------------------------------------------------------------------------

$script:SecretKey = '(?i)(^|_|-)(secret|secrets|token|password|passwd|code|codes|sealed_secret|signature|private_key|api_key|apikey|key_material)($|_|-)'
$script:SafeKeys = @('token_environment_name', 'code_file_present', 'exit_code', 'status_code', 'http_status', 'error_code', 'zip_code')

function Protect-LabText {
    param([AllowNull()][AllowEmptyString()][string]$Text)
    if ($null -eq $Text) { return $Text }
    $t = $Text -replace '(?i)ARENA-?[A-Z2-9]{4}(-?[A-Z2-9]{4}){3}', '[REDACTED-CODE]'
    $t = $t -replace '(?i)(bearer\s+)[^\s"'']+', '$1[REDACTED]'
    $t = $t -replace '(?i)(x-(infrastructure|capstone-developer)-token\s*[:=]\s*)[^\s"'']+', '$1[REDACTED]'
    $t = $t -replace '(?i)("?(sealed_secret|secret|password|token)"?\s*[:=]\s*")[^"]*(")', '$1[REDACTED]$3'
    return $t
}

function Protect-LabObject {
    param($InputObject, [int]$Depth = 0)
    if ($null -eq $InputObject) { return $null }
    if ($Depth -gt 20) { return '[depth limit]' }
    if ($InputObject -is [string]) { return (Protect-LabText $InputObject) }
    if ($InputObject -is [ValueType]) { return $InputObject }
    if ($InputObject -is [Collections.IDictionary]) {
        $out = [ordered]@{}
        foreach ($k in @($InputObject.Keys)) {
            $name = [string]$k
            if (($script:SafeKeys -notcontains $name.ToLowerInvariant()) -and $name -match $script:SecretKey -and $null -ne $InputObject[$k] -and -not ($InputObject[$k] -is [bool])) {
                $out[$name] = '[REDACTED]'
            } else { $out[$name] = Protect-LabObject $InputObject[$k] ($Depth + 1) }
        }
        return [pscustomobject]$out
    }
    if ($InputObject -is [Collections.IEnumerable]) {
        $list = @()
        foreach ($item in $InputObject) { $list += ,(Protect-LabObject $item ($Depth + 1)) }
        return ,$list
    }
    $props = @($InputObject.PSObject.Properties | Where-Object { $_.MemberType -in 'NoteProperty', 'Property' })
    if ($props.Count -eq 0) { return (Protect-LabText ([string]$InputObject)) }
    $h = [ordered]@{}
    foreach ($p in $props) { $h[$p.Name] = $p.Value }
    return (Protect-LabObject $h $Depth)
}

# ---------------------------------------------------------------------------
# Runs, logging and the change journal
# ---------------------------------------------------------------------------

function New-LabRun {
    param([Parameter(Mandatory)][string]$Role, [string]$Root = '', [string]$UsbRoot = '', $Config = $null)
    $base = Get-LabRoot $Root
    $id = '{0}-{1}-{2}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $env:COMPUTERNAME, $Role
    $folder = Join-Path $base ('runs\' + $id)
    foreach ($d in @($folder, (Join-Path $folder 'backups'), (Join-Path $base 'reports'), (Join-Path $base 'state'))) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    $run = [pscustomobject][ordered]@{
        Id = $id; Role = $Role; Root = $base; Folder = $folder
        Backups = (Join-Path $folder 'backups'); Log = (Join-Path $folder 'run.log')
        ChangesFile = (Join-Path $folder 'changes.json'); StateDir = (Join-Path $base 'state')
        ReportsDir = (Join-Path $base 'reports'); UsbRoot = $UsbRoot; Config = $Config
        StartedAt = (Get-Date).ToString('o'); Version = $script:Version
        Steps = New-Object System.Collections.ArrayList
        Changes = New-Object System.Collections.ArrayList
        Registration = New-Object System.Collections.ArrayList
        Verification = New-Object System.Collections.ArrayList
        Warnings = New-Object System.Collections.ArrayList
        Pending = New-Object System.Collections.ArrayList
        Approvals = New-Object System.Collections.ArrayList
        Inspection = $null; Plan = $null; Models = $null
        Stopped = $false; StopReason = ''
    }
    $script:Run = $run
    Write-LabLog ("Agent Arena Lab Provisioner {0}, run {1}" -f $script:Version, $id) -Level STEP
    return $run
}

function Get-LabRun { return $script:Run }
function Set-LabRun { param($Run) $script:Run = $Run }

function Write-LabLog {
    param([Parameter(Mandatory, Position = 0)][AllowEmptyString()][string]$Message,
          [ValidateSet('INFO', 'STEP', 'OK', 'WARN', 'ERROR', 'DEBUG')][string]$Level = 'INFO', [switch]$Quiet)
    $safe = Protect-LabText $Message
    if (-not $Quiet -and $Level -ne 'DEBUG') {
        $colour = @{ INFO = 'Gray'; STEP = 'Cyan'; OK = 'Green'; WARN = 'Yellow'; ERROR = 'Red' }[$Level]
        try { Write-Host $safe -ForegroundColor $colour } catch { }
    }
    if ($script:Run) {
        try { Add-Content -LiteralPath $script:Run.Log -Value ('{0} [{1,-5}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $safe) -Encoding UTF8 } catch { }
    }
}

function Write-LabStage {
    param([Parameter(Mandatory)][ValidatePattern('^[A-F]$')][string]$Letter, [Parameter(Mandatory)][string]$Title)
    Write-LabLog '' -Level INFO
    Write-LabLog ('==== Stage {0} - {1} ====' -f $Letter, $Title) -Level STEP
}

function Add-LabWarning { param([Parameter(Mandatory)][string]$Text) if ($script:Run) { [void]$script:Run.Warnings.Add((Protect-LabText $Text)) }; Write-LabLog ('WARNING: ' + $Text) -Level WARN }
function Add-LabPending { param([Parameter(Mandatory)][string]$Area, [Parameter(Mandatory)][string]$Text) if ($script:Run) { [void]$script:Run.Pending.Add([pscustomobject]@{ area = $Area; detail = (Protect-LabText $Text) }) }; Write-LabLog ('PENDING ({0}): {1}' -f $Area, $Text) -Level WARN }

function Add-LabVerification {
    <#  One verification result. Status: PASS, FAIL, PENDING (needs another machine or an external
        party) or SKIPPED (does not apply). Scope says where the evidence comes from: local
        (this machine), lan (a real connection over the LAN) or external (another project).  #>
    param([Parameter(Mandatory)][string]$Check, [Parameter(Mandatory)][ValidateSet('PASS', 'FAIL', 'PENDING', 'SKIPPED')][string]$Status,
          [string]$Detail = '', [ValidateSet('local', 'lan', 'external')][string]$Scope = 'local', [bool]$Required = $true)
    $entry = [pscustomobject][ordered]@{ check = $Check; status = $Status; scope = $Scope; required = $Required; detail = (Protect-LabText $Detail) }
    if ($script:Run) { [void]$script:Run.Verification.Add($entry) }
    $level = @{ PASS = 'OK'; FAIL = 'ERROR'; PENDING = 'WARN'; SKIPPED = 'INFO' }[$Status]
    Write-LabLog ('  {0,-7} {1}: {2}' -f $Status, $Check, $Detail) -Level $level
    return $entry
}

function Add-LabRegistration {
    param([Parameter(Mandatory)][string]$Item, [Parameter(Mandatory)][ValidateSet('REGISTERED', 'ALREADY_REGISTERED', 'PENDING', 'FAILED', 'NOT_APPLICABLE')][string]$Status, [string]$Detail = '')
    $entry = [pscustomobject][ordered]@{ item = $Item; status = $Status; detail = (Protect-LabText $Detail) }
    if ($script:Run) { [void]$script:Run.Registration.Add($entry) }
    $level = @{ REGISTERED = 'OK'; ALREADY_REGISTERED = 'OK'; PENDING = 'WARN'; FAILED = 'ERROR'; NOT_APPLICABLE = 'INFO' }[$Status]
    Write-LabLog ('  {0,-18} {1}: {2}' -f $Status, $Item, $Detail) -Level $level
    return $entry
}

function Register-LabChange {
    <#  Records one applied change with what is needed to undo it. Written to disk at once,
        so a run that dies half-way can still be rolled back.  #>
    param([Parameter(Mandatory)][string]$Kind, [Parameter(Mandatory)][string]$Description, $Undo = $null, [string]$Backup = '')
    $entry = [pscustomobject][ordered]@{ at = (Get-Date).ToString('o'); kind = $Kind; description = $Description; backup = $Backup; undo = $Undo; undone = $false }
    if ($script:Run) {
        [void]$script:Run.Changes.Add($entry)
        Save-LabJson -InputObject ([pscustomobject]@{ run = $script:Run.Id; computer = $env:COMPUTERNAME; changes = @($script:Run.Changes) }) -Path $script:Run.ChangesFile
    }
    Write-LabLog ('  changed: ' + $Description) -Level OK
    return $entry
}

# ---------------------------------------------------------------------------
# Operator interaction
# ---------------------------------------------------------------------------

function Set-LabApprovalProvider { param([scriptblock]$Provider) $script:ApprovalProvider = $Provider }
function Set-LabInputProvider { param([scriptblock]$Provider) $script:InputProvider = $Provider }

function Request-LabApproval {
    <#  Explicit approval. The operator types the exact word; anything else is a refusal.  #>
    param([Parameter(Mandatory)][string]$Prompt, [string]$Word = 'APPROVE', [ValidateSet('change', 'privileged', 'disruptive', 'destructive')][string]$Kind = 'change')
    if ($script:ApprovalProvider) { $answer = & $script:ApprovalProvider $Prompt $Word $Kind }
    else {
        $colour = 'Yellow'; if ($Kind -in 'disruptive', 'destructive') { $colour = 'Red' }
        Write-Host ''
        Write-Host ('  ' + $Prompt) -ForegroundColor $colour
        $answer = Read-Host ('  Type {0} to go ahead, or press Enter to decline' -f $Word)
    }
    $ok = ([string]$answer).Trim() -ceq $Word
    Write-LabLog ('  approval ({0}) "{1}": {2}' -f $Kind, $Prompt, $(if ($ok) { 'approved' } else { 'declined' })) -Quiet
    # Kept in the report too: the run log stays on the machine, the report is what reaches the USB.
    if ($script:Run -and $script:Run.PSObject.Properties.Name -contains 'Approvals') { [void]$script:Run.Approvals.Add([pscustomobject][ordered]@{ at = (Get-Date).ToString('o'); kind = $Kind; word = $Word; prompt = (Protect-LabText $Prompt); result = $(if ($ok) { 'approved' } else { 'declined' }) }) }
    return $ok
}

function Read-LabInput {
    <#  Free text from the operator. -Secret input is never logged or echoed into run files.  #>
    param([Parameter(Mandatory)][string]$Prompt, [switch]$Secret, [string]$Default = '')
    if ($script:InputProvider) { $value = & $script:InputProvider $Prompt }
    else { $value = Read-Host ('  ' + $Prompt) }
    if ([string]::IsNullOrWhiteSpace($value)) { $value = $Default }
    if (-not $Secret) { Write-LabLog ('  input "{0}": {1}' -f $Prompt, $value) -Quiet }
    else { Write-LabLog ('  input "{0}": (secret, {1} characters, not recorded)' -f $Prompt, ([string]$value).Length) -Quiet }
    return ([string]$value).Trim()
}

$script:SecretProvider = $null
function Set-LabSecretProvider { param([scriptblock]$Provider) $script:SecretProvider = $Provider }

function Test-LabSecretPortable {
    <#  Pure - unit tested. True when a password survives this computer's ANSI code page unchanged, which
        is how it reaches another program's stdin (the Gateway installer's --password-stdin), and holds
        no control character.  #>
    param([string]$Text)
    if ($Text -match '[\x00-\x1F\x7F]') { return $false }
    $enc = [Text.Encoding]::GetEncoding([Text.Encoding]::Default.CodePage, (New-Object Text.EncoderExceptionFallback), (New-Object Text.DecoderExceptionFallback))
    try { return ($enc.GetString($enc.GetBytes($Text)) -ceq $Text) } catch { return $false }
}

function Read-LabSecret {
    <#  A password typed twice, masked, returned as a SecureString. Never logged, never written.  #>
    param([Parameter(Mandatory)][string]$Prompt, [switch]$Confirm)
    if ($script:SecretProvider) { return (& $script:SecretProvider $Prompt) }
    for ($i = 0; $i -lt 3; $i++) {
        $a = Read-Host -AsSecureString ('  ' + $Prompt)
        if (-not $Confirm) { return $a }
        $b = Read-Host -AsSecureString '  Type it again'
        $pa = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($a); $pb = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($b)
        $portable = $true
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pa); $same = ($plain -ceq [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pb)) -and $a.Length -ge 14; $portable = Test-LabSecretPortable $plain; $plain = $null }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pa); [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pb) }
        if ($same -and $portable) { return $a }
        if ($same) { Write-Host '  That password has a character this computer''s installer cannot pass on unchanged. Use letters, digits and keyboard symbols such as ! @ # $ % ^ & * - _ + = ? . , and try again.' -ForegroundColor Yellow; continue }
        Write-Host '  The two entries differ, or the password is shorter than 14 characters. Try again.' -ForegroundColor Yellow
    }
    throw 'no password confirmed'
}

# ---------------------------------------------------------------------------
# Steps. A step that fails stops the stage: nothing after it runs and the run
# can never report success (requirement: no silent continuation).
# ---------------------------------------------------------------------------

function Invoke-LabStep {
    param([Parameter(Mandatory)][string]$Id, [Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][scriptblock]$Action, [switch]$ContinueOnFailure)
    $run = $script:Run
    $step = [pscustomobject][ordered]@{ id = $Id; title = $Title; status = 'RUNNING'; started = (Get-Date).ToString('o'); finished = ''; detail = '' }
    if ($run) {
        if ($run.Stopped) { $step.status = 'NOT_RUN'; $step.detail = 'an earlier step failed: ' + $run.StopReason; [void]$run.Steps.Add($step); Write-LabLog ('  NOT RUN {0}' -f $Title) -Level WARN; return $null }
        [void]$run.Steps.Add($step)
    }
    Write-LabLog ('-> {0}' -f $Title) -Level STEP
    try {
        $result = & $Action
        $outcome = 'DONE'; $detail = ''
        if ($result -is [pscustomobject] -or $result -is [hashtable]) {
            if ($result.PSObject.Properties.Name -contains 'Outcome' -and $result.Outcome) { $outcome = [string]$result.Outcome }
            if ($result.PSObject.Properties.Name -contains 'Detail') { $detail = [string]$result.Detail }
            if ($result -is [hashtable]) { if ($result.Outcome) { $outcome = [string]$result.Outcome }; if ($result.Detail) { $detail = [string]$result.Detail } }
        }
        if ($outcome -notin 'DONE', 'UNCHANGED', 'SKIPPED', 'PENDING', 'DECLINED', 'FAILED') { $outcome = 'DONE' }
        $step.status = $outcome; $step.detail = Protect-LabText $detail
        if ($outcome -eq 'FAILED') { throw ('step reported failure: ' + $detail) }
        $level = 'OK'; if ($outcome -in 'PENDING', 'DECLINED') { $level = 'WARN' }
        Write-LabLog ('   {0}: {1}' -f $outcome, $detail) -Level $level
        return $result
    } catch {
        $step.status = 'FAILED'; $step.detail = Protect-LabText $_.Exception.Message
        Write-LabLog ('   FAILED: {0}' -f $_.Exception.Message) -Level ERROR
        if ($run -and -not $ContinueOnFailure) { $run.Stopped = $true; $run.StopReason = ('{0}: {1}' -f $Title, $_.Exception.Message) }
        return $null
    } finally { $step.finished = (Get-Date).ToString('o') }
}

function New-LabOutcome { param([string]$Outcome = 'DONE', [string]$Detail = '', $Data = $null) return [pscustomobject]@{ Outcome = $Outcome; Detail = $Detail; Data = $Data } }

function Get-LabStagedStatus {
    <#  Pure - unit tested. Verification that knows the staged rollout: a check on what a later stage
        installs is PENDING until that stage is complete, and FAIL only once it is complete and the check
        still does not hold. A stage is never treated as complete because a check failed.  #>
    param($Ok, $StageComplete)     # untyped: an absent fact ($null, '') is "not yet", never a parse error
    if ([bool]$Ok) { return 'PASS' }
    if ([bool]$StageComplete) { return 'FAIL' }
    return 'PENDING'
}

# ---------------------------------------------------------------------------
# Small helpers shared by several modules
# ---------------------------------------------------------------------------

function Test-LabTcp {
    param([Parameter(Mandatory)][string]$HostName, [Parameter(Mandatory)][int]$Port, [int]$TimeoutMs = 2500)
    $client = New-Object Net.Sockets.TcpClient
    try { $ok = $client.BeginConnect($HostName, $Port, $null, $null).AsyncWaitHandle.WaitOne($TimeoutMs) -and $client.Connected } catch { $ok = $false }
    try { $client.Close() } catch { }
    return [bool]$ok
}

function Invoke-LabHttp {
    <#  Plain HTTP without a proxy (WPAD discovery stalls the first request on the isolated LAN).  #>
    param([Parameter(Mandatory)][string]$Url, [string]$Method = 'GET', [byte[]]$Body = $null, [hashtable]$Headers = @{}, [int]$TimeoutSec = 10)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $req = [Net.HttpWebRequest][Net.WebRequest]::Create($Url)
        $req.Proxy = $null; $req.Method = $Method; $req.Timeout = $TimeoutSec * 1000; $req.ReadWriteTimeout = $TimeoutSec * 1000; $req.KeepAlive = $false
        foreach ($k in $Headers.Keys) { if ($k -eq 'Content-Type') { $req.ContentType = $Headers[$k] } else { $req.Headers[$k] = $Headers[$k] } }
        if ($Body) { $req.ContentLength = $Body.Length; $s = $req.GetRequestStream(); $s.Write($Body, 0, $Body.Length); $s.Close() }
        $resp = $req.GetResponse()
        try {
            $ms = New-Object IO.MemoryStream; $resp.GetResponseStream().CopyTo($ms); $bytes = $ms.ToArray()
            $h = @{}; foreach ($k in $resp.Headers.AllKeys) { $h[$k] = $resp.Headers[$k] }
            return [pscustomobject]@{ answered = $true; status = [int]$resp.StatusCode; bytes = $bytes; text = [Text.Encoding]::UTF8.GetString($bytes); headers = $h; error = ''; ms = $watch.ElapsedMilliseconds }
        } finally { $resp.Close() }
    } catch [Net.WebException] {
        $r = $_.Exception.Response
        if ($r) {
            $ms = New-Object IO.MemoryStream; try { $r.GetResponseStream().CopyTo($ms) } catch { }; $bytes = $ms.ToArray()
            $h = @{}; foreach ($k in $r.Headers.AllKeys) { $h[$k] = $r.Headers[$k] }
            $code = [int]$r.StatusCode; $r.Close()
            return [pscustomobject]@{ answered = $true; status = $code; bytes = $bytes; text = [Text.Encoding]::UTF8.GetString($bytes); headers = $h; error = $_.Exception.Message; ms = $watch.ElapsedMilliseconds }
        }
        return [pscustomobject]@{ answered = $false; status = 0; bytes = $null; text = ''; headers = @{}; error = [string]$_.Exception.Status + ': ' + $_.Exception.Message; ms = $watch.ElapsedMilliseconds }
    } catch {
        return [pscustomobject]@{ answered = $false; status = 0; bytes = $null; text = ''; headers = @{}; error = $_.Exception.Message; ms = $watch.ElapsedMilliseconds }
    }
}

function Get-LabJsonFromUrl {
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 5)
    $r = Invoke-LabHttp -Url $Url -TimeoutSec $TimeoutSec
    if ($r.status -eq 200) { try { return ($r.text | ConvertFrom-Json) } catch { return $null } }
    return $null
}

function Test-LabAddressInSubnet {
    param([Parameter(Mandatory)][string]$Address, [string]$Prefix = '192.0.2')
    return ($Address -match ('^' + [regex]::Escape($Prefix) + '\.(\d{1,3})$') -and [int]$Matches[1] -ge 1 -and [int]$Matches[1] -le 254)
}

function Get-LabHostNumber {
    param([Parameter(Mandatory)][string]$Address)
    if ($Address -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.(\d{1,3})$') { return [int]$Matches[1] }
    return -1
}

function Start-LabUnelevated {
    <#  Starts a .cmd as the signed-in user without administrator rights, through the desktop
        shell. Per-user installers (Arena viewer, hub, Ticketing adapter) must not run elevated.  #>
    param([Parameter(Mandatory)][string]$CmdFile)
    if (-not (Test-LabElevated)) { Start-Process -FilePath $CmdFile | Out-Null; return 'direct' }
    # The shell hand-off runs as whoever owns the desktop. If that is another account (a standard
    # user who typed an administrator's password at the UAC prompt), the per-user install would
    # land in the wrong profile: refuse instead.
    $shellOwner = ''
    try { $e = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop) | Select-Object -First 1; if ($e) { $o = Invoke-CimMethod -InputObject $e -MethodName GetOwner; $shellOwner = ('{0}\{1}' -f $o.Domain, $o.User) } } catch { }
    if ($shellOwner -and $shellOwner -ine ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME)) { throw ("the desktop belongs to $shellOwner but this window runs as $env:USERDOMAIN\$env:USERNAME. Sign in with an account that is a local administrator and start again.") }
    Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList ('"{0}"' -f $CmdFile) | Out-Null
    return 'shell'
}

function Test-LabZipFits {
    <#  Pure - unit tested. Whether an archive unpacked into $Destination keeps every path inside Windows'
        limits (a file under 260 characters, a folder under 248), which Windows PowerShell 5.1's unpacker
        enforces. Capstone rc.3 has entries of 195 characters: unpacked into the run folder they reached
        about 300 and the install stopped (DESKTOP-REDACT11, 2026-11-07).  #>
    param([Parameter(Mandatory)][string]$Destination, [Parameter(Mandatory)][string[]]$Entries)
    $base = $Destination.TrimEnd('\').Length + 1
    $longest = ''; foreach ($e in $Entries) { if ($e.Length -gt $longest.Length) { $longest = $e } }
    $file = $base + $longest.Length
    $folders = 0; foreach ($e in $Entries) { $d = $e.Replace('/', '\').TrimEnd('\'); $i = $d.LastIndexOf('\'); if ($i -gt 0 -and $i -gt $folders) { $folders = $i } }
    $folder = $base + $folders
    return [pscustomobject]@{ Ok = ($file -lt 260 -and $folder -lt 248); Longest = $file; LongestFolder = $folder; Entry = $longest; Room = 259 - $file }
}

function Expand-LabZipChecked {
    <#  Unpacks an archive after checking that every path fits (Test-LabZipFits), so a too-deep folder is
        refused before anything is written, with the numbers, instead of failing half-way.  #>
    param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($Zip)
    try { $entries = @($z.Entries | ForEach-Object { $_.FullName }) } finally { $z.Dispose() }
    $fit = Test-LabZipFits -Destination $Destination -Entries $entries
    if (-not $fit.Ok) { throw ('{0} cannot be unpacked into {1}: its longest path would be {2} characters (Windows allows 259 for a file, 247 for a folder): {3}. Nothing was unpacked.' -f (Split-Path -Leaf $Zip), $Destination, $fit.Longest, $fit.Entry) }
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Destination)
    return $fit
}

function Get-LabUsbLabel {
    <#  How a message names the provisioner stick: by the drive letter it has on this computer, which is not
        always F: (the Capstone laptop gave it E:, 2026-11-07). The provisioner finds its own files from where
        it was started, never from a fixed letter.  #>
    param([string]$UsbRoot = '')
    if ($UsbRoot -match '^([A-Za-z]:)') { return ('the provisioner stick ({0})' -f $Matches[1].ToUpperInvariant()) }
    return 'the provisioner stick'
}

Export-ModuleMember -Function @(
    'Test-LabZipFits', 'Expand-LabZipChecked', 'Get-LabUsbLabel',
    'Get-LabVersion', 'Get-LabRoot', 'Get-LabConfig', 'Test-LabElevated', 'Save-LabJson', 'Read-LabJson',
    'Protect-LabText', 'Protect-LabObject', 'New-LabRun', 'Get-LabRun', 'Set-LabRun', 'Write-LabLog', 'Write-LabStage',
    'Add-LabWarning', 'Add-LabPending', 'Add-LabVerification', 'Add-LabRegistration', 'Register-LabChange',
    'Set-LabApprovalProvider', 'Set-LabInputProvider', 'Request-LabApproval', 'Read-LabInput', 'Set-LabSecretProvider', 'Read-LabSecret', 'Test-LabSecretPortable',
    'Invoke-LabStep', 'New-LabOutcome', 'Get-LabStagedStatus', 'Test-LabTcp', 'Invoke-LabHttp', 'Get-LabJsonFromUrl',
    'Test-LabAddressInSubnet', 'Get-LabHostNumber', 'Start-LabUnelevated'
)
