<#
    Lab.Arena - registration with the Agent Arena event service, through documented contracts only.

      * GET  /v2/health            unauthenticated identity check (service, service_id, version)
      * POST /v2/claim             CONTRACT.md section 1 "Addresses": a provisioned machine takes the
                                   address reserved for it in the admin console with a one-time
                                   claim code, sent from that address; a head claim registers the
                                   head and its display. Signed as in CONTRACT.md section 2
                                   (X-Arena-Credential claim:<address>, key from the normalised
                                   code) and the signed answer is verified.
      * head viewer enrollment     the AGENT-ARENA-<version> toolset on the stick (2-INSTALL-AND-VERIFY),
                                   run unchanged as the signed-in user, with the one-time display
                                   code the operator types (never stored on the USB); an enrolled
                                   viewer is updated with no window and no code (--install-quiet).
      * admin hub, Ticketing       the existing AGENT-ARENA-HUB and AGENT-ARENA-TICKETING installers,
                                   run unchanged as the signed-in user.

    No endpoint is invented. When the hub does not answer, registration is reported PENDING.
#>

# ---------------------------------------------------------------------------
# arena.v2 signing (pure - checked against the CONTRACT.md test vectors)
# ---------------------------------------------------------------------------

function ConvertTo-LabB64u { param([byte[]]$Bytes) return ([Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')) }
function ConvertFrom-LabB64u { param([string]$Text) $t = $Text.Replace('-', '+').Replace('_', '/'); while ($t.Length % 4) { $t += '=' }; return [Convert]::FromBase64String($t) }

function Get-LabSha256Hex {
    param([byte[]]$Bytes)
    if ($null -eq $Bytes) { $Bytes = [byte[]]@() }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (-join ($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') })) } finally { $sha.Dispose() }
}

function Get-LabHmac {
    param([byte[]]$Key, [byte[]]$Message)
    $h = New-Object Security.Cryptography.HMACSHA256 (, $Key)
    try { return $h.ComputeHash($Message) } finally { $h.Dispose() }
}

function Get-LabArenaKey { param([Parameter(Mandatory)][string]$Secret) return (Get-LabHmac -Key ([Text.Encoding]::UTF8.GetBytes($Secret)) -Message ([Text.Encoding]::UTF8.GetBytes('arena.v2'))) }

function ConvertTo-LabArenaCode {
    <#  arena_service.crypto.normalize_code  #>
    param([Parameter(Mandatory)][string]$Code)
    $raw = (-join ($Code.ToUpperInvariant().ToCharArray() | Where-Object { [char]::IsLetterOrDigit($_) }))
    if ($raw.StartsWith('ARENA')) { $raw = $raw.Substring(5) }
    $groups = @(); for ($i = 0; $i -lt $raw.Length; $i += 4) { $groups += $raw.Substring($i, [Math]::Min(4, $raw.Length - $i)) }
    return ('ARENA-' + ($groups -join '-'))
}

function Get-LabArenaSignature {
    param([Parameter(Mandatory)][byte[]]$Key, [Parameter(Mandatory)][string]$Method, [Parameter(Mandatory)][string]$Target, [Parameter(Mandatory)][string]$Timestamp, [Parameter(Mandatory)][string]$Nonce, [byte[]]$Body)
    $canonical = ($Method.ToUpperInvariant(), $Target, $Timestamp, $Nonce, (Get-LabSha256Hex $Body)) -join "`n"
    return (ConvertTo-LabB64u (Get-LabHmac -Key $Key -Message ([Text.Encoding]::UTF8.GetBytes($canonical))))
}

function Test-LabArenaResponse {
    param([Parameter(Mandatory)][byte[]]$Key, [Parameter(Mandatory)][int]$Status, [Parameter(Mandatory)][string]$Nonce, [byte[]]$Body, [string]$Signature)
    if (-not $Signature) { return $false }
    $expected = Get-LabHmac -Key $Key -Message ([Text.Encoding]::UTF8.GetBytes((([string]$Status), $Nonce, (Get-LabSha256Hex $Body)) -join "`n"))
    try { $given = ConvertFrom-LabB64u $Signature } catch { return $false }
    if ($given.Length -ne $expected.Length) { return $false }
    $diff = 0; for ($i = 0; $i -lt $given.Length; $i++) { $diff = $diff -bor ($given[$i] -bxor $expected[$i]) }
    return ($diff -eq 0)
}

function Get-LabMachineHash {
    <#  The desktop app's Identity.MachineHash: sha256(MachineGuid + "|agent-arena"), lowercase hex.  #>
    $guid = 'unknown'
    try { $guid = [string](Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction Stop).MachineGuid } catch { }
    return (Get-LabSha256Hex ([Text.Encoding]::UTF8.GetBytes($guid + '|agent-arena')))
}

# ---------------------------------------------------------------------------
# Service calls
# ---------------------------------------------------------------------------

function Get-LabArenaHealth {
    param([Parameter(Mandatory)][string]$Endpoint, [string]$ExpectedServiceId = '')
    $r = Invoke-LabHttp -Url ($Endpoint.TrimEnd('/') + '/v2/health') -TimeoutSec 8
    if (-not $r.answered) { $r = Invoke-LabHttp -Url ($Endpoint.TrimEnd('/') + '/v2/health') -TimeoutSec 12 }
    $h = $null; if ($r.status -eq 200) { try { $h = $r.text | ConvertFrom-Json } catch { } }
    $verdict = 'unreachable'
    if ($h -and $h.service -eq 'agent-arena-event-service') { if (-not $ExpectedServiceId -or $h.service_id -eq $ExpectedServiceId) { $verdict = 'ok' } else { $verdict = 'other_arena_service' } }
    elseif ($r.answered) { $verdict = 'wrong_service' }
    return [pscustomobject][ordered]@{ Endpoint = $Endpoint; Verdict = $verdict; ServiceId = $(if ($h) { $h.service_id } else { '' }); Version = $(if ($h) { $h.version } else { '' }); Error = $r.error; Ms = $r.ms }
}

function Get-LabArenaWhoami {
    <#  GET /v2/whoami (unauthenticated, used by the 2.1.x installers): which displays the registry
        holds for the address this request comes from. A proposal only; codes still authorize.  #>
    param([Parameter(Mandatory)][string]$Endpoint)
    $r = Invoke-LabHttp -Url ($Endpoint.TrimEnd('/') + '/v2/whoami') -TimeoutSec 10
    if ($r.status -ne 200) { return [pscustomobject]@{ Answered = $r.answered; Status = $r.status; Address = ''; Displays = @(); ServiceId = '' } }
    $j = $null; try { $j = $r.text | ConvertFrom-Json } catch { }
    if (-not $j) { return [pscustomobject]@{ Answered = $true; Status = $r.status; Address = ''; Displays = @(); ServiceId = '' } }
    return [pscustomobject]@{ Answered = $true; Status = 200; Address = [string]$j.address; ServiceId = [string]$j.service_id
                              Displays = @($j.displays | ForEach-Object { [pscustomobject]@{ display_id = $_.display_id; kind = $_.kind; location_id = $_.location_id } }) }
}

function Invoke-LabArenaRequest {
    param([Parameter(Mandatory)][string]$Endpoint, [Parameter(Mandatory)][string]$Credential, [Parameter(Mandatory)][byte[]]$Key, [Parameter(Mandatory)][string]$Method, [Parameter(Mandatory)][string]$Target, $Payload)
    $body = [byte[]]@()
    if ($null -ne $Payload) { $body = [Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Compress -Depth 6)) }
    $stamp = [string][int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $nb = New-Object byte[] 16; $rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $rng.GetBytes($nb); $rng.Dispose()
    $nonce = ConvertTo-LabB64u $nb
    $path = ([Uri]$Endpoint).AbsolutePath.TrimEnd('/') + $Target
    $sig = Get-LabArenaSignature -Key $Key -Method $Method -Target $path -Timestamp $stamp -Nonce $nonce -Body $body
    $headers = @{ 'X-Arena-Credential' = $Credential; 'X-Arena-Timestamp' = $stamp; 'X-Arena-Nonce' = $nonce; 'X-Arena-Signature' = $sig; 'Content-Type' = 'application/json' }
    $r = Invoke-LabHttp -Url ($Endpoint.TrimEnd('/') + $Target) -Method $Method -Body $body -Headers $headers -TimeoutSec 30
    if (-not $r.answered) { return [pscustomobject]@{ Ok = $false; Kind = 'unreachable'; Status = 0; Data = $null; Message = $r.error } }
    $signed = Test-LabArenaResponse -Key $Key -Status $r.status -Nonce $nonce -Body $r.bytes -Signature ([string]$r.headers['X-Arena-Response-Signature'])
    $data = $null; try { if ($r.text) { $data = $r.text | ConvertFrom-Json } } catch { }
    if (-not $signed) {
        if ($r.status -in 401, 403) { return [pscustomobject]@{ Ok = $false; Kind = 'code_refused'; Status = $r.status; Data = $null; Message = 'the service refused the code (unknown, used, expired, replaced, or for another address)' } }
        return [pscustomobject]@{ Ok = $false; Kind = 'unsigned_response'; Status = $r.status; Data = $null; Message = 'the answer is not signed by the Arena event service for this code; not trusted' }
    }
    if ($r.status -ge 400) { return [pscustomobject]@{ Ok = $false; Kind = $(if ($data) { [string]$data.error } else { 'error' }); Status = $r.status; Data = $data; Message = $(if ($data) { [string]$data.message } else { '' }) } }
    return [pscustomobject]@{ Ok = $true; Kind = 'ok'; Status = $r.status; Data = $data; Message = '' }
}

function Get-LabPriorClaim {
    <#  This machine's record that Arena accepted a claim of this address: an earlier run's report (kept in
        C:\ProgramData\AgentArenaLab\reports) with "Arena address claim <address>" REGISTERED. '' when there is none.
        An application laptop's claim has no display, so Arena's whoami cannot show it (1.4.7 asked DESKTOP-REDACT11 again).  #>
    param([Parameter(Mandatory)][string]$ReportsDir, [Parameter(Mandatory)][string]$Address)
    foreach ($f in @(Get-ChildItem -LiteralPath $ReportsDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
        $j = Read-LabJson $f.FullName
        if (-not $j -or -not ($j.PSObject.Properties.Name -contains 'registration')) { continue }
        $hit = @($j.registration | Where-Object { [string]$_.item -eq "Arena address claim $Address" -and [string]$_.status -eq 'REGISTERED' }) | Select-Object -First 1
        if ($hit) { return ('already claimed in run {0}: {1} (a claim code works once; none is needed again)' -f $j.run, $hit.detail) }
    }
    return ''
}

function Invoke-LabArenaClaim {
    <#  POST /v2/claim. Must be sent from the reserved address, so run it after the address is set.  #>
    param([Parameter(Mandatory)][string]$Endpoint, [Parameter(Mandatory)][string]$Address, [Parameter(Mandatory)][string]$Code, [string]$ExpectedServiceId = '')
    $normal = ConvertTo-LabArenaCode $Code
    $key = Get-LabArenaKey -Secret $normal
    $r = Invoke-LabArenaRequest -Endpoint $Endpoint -Credential ('claim:' + $Address) -Key $key -Method 'POST' -Target '/v2/claim' -Payload ([ordered]@{ computer = $env:COMPUTERNAME; machine_hash = (Get-LabMachineHash) })
    if ($r.Ok -and $ExpectedServiceId -and $r.Data.service_id -ne $ExpectedServiceId) { return [pscustomobject]@{ Ok = $false; Kind = 'other_service'; Status = $r.Status; Data = $r.Data; Message = ('answered by service {0}, not {1}' -f $r.Data.service_id, $ExpectedServiceId) } }
    return $r
}

# ---------------------------------------------------------------------------
# Head viewer (AGENT-ARENA-2.1.2), unchanged, as the signed-in user
# ---------------------------------------------------------------------------

function Get-LabViewerState {
    $programs = Join-Path $env:LOCALAPPDATA 'Programs\AgentArena'
    $data = Join-Path $env:LOCALAPPDATA 'AgentArena'
    $pointer = Join-Path $programs 'current.txt'
    $current = $null; if (Test-Path -LiteralPath $pointer) { $current = (Get-Content -LiteralPath $pointer -ErrorAction SilentlyContinue | Select-Object -First 1); if ($current) { $current = $current.Trim() } }
    $run = $null; try { $run = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name AgentArena -ErrorAction Stop).AgentArena } catch { }
    $endpoints = @()
    $cfg = Read-LabJson (Join-Path $data 'config.json')
    # The installed config.json names them "endpoints"; "event_service_endpoints" is the package's deployment file.
    # Reading only the latter left them blank, so a head still pointed at the old laptop (192.0.2.1) passed (head 6).
    if ($cfg -and $cfg.PSObject.Properties.Name -contains 'endpoints') { $endpoints = @($cfg.endpoints) }
    elseif ($cfg -and $cfg.PSObject.Properties.Name -contains 'event_service_endpoints') { $endpoints = @($cfg.event_service_endpoints) }
    return [pscustomobject][ordered]@{ Installed = [bool]$current; Version = $current; CredentialPresent = (Test-Path -LiteralPath (Join-Path $data 'credential.bin'))
                                       StartAtSignIn = [bool]$run; Running = @(Get-Process AgentArena -ErrorAction SilentlyContinue).Count; Endpoints = $endpoints; User = $env:USERNAME }
}

function ConvertTo-LabViewerConfigText {
    <#  Pure - unit tested. The viewer's config.json text with the hub as its only event service endpoint, and
        whether that changed anything. Every other setting is kept.  #>
    param([Parameter(Mandatory)][string]$Text, [Parameter(Mandatory)][string]$Hub)
    $cfg = $Text | ConvertFrom-Json
    $before = @(); if ($cfg.PSObject.Properties.Name -contains 'endpoints') { $before = @($cfg.endpoints) }
    if ($before.Count -eq 1 -and [string]$before[0] -eq $Hub) { return [pscustomobject]@{ Changed = $false; Text = $Text; Before = $before } }
    $cfg | Add-Member -NotePropertyName endpoints -NotePropertyValue ([object[]]@($Hub)) -Force
    return [pscustomobject]@{ Changed = $true; Text = ($cfg | ConvertTo-Json -Depth 6); Before = $before }
}

function Get-LabViewerDiagnosticsVerdict {
    <#  Pure - unit tested. From the viewer's own --diagnostics-report: which address it uses, and whether the
        event service answered, accepted this head's credential and sent its live view.  #>
    param([AllowEmptyString()][string]$Text = '')
    $line = { param($name) @(($Text -split "`r?`n") | Where-Object { $_ -match ('^\[(PASS|FAIL|WARN|INFO)\]\s+' + [regex]::Escape($name) + '\b') }) | Select-Object -First 1 }
    $addr = ''; $a = & $line 'Event service address'; if ($a -match 'Event service address\s+(\S+)') { $addr = $Matches[1].TrimEnd(',') }
    $r = [pscustomobject][ordered]@{ Address = $addr
        Reachable = [bool]((& $line 'Service reachability') -match '^\[PASS\]')
        Authenticated = [bool]((& $line 'Authentication') -match '^\[PASS\]')
        Subscribed = [bool]((& $line 'Viewer subscription') -match '^\[PASS\]')
        Detail = ((@('Service reachability', 'Authentication', 'Viewer subscription') | ForEach-Object { & $line $_ } | Where-Object { $_ }) -join ' | ') }
    $r | Add-Member -NotePropertyName Ok -NotePropertyValue ($r.Reachable -and $r.Authenticated -and $r.Subscribed)
    return $r
}

function ConvertTo-LabSmartAppControl {
    <#  Pure - unit tested. HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy VerifiedAndReputablePolicyState.  #>
    param($Value)
    switch ("$Value") { '0' { 'off' } '1' { 'on (blocks unsigned programs)' } '2' { 'evaluation (may switch itself on)' } default { 'unknown' } }
}

function Test-LabAppControlMessage {
    <#  Pure - unit tested. Whether an error is Windows refusing to run a file (Smart App Control or an App
        Control / WDAC policy), as Start-Process words it.  #>
    param([AllowEmptyString()][string]$Message = '')
    return ($Message -match 'Application Control policy has blocked|blocked by (your )?(organization|administrator)|Smart App Control|0x800711C7|0x800704EC')
}

function Get-LabAppControlState {
    <#  Read-only: Smart App Control's state and the files Code Integrity blocked in the last day.  #>
    $sac = 'unknown'
    try { $sac = ConvertTo-LabSmartAppControl (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState } catch { }
    $blocks = @()
    try {
        $blocks = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = 3077, 3033, 3034, 3076; StartTime = (Get-Date).AddDays(-1) } -MaxEvents 50 -ErrorAction Stop |
            ForEach-Object { $f = ''; if ($_.Message -match '(?m)attempted to load ([^\s].*?) that did not meet') { $f = $Matches[1] } elseif ($_.Message -match '(\\Device\\[^\s]+|[A-Z]:\\[^\s]+\.(exe|dll))') { $f = $Matches[1] }
                [pscustomobject]@{ At = $_.TimeCreated; Id = $_.Id; File = $(if ($f) { Split-Path -Leaf $f } else { '?' }); Path = $f } } | Where-Object { $_.File -ne '?' })
    } catch { }
    return [pscustomobject]@{ SmartAppControl = $sac; Blocks = $blocks }
}

function Disable-LabSmartAppControl {
    <#  Turns Smart App Control off (the same setting as Windows Security > App & browser control > Smart App
        Control > Off) and asks Code Integrity to reload its policy. Returns whether it is off now and whether a
        restart is still needed. The earlier value is recorded.  #>
    param([Parameter(Mandatory)]$Run)
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy'
    $before = $null; try { $before = (Get-ItemProperty $key -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState } catch { }
    if ("$before" -eq '0') { return [pscustomobject]@{ Off = $true; Changed = $false; Detail = 'Smart App Control is already off' } }
    New-Item -Path $key -Force | Out-Null
    Set-ItemProperty -Path $key -Name VerifiedAndReputablePolicyState -Value 0 -Type DWord
    # No CiTool --refresh: on DESKTOP-REDACT11 (2026-11-07) the run stopped right after TURN-OFF, with CiTool's output
    # captured, so whatever it waited for was invisible. The setting takes effect at the next restart; the report says so.
    $refreshed = $false
    Register-LabChange -Kind 'security' -Description ("Smart App Control turned off (was {0})" -f (ConvertTo-LabSmartAppControl $before)) `
        -Undo ([pscustomobject]@{ type = 'manual'; instructions = 'Windows Security > App & browser control > Smart App Control settings: choose On (some Windows builds allow this only after a reset)' }) | Out-Null
    return [pscustomobject]@{ Off = $true; Changed = $true; Refreshed = $refreshed
        Detail = ('Smart App Control turned off (was {0}); {1}' -f (ConvertTo-LabSmartAppControl $before), $(if ($refreshed) { 'policy reloaded now' } else { 'takes effect after a restart' })) }
}

function Request-LabSmartAppControlOff {
    <#  When Smart App Control is on (or in evaluation, when -IncludeEvaluation), asks the operator and turns it off.
        Returns $null when nothing needed doing.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Why, [switch]$IncludeEvaluation)
    $ac = Get-LabAppControlState
    $on = ($ac.SmartAppControl -like 'on*') -or ($IncludeEvaluation -and $ac.SmartAppControl -like 'evaluation*')
    if (-not $on) { return $null }
    if (-not (Request-LabApproval -Prompt ("Smart App Control is {0} on this computer. {1} Turn Smart App Control off here? (Windows Security > App & browser control shows the same setting.)" -f $ac.SmartAppControl, $Why) -Word 'TURN-OFF' -Kind 'privileged')) { return [pscustomobject]@{ Off = $false; Changed = $false; Detail = 'the operator kept Smart App Control on' } }
    return (Disable-LabSmartAppControl -Run $Run)
}

function Get-LabViewerBlockedText {
    <#  What the operator does when Windows refuses to run the Arena viewer.  #>
    param([string]$Version = '')
    $ac = Get-LabAppControlState
    return ('Windows refuses to run the Arena viewer {0} on this head: Smart App Control is {1}, and AgentArena.exe is not signed by a publisher Windows knows. The other heads do not have Smart App Control switched on. Fix on this head: Start > type "Smart App Control" > Smart App Control settings > Off, then START-LAB-PROVISIONER > Existing machine > 8 again' -f $Version, $ac.SmartAppControl)
}

function Get-LabViewerDiagnostics {
    <#  Runs the installed viewer's own --diagnostics-report (read-only: it changes nothing) and judges it.  #>
    param([Parameter(Mandatory)][string]$OutFile)
    $v = Get-LabViewerState
    if (-not $v.Installed) { return $null }
    $exe = Join-Path $env:LOCALAPPDATA ('Programs\AgentArena\{0}\AgentArena.exe' -f $v.Version)
    if (-not (Test-Path -LiteralPath $exe)) { return $null }
    try { $p = Start-Process -FilePath $exe -ArgumentList '--diagnostics-report', ('"' + $OutFile + '"') -PassThru -WindowStyle Hidden -ErrorAction Stop }
    catch {
        if (Test-LabAppControlMessage $_.Exception.Message) { $r = Get-LabViewerDiagnosticsVerdict -Text ''; $r | Add-Member -NotePropertyName Blocked -NotePropertyValue $true; $r.Detail = (Get-LabViewerBlockedText -Version $v.Version); return $r }
        throw
    }
    if (-not $p.WaitForExit(90000)) { try { $p.Kill() } catch { }; return (Get-LabViewerDiagnosticsVerdict -Text 'diagnostics did not finish in 90 s') }
    $r = Get-LabViewerDiagnosticsVerdict -Text $(if (Test-Path -LiteralPath $OutFile) { Get-Content -LiteralPath $OutFile -Raw } else { '' })
    # Started, but stopped by Code Integrity before it wrote anything (head 10's verify: no report, no error).
    if (-not (Test-Path -LiteralPath $OutFile) -and @((Get-LabAppControlState).Blocks | Where-Object { $_.File -ieq 'AgentArena.exe' }).Count) {
        $r | Add-Member -NotePropertyName Blocked -NotePropertyValue $true -Force; $r.Detail = (Get-LabViewerBlockedText -Version $v.Version)
    }
    return $r
}

function Repair-LabViewerEndpoint {
    <#  The viewer keeps its event service address across updates. Heads enrolled while Arena ran on the
        development laptop still ask 192.0.2.1 and show "disconnected" (head 6, 2026-11-06). This points it at the
        hub (its config.json "endpoints", the file backed up first), restarts the viewer as the signed-in user,
        and proves the connection with the viewer's own diagnostics. The credential is untouched: the hub holds
        the moved service, so the same credential is accepted there.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Hub)
    $file = Join-Path $env:LOCALAPPDATA 'AgentArena\config.json'
    if (-not (Test-Path -LiteralPath $file)) { return (New-LabOutcome 'PENDING' 'no viewer configuration for this user') }
    $text = [IO.File]::ReadAllText($file)
    $new = ConvertTo-LabViewerConfigText -Text $text -Hub $Hub
    $diag = Join-Path $Run.Folder 'viewer-diagnostics.txt'
    if ($new.Changed) {
        $backup = Join-Path $Run.Backups 'viewer-config-before.json'; Copy-Item -LiteralPath $file -Destination $backup -Force
        $v = Get-LabViewerState
        @(Get-Process AgentArena -ErrorAction SilentlyContinue) | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        [IO.File]::WriteAllText($file, $new.Text, (New-Object Text.UTF8Encoding $false))
        Register-LabChange -Kind 'viewer' -Description ('Arena viewer event service: {0} -> {1}' -f (@($new.Before) -join ', '), $Hub) -Backup $backup `
            -Undo ([pscustomobject]@{ type = 'manual'; instructions = ("close Agent Arena, copy $backup over $file, start Agent Arena") }) | Out-Null
        $exe = Join-Path $env:LOCALAPPDATA ('Programs\AgentArena\{0}\AgentArena.exe' -f $v.Version)
        $start = Join-Path $Run.Folder 'start-agent-arena.cmd'
        [IO.File]::WriteAllText($start, ('@start "" "{0}"' -f $exe) + "`r`n", (New-Object Text.ASCIIEncoding))
        Start-LabUnelevated -CmdFile $start | Out-Null
    }
    $d = Get-LabViewerDiagnostics -OutFile $diag
    if (-not $d) { return (New-LabOutcome 'PENDING' 'the viewer is not installed for this user') }
    $what = $(if ($new.Changed) { 'pointed at {0} (was {1}); ' -f $Hub, (@($new.Before) -join ', ') } else { "already uses $Hub; " })
    if ($d.Ok) { return (New-LabOutcome $(if ($new.Changed) { 'DONE' } else { 'UNCHANGED' }) ($what + 'its own diagnostics: Arena answers, accepts this head and sends its live view')) }
    if ($d.PSObject.Properties.Name -contains 'Blocked' -and $d.Blocked) { throw ($what + 'but ' + $d.Detail) }
    throw ($what + 'but its own diagnostics still fail: ' + $d.Detail + " (full report: $diag)")
}

function Get-LabViewerToolset {
    <#  The newest AGENT-ARENA-<version> toolset in the payload, with its version; $null when there is none.  #>
    param([Parameter(Mandatory)][string]$Payload)
    $t = @(Get-ChildItem -LiteralPath $Payload -Directory -Filter 'AGENT-ARENA-*' -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^AGENT-ARENA-(\d+\.\d+\.\d+)$' } |
        Sort-Object { [version]($_.Name -replace '^AGENT-ARENA-', '') } -Descending) | Select-Object -First 1
    if (-not $t) { return $null }
    return [pscustomobject]@{ Path = $t.FullName; Version = ($t.Name -replace '^AGENT-ARENA-', '') }
}

function ConvertTo-LabViewerUpdateCmd {
    <#  Pure - unit tested. The .cmd that updates the viewer as the signed-in user: waits up to 3 minutes for the hub
        (the installer finds this head's display there), runs the package's --install-quiet (keeps the credential and
        the configuration), logs every line, then starts whichever version is current.  #>
    param([Parameter(Mandatory)][string]$Exe, [Parameter(Mandatory)][string]$Log, [Parameter(Mandatory)][string]$HubHost, [Parameter(Mandatory)][string]$Version)
    return (@(
        '@echo off'
        "rem Agent Arena $Version quiet update for %USERNAME%, written by the lab provisioner. It keeps this head's viewer credential."
        "echo started %DATE% %TIME% as %USERNAME% > ""$Log"""
        "for /l %%i in (1,1,36) do ( ping -n 1 -w 1000 $HubHost | find ""TTL="" >nul && goto ready & timeout /t 5 /nobreak >nul )"
        "echo the hub $HubHost did not answer for 3 minutes; trying anyway >> ""$Log"""
        ':ready'
        """$Exe"" --install-quiet >> ""$Log"" 2>&1"
        "echo exit %ERRORLEVEL% >> ""$Log"""
        'set CUR='
        'set /p CUR=<"%LOCALAPPDATA%\Programs\AgentArena\current.txt"'
        'if defined CUR if exist "%LOCALAPPDATA%\Programs\AgentArena\%CUR%\AgentArena.exe" start "" "%LOCALAPPDATA%\Programs\AgentArena\%CUR%\AgentArena.exe" --background'
        "echo done %DATE% %TIME% >> ""$Log"""
    ) -join "`r`n") + "`r`n"
}

function Update-LabViewerQuiet {
    <#  Updates an installed, enrolled Arena viewer to the toolset's version for the signed-in user: no window, no
        enrollment code (the installer's --install-quiet keeps the DPAPI credential and the configuration). 2.1.3
        stops the viewer's memory growth (heads ran out of commit memory after days with 2.1.2). When the hub does
        not answer yet, the same update runs once at the next sign-in (HKCU RunOnce) and the result is PENDING.  #>
    param([Parameter(Mandatory)]$Toolset, [Parameter(Mandatory)][string]$EvidenceFolder, [Parameter(Mandatory)][string]$Hub, [int]$TimeoutSeconds = 240)
    $ver = [string]$Toolset.Version
    $v = Get-LabViewerState
    if (-not $v.Installed) { return (New-LabOutcome 'PENDING' "Agent Arena is not installed for ${env:USERNAME}: START-LAB-PROVISIONER > Existing machine > 8 installs it") }
    if (-not $v.CredentialPresent) { return (New-LabOutcome 'PENDING' "Agent Arena $($v.Version) is not enrolled for ${env:USERNAME}: START-LAB-PROVISIONER > Existing machine > 8 enrolls it") }
    if ([version]$v.Version -ge [version]$ver) { return (New-LabOutcome 'UNCHANGED' "Agent Arena $($v.Version) is installed for $env:USERNAME") }
    $pkg = Join-Path $Toolset.Path ("package\Agent-Arena-$ver\app")
    if (-not (Test-Path -LiteralPath (Join-Path $pkg 'AgentArena.exe'))) { throw "the stick's Agent Arena $ver package is missing ($pkg)" }
    $owner = ''
    try { $e = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop) | Select-Object -First 1; if ($e) { $o = Invoke-CimMethod -InputObject $e -MethodName GetOwner; $owner = ('{0}\{1}' -f $o.Domain, $o.User) } } catch { }
    if ($owner -and $owner -ine ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME)) { return (New-LabOutcome 'PENDING' "the desktop belongs to $owner, not ${env:USERNAME}: sign in as $env:USERNAME and run this again") }
    # A local copy: the stick may be gone when it runs (at the next sign-in).
    $work = Join-Path $env:LOCALAPPDATA ("AgentArenaLab\viewer-update\$ver")
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    New-Item -ItemType Directory -Path (Join-Path $work 'app') -Force | Out-Null
    Copy-Item -Path (Join-Path $pkg '*') -Destination (Join-Path $work 'app') -Recurse -Force
    $log = Join-Path $work 'update.log'; $cmd = Join-Path $work 'update-agent-arena.cmd'
    $hubHost = ([uri]$Hub).Host
    [IO.File]::WriteAllText($cmd, (ConvertTo-LabViewerUpdateCmd -Exe (Join-Path $work 'app\AgentArena.exe') -Log $log -HubHost $hubHost -Version $ver), (New-Object Text.ASCIIEncoding))
    if (-not (Test-Connection -ComputerName $hubHost -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
        Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'AgentArenaLabViewerUpdate' -Value ('cmd.exe /c "{0}"' -f $cmd)
        return (New-LabOutcome 'PENDING' "the hub $hubHost does not answer yet: Agent Arena $ver installs by itself at ${env:USERNAME}'s next sign-in (log $log)")
    }
    Start-LabUnelevated -CmdFile $cmd | Out-Null
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds); $text = ''
    # The installer holds the log open while it writes (head 7, 2026-11-10: ReadAllText failed on it and the update,
    # which succeeded, was reported as failed): read it shared.
    $read = { try { $fs = New-Object IO.FileStream($log, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete'); try { (New-Object IO.StreamReader($fs)).ReadToEnd() } finally { $fs.Dispose() } } catch { '' } }
    while ((Get-Date) -lt $deadline) { Start-Sleep -Seconds 3; if (Test-Path -LiteralPath $log) { $text = & $read; if ($text -match '(?m)^done ') { break } } }
    if (Test-Path -LiteralPath $log) { $text = & $read; [IO.File]::WriteAllText((Join-Path $EvidenceFolder "viewer-update-$ver.log"), $text, (New-Object Text.UTF8Encoding $false)) }
    $now = Get-LabViewerState
    if ($now.Version -eq $ver -and $now.CredentialPresent -and $text -match 'RESULT success') { return (New-LabOutcome 'DONE' "Agent Arena $($v.Version) -> $ver for ${env:USERNAME}; credential kept; started again") }
    $last = @(($text -split "`r?`n") | Where-Object { $_ -match 'FAILED|RESULT' }) | Select-Object -Last 2
    throw ("Agent Arena did not update to $ver (still {0}): {1}" -f $now.Version, $(if ($last) { $last -join ' | ' } else { "no result in $TimeoutSeconds s; log $log" }))
}

function Invoke-LabViewerInstall {
    <#  Runs the toolset's own 2-INSTALL-AND-VERIFY from a local copy (so the one-time code is
        never written to the USB), waits for its SUMMARY.txt, and keeps its evidence.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$Toolset, [string]$DisplayId = '', [string]$Code = '', [int]$TimeoutMinutes = 30)
    $leaf = Split-Path -Leaf $Toolset
    $work = Join-Path $env:LOCALAPPDATA ('AgentArenaLab\head-viewer\' + $leaf)
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    Copy-Item -Path (Join-Path $Toolset '*') -Destination $work -Recurse -Force
    $codeFile = ''
    if ($Code -and $DisplayId) {
        $codeFile = Join-Path $work ('enroll\' + $DisplayId + '.code.txt')
        New-Item -ItemType Directory -Path (Split-Path -Parent $codeFile) -Force | Out-Null
        [IO.File]::WriteAllText($codeFile, ((ConvertTo-LabArenaCode $Code) + "`r`ndisplay $DisplayId`r`nwritten by the lab provisioner; deleted after the run`r`n"), (New-Object Text.UTF8Encoding $false))
    }
    $started = Get-Date
    $how = Start-LabUnelevated -CmdFile (Join-Path $work '2-INSTALL-AND-VERIFY.cmd')
    Write-LabLog ("  The Agent Arena install window opened as $env:USERNAME ($how). Follow it: check the detected identity, click Install, then Launch Arena.") -Level WARN
    Write-LabLog '  This window waits for its result. Press Q here to stop waiting.' -Level INFO
    $summary = $null; $deadline = $started.AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline -and -not $summary) {
        Start-Sleep -Seconds 2
        $summary = @(Get-ChildItem -LiteralPath (Join-Path $work 'evidence') -Recurse -Filter SUMMARY.txt -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt $started } | Sort-Object LastWriteTime -Descending) | Select-Object -First 1
        try { if ([Console]::KeyAvailable -and ([Console]::ReadKey($true).Key -eq 'Q')) { break } } catch { }
    }
    if ($codeFile -and (Test-Path -LiteralPath $codeFile)) { Remove-Item -LiteralPath $codeFile -Force }    # never leave a code behind
    if (Test-Path -LiteralPath (Join-Path $work 'evidence')) { Copy-Item -LiteralPath (Join-Path $work 'evidence') -Destination (Join-Path $Run.Folder 'viewer-evidence') -Recurse -Force }
    if (-not $summary) { return (New-LabOutcome 'PENDING' 'no result from the Agent Arena installer yet; run the viewer step again') }
    $text = Get-Content -LiteralPath $summary.FullName -Raw
    if ($text -match 'ALL CHECKS PASSED') { return (New-LabOutcome 'DONE' ('Agent Arena viewer installed and enrolled; evidence ' + $summary.DirectoryName)) }
    $line = (($text -split "`r?`n") | Where-Object { $_ -match 'STOPPED|NOT INSTALLED|FINISHED WITH' } | Select-Object -First 1)
    return (New-LabOutcome 'FAILED' ('the viewer toolset reported: ' + $line))
}

# ---------------------------------------------------------------------------
# Admin hub and Ticketing adapter state (read-only) and their installers
# ---------------------------------------------------------------------------

function Get-LabArenaListenEndpoint {
    <#  Pure - unit tested. Where the event service listens, from its aes.config.json: the same host and
        port arena_service\server.py binds (defaults 127.0.0.1:8790) and Arena's own admin console and
        hub_migrate probe. The hub installer binds the LAN address only (host=192.0.2.254), so
        127.0.0.1:8790 is refused there by design. A wildcard bind is probed on loopback.  #>
    param($ArenaConfig = $null)
    $h = '127.0.0.1'; $p = 8790
    if ($ArenaConfig) {
        if ($ArenaConfig.PSObject.Properties.Name -contains 'host' -and -not [string]::IsNullOrWhiteSpace([string]$ArenaConfig.host)) { $h = ([string]$ArenaConfig.host).Trim() }
        if ($ArenaConfig.PSObject.Properties.Name -contains 'port' -and [int]$ArenaConfig.port -gt 0) { $p = [int]$ArenaConfig.port }
    }
    if ($h -in '0.0.0.0', '*', '::', '[::]') { $h = '127.0.0.1' }
    return ('http://{0}:{1}' -f $h, $p)
}

function Get-LabHubState {
    <#  The live Arena hub install (Arena installs per user: after a standard-account migration it is in
        the Arena account's profile, not the administrator's running F:), probed where it listens.  #>
    param([Parameter(Mandatory)]$Config, $Identity = $null)
    $active = $null
    try { if (-not $Identity) { $Identity = Get-LabArenaIdentity }; $active = Select-LabActiveArenaInstall -Installed @($Identity.Installed) -RunningSids @(@($Identity.Running) | ForEach-Object { $_.Sid }) } catch { }
    if ($active) { $program = Join-Path $active.Profile 'AppData\Local\Programs\AgentArenaHub\app'; $cfgPath = $active.ConfigPath; $account = $active.Account }
    else { $program = Join-Path $env:LOCALAPPDATA 'Programs\AgentArenaHub\app'; $cfgPath = Join-Path $env:LOCALAPPDATA 'AgentArenaService\aes.config.json'; $account = "$env:USERDOMAIN\$env:USERNAME" }
    $cfg = Read-LabJson $cfgPath
    $listen = Get-LabArenaListenEndpoint -ArenaConfig $cfg
    $lab = ('http://{0}:8790' -f $Config.hub)
    $health = Get-LabArenaHealth -Endpoint $listen -ExpectedServiceId $Config.arena.service_id
    $healthHub = $(if ($listen -eq $lab) { $health } else { Get-LabArenaHealth -Endpoint $lab -ExpectedServiceId $Config.arena.service_id })
    $rule = @(Get-NetFirewallRule -DisplayName 'Agent Arena Event Service' -ErrorAction SilentlyContinue).Count -gt 0
    $marker = $null; $db = $null
    if ($cfg -and $cfg.db_path) { $db = [string]$cfg.db_path; $marker = Test-Path -LiteralPath (Join-Path (Split-Path -Parent $db) 'moved-to-hub.json') }
    return [pscustomobject][ordered]@{ ProgramInstalled = (Test-Path -LiteralPath $program); Program = $program; ConfigPresent = [bool]$cfg; ConfigPath = $cfgPath; Account = $account
                                       ListenEndpoint = $listen; DatabasePresent = $(if ($db) { Test-Path -LiteralPath $db } else { $false }); MovedMarker = $marker
                                       LocalHealth = $health; HubHealth = $healthHub; FirewallRule = $rule
                                       AdminWindowRunning = @(Get-Process AgentArena -ErrorAction SilentlyContinue).Count }
}

function Get-LabTicketingState {
    $local = $env:LOCALAPPDATA
    $program = Join-Path $local 'Programs\AgentArenaAdapter\app'
    $data = Join-Path $local 'AgentArenaService'
    $running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*AgentArenaAdapter*' }).Count
    return [pscustomobject][ordered]@{ ProgramInstalled = (Test-Path -LiteralPath $program); ConfigPresent = (Test-Path -LiteralPath (Join-Path $data 'ticketing-adapter.json'))
                                       CredentialPresent = (Test-Path -LiteralPath (Join-Path $data 'ticketing-producer.dpapi')); Running = $running }
}

function Compare-LabTicketingProgram {
    <#  Which program files of the installed Ticketing adapter differ from the stick's (its venv and caches aside).  #>
    param([Parameter(Mandatory)][string]$UsbFolder, [string]$Program = (Join-Path $env:LOCALAPPDATA 'Programs\AgentArenaAdapter\app'))
    $src = Join-Path $UsbFolder 'app'
    if (-not (Test-Path -LiteralPath $src) -or -not (Test-Path -LiteralPath $Program)) { return @() }
    $out = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $src -File -Recurse | Where-Object { $_.FullName -notmatch '\\(\.venv|__pycache__)\\' })) {
        $rel = $f.FullName.Substring($src.Length + 1)
        $dst = Join-Path $Program $rel
        if (-not (Test-Path -LiteralPath $dst) -or (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash) { $out += $rel }
    }
    return $out
}

function Stop-LabTicketingAdapter {
    <#  Stops this user's running Ticketing adapter (its Python and its minimized window), so its files can be replaced.  #>
    $n = 0
    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like '*AgentArenaAdapter*' -and $_.Name -match '^(pythonw?|cmd)\.exe$' })) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
    Start-Sleep -Milliseconds 800
    return $n
}

function Get-LabPython312 {
    <#  Python 3.12+ for the per-user Arena installers (hub, Ticketing), which run "py -3" or "python" as
        the signed-in user. Each candidate is run and must name a real python.exe and its version.
        1.3.1 and earlier probed with print("%d.%d.%d" % ...): Windows PowerShell 5.1 strips the inner
        double quotes from a native argument, so Python got a syntax error (exit 1) and every working
        Python read "not on PATH" (DESKTOP-REDACT10, 2026-11-05). The probe below has no quote characters.
        This is never the Gateway's runtime: that is the machine-wide Python of Get-LabRuntimeState.  #>
    $ErrorActionPreference = 'Continue'
    $probe = 'import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2]); print(sys.executable)'
    $cands = New-Object System.Collections.ArrayList
    [void]$cands.Add(@('py', '-3')); [void]$cands.Add(@('python'))
    # A per-user python.org install of the signed-in user that is not on this window's PATH.
    foreach ($d in @(Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) { [void]$cands.Add(@((Join-Path $d.FullName 'python.exe'))) }
    $attempts = @()
    foreach ($c in $cands) {
        $exe = $c[0]; $more = @($c | Select-Object -Skip 1); $cmd = ($c -join ' ')
        $a = [ordered]@{ command = $cmd; resolved = ''; exit = $null; version = ''; executable = ''; outcome = '' }
        $gc = Get-Command $exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $gc) { $a.outcome = $(if ($exe -match '\\') { 'file not present' } else { 'not on PATH for this window' }); $attempts += [pscustomobject]$a; continue }
        $a.resolved = [string]$gc.Source
        try {
            $out = @(& $gc.Source @more -c $probe 2>&1); $a.exit = $LASTEXITCODE
            $text = @($out | Where-Object { $_ -isnot [Management.Automation.ErrorRecord] } | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
            $err = @($out | Where-Object { $_ -is [Management.Automation.ErrorRecord] } | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
            if ($a.exit -ne 0 -or $text.Count -lt 2 -or $text[0] -notmatch '^(\d+) (\d+) (\d+)$') { $a.outcome = ('version check failed (exit {0}): {1}' -f $a.exit, ((@($err) + @($text) | Select-Object -Last 1) -join ' ')); $attempts += [pscustomobject]$a; continue }
            $a.version = '{0}.{1}.{2}' -f $Matches[1], $Matches[2], $Matches[3]; $a.executable = $text[1]
            $ok = ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 12))
            if (-not (Test-Path -LiteralPath $a.executable)) { $a.outcome = 'reported an executable that does not exist'; $attempts += [pscustomobject]$a; continue }
            if (-not $ok) { $a.outcome = 'older than 3.12'; $attempts += [pscustomobject]$a; continue }
            $a.outcome = 'ok'; $attempts += [pscustomobject]$a
            return [pscustomobject]@{ Found = $true; Version = $a.version; Command = $cmd; Executable = $a.executable; OnPath = ($exe -notmatch '\\')
                                      Account = "$env:USERDOMAIN\$env:USERNAME"; Elevated = [bool](Test-LabElevated); Attempts = $attempts; Reason = '' }
        } catch { $a.outcome = 'could not run: ' + $_.Exception.Message; $attempts += [pscustomobject]$a }
    }
    $reason = 'no Python 3.12+ found for {0} (elevated {1}): {2}' -f "$env:USERDOMAIN\$env:USERNAME", [bool](Test-LabElevated), (($attempts | ForEach-Object { '{0}{1} -> {2}' -f $_.command, $(if ($_.resolved -and $_.resolved -ne $_.command) { ' [' + $_.resolved + ']' } else { '' }), $_.outcome }) -join '; ')
    return [pscustomobject]@{ Found = $false; Version = ''; Command = ''; Executable = ''; OnPath = $false; Account = "$env:USERDOMAIN\$env:USERNAME"; Elevated = [bool](Test-LabElevated); Attempts = $attempts; Reason = $reason }
}

function Start-LabPayloadInstaller {
    <#  Opens one of the existing installers (unchanged) as the signed-in user, with arguments
        written into a local launcher so the shell hand-off can carry them.  #>
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$CmdFile, [string]$Arguments = '', $Python = $null)
    $launcher = Join-Path $env:LOCALAPPDATA ('AgentArenaLab\launch-' + [IO.Path]::GetFileNameWithoutExtension($CmdFile) + '.cmd')
    New-Item -ItemType Directory -Path (Split-Path -Parent $launcher) -Force | Out-Null
    # The installer finds Python itself ("py -3", else "python"). When the validated Python is not on PATH,
    # its folder is put first on PATH for this one launched window only: no user or machine PATH changes.
    $pathLine = ''; if ($Python -and $Python.Found -and -not $Python.OnPath -and $Python.Executable) { $pathLine = ('set "PATH={0};%PATH%"' -f (Split-Path -Parent $Python.Executable)) + "`r`n" }
    [IO.File]::WriteAllText($launcher, ("@echo off`r`n{2}call `"{0}`" {1}`r`n" -f $CmdFile, $Arguments, $pathLine), (New-Object Text.ASCIIEncoding))
    if ($Python -and $Python.Found) { Write-LabLog ('  installer Python: {0} {1} ({2}), checked as {3}' -f $Python.Command, $Python.Version, $Python.Executable, $Python.Account) }
    $how = Start-LabUnelevated -CmdFile $launcher
    return $how
}

function Wait-LabOperator {
    param([Parameter(Mandatory)][string]$What)
    $answer = Read-LabInput -Prompt ("When $What has finished, type DONE (or SKIP if it did not run)")
    return ($answer -ieq 'DONE')
}

Export-ModuleMember -Function @(
    'ConvertTo-LabB64u', 'ConvertFrom-LabB64u', 'Get-LabSha256Hex', 'Get-LabHmac', 'Get-LabArenaKey', 'ConvertTo-LabArenaCode', 'Get-LabArenaSignature',
    'Test-LabArenaResponse', 'Get-LabMachineHash', 'Get-LabArenaHealth', 'Get-LabArenaWhoami', 'Invoke-LabArenaRequest', 'Invoke-LabArenaClaim', 'Get-LabPriorClaim',
    'Get-LabViewerState', 'ConvertTo-LabViewerConfigText', 'Get-LabViewerDiagnosticsVerdict', 'Get-LabViewerDiagnostics',
    'Get-LabViewerToolset', 'ConvertTo-LabViewerUpdateCmd', 'Update-LabViewerQuiet',
    'ConvertTo-LabSmartAppControl', 'Test-LabAppControlMessage', 'Get-LabAppControlState', 'Get-LabViewerBlockedText', 'Disable-LabSmartAppControl', 'Request-LabSmartAppControlOff', 'Repair-LabViewerEndpoint', 'Invoke-LabViewerInstall', 'Get-LabArenaListenEndpoint', 'Get-LabHubState','Get-LabTicketingState', 'Compare-LabTicketingProgram', 'Stop-LabTicketingAdapter', 'Get-LabPython312', 'Start-LabPayloadInstaller', 'Wait-LabOperator'
)
