<#
    The guided Inference Gateway 0.3.0 installation on the administration hub, dot-sourced by
    Start-LabProvisioner.ps1. Order and semantics come from the release's deployment-manifest.json
    ("order", "commands", exit 0/1/2) and docs\OPERATOR-CHECKLIST.md.

    F: performs only the machine prerequisites (runtimes, the service account and its rights, the
    listener firewall rule) and Arena's own settings. Every other step runs the Gateway's own command
    and continues only on its "ok": true. Each step looks at the current state first, so a run that
    stopped at a physical gate (moving the CA key offline, carrying a token, qualifying a model) can
    be started again and continues where it stopped.

    One deliberate difference from the manifest's "order": application credentials are issued before
    bootstrap. The Gateway's bootstrap only updates clients declared "credential": "provisioned" and
    refuses one that holds no credential yet, so bootstrap-then-issue fails on a first installation
    (checked against the 0.3.0 code on a throwaway ledger). Issue reads the same reviewed policy.
    With no reachable head the sequence stops safely after install (g-inventory PENDING); running
    Administration hub > 2 again resumes there without resetting the service, certificate, ledger,
    credentials or ledger acceptance.
#>

# This file's folder (the engine), captured when the file is dot-sourced: steps run as script blocks from a module, where
# $PSScriptRoot is not reliable.
$script:LabGatewayFlowRoot = $PSScriptRoot

function Get-LabGatewayPaths {
    param([Parameter(Mandatory)]$Manifest)
    $d = 'C:\ProgramData\InferenceGateway'
    foreach ($f in @($Manifest.folders)) { if ($f.content -eq 'data root, inheritance removed') { $d = [string]$f.path } }
    return [pscustomobject]@{ Data = $d; Ledger = (Join-Path $d 'state\capacity-ledger.db'); PublisherState = (Join-Path $d 'observation-state\publisher-state.json')
        Observation = [string]$Manifest.observation.file; Config = (Join-Path $d 'config\gateway-service.json'); Review = (Join-Path $d 'config\policy-review.json')
        Ca = [string]$Manifest.tls.ca_file_for_clients; CaKey = (($Manifest.tls.ca_private_key -split ' \(')[0]); Backups = (Join-Path $d 'backups') }
}

function Invoke-LabGatewayFlow {
    param([Parameter(Mandatory)]$Run)
    $plan = $Run.Plan; $insp = $Run.Inspection
    $py = [string]$Config.runtime.python.path
    $ctx = @{ Root = ''; Manifest = $null; Settings = ''; Paths = $null; ArenaAccounts = @(); Inventory = ''; Review = ''; NoHeads = $false }
    $deploy = { param([string]$Verb, [string[]]$Extra = @(), [Security.SecureString]$Password = $null) Invoke-LabGatewayCommand -FileName $py -Arguments (Get-LabGatewayDeployArgs -Package $ctx.Root -Verb $Verb -Settings $ctx.Settings -Extra $Extra) -Label $Verb -Password $Password -Run $Run }
    $launch = { param([string]$Label, [string[]]$Arguments) Invoke-LabGatewayCommand -FileName $py -Arguments (Get-LabGatewayLauncherArgs -Manifest $ctx.Manifest -Arguments $Arguments) -Label $Label -Run $Run }
    $need = { param($r, [string]$What) if (-not $r.Ok) { throw ('{0}: {1} (exit {2}){3}' -f $What, $r.Meaning, $r.ExitCode, $(if ($r.Json -and $r.Json.error) { ' - ' + $r.Json.error } elseif ($r.Stderr) { ' - ' + (($r.Stderr -split "`r?`n") | Select-Object -Last 1) } else { '' })) } }

    Write-LabStage -Letter 'C' -Title 'Inference Gateway: machine prerequisites'
    Invoke-LabStep -Id 'g-intake' -Title 'Gateway release intake' -Action {
        $zip = Find-LabGatewayPackage -Folder (Join-Path $Packages 'gateway')
        if (-not $zip) { throw 'no inference-gateway-*.zip in packages\gateway' }
        $x = Expand-LabGatewayPackage -Zip $zip.FullName -Staging (Join-Path $Run.Root ('staging\' + [IO.Path]::GetFileNameWithoutExtension($zip.Name)))
        $t = Test-LabGatewayPackage -Root $x.Root -Config $Config
        foreach ($w in $t.Warnings) { Add-LabWarning ('Gateway release: ' + $w) }
        if (-not $t.Ok) { throw ('the Gateway release is refused: ' + ($t.Findings -join '; ')) }
        $ctx.Root = $x.Root; $ctx.Manifest = $t.Manifest; $ctx.Paths = Get-LabGatewayPaths -Manifest $t.Manifest
        New-LabOutcome 'DONE' ('{0}: archive {1} matches its .sha256; {2} files match SHA256SUMS and the manifest' -f $zip.Name, $x.ArchiveSha256.Substring(0, 12), $t.Files)
    } | Out-Null
    Invoke-LabStep -Id 'g-runtime' -Title 'Machine-wide Python 3.12.10 and PowerShell 7.4' -Action {
        $dir = Join-Path $Packages 'runtime'; $notes = @()
        foreach ($k in 'python', 'pwsh') {
            $st = Get-LabRuntimeState -Config $Config
            if (($k -eq 'python' -and $st.Python.Ok) -or ($k -eq 'pwsh' -and $st.Pwsh.Ok)) { $notes += "$k present"; continue }
            if (-not (Request-LabApproval -Prompt ("Install {0} {1} machine-wide from {2} (publisher hash and signature are checked first)?" -f $k, $Config.runtime.$k.version, (Get-LabUsbLabel $UsbRoot)) -Word 'APPROVE' -Kind 'privileged')) { throw "$k is required by the Gateway and was declined" }
            $notes += (Install-LabRuntime -Run $Run -Config $Config -Kind $k -PackageDir $dir).Detail
        }
        New-LabOutcome 'DONE' ($notes -join '; ')
    } | Out-Null
    Invoke-LabStep -Id 'g-account' -Title 'Gateway service account' -Action {
        $name = [string]$ctx.Manifest.service_account.name
        $a = Get-LabLocalAccount -Name $name; $notes = @()
        if (-not $a.Exists) {
            if (-not (Request-LabApproval -Prompt ("Create the local standard account {0} (Users only) for the Gateway service? You type its password; it is never stored." -f $name) -Word 'CREATE-ACCOUNT' -Kind 'privileged')) { throw 'the Gateway service account was declined' }
            $pw = Read-LabSecret -Prompt ("Password for {0} (14+ characters; you type it again at install)" -f $name) -Confirm
            $notes += (New-LabStandardAccount -Run $Run -Name $name -Password $pw -Description 'Inference Gateway service (Agent Arena Lab)').Detail
            $pw = $null; $a = Get-LabLocalAccount -Name $name
        }
        $m = @(Get-LabPrivilegedMembership -Sids @($a.Sid))[0]
        if ($m.State -ne 'standard') { throw ("{0} must be a standard account; it is {1} {2}" -f $name, $m.State, ($m.Groups -join ',')) }
        foreach ($r in @($ctx.Manifest.service_account.rights)) { if (Grant-LabUserRight -Sid $a.Sid -Right $r -Account $name) { $notes += "granted $r" } }
        foreach ($r in @($ctx.Manifest.service_account.deny)) { $deny = $r -replace '^Se', 'SeDeny'; if (Grant-LabUserRight -Sid $a.Sid -Right $deny -Account $name) { $notes += "set $deny" } }
        $rights = Get-LabUserRights -Sid $a.Sid
        $want = @($ctx.Manifest.service_account.rights) + @($ctx.Manifest.service_account.deny | ForEach-Object { $_ -replace '^Se', 'SeDeny' })
        $missing = @($want | Where-Object { $rights -notcontains $_ }); $extra = @($rights | Where-Object { $want -notcontains $_ })
        if ($missing.Count) { throw ('rights missing on {0}: {1}' -f $name, ($missing -join ', ')) }
        if ($extra.Count) { Add-LabWarning ("$name also holds: " + ($extra -join ', ') + '. Review them (not removed).') }
        New-LabOutcome $(if ($notes.Count) { 'DONE' } else { 'UNCHANGED' }) ("{0} ({1}): standard; rights {2}{3}" -f $name, $a.Sid, ($rights -join ', '), $(if ($notes.Count) { '; ' + ($notes -join '; ') } else { '' }))
    } | Out-Null
    Invoke-LabStep -Id 'g-arena-accounts' -Title 'Arena accounts are standard accounts' -Action {
        $h = Get-LabHubGatewayReadiness -Config $Config
        $fixed = @()
        if ($h.Accounts.State -eq 'BLOCKED' -and (Test-LabGatewayFolderGrantsOnly -Blockers @($h.Accounts.Blockers))) {
            # 1.6.15: the only problem is a Gateway folder that grants Arena (or a broad group) access: offer to remove it.
            if (-not (Request-LabApproval -Prompt ('The Gateway refuses to run while ' + ($h.Accounts.Blockers -join '; ') + '. Remove only that access now (the Gateway''s own account, SYSTEM and Administrators keep theirs; nothing is deleted)?') -Word 'REMOVE-ACCESS' -Kind 'privileged')) { throw ('declined: ' + ($h.Accounts.Blockers -join '; ')) }
            $fixed = @(Repair-LabGatewayFolderGrants -Readiness $h)
            $h = Get-LabHubGatewayReadiness -Config $Config
        }
        if ($h.Accounts.State -eq 'BLOCKED') { throw ('the Gateway refuses this hub''s Arena accounts: ' + ($h.Accounts.Blockers -join '; ') + '. Run Administration hub > Arena standard-account migration first.') }
        if ($h.Accounts.State -ne 'OK') { throw ('Arena account check incomplete: ' + ($h.Accounts.Unknown -join '; ')) }
        $ctx.ArenaAccounts = @($h.ArenaSids | ForEach-Object { Resolve-LabAccount $_ })
        New-LabOutcome 'DONE' ('Arena accounts: ' + ($ctx.ArenaAccounts -join ', ') + ' (standard, transitive check)' + $(if ($fixed.Count) { '; ' + ($fixed -join '; ') } else { '' }))
    } | Out-Null
    Invoke-LabStep -Id 'g-settings' -Title 'Gateway installer settings' -Action {
        $r = New-LabGatewaySettings -Run $Run -Root $ctx.Root -Manifest $ctx.Manifest -ArenaAccounts $ctx.ArenaAccounts
        $ctx.Settings = [string]$r.Data; $r
    } | Out-Null

    Write-LabStage -Letter 'C' -Title 'Inference Gateway: its own installer'
    $state = @{ tls = $false; ledger = $false; service = $null }
    Invoke-LabStep -Id 'g-plan' -Title 'Gateway: plan' -Action {
        $r = & $deploy 'plan'
        if (-not $r.Json -or [int]$r.ExitCode -eq 2) { & $need $r 'plan' }
        $checks = $r.Json.checks
        if (-not $checks) { throw 'plan printed no checks' }
        $state.tls = [bool]$checks.tls.ok; $state.ledger = [bool]$checks.ledger.ok; $state.service = $checks.service.state
        $bad = @($checks.PSObject.Properties | Where-Object { -not $_.Value.ok -and $_.Name -notin 'tls', 'ledger' -and -not ($_.Name -eq 'service' -and $_.Value.state) } | ForEach-Object { $_.Name })
        if ($bad.Count) { throw ('plan refuses: ' + ($bad -join ', ') + ' (see gateway-plan.json in the run folder)') }
        New-LabOutcome 'DONE' ('checks ok except: ' + $(if (-not $state.tls -or -not $state.ledger) { (@(@('tls') * (-not $state.tls) + @('ledger') * (-not $state.ledger))) -join ', ' } else { 'none' }) + $(if ($state.service) { "; service $($state.service)" } else { '' }))
    } | Out-Null
    Invoke-LabStep -Id 'g-certificate' -Title 'Gateway: certificate' -Action {
        if ($state.tls) { $out = 'certificate present' }
        else {
            if (-not (Request-LabApproval -Prompt ('Create the Gateway lab CA and server certificate (SAN {0}) with the Gateway''s certificate command?' -f (@($ctx.Manifest.tls.san) -join ', ')) -Word 'APPROVE' -Kind 'privileged')) { throw 'certificate declined' }
            $r = & $deploy 'certificate'; & $need $r 'certificate'
            $out = 'issued' + $(if ($r.Json.not_after) { ', valid until ' + $r.Json.not_after } else { '' })
        }
        if (Test-Path -LiteralPath $ctx.Paths.CaKey) {
            Write-LabLog ("  Move the CA private key {0} to offline media now (the Gateway contract: bring it back only to renew)." -f $ctx.Paths.CaKey) -Level WARN
            $ans = Read-LabInput -Prompt 'Type MOVED once ca.key is on offline media and no longer on this disk, or LATER'
            if ($ans -ieq 'MOVED' -and (Test-Path -LiteralPath $ctx.Paths.CaKey)) { throw "ca.key is still at $($ctx.Paths.CaKey)" }
            if ($ans -ine 'MOVED') { Add-LabPending -Area 'gateway' -Text ("the CA private key is still on the hub at $($ctx.Paths.CaKey): move it to offline media") }
        }
        if (Test-Path -LiteralPath $ctx.Paths.Ca) { $e = Export-LabGatewayCa -CaFile $ctx.Paths.Ca -UsbRoot $UsbRoot; $out += ('; public CA copied to {0} (sha256 {1})' -f $e.Path, $e.Sha256.Substring(0, 12)) }
        New-LabOutcome 'DONE' $out
    } | Out-Null
    Invoke-LabStep -Id 'g-install' -Title 'Gateway: install' -Action {
        $svcName = [string]$ctx.Manifest.service.name
        $svc = Get-LabGatewayService -Name $svcName
        if ($svc.Present -and $svc.State -eq 'Running') {
            # A running older release is updated with the Gateway's own "update" (0.3.0 -> 0.4.0 on DESKTOP-REDACT10): it backs
            # up the ledger, drains the old service, starts the new release on the SAME ledger (ledger.kept), and holds
            # the heads quarantined at that moment for its own probes (held_at_upgrade).
            $want = [string]$ctx.Manifest.release; $have = [string]$svc.Release
            if (-not $have -or $have -eq $want -or [version]$have -gt [version]$want) { return (New-LabOutcome 'UNCHANGED' ("service $svcName running release $(if ($have) { $have } else { '(unknown)' }) as $($svc.Account); this stick carries $want")) }
            $drain = [int]$ctx.Manifest.service.drain_seconds; if (-not $drain) { $drain = 90 }
            if (-not (Request-LabApproval -Prompt ("Update the Inference Gateway {0} to {1} with its own update? It backs up the ledger, lets {0} finish its running requests (up to {2} s, Capstone waits meanwhile), then starts {1} on the same ledger. Heads quarantined now are tested again by {1} from 60 s after." -f $have, $want, $drain) -Word 'UPDATE' -Kind 'disruptive')) { return (New-LabOutcome 'DECLINED' 'operator declined the update') }
            $r = & $deploy 'update'
            if (-not $r.Ok) {
                # pip --target leaves the new release's files owner-only (see Repair-LabGatewayReleaseAcl): the new service
                # cannot start (DESKTOP-REDACT10 2026-11-12: 1053, 43 of 43 files owner-only). Gateway 0.4.0's update then does NOT
                # return to the old release: StartServiceW's error escapes its switch-back, leaving the service pointed at
                # the new release, stopped. So: repair, then once more, whatever the service now points at (until 1.6.7 this
                # retried only when it still pointed at the old release, i.e. never).
                $acl = Repair-LabGatewayReleaseAcl -Run $Run; Write-LabLog ('  the update did not start {0} ({1}); release files: {2}; updating again' -f $want, $(if ($r.Json -and $r.Json.error) { $r.Json.error } else { $r.Meaning }), $acl.Detail) -Level WARN
                $r = & $deploy 'update'
            }
            if (-not $r.Ok -and (Get-LabGatewayService -Name $svcName).State -ne 'Running') {
                # Still not running: back to the old release with the Gateway's own switch-back (drains, expires leftover
                # leases, points the service at it, starts it, waits) and the record its update writes, so Capstone works.
                $back = Invoke-LabGatewaySwitchBack -Python $py -PackageRoot $ctx.Root -Settings $ctx.Settings -To $have -From $want -Folder $Run.Folder
                Write-LabLog ('  {0} did not start twice; switched back to {1}: {2}' -f $want, $have, $back.Detail) -Level $(if ($back.Ok) { 'WARN' } else { 'ERROR' })
                throw ('update: {0} did not start ({1}); {2}' -f $want, $(if ($r.Json -and $r.Json.error) { $r.Json.error } else { $r.Meaning }), $(if ($back.Ok) { "the Gateway runs $have again" } else { 'the switch back to ' + $have + ' failed too: ' + $back.Detail }))
            }
            & $need $r 'update'
            $v = Get-LabGatewayUpdateVerdict -Json $r.Json
            try { Save-LabJson -InputObject $r.Json -Path (Join-Path $Run.Folder ('gateway-update-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))) } catch { }
            if ($UsbRoot) { try { $d = Join-Path $UsbRoot "reports\$env:COMPUTERNAME"; New-Item -ItemType Directory -Path $d -Force | Out-Null; Save-LabJson -InputObject $r.Json -Path (Join-Path $d ('gateway-update-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))) } catch { } }
            if (-not $v.Ok) { throw ('update: ' + $v.Detail) }
            Register-LabChange -Kind 'gateway' -Description ("Gateway updated $have -> $want by its own update") -Undo ([pscustomobject]@{ type = 'manual'; instructions = ('"{0}" -I "{1}" rollback --settings "{2}"   (the ledger is kept; 0.3.x ignores the 0.4.0 tables)' -f $py, (Join-Path $ctx.Root 'bin\gateway-deploy.py'), $ctx.Settings) }) | Out-Null
            if ($v.Held.Count) { Add-LabPending -Area 'gateway' -Text ("Gateway $want holds these heads from before the update and tests them itself (about 1-25 minutes): " + ($v.Held -join ', ') + '. Fleet > Heads shows when they return.') }
            return (New-LabOutcome 'DONE' $v.Detail)
        }
        if ($svc.Present) {
            # Installed by an earlier run whose first start did not finish (DESKTOP-REDACT10, 1.3.4: 1053). The
            # installation itself is complete (release, config, ledger, service); only the start is left.
            $acl = Repair-LabGatewayReleaseAcl -Run $Run; Write-LabLog ('  release files: ' + $acl.Detail)
            Write-LabLog ("  service {0} is installed but {1}: starting it (up to 150 s)" -f $svcName, $svc.State)
            $st = Start-LabGatewayService -Name $svcName
            if ($st.Ok) { return (New-LabOutcome 'DONE' ("service $svcName was installed but $($svc.State); started now ($($st.Seconds) s), as $($svc.Account); " + $acl.Detail)) }
            $d = Save-LabGatewayDiagnostics -Run $Run -Name $svcName -Python $py
            throw ("the {0} service is installed but does not start ({1}{2}). {3}Diagnostics: {4}" -f $svcName, $st.Status, $(if ($st.Error) { ': ' + $st.Error } else { '' }), $(if ($d.Summary) { 'Windows says: ' + $d.Summary + '. ' } else { '' }), $d.Where)
        }
        $mode = Read-LabInput -Prompt 'Ledger: type NEW for a first installation, or the full path of a verified backup to restore' -Default ''
        $extra = $(if ($mode -ieq 'NEW') { @('--new-ledger') } elseif ($mode -and (Test-Path -LiteralPath $mode)) { @('--restore-backup', $mode) } else { throw 'no ledger choice: NEW or a backup path' })
        if ($extra[0] -eq '--new-ledger' -and $state.ledger) { throw 'a ledger already exists: never bootstrap a fresh ledger over an existing authority' }
        if (-not (Request-LabApproval -Prompt ("Run the Gateway installer ({0}) to create the InferenceGateway service as {1}?" -f ($extra -join ' '), (ConvertTo-LabResolvableAccount $ctx.Manifest.service_account.qualified)) -Word 'INSTALL' -Kind 'disruptive')) { throw 'install declined' }
        $pw = Read-LabSecret -Prompt ("Password of {0}, the same one you gave the account (handed to the installer on stdin)" -f (ConvertTo-LabResolvableAccount $ctx.Manifest.service_account.qualified)) -Confirm   # typed twice: a typo here only shows later, as a service that cannot log on
        $r = & $deploy 'install' (@('--password-stdin') + $extra) $pw; $pw = $null
        if (-not $r.Ok -and (Get-LabGatewayService -Name $svcName).Present) {
            # The installer created the service, and only its start failed. On DESKTOP-REDACT10 the cause was
            # owner-only release files (pip --target under Python 3.12.10, see Repair-LabGatewayReleaseAcl):
            # 7009 timeout, no Gateway log. Repair, start once more, and keep Windows' evidence if it still fails.
            Register-LabChange -Kind 'gateway' -Description 'Gateway installed by its own installer (its first start did not finish)' -Undo ([pscustomobject]@{ type = 'manual'; instructions = ('"{0}" -I "{1}" uninstall --settings "{2}"   (data is never deleted)' -f $py, (Join-Path $ctx.Root 'bin\gateway-deploy.py'), $ctx.Settings) }) | Out-Null
            Write-LabLog ('  the installer created {0}, but its start did not finish ({1}); starting it again (up to 150 s)' -f $svcName, $(if ($r.Json -and $r.Json.error) { $r.Json.error } else { $r.Meaning })) -Level WARN
            $acl = Repair-LabGatewayReleaseAcl -Run $Run; Write-LabLog ('  release files: ' + $acl.Detail)
            $st = Start-LabGatewayService -Name $svcName
            if ($st.Ok) { return (New-LabOutcome 'DONE' ("InferenceGateway service installed; the installer's first start did not finish, the second start ran ($($st.Seconds) s); " + $acl.Detail)) }
            $d = Save-LabGatewayDiagnostics -Run $Run -Name $svcName -Python $py
            throw ("install: the service was created but does not start ({0}{1}). {2}Diagnostics: {3}" -f $st.Status, $(if ($st.Error) { ': ' + $st.Error } else { '' }), $(if ($d.Summary) { 'Windows says: ' + $d.Summary + '. ' } else { '' }), $d.Where)
        }
        & $need $r 'install'
        Register-LabChange -Kind 'gateway' -Description 'Gateway installed by its own installer' -Undo ([pscustomobject]@{ type = 'manual'; instructions = ('"{0}" -I "{1}" uninstall --settings "{2}"   (data is never deleted)' -f $py, (Join-Path $ctx.Root 'bin\gateway-deploy.py'), $ctx.Settings) }) | Out-Null
        New-LabOutcome 'DONE' 'InferenceGateway service installed'
    } | Out-Null

    Write-LabStage -Letter 'D' -Title 'Inference Gateway: heads, clients, qualification, observation'
    $ctx.NoHeads = $false
    Invoke-LabStep -Id 'g-inventory' -Title 'Verified head inventory for the Gateway' -Action {
        $probes = @(Get-LabHeadProbe -Heads @($Config.known_heads) -Port ([int]$Config.ollama.port))
        $inv = New-LabHeadInventory -Probes $probes -Config $Config -From $Config.hub -Computer $env:COMPUTERNAME
        $ctx.Inventory = Join-Path $Run.Root ('gateway\verified-head-inventory-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Save-LabJson -InputObject (Protect-LabObject $inv) -Path $ctx.Inventory
        Copy-Item -LiteralPath $ctx.Inventory -Destination (Join-Path $Run.Folder 'verified-head-inventory.json') -Force
        if (-not @($inv.heads).Count) {
            # The planned staging point, not a failure: the service, certificate, ledger and accounts stay as
            # they are, and running this choice again resumes here once a head admits the hub.
            $ctx.NoHeads = $true
            return (New-LabOutcome 'PENDING' 'no head answered as Ollama from the hub yet')
        }
        New-LabOutcome 'DONE' ('{0} verified head(s), {1} excluded: {2}' -f @($inv.heads).Count, @($inv.excluded).Count, $ctx.Inventory)
    } | Out-Null
    if ($ctx.NoHeads -and -not $Run.Stopped) {
        Add-LabPending -Area 'gateway' -Text ('SAFE STOP after install: no inference head admits the hub yet. Nothing is reset by stopping here. Prepare one CPU head (New CPU inference head, or Existing machine > Ollama firewall on an existing head), then run Administration hub > 2 again: finished steps report UNCHANGED and the sequence continues at the head inventory.')
        foreach ($later in 'g-heads', 'g-credentials', 'g-bootstrap', 'g-qualify', 'g-accept', 'g-acl', 'g-verify', 'g-listener', 'g-cutover') {
            [void]$Run.Steps.Add([pscustomobject][ordered]@{ id = $later; title = $later; status = 'PENDING'; started = ''; finished = ''; detail = 'waiting for a reachable head (safe stop)' })
        }
        Write-LabStage -Letter 'E' -Title 'Verify what is installed so far'
        Add-LabGatewayVerification -Run $Run -Manifest $ctx.Manifest -Paths $ctx.Paths
        return
    }
    Invoke-LabStep -Id 'g-heads' -Title 'Gateway: heads (reviewable policy)' -Action {
        # The approved policy travels on this USB (reports\GATEWAY); Enter uses it, whatever the drive letter.
        $onUsb = $(if ($UsbRoot) { Join-Path $UsbRoot 'reports\GATEWAY\gateway-policy-approved.json' } else { '' })
        if ($onUsb) {
            # The model menu: the models installed on the verified heads, numbered, the allowed ones marked.
            # Enter keeps the list; numbers replace it. The policy on the USB is rewritten (the previous one
            # is kept beside it) and qualification below then covers exactly the allowed models.
            $current = $null; if (Test-Path -LiteralPath $onUsb) { $current = Get-Content -LiteralPath $onUsb -Raw | ConvertFrom-Json }
            # The Gateway Models screen on this hub (Administration hub > 5) keeps the policy it last applied. It
            # wins over the USB copy, so a re-run of this choice never undoes a change made on the screen; the USB
            # copy is refreshed from it (the previous one is kept beside it).
            $hubCopy = Get-LabHubApprovedPolicy
            if ($hubCopy) {
                $dir = Split-Path -Parent $onUsb; if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                if ($current) { Copy-Item -LiteralPath $onUsb -Destination (Join-Path $dir ('gateway-policy-approved.before-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force }
                [IO.File]::WriteAllText($onUsb, (($hubCopy.Policy | ConvertTo-Json -Depth 10) -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false))
                $current = $hubCopy.Policy
                Write-LabLog ('  allowed models from the Gateway Models screen on this hub ({0}, {1}); copied to the USB' -f $hubCopy.Path, $hubCopy.Written.ToString('yyyy-MM-dd HH:mm'))
            }
            $choices = @(Get-LabModelChoices -Inventory (Get-Content -LiteralPath $ctx.Inventory -Raw | ConvertFrom-Json))
            $allowed = @(); if ($current) { $allowed = @(@(@($current.clients) | Where-Object { $_.app_id -eq 'capstone' }) | ForEach-Object { @($_.models) }) }
            Write-Host ''
            Write-Host '  Models the Gateway may use (installed on the verified heads; [x] = allowed now):'
            for ($i = 0; $i -lt $choices.Count; $i++) { $c = $choices[$i]; Write-Host ('   {0,2}  [{1}] {2,-22} heads {3}{4}' -f ($i + 1), $(if ($allowed -contains $c.Name) { 'x' } else { ' ' }), $c.Name, ($c.Heads -join ' '), $(if ($c.Note) { '   (' + $c.Note + ')' } else { '' })) }
            if (-not $choices.Count) { Write-Host '   (no chat model is installed on the verified heads)' }
            foreach ($a in @($allowed | Where-Object { @($choices | ForEach-Object Name) -notcontains $_ })) { Write-Host ('        [x] {0,-22} on no verified head now' -f $a) }
            for ($try = 0; $try -lt 3 -and ($choices.Count -or $current); $try++) {
                $ans = Read-LabInput -Prompt $(if ($current) { 'Press Enter to keep the allowed models, or type the numbers to allow (for example 1,4,7)' } else { 'Type the numbers of the models to allow (for example 1,4,7)' })
                if (-not $ans -and $current) { Write-LabLog ('  allowed models kept: ' + ($allowed -join ', ')); break }
                $sel = ConvertFrom-LabModelSelection -Answer $ans -Choices $choices
                if (-not $sel.Ok) { Write-Host ('  ' + $sel.Reason + '. Try again.') -ForegroundColor Yellow; continue }
                $template = $null; $tf = Join-Path $ctx.Root 'config\policy.template.json'; if (Test-Path -LiteralPath $tf) { $template = Get-Content -LiteralPath $tf -Raw | ConvertFrom-Json }
                $newPol = New-LabPolicyWithModels -Policy $current -Models $sel.Models -Template $template
                $dir = Split-Path -Parent $onUsb; if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                if ($current) { Copy-Item -LiteralPath $onUsb -Destination (Join-Path $dir ('gateway-policy-approved.before-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force }
                [IO.File]::WriteAllText($onUsb, (($newPol | ConvertTo-Json -Depth 10) -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false))
                # The screen's copy follows a menu choice, or the next run would take the older copy back.
                if ($hubCopy) { [IO.File]::WriteAllText($hubCopy.Path, (($newPol | ConvertTo-Json -Depth 10) -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false)) }
                $bad = @($choices | Where-Object { $sel.Models -contains $_.Name -and $_.Note })
                foreach ($b in $bad) { Add-LabWarning ("{0} was allowed, but it {1}; Capstone gets nothing from it until it passes qualification" -f $b.Name, $b.Note) }
                Write-LabLog ('  allowed models now: capstone ' + (@(@($newPol.clients) | Where-Object app_id -eq 'capstone')[0].models -join ', ') + '; capstone-research ' + (@(@($newPol.clients) | Where-Object app_id -eq 'capstone-research')[0].models -join ', ') + " (written to $onUsb)")
                $current = $newPol; break
            }
            $approved = $(if ($current) { $onUsb } else { '' })
        }
        else { $approved = Read-LabInput -Prompt 'Path of the APPROVED Gateway policy (made from the release''s config\policy.template.json, with approved models)' }
        $approved = ([string]$approved).Trim().Trim('"')
        if (-not $approved -or -not (Test-Path -LiteralPath $approved)) { Add-LabPending -Area 'gateway' -Text 'no approved Gateway policy yet: the operator writes it from config\policy.template.json'; return (New-LabOutcome 'PENDING' 'no approved policy') }
        $pol = Get-Content -LiteralPath $approved -Raw | ConvertFrom-Json
        $idc = Test-LabGatewayPolicyIdentities -Policy $pol
        foreach ($n in $idc.Notes) { Add-LabWarning $n }
        if (-not $idc.Ok) { throw ('policy refused: ' + ($idc.Findings -join '; ')) }
        $notIssued = @(@($pol.clients) | Where-Object { $_.credential -eq 'provisioned' -and [string]$_.app_id -notin 'capstone', 'capstone-research' } | ForEach-Object { [string]$_.app_id })
        if ($notIssued.Count) { Add-LabWarning ('the approved policy declares {0}, whose credential F: does not issue (coder: open item O5). The Gateway''s bootstrap refuses a declared client without a credential: remove it from the approved policy for this rollout.' -f ($notIssued -join ', ')) }
        # The heads serve the identities F: issues that the approved policy declares; coder joins only with
        # open item O5. (The manifest's example "capstone,coder" left capstone-research without a head.)
        $apps = (@(@($pol.clients) | ForEach-Object { [string]$_.app_id } | Where-Object { $_ -in 'capstone', 'capstone-research' } | Select-Object -Unique)) -join ','
        Write-LabLog ('  the heads serve: ' + $apps)
        # The Gateway refuses to overwrite a review: each run writes a new one.
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'; $n = 1
        $ctx.Review = Join-Path (Split-Path -Parent $ctx.Paths.Review) ('policy-review-{0}.json' -f $stamp)
        while (Test-Path -LiteralPath $ctx.Review) { $n++; $ctx.Review = Join-Path (Split-Path -Parent $ctx.Paths.Review) ('policy-review-{0}-{1}.json' -f $stamp, $n) }   # two runs in one second
        $r = & $launch 'heads' @('heads', '--inventory', $ctx.Inventory, '--policy', $approved, '--applications', $apps, '--output', $ctx.Review); & $need $r 'heads'
        $rev = Get-Content -LiteralPath $ctx.Review -Raw | ConvertFrom-Json
        Write-LabLog ('  review: {0} resource(s): {1}; clients {2}' -f @($rev.resources).Count, ((@($rev.resources) | ForEach-Object { $_.resource_id }) -join ', '), ((@($rev.clients) | ForEach-Object { $_.app_id }) -join ', '))
        New-LabOutcome 'DONE' ("reviewable policy written: $($ctx.Review)")
    } | Out-Null
    if (-not $ctx.Review) { $latest = @(Get-ChildItem -LiteralPath (Split-Path -Parent $ctx.Paths.Review) -Filter 'policy-review*.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending) | Select-Object -First 1; if ($latest) { $ctx.Review = $latest.FullName } }
    Invoke-LabStep -Id 'g-credentials' -Title 'Gateway: application credentials' -ContinueOnFailure -Action {
        if (-not $ctx.Review -or -not (Test-Path -LiteralPath $ctx.Review)) { return (New-LabOutcome 'PENDING' 'no reviewed policy') }
        $outbox = Join-Path $Run.Root 'gateway\outbox'
        if (-not (Test-Path -LiteralPath $outbox)) { New-Item -ItemType Directory -Path $outbox -Force | Out-Null; & icacls.exe $outbox /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null }
        # The Gateway's own list says which applications already hold a credential. Issuing again is
        # refused by the Gateway ("use rotate"); rotating replaces the token the application holds, so it
        # is offered only as an explicit choice.
        $list = & $launch 'credentials-list' @('credentials', 'list', '--database', $ctx.Paths.Ledger); & $need $list 'credentials list'
        $have = @(@($list.Json.applications) | ForEach-Object { [string]$_.app_id })
        $done = @(); $waiting = @()
        $apps = @('capstone', 'capstone-research')   # coder: open item O5 (no client, no declared reader account yet)
        $new = @($apps | Where-Object { $have -notcontains $_ })
        $issueOk = $false
        if ($new.Count) { $issueOk = Request-LabApproval -Prompt ("Issue the Gateway credentials for {0}? Each is written once to {1}, readable on its target by {2}; the provisioner never reads them." -f ($new -join ' and '), $outbox, $ctx.Manifest.tokens.capstone.reader) -Word 'ISSUE' -Kind 'change' }
        foreach ($app in $apps) {
            $file = Join-Path $outbox ($app + '.token')
            if ($have -contains $app) {
                if (Test-Path -LiteralPath $file) { $waiting += $app; continue }
                $ans = Read-LabInput -Prompt ("'{0}' already holds a Gateway credential. Press Enter to keep it, or type ROTATE to replace its token (the application must then get the new file)" -f $app)
                if ($ans -cne 'ROTATE') { $done += "$app kept"; continue }
                if (-not (Request-LabApproval -Prompt ("Rotate the credential of '{0}'? Its current token stops working at once." -f $app) -Word 'ROTATE' -Kind 'disruptive')) { $done += "$app kept"; continue }
                $r = & $launch ('credentials-rotate-' + $app) @('credentials', 'rotate', '--database', $ctx.Paths.Ledger, '--app-id', $app, '--output', $file, '--reader', [string]$ctx.Manifest.tokens.capstone.reader); & $need $r "credentials rotate $app"
                $waiting += $app; $done += "$app rotated"; continue
            }
            if (-not $issueOk) { continue }
            $r = & $launch ('credentials-' + $app) @('credentials', 'issue', '--database', $ctx.Paths.Ledger, '--policy', $ctx.Review, '--app-id', $app, '--output', $file, '--reader', [string]$ctx.Manifest.tokens.capstone.reader); & $need $r "credentials issue $app"
            $waiting += $app; $done += "$app issued"
        }
        Add-LabPending -Area 'gateway' -Text 'coder credential: issued when Coder adopts the Gateway client (open item O5)'
        if ($waiting.Count) { Add-LabPending -Area 'gateway' -Text ("carry each token file from $outbox to its application host on a second USB stick (not the provisioner stick), then delete the hub copy: " + ($waiting -join ', ')) }
        New-LabOutcome $(if ($done.Count) { 'DONE' } else { 'PENDING' }) $(if ($done.Count) { $done -join '; ' } else { 'none issued' })
    } | Out-Null
    Invoke-LabStep -Id 'g-bootstrap' -Title 'Gateway: bootstrap' -Action {
        if (-not $ctx.Review -or -not (Test-Path -LiteralPath $ctx.Review)) { return (New-LabOutcome 'PENDING' 'no reviewed policy') }
        $idc = Test-LabGatewayPolicyIdentities -Policy (Get-Content -LiteralPath $ctx.Review -Raw | ConvertFrom-Json)
        if (-not $idc.Ok) { throw ('reviewed policy refused: ' + ($idc.Findings -join '; ')) }
        # The Gateway's bootstrap only updates the policy of a client declared "credential": "provisioned"
        # and refuses one that holds no credential yet (capacity_broker.update_client_policy). So credentials
        # are issued first, and a declared client without one (coder until O5) is reported, not sent.
        $declared = @(@((Get-Content -LiteralPath $ctx.Review -Raw | ConvertFrom-Json).clients) | Where-Object { $_.credential -eq 'provisioned' } | ForEach-Object { [string]$_.app_id })
        $list = & $launch 'credentials-list' @('credentials', 'list', '--database', $ctx.Paths.Ledger); & $need $list 'credentials list'
        $have = @(@($list.Json.applications) | ForEach-Object { [string]$_.app_id })
        $without = @($declared | Where-Object { $have -notcontains $_ })
        if ($without.Count) {
            Add-LabPending -Area 'gateway' -Text ('the reviewed policy declares {0} without a credential, and the Gateway''s bootstrap refuses such a client. Issue it (credentials step) or remove it from the approved policy (coder stays out until open item O5), then run Administration hub > 2 again.' -f ($without -join ', '))
            return (New-LabOutcome 'PENDING' ('no credential yet for ' + ($without -join ', ')))
        }
        if (-not (Request-LabApproval -Prompt ("Apply the reviewed policy {0} (enrolls its heads and clients; never re-enables a revoked client)? Read it first." -f $ctx.Review) -Word 'APPLY' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        $r = & $launch 'bootstrap' @('bootstrap', '--database', $ctx.Paths.Ledger, '--policy', $ctx.Review); & $need $r 'bootstrap'
        New-LabOutcome 'DONE' ('policy applied: ' + $ctx.Review)
    } | Out-Null
    Invoke-LabStep -Id 'g-qualify' -Title 'Gateway: qualification' -ContinueOnFailure -Action {
        # Every model the reviewed policy names, on every verified head that has it installed, at 8192 tokens:
        # Capstone sends num_ctx 8192, and the Gateway refuses a request above the qualified context
        # (context_exceeds_qualification). One approval covers the list; nothing is typed per head.
        # Each model gets the test it can pass: an embedding model the Gateway's embedding test, every other model the
        # text test (Get-LabQualifyPurpose). Until 1.6.7 every model got the text test, and since a failed test takes a
        # model off the head, this step took all-minilm (Capstone's lookups) off all nine heads (2026-11-12).
        $done = @(); $results = @(); $clients = @()
        $models = @()
        if ($ctx.Review -and (Test-Path -LiteralPath $ctx.Review)) { $clients = @((Get-Content -LiteralPath $ctx.Review -Raw | ConvertFrom-Json).clients); $models = @($clients | ForEach-Object { @($_.models) } | Where-Object { $_ } | Select-Object -Unique) }
        $pairs = @()
        # A model marked Do not use on a head in the Gateway Models screen is never qualified there again from here:
        # qualifying would put it back on that head's list.
        $blocked = @(Get-LabDoNotUse); $kept = @()
        if ($ctx.Inventory -and (Test-Path -LiteralPath $ctx.Inventory)) {
            foreach ($h in @((Get-Content -LiteralPath $ctx.Inventory -Raw | ConvertFrom-Json).heads)) {
                $have = @(@($h.verified.models) | ForEach-Object { [string]$_.name })
                foreach ($m in $models) {
                    if ($have -notcontains $m) { continue }
                    if ($blocked -contains ('{0}|{1}' -f $h.resource_id, $m)) { $kept += ('{0} {1}' -f $h.resource_id, $m); continue }
                    $pairs += [pscustomobject]@{ Resource = [string]$h.resource_id; Model = [string]$m }
                }
            }
        }
        if ($kept.Count) { Write-LabLog ('  not qualified (Do not use in the Gateway Models screen): ' + ($kept -join ', ')) }
        if (-not $pairs.Count) { Add-LabPending -Area 'gateway' -Text 'no verified head has a model the approved policy names: install one there (Existing machine > models), then run Administration hub > 2 again'; return (New-LabOutcome 'PENDING' 'nothing to qualify') }
        # 1.6.7: only the pairs that are not usable now are tested (a Gateway update keeps the others), each with the right
        # purpose (Get-LabQualifyPurpose), through the Gateway Models screen's run_head_tests: the model is loaded on the
        # head first, so a slow first load never runs out the Gateway's test time and holds the head; heads side by side.
        $qp = Join-Path $script:LabGatewayFlowRoot 'hub-tools\gateway-models\qualify_pairs.py'
        $launcher = @(Get-LabGatewayLauncherArgs -Manifest $ctx.Manifest -Arguments @())[1]; $releaseDir = Split-Path -Parent (Split-Path -Parent $launcher)
        $pairsFile = Join-Path $Run.Folder 'qualify-pairs.json'; $planFile = Join-Path $Run.Folder 'qualify-plan.json'; $outFile = Join-Path $Run.Folder 'qualify-results.json'
        [IO.File]::WriteAllText($pairsFile, (ConvertTo-Json -InputObject @($pairs | ForEach-Object { , @($_.Resource, $_.Model) }) -Depth 3 -Compress), (New-Object Text.UTF8Encoding $false))
        $qa = @('-I', $qp, '--python', $py, '--release', $releaseDir, '--ledger', $ctx.Paths.Ledger, '--evidence', $ctx.Paths.Backups, '--operator', $env:USERNAME, '--pairs', $pairsFile)
        & $py @qa --out $planFile --plan-only 2>&1 | ForEach-Object { Write-LabLog ('  ' + [string]$_) }
        $plan = $null; try { $plan = Get-Content -LiteralPath $planFile -Raw | ConvertFrom-Json } catch { }
        if (-not $plan) { throw "the qualification plan could not be made ($qp)" }
        $keptNow = @(@($plan.kept) | Where-Object { $_ }); $toTest = @(@($plan.to_test) | Where-Object { $_ })
        foreach ($k in $keptNow) { $results += [pscustomobject]@{ Resource = [string]$k[0]; Model = [string]$k[1]; Passed = $true } }
        if (-not $toTest.Count) { return (New-LabOutcome 'UNCHANGED' ('all {0} head/model pair(s) are usable now; nothing to test' -f $keptNow.Count)) }
        $list = ($toTest | ForEach-Object { '{0} {1}' -f $_[0], $_[1] }) -join ', '
        if (Request-LabApproval -Prompt ("Qualify the {0} head/model pair(s) not usable now ({1} usable now are kept)? Each model is loaded on its head first, then the Gateway's own test; heads side by side, a few minutes per head: {2}" -f $toTest.Count, $keptNow.Count, $list) -Word 'QUALIFY' -Kind 'disruptive') {
            & $py @qa --out $outFile 2>&1 | ForEach-Object { Write-LabLog ('  ' + [string]$_) }
            $got = $null; try { $got = Get-Content -LiteralPath $outFile -Raw | ConvertFrom-Json } catch { }
            if (-not $got) { throw "the qualification did not finish: no result from $qp" }
            foreach ($q in @($got.results)) {
                $results += [pscustomobject]@{ Resource = [string]$q.resource; Model = [string]$q.model; Passed = [bool]$q.passed }
                $line = ('{0} {1}: {2}' -f $q.resource, $q.model, $(if ($q.passed) { 'passed' } else { 'not usable on this head (the Gateway''s ' + $q.purpose + ' test: ' + $q.why + ')' }))
                $done += $line
            }
        }
        if (-not $done.Count -and -not $keptNow.Count) { Add-LabPending -Area 'gateway' -Text 'no model qualified yet: nothing is usable through the Gateway until qualification' }
        $sum = Get-LabQualifySummary -Results $results -Clients $clients
        if ($sum.Stranded.Count) { Add-LabPending -Area 'gateway' -Text ('no head passed the Gateway''s test for any model of: ' + ($sum.Stranded -join '; ')) }
        New-LabOutcome $sum.Status (('{0} usable ({1} kept, {2} newly passed), {3} not usable on their head; ' -f $sum.Passed, $keptNow.Count, ($sum.Passed - $keptNow.Count), $sum.Failed) + ($done -join '; '))
    } | Out-Null
    Invoke-LabStep -Id 'g-accept' -Title 'Gateway: observation ledger acceptance' -Action {
        $st = & $launch 'lifecycle-status' @('lifecycle', 'status', '--database', $ctx.Paths.Ledger); & $need $st 'lifecycle status'
        $lid = [string]$st.Json.ledger_id
        $ps = & $launch 'publisher-status' @('publisher', 'status', '--database', $ctx.Paths.Ledger, '--state', $ctx.Paths.PublisherState)
        if ($ps.Ok -and $lid -and [string]$ps.Json.accepted_ledger_id -eq $lid) { return (New-LabOutcome 'UNCHANGED' ("ledger $lid already accepted (publisher status)")) }
        Write-LabLog ("  ledger_id {0}: tell the Arena operator before it is accepted (ARENA_INTEGRATION section 6)." -f $lid) -Level WARN
        $op = $env:USERNAME   # the signed-in maintenance administrator is the operator of record
        if (-not (Request-LabApproval -Prompt ("The Arena operator has been told ledger_id {0}. Accept it for publication?" -f $lid) -Word 'ACCEPT' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'not accepted yet: Arena shows stale until it is') }
        $r = & $launch 'accept-ledger' @('publisher', 'accept-ledger', '--database', $ctx.Paths.Ledger, '--state', $ctx.Paths.PublisherState, '--operator', $op); & $need $r 'accept-ledger'
        New-LabOutcome 'DONE' ("ledger $lid accepted by $op")
    } | Out-Null
    Invoke-LabStep -Id 'g-acl' -Title 'Gateway: observation publication ACL' -Action {
        $pwsh = [string]$Config.runtime.pwsh.path
        $script = Join-Path $ctx.Root 'scripts\Set-ObservationPublicationAcl.ps1'
        # 1.6.21: Gateway 0.4.4 opens and closes the ledger all the time, so SQLite's capacity-ledger.db-shm comes and
        # goes; the Gateway script lists the ledger folder and then reads each file's ACL, and failed on every run when
        # the file was gone by then (hub, 2026-11-17 21:50 and 22:07). A corrected copy skips a file only when it no
        # longer exists; every other read error still fails the check. The Gateway source has the same fix.
        $script = Get-LabFixedPublicationAclScript -Source $script -Folder $Run.Folder
        $d = $ctx.Paths.Data
        $base = "& '$script' -PublishDirectory '$d\observation' -StateDirectory '$d\observation-state' -LogDirectory '$d\logs' -LedgerDirectory '$d\state' -GatewayAccount '$(ConvertTo-LabResolvableAccount $ctx.Manifest.service_account.qualified)' -ArenaAccount " + ((@($ctx.ArenaAccounts) | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ',')
        $p = Invoke-LabGatewayCommand -FileName $pwsh -Arguments @('-NoProfile', '-NonInteractive', '-Command', ($base + ' -PlanOnly')) -Label 'publication-acl-plan' -Run $Run
        # 1.6.20: SQLite's -shm/-wal files come and go while the Gateway runs; the Gateway script lists the ledger folder,
        # then reads each file's ACL, so a file that vanished in between failed the step (hub, 2026-11-17 21:50). Once more.
        $vanished = { param($x) (-not $x.Ok) -and ("$($x.Stderr) $(if ($x.Json) { $x.Json | ConvertTo-Json -Depth 6 -Compress })" -match "Cannot find path '[^']*-(shm|wal|journal)'") }
        if (& $vanished $p) { Start-Sleep -Seconds 2; $p = Invoke-LabGatewayCommand -FileName $pwsh -Arguments @('-NoProfile', '-NonInteractive', '-Command', ($base + ' -PlanOnly')) -Label 'publication-acl-plan-retry' -Run $Run }
        & $need $p 'publication ACL (PlanOnly)'
        if (-not (Request-LabApproval -Prompt ('Apply the publication ACL for Arena account(s) ' + ($ctx.ArenaAccounts -join ', ') + '?') -Word 'APPROVE' -Kind 'change')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        $r = Invoke-LabGatewayCommand -FileName $pwsh -Arguments @('-NoProfile', '-NonInteractive', '-Command', $base) -Label 'publication-acl' -Run $Run
        if (& $vanished $r) { Start-Sleep -Seconds 2; $r = Invoke-LabGatewayCommand -FileName $pwsh -Arguments @('-NoProfile', '-NonInteractive', '-Command', $base) -Label 'publication-acl-retry' -Run $Run }
        & $need $r 'publication ACL'
        New-LabOutcome 'DONE' 'publication ACL applied and verified by the Gateway script (ok: true)'
    } | Out-Null
    Invoke-LabStep -Id 'g-verify' -Title 'Gateway: verify' -Action {
        $r = & $deploy 'verify'; & $need $r 'verify'
        New-LabOutcome 'DONE' ('verify ok' + $(if ($r.Json.checks.tls.not_after) { '; certificate until ' + $r.Json.checks.tls.not_after } else { '' }))
    } | Out-Null

    Write-LabStage -Letter 'C' -Title 'Inference Gateway: client listener'
    Invoke-LabStep -Id 'g-listener' -Title 'Gateway listener firewall rule' -Action {
        $alias = ''; try { $alias = (Get-NetIPAddress -IPAddress $Config.hub -ErrorAction Stop | Select-Object -First 1).InterfaceAlias } catch { }
        $cat = ''; if ($alias) { $cat = [string](Get-NetConnectionProfile -InterfaceAlias $alias -ErrorAction SilentlyContinue).NetworkCategory }
        if ($cat -ne [string]$ctx.Manifest.listener.firewall_rule.profile) { throw ("the lab adapter's network profile is '{0}', but the listener rule applies to the {1} profile: run the hub's network profile step first" -f $cat, $ctx.Manifest.listener.firewall_rule.profile) }
        $spec = New-LabGatewayListenerRuleSpec -Manifest $ctx.Manifest
        $have = $null; try { $have = @((Get-LabFirewallSnapshot).Rules | Where-Object { $_.Name -eq $spec.Name }) | Select-Object -First 1 } catch { }
        if ($have) { return (Set-LabGatewayListenerRule -Run $Run -Spec $spec) }   # present: compared, never re-asked (UNCHANGED, or refused if it differs)
        if (-not (Request-LabApproval -Prompt ('Add the inbound rule {0}: TCP {1} on {2} from {3} only?' -f $ctx.Manifest.listener.firewall_rule.name, $ctx.Manifest.listener.port, $ctx.Manifest.listener.bind, (@($ctx.Manifest.listener.allowed_clients) -join ', ')) -Word 'APPROVE' -Kind 'privileged')) { throw 'listener rule declined' }
        Set-LabGatewayListenerRule -Run $Run -Spec (New-LabGatewayListenerRuleSpec -Manifest $ctx.Manifest)
    } | Out-Null
    Invoke-LabStep -Id 'g-cutover' -Title ('Temporary cutover access for {0} (optional)' -f (@($ctx.Manifest.listener.cutover_clients) -join ', ')) -ContinueOnFailure -Action {
        $cutName = [string]$ctx.Manifest.listener.firewall_rule.name + '-cutover'
        $open = $null; try { $open = @((Get-LabFirewallSnapshot).Rules | Where-Object { $_.Name -eq $cutName }) | Select-Object -First 1 } catch { }
        if ($open) { return (New-LabOutcome 'UNCHANGED' ("$cutName is open ($($open.DisplayName)); remove it when the cutover ends")) }
        $ans = Read-LabInput -Prompt ('Open the temporary cutover rule for {0} for an APPROVED Capstone migration? Type its end date (yyyy-mm-dd) or press Enter for no' -f (@($ctx.Manifest.listener.cutover_clients) -join ', '))
        if (-not $ans) { return (New-LabOutcome 'SKIPPED' 'no cutover rule (the default)') }
        if ($ans -notmatch '^\d{4}-\d{2}-\d{2}$') { throw 'an end date yyyy-mm-dd is required' }
        if (-not (Request-LabApproval -Prompt ("Add the SEPARATE cutover rule for {0} until {1} (open item O2)?" -f (@($ctx.Manifest.listener.cutover_clients) -join ', '), $ans) -Word 'CUTOVER' -Kind 'privileged')) { return (New-LabOutcome 'DECLINED' 'operator declined') }
        $r = Set-LabGatewayListenerRule -Run $Run -Spec (New-LabGatewayListenerRuleSpec -Manifest $ctx.Manifest -Cutover -Until $ans)
        Add-LabPending -Area 'gateway' -Text ("remove the cutover rule $($ctx.Manifest.listener.firewall_rule.name)-cutover on $ans (Existing machine > roll back, or Remove-NetFirewallRule)")
        $r
    } | Out-Null

    Write-LabStage -Letter 'E' -Title 'Verify the Gateway and Arena, separately'
    Add-LabGatewayVerification -Run $Run -Manifest $ctx.Manifest -Paths $ctx.Paths
}

function Get-LabHubStageState {
    <#  How far the staged hub rollout has got, read from the machine itself (a Verify-only run has no
        steps of its own): Gateway installed = its service exists; Gateway complete = its listener rule
        exists, which the guided installation creates only after the Gateway's own verify returned ok.  #>
    param($Manifest = $null, $Snapshot = $null)
    $svcName = $(if ($Manifest) { [string]$Manifest.service.name } else { [string]$Config.gateway.service_name })
    $ruleName = $(if ($Manifest) { [string]$Manifest.listener.firewall_rule.name } else { 'InferenceGateway-HTTPS-{0}' -f $Config.gateway.port })
    $svc = Get-LabGatewayService -Name $svcName
    if (-not $Snapshot) { try { $Snapshot = Get-LabFirewallSnapshot } catch { } }
    $rule = [bool]($Snapshot -and @($Snapshot.Rules | Where-Object { $_.Name -eq $ruleName }).Count)
    return [pscustomobject][ordered]@{ Service = $svc; GatewayInstalled = [bool]$svc.Present; ListenerRule = $rule; ListenerRuleName = $ruleName; GatewayComplete = ([bool]$svc.Present -and $rule); Snapshot = $Snapshot }
}

function Add-LabGatewayVerification {
    param([Parameter(Mandatory)]$Run, $Manifest = $null, $Paths = $null, $Stage = $null)
    if (-not $Manifest) {
        $zip = Find-LabGatewayPackage -Folder (Join-Path $Packages 'gateway')
        if ($zip) { $m = Read-LabGatewayManifestFromZip -Zip $zip.FullName; if ($m) { $Manifest = $m.Manifest } }
    }
    if (-not $Manifest) { Add-LabVerification -Check 'Inference Gateway' -Status PENDING -Scope external -Detail 'no Gateway release on this USB' | Out-Null; return }
    if (-not $Paths) { $Paths = Get-LabGatewayPaths -Manifest $Manifest }
    if (-not $Stage) { $Stage = Get-LabHubStageState -Manifest $Manifest }
    $svc = $Stage.Service
    Add-LabVerification -Check 'InferenceGateway service runs (delayed automatic, as the service account)' -Status $(if ($svc.Present -and $svc.State -eq 'Running' -and $svc.Account -match [regex]::Escape([string]$Manifest.service_account.name)) { 'PASS' } elseif ($svc.Present) { 'FAIL' } else { 'PENDING' }) -Detail $(if ($svc.Present) { '{0}, {1}{2}, as {3}' -f $svc.State, $svc.StartMode, $(if ($svc.Delayed) { ' (delayed)' } else { '' }), $svc.Account } else { 'not installed' }) | Out-Null
    $ledger = 'unknown'
    if ($svc.Present) {
        $py = [string]$Config.runtime.python.path
        $st = Invoke-LabGatewayCommand -FileName $py -Arguments (Get-LabGatewayLauncherArgs -Manifest $Manifest -Arguments @('status', '--config', $Paths.Config, '--observation-file', $Paths.Observation, '--publisher-state', $Paths.PublisherState)) -Label 'status' -Run $Run
        # Ledger acceptance from the publisher's own status: blocked is null only for an accepted ledger.
        $pub = Invoke-LabGatewayCommand -FileName $py -Arguments (Get-LabGatewayLauncherArgs -Manifest $Manifest -Arguments @('publisher', 'status', '--database', $Paths.Ledger, '--state', $Paths.PublisherState)) -Label 'publisher-status' -Run $Run
        if ($pub.Json -and $pub.Json.PSObject.Properties.Name -contains 'blocked') { $ledger = $(if ($null -eq $pub.Json.blocked) { 'accepted' } else { 'not accepted: ' + [string]$pub.Json.blocked }) }
        Add-LabVerification -Check 'Gateway status command' -Status $(if ($st.Ok) { 'PASS' } else { 'FAIL' }) -Detail ('{0}; ledger acceptance {1} (see gateway-status.json)' -f $st.Meaning, $ledger) | Out-Null
        $h = Test-LabGatewayHealth -BaseUrl ('https://{0}:{1}' -f $Manifest.listener.bind, $Manifest.listener.port) -CaFile $Paths.Ca -ExpectedServiceId ([string]$Manifest.observation.service_id)
        Add-LabVerification -Check 'Gateway /health and /ready over TLS with the Gateway CA (from the hub)' -Status $(if ($h.Ok) { 'PASS' } else { 'FAIL' }) -Detail $h.Detail | Out-Null
    }
    $snap = $Stage.Snapshot; if (-not $snap) { try { $snap = Get-LabFirewallSnapshot } catch { } }
    if ($snap) {
        $have = @($snap.Rules | Where-Object { $_.Name -eq [string]$Manifest.listener.firewall_rule.name })
        if ($have.Count) {
            # The profile of the adapter that holds the listener address. Without it the analysis took the first
            # connection: on DESKTOP-REDACT10 the Public Wi-Fi, so the Private-profile rule "admitted nobody" (2026-11-06)
            # although .1 connected through it.
            $lalias = ''; try { $lalias = (Get-NetIPAddress -IPAddress ([string]$Manifest.listener.bind) -ErrorAction Stop | Select-Object -First 1).InterfaceAlias } catch { }
            $exp = Get-LabListenerExposure -Snapshot $snap -Manifest $Manifest -Config $Config -InterfaceAlias $lalias
            $cut = @($snap.Rules | Where-Object { $_.Name -eq ([string]$Manifest.listener.firewall_rule.name + '-cutover') -and $_.Enabled }).Count -gt 0
            foreach ($s in @($exp.Sources)) {
                $want = $(if (@($Manifest.listener.allowed_clients) -contains $s.Source -or ($cut -and @($Manifest.listener.cutover_clients) -contains $s.Source)) { 'ADMITTED' } else { 'DENIED' })
                Add-LabVerification -Check ("listener 8443 from $($s.Source) is $want (rule analysis)") -Status $(if ($s.Verdict -eq $want) { 'PASS' } else { 'FAIL' }) -Detail $s.Reason | Out-Null
            }
        } else { Add-LabVerification -Check 'Gateway listener rule' -Status PENDING -Detail 'not created yet (it is created after the Gateway verify)' | Out-Null }
    }
    $gw = Get-LabHubGatewayReadiness -Config $Config -LedgerAccepted $ledger
    $o = $gw.Observation
    # Stage-aware: the observation is a product of the whole Gateway sequence (install, heads, credentials,
    # bootstrap, ledger acceptance, publication ACL, verify, listener) and Arena's own configuration, so it
    # can only fail once both are complete; until then it is PENDING with what is missing.
    $obsReady = ($Stage.GatewayComplete -and $o.configured)
    $obsWhy = $(if (-not $Stage.GatewayInstalled) { 'pending Gateway installation (Administration hub > 2); ' } elseif (-not $Stage.GatewayComplete) { 'pending: the Gateway installation has not reached its listener step; ' } elseif (-not $o.configured) { 'pending: Arena is not configured to read it yet (Administration hub > 1); ' } else { '' })
    Add-LabVerification -Check 'Arena observation (separate from the Gateway''s own health)' -Status (Get-LabStagedStatus -Ok $o.live -StageComplete $obsReady) -Scope external -Detail ('{0}{1} (configured {2}, file {3}, access {4}, fresh {5}, service {6}, ledger {7})' -f $obsWhy, $o.summary, $o.configured, $o.file_present, $o.access, $o.fresh, $o.service_running, $o.ledger_accepted) | Out-Null
    # The account rule protects the Gateway's ledger, so it fails once the Gateway is installed. Before
    # that it is a prerequisite of the installation stage, which refuses to start (plan BLOCKED and the
    # g-arena-accounts step) while it is unmet: PENDING here, with the way forward.
    $accWhy = $(if ($gw.Accounts.State -eq 'BLOCKED' -and -not $Stage.GatewayInstalled) { 'prerequisite of the Gateway installation, which refuses to start until it is met (Administration hub > 3 moves Arena to an existing standard account): ' } else { '' })
    Add-LabVerification -Check 'Arena accounts meet the Gateway account rule (transitive)' -Status $(if ($gw.Accounts.State -eq 'OK') { 'PASS' } elseif ($gw.Accounts.State -eq 'BLOCKED') { Get-LabStagedStatus -Ok $false -StageComplete $Stage.GatewayInstalled } else { 'PENDING' }) -Detail ($accWhy + ((@($gw.Accounts.Blockers) + @($gw.Accounts.Unknown) + @($gw.Accounts.Notes)) -join '; ')) | Out-Null
    $outbox = Join-Path $Run.Root 'gateway\outbox'
    $left = @(Get-ChildItem -LiteralPath $outbox -Filter '*.token' -ErrorAction SilentlyContinue)
    if ($left.Count) { Add-LabVerification -Check 'no application token left on the hub' -Status PENDING -Required $false -Detail ('still in the outbox: ' + (($left | ForEach-Object Name) -join ', ') + ' (they wait there for the Capstone stage: carry them to .201, then delete the hub copy)') | Out-Null }
    if (Test-Path -LiteralPath $Paths.CaKey) { Add-LabVerification -Check 'CA private key moved offline' -Status PENDING -Required $false -Detail $Paths.CaKey | Out-Null }
}
