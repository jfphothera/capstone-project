# 1-DISCOVER: read-only inventory of this computer. Changes nothing.
. (Join-Path $PSScriptRoot 'HeadCommon.ps1')
$ev = Join-Path $UsbRoot ("evidence\" + $env:COMPUTERNAME + "\" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-discover")
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript (Join-Path $ev 'transcript.txt') | Out-Null
Write-Banner 'READ-ONLY DISCOVERY (nothing on this computer is changed)'
$d = Get-Discovery 'discover'
Save-Discovery $d $ev | ForEach-Object { Write-Host "  $_" }
Write-Host ""
$c = @($d.identity.candidates)
if ($c.Count -eq 1) { Write-Host "  OK    This computer is $($c[0].display_id) ($($c[0].role), $($c[0].address); from the $($d.identity.source))." -ForegroundColor Green }
elseif ($c.Count -gt 1) { Write-Host "  NOTE  Several identities match: $(($c | ForEach-Object { $_.display_id }) -join ', '). 2-INSTALL-AND-VERIFY asks which one this computer is." -ForegroundColor Yellow }
else { Write-Host "  STOP  This computer is not a registered Arena display. $(@($d.identity.notes) -join ' ')" -ForegroundColor Red }
switch ($d.coordinator.verdict) {
    'ok'                  { Write-Host "  OK    The Arena Event Service at $($d.coordinator.event_service) answers and is the expected service ($($d.coordinator.health.version))." -ForegroundColor Green }
    'other_arena_service' { Write-Host "  STOP  An Arena Event Service answers at $($d.coordinator.event_service), but it is $($d.coordinator.health.service_id), not $($UsbProfile.service_id). This USB was prepared for another service." -ForegroundColor Red }
    'wrong_service'       { Write-Host "  STOP  Something answers at $($d.coordinator.event_service) but it is not the Arena Event Service." -ForegroundColor Red }
    'slow_or_no_answer'   { Write-Host "  STOP  The coordinator accepts connections at $($d.coordinator.event_service) but gave no answer in two tries (slow or busy, not the wrong service). Run 1-DISCOVER again; if it repeats, check the service window on the coordinator." -ForegroundColor Red }
    default               { Write-Host "  STOP  The coordinator's Arena Event Service ($($d.coordinator.event_service)) is not reachable from here (cable, address or service down)." -ForegroundColor Red }
}
if ($d.webview2) { Write-Host "  OK    WebView2 Runtime $($d.webview2)." -ForegroundColor Green } else { Write-Host "  STOP  WebView2 Runtime is missing; Agent Arena cannot display." -ForegroundColor Red }
if ([int]$d.dotnet_release -ge 528040) { Write-Host "  OK    .NET Framework 4.8 present." -ForegroundColor Green } else { Write-Host "  STOP  .NET Framework 4.8 missing (release $($d.dotnet_release))." -ForegroundColor Red }
if ($d.ollama.version) { Write-Host "  OK    Local Ollama $($d.ollama.version) with $($d.ollama.model_count) models (read-only check)." -ForegroundColor Green } else { Write-Host "  NOTE  Local Ollama did not answer on 127.0.0.1:11434 (normal on an orchestrator display)." -ForegroundColor Yellow }
$lv = $d.legacy_viewer
if ($lv.folder_exists -or $lv.startup_shortcut -or $lv.scheduled_task -or @($lv.processes).Count) {
    Write-Host "  NOTE  The original Phase 4.2 viewer left items here: folder=$($lv.folder_exists) ($(@($lv.files).Count) files) shortcut=$([bool]$lv.startup_shortcut) task=$([bool]$lv.scheduled_task) running=$(@($lv.processes).Count)." -ForegroundColor Yellow
    Write-Host "        0-CLEAN-SLATE removes them; 2-INSTALL-AND-VERIFY retires only its owned items." -ForegroundColor Yellow
} else { Write-Host "  OK    No trace of the original Phase 4.2 viewer for this Windows user." -ForegroundColor Green }
$installed = $d.agent_arena.current_version
if ($d.uninstall_verdict) { if ($d.uninstall_verdict.clean) { Write-Host "  OK    Agent Arena was uninstalled cleanly: $($d.uninstall_verdict.detail)." -ForegroundColor Green } else { Write-Host "  NOTE  Agent Arena uninstall left: $($d.uninstall_verdict.detail)." -ForegroundColor Yellow } }
elseif ($installed -and (Compare-Version $installed $UsbProfile.version) -gt 0) { Write-Host "  NOTE  Agent Arena $installed is installed, NEWER than this USB's $($UsbProfile.version). Do not install from this USB." -ForegroundColor Yellow }
elseif ($installed) { Write-Host "  NOTE  Agent Arena $installed is installed; 2-INSTALL-AND-VERIFY will upgrade or repair it to $($UsbProfile.version)." -ForegroundColor Yellow }
else { Write-Host "  OK    Agent Arena is not installed for this Windows user." -ForegroundColor Green }
Write-Host "`n  Evidence saved to $ev`n"
Stop-Transcript | Out-Null
