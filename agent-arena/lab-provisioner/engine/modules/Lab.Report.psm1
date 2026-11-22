<#
    Lab.Report - Stage F. One structured (JSON) and one human-readable (TXT) report per run,
    sanitized, saved locally and optionally exported to the USB.

    Final status:
      verified            every step succeeded, every required check passed (including the LAN
                          checks this machine can make), nothing is pending
      partially verified  nothing failed, but something is pending: a check that needs another
                          machine or a reboot, an external party's credential or release
      blocked             the plan could not be carried out (refused address, unsuitable
                          hardware, an existing machine, the operator declined)
      failed              a step failed or a required check failed
    A local install is never reported as LAN integration: LAN checks have their own scope.
#>

function Get-LabFinalStatus {
    <#  Pure - unit tested.  #>
    param([AllowEmptyCollection()][array]$Steps = @(), [AllowEmptyCollection()][array]$Verification = @(), [AllowEmptyCollection()][array]$Registration = @(),
          [AllowEmptyCollection()][array]$Pending = @(), [AllowEmptyCollection()][array]$Blocked = @(), [bool]$Declined = $false, [bool]$Configured = $true)
    if (@($Blocked).Count -or $Declined) { return 'blocked' }
    if (@($Steps | Where-Object { $_.status -eq 'FAILED' }).Count) { return 'failed' }
    if (@($Verification | Where-Object { $_.status -eq 'FAIL' -and $_.required }).Count) { return 'failed' }
    if (@($Registration | Where-Object { $_.status -eq 'FAILED' }).Count) { return 'failed' }
    if (-not $Configured) { return 'inspection and plan only' }
    $open = @($Steps | Where-Object { $_.status -in 'PENDING', 'DECLINED', 'NOT_RUN' }).Count + @($Pending).Count +
            @($Verification | Where-Object { $_.status -eq 'PENDING' -and $_.required }).Count + @($Registration | Where-Object { $_.status -eq 'PENDING' }).Count
    if ($open -gt 0) { return 'partially verified' }
    if (@($Verification | Where-Object { $_.status -eq 'PASS' }).Count -eq 0) { return 'partially verified' }
    return 'verified'
}

function New-LabReport {
    param([Parameter(Mandatory)]$Run, [bool]$Configured = $true, [bool]$Declined = $false, $Extra = $null)
    $i = $Run.Inspection; $p = $Run.Plan
    $blocked = @(); if ($p) { $blocked = @($p.blocked) }
    $approvals = @(); if ($Run.PSObject.Properties.Name -contains 'Approvals') { $approvals = @($Run.Approvals) }
    # An approved plan that changes the machine, after which no step ran at all, is a provisioner defect
    # (1.3.0 on DESKTOP-REDACT10: the hub choice was lost and the run went straight to Verify). Say so.
    if ($p -and $Configured -and -not $Declined -and -not @($Run.Steps).Count) {
        $work = @(@($p.steps) | Where-Object { $_.approval -and $_.state -eq 'planned' })
        if ($work.Count -and @($approvals | Where-Object { $_.result -eq 'approved' }).Count) { [void]$Run.Warnings.Add(('the plan was approved, but none of its {0} step(s) that change this machine ran: {1}' -f $work.Count, (($work | ForEach-Object { $_.id }) -join ', '))) }
    }
    $status = Get-LabFinalStatus -Steps @($Run.Steps) -Verification @($Run.Verification) -Registration @($Run.Registration) -Pending @($Run.Pending) -Blocked $blocked -Declined $Declined -Configured $Configured
    $software = [ordered]@{}
    if ($i) {
        if ($i.ollama) { $software.ollama = $(if ($i.ollama.Executable) { $i.ollama.Version } else { 'not installed' }) }
        if ($i.arena) { $software.arena_viewer = $(if ($i.arena.Viewer.Installed) { $i.arena.Viewer.Version } else { 'not installed' }); if ($i.arena.Hub) { $software.arena_hub = $(if ($i.arena.Hub.ProgramInstalled) { 'installed (service ' + $i.arena.Hub.LocalHealth.Version + ')' } else { 'not installed' }) }; if ($i.arena.Ticketing.ProgramInstalled) { $software.ticketing_adapter = 'installed' } }
        if ($i.capstone -and $i.capstone.Installed) { $software.capstone = $i.capstone.Version }
    }
    $report = [ordered]@{
        schema = 'agent-arena.lab-report/1'; provisioner = ('Agent Arena Lab Provisioner ' + $Run.Version); run = $Run.Id; role = $Run.Role
        computer = $env:COMPUTERNAME; started = $Run.StartedAt; finished = (Get-Date).ToString('o'); final_status = $status
        identity = $(if ($i) { [ordered]@{ detected = $i.detected; os = $i.os; user = $i.user; elevated = $i.elevated } } else { $null })
        inventory = @(if ($i) { $i.facts })   # 1.6.22: a single fact stays an array ($(...) unrolled it)
        plan = $(if ($p) { [ordered]@{ address = $p.address; address_verdict = $p.address_verdict; conflict = $p.conflict; hardware = $p.hardware; profile = $p.profile; models = $p.models
                                       client_mode = $p.client_mode; clients = $p.clients; denied = $p.denied; steps = $p.steps; external = $p.external; warnings = $p.warnings; blocked = $p.blocked; preserves = $p.preserves
                                       hub_mode = $(if ($p.PSObject.Properties.Name -contains 'hub_mode') { $p.hub_mode } else { $null }) } } else { $null })
        approvals = $approvals
        actual = @($Run.Steps); software = $software; models = $Run.Models
        registration = @($Run.Registration); verification = @($Run.Verification)
        security = $(if ($i -and $i.firewall) { $i.firewall.Exposure } else { $null })
        changes = @($Run.Changes); rollback = [ordered]@{ change_record = $Run.ChangesFile; backups = $Run.Backups; how = 'START-LAB-PROVISIONER.cmd > Existing machine > Roll back a previous run' }
        warnings = @($Run.Warnings); pending = @($Run.Pending); stop_reason = $Run.StopReason; extra = $Extra
        scope_note = 'Checks marked scope=lan used real connections over the LAN; scope=local are on this machine only; scope=external need another project or machine.'
    }
    $safe = Protect-LabObject $report
    $base = ('{0}-{1}' -f $env:COMPUTERNAME, $Run.Id)
    $json = Join-Path $Run.ReportsDir ($base + '.json'); $txt = Join-Path $Run.ReportsDir ($base + '.txt')
    Save-LabJson -InputObject $safe -Path $json -Depth 14
    Copy-Item -LiteralPath $json -Destination (Join-Path $Run.Folder 'report.json') -Force
    $lines = Format-LabReportText -Report $safe
    [IO.File]::WriteAllText($txt, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
    Copy-Item -LiteralPath $txt -Destination (Join-Path $Run.Folder 'report.txt') -Force
    return [pscustomobject]@{ Status = $status; Json = $json; Text = $txt; Report = $safe }
}

function Format-LabReportText {
    param([Parameter(Mandatory)]$Report)
    $l = New-Object System.Collections.Generic.List[string]
    $l.Add('AGENT ARENA LAB - INSTALLATION REPORT'); $l.Add(('=' * 60))
    $l.Add(('Final status : {0}' -f $Report.final_status.ToUpperInvariant()))
    $l.Add(('Role         : {0}' -f $Report.role)); $l.Add(('Computer     : {0}' -f $Report.computer)); $l.Add(('Run          : {0}' -f $Report.run))
    $l.Add(('Provisioner  : {0}' -f $Report.provisioner)); $l.Add(('Started      : {0}' -f $Report.started)); $l.Add(('Finished     : {0}' -f $Report.finished))
    if ($Report.identity -and $Report.identity.detected) { $l.Add(('Appears to be: {0} ({1})' -f $Report.identity.detected.Role, $Report.identity.detected.Reason)) }
    $l.Add(''); $l.Add('INVENTORY (observed / inferred / missing / inaccessible)'); $l.Add(('-' * 60))
    $area = ''
    foreach ($f in @($Report.inventory)) { if ($f.area -ne $area) { $area = $f.area; $l.Add(('[{0}]' -f $area)) }; $l.Add(('  {0,-12} {1}: {2}' -f $f.status, $f.name, $f.value)) }
    if ($Report.plan) {
        $l.Add(''); $l.Add('PLAN'); $l.Add(('-' * 60))
        if ($Report.plan.address) { $l.Add(('Address: {0}' -f $Report.plan.address)) }
        if ($Report.plan.PSObject.Properties.Name -contains 'hub_mode' -and $Report.plan.hub_mode) { $l.Add(('Hub choice: {0}' -f $Report.plan.hub_mode)) }
        foreach ($s in @($Report.plan.steps)) { $l.Add(('  [{0}] {1}{2}' -f $s.kind, $s.title, $(if ($s.state -eq 'pending') { ' (PENDING)' } else { '' }))) }
        foreach ($b in @($Report.plan.blocked)) { $l.Add(('  BLOCKED: {0}' -f $b)) }
        foreach ($e in @($Report.plan.external)) { $l.Add(('  needs: {0}' -f $e)) }
    }
    if ($Report.PSObject.Properties.Name -contains 'approvals' -and @($Report.approvals).Count) {
        $l.Add(''); $l.Add('APPROVALS (operator answers, in order)'); $l.Add(('-' * 60))
        foreach ($a in @($Report.approvals)) { $l.Add(('  {0,-9} [{1}, {2}] {3}' -f $a.result, $a.kind, $a.word, $a.prompt)) }
    }
    $l.Add(''); $l.Add('WHAT WAS DONE (planned versus actual)'); $l.Add(('-' * 60))
    foreach ($s in @($Report.actual)) { $l.Add(('  {0,-9} {1}{2}' -f $s.status, $s.title, $(if ($s.detail) { ' - ' + $s.detail } else { '' }))) }
    if (-not @($Report.actual).Count) { $l.Add($(if (@($Report.changes).Count) { '  no step recorded' } else { '  no step ran; nothing was changed' })) }
    $l.Add(('  changes recorded: {0}' -f @($Report.changes).Count))
    if ($Report.software) { $l.Add(''); $l.Add('SOFTWARE'); $l.Add(('-' * 60)); foreach ($k in $Report.software.PSObject.Properties) { $l.Add(('  {0}: {1}' -f $k.Name, $k.Value)) } }
    if ($Report.models -and @($Report.models.Records).Count) {
        $l.Add(''); $l.Add('MODELS (selected / attempted / completed / installed / digest / inference / Gateway)'); $l.Add(('-' * 60))
        foreach ($m in @($Report.models.Records)) { $l.Add(('  {0,-26} {1,-22} attempted={2} installed={3} digest={4} inference={5} gateway=not claimed{6}' -f $m.tag, $m.final, $m.download_attempted, $m.installed_locally, $m.digest, $m.verified_by_inference, $(if ($m.failure_category) { ' [' + $m.failure_category + ']' } else { '' }))) }
    }
    $l.Add(''); $l.Add('REGISTRATION'); $l.Add(('-' * 60))
    foreach ($r in @($Report.registration)) { $l.Add(('  {0,-18} {1}: {2}' -f $r.status, $r.item, $r.detail)) }
    if (-not @($Report.registration).Count) { $l.Add('  none attempted') }
    $l.Add(''); $l.Add('VERIFICATION'); $l.Add(('-' * 60))
    foreach ($v in @($Report.verification)) { $l.Add(('  {0,-7} [{1}] {2}: {3}' -f $v.status, $v.scope, $v.check, $v.detail)) }
    if ($Report.security -and $Report.security.Sources) {
        $l.Add(''); $l.Add(('SECURITY: Ollama port exposure ({0})' -f $Report.security.Analysis)); $l.Add(('-' * 60))
        foreach ($s in @($Report.security.Sources)) { $l.Add(('  from {0,-13} {1}: {2}' -f $s.Source, $s.Verdict, $s.Reason)) }
    }
    if (@($Report.changes).Count) { $l.Add(''); $l.Add('CHANGES MADE (each can be undone)'); $l.Add(('-' * 60)); foreach ($c in @($Report.changes)) { $l.Add(('  {0} {1}' -f $c.kind, $c.description)) } }
    $l.Add(''); $l.Add(('Rollback: {0}' -f $Report.rollback.how)); $l.Add(('  record  {0}' -f $Report.rollback.change_record)); $l.Add(('  backups {0}' -f $Report.rollback.backups))
    if (@($Report.warnings).Count) { $l.Add(''); $l.Add('WARNINGS'); $l.Add(('-' * 60)); foreach ($w in @($Report.warnings)) { $l.Add('  ' + $w) } }
    if (@($Report.pending).Count) { $l.Add(''); $l.Add('PENDING (external dependencies and later checks)'); $l.Add(('-' * 60)); foreach ($p in @($Report.pending)) { $l.Add(('  [{0}] {1}' -f $p.area, $p.detail)) } }
    if ($Report.stop_reason) { $l.Add(''); $l.Add('STOPPED: ' + $Report.stop_reason) }
    $l.Add(''); $l.Add($Report.scope_note)
    $l.Add('Credentials, codes and tokens are redacted; none are written to this report.')
    return $l
}

function Export-LabReport {
    <#  Copies the sanitized report to <USB>\reports\<computer>\ .  #>
    param([Parameter(Mandatory)]$Result, [Parameter(Mandatory)][string]$UsbRoot)
    $dest = Join-Path $UsbRoot ('reports\' + $env:COMPUTERNAME)
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Copy-Item -LiteralPath $Result.Json, $Result.Text -Destination $dest -Force
    return $dest
}

Export-ModuleMember -Function @('Get-LabFinalStatus', 'New-LabReport', 'Format-LabReportText', 'Export-LabReport')
