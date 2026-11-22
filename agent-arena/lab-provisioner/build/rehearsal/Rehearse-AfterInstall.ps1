# Rehearses every hub > 2 step after "install" with the provisioner's own argument builders, the real
# Gateway 0.3.0 code (scratch release), the real heads and a throwaway ledger. Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$S = $PSScriptRoot
foreach ($m in 'Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Preflight') { Import-Module (Join-Path $root "engine\head-engine\modules\$m.psm1") -Force -Global -DisableNameChecking -WarningAction SilentlyContinue }
foreach ($m in 'Lab.Core', 'Lab.Firewall', 'Lab.Accounts', 'Lab.OllamaRuntime', 'Lab.Head', 'Lab.Gateway', 'Lab.Hub', 'Lab.Models', 'Lab.Arena', 'Lab.Apps', 'Lab.Inspect', 'Lab.Plan', 'Lab.Report') { Import-Module (Join-Path $root "engine\modules\$m.psm1") -Force -Global -DisableNameChecking -WarningAction SilentlyContinue }
$Config = Get-LabConfig -Path (Join-Path $root 'config\lab-network.json')
$py = (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python312\python.exe')
$manifest = Get-Content (Join-Path $env:USERPROFILE 'PycharmProjects\Inference Gateway\release\inference-gateway-0.3.0\deployment-manifest.json') -Raw | ConvertFrom-Json
$data = Join-Path $S 'data'; $ledger = Join-Path $data 'state\capacity-ledger.db'; $pubState = Join-Path $data 'observation-state\publisher-state.json'
$backups = Join-Path $data 'backups'; New-Item -ItemType Directory $backups -Force | Out-Null
$launch = { param([string]$Label, [string[]]$Arguments) Invoke-LabGatewayCommand -FileName $py -Arguments (Get-LabGatewayLauncherArgs -Manifest $manifest -Arguments $Arguments -InstallRoot $S) -Label $Label }
$results = New-Object Collections.ArrayList
function Show($name, $r) {
    $ok = [bool]$r.Ok
    $why = $(if ($r.Json -and $r.Json.error) { $r.Json.error } elseif (-not $ok -and $r.Stderr) { ($r.Stderr -split "`r?`n" | Select-Object -Last 2) -join ' / ' } else { '' })
    [void]$results.Add([pscustomobject]@{ Step = $name; Ok = $ok; Exit = $r.ExitCode; Note = $why })
    Write-Host ('{0,-5} {1,-28} exit {2} {3}' -f $(if ($ok) { 'OK' } else { 'FAIL' }), $name, $r.ExitCode, $why)
}

# g-inventory (the provisioner's probe and inventory, real heads)
$probes = @(Get-LabHeadProbe -Heads @($Config.known_heads) -Port ([int]$Config.ollama.port))
$inv = New-LabHeadInventory -Probes $probes -Config $Config -From $Config.hub -Computer 'DESKTOP-REDACT10'
$invFile = Join-Path $S 'verified-head-inventory.json'; Save-LabJson -InputObject (Protect-LabObject $inv) -Path $invFile
Write-Host ('inventory: ' + ((@($inv.heads) | ForEach-Object { $_.resource_id }) -join ', '))

# g-heads (the approved policy on F:, the identity check, the new default applications)
$approved = 'F:\reports\GATEWAY\gateway-policy-approved.json'
$idc = Test-LabGatewayPolicyIdentities -Policy (Get-Content $approved -Raw | ConvertFrom-Json); Write-Host "policy identities ok: $($idc.Ok) $($idc.Findings -join '; ')"
$review = Join-Path $data ('config\policy-review-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Show 'heads' (& $launch 'heads' @('heads', '--inventory', $invFile, '--policy', $approved, '--applications', 'capstone,capstone-research', '--output', $review))

# g-credentials (outbox ACL as the flow sets it; reader from the manifest)
$outbox = Join-Path $S 'outbox'; New-Item -ItemType Directory $outbox -Force | Out-Null
& icacls.exe $outbox /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ("*{0}:(OI)(CI)F" -f ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)) /Q | Out-Null
Show 'credentials list' (& $launch 'credentials-list' @('credentials', 'list', '--database', $ledger))
foreach ($app in 'capstone', 'capstone-research') { Show "credentials issue $app" (& $launch ('credentials-' + $app) @('credentials', 'issue', '--database', $ledger, '--policy', $review, '--app-id', $app, '--output', (Join-Path $outbox ($app + '.token')), '--reader', [string]$manifest.tokens.capstone.reader)) }

# g-bootstrap
Show 'bootstrap' (& $launch 'bootstrap' @('bootstrap', '--database', $ledger, '--policy', $review))

# g-qualify: one real round with the flow's arguments and the new 8192 default
$res = 'head-192-0-2-3'; $model = 'qwen3.5:4b'
$args2 = @('qualify', '--database', $ledger, '--resource', $res, '--model', $model, '--operator', 'LabOperator1', '--max-context', '8192', '--output', (Join-Path $backups ('qual-{0}-{1}.json' -f $res, ($model -replace '[^\w\.-]', '_'))))
Show 'qualify head-3 qwen3.5:4b' (& $launch ('qualify-' + $res) $args2)

# g-accept
$st = & $launch 'lifecycle-status' @('lifecycle', 'status', '--database', $ledger); Show 'lifecycle status' $st
$ps = & $launch 'publisher-status' @('publisher', 'status', '--database', $ledger, '--state', $pubState); Show 'publisher status (before)' $ps
Show 'accept-ledger' (& $launch 'accept-ledger' @('publisher', 'accept-ledger', '--database', $ledger, '--state', $pubState, '--operator', 'LabOperator1'))
$ps2 = & $launch 'publisher-status' @('publisher', 'status', '--database', $ledger, '--state', $pubState); Show 'publisher status (after)' $ps2
Write-Host ("  ledger_id {0}; accepted_ledger_id {1}" -f $st.Json.ledger_id, $ps2.Json.accepted_ledger_id)

# g-acl, PlanOnly (accounts that exist here stand in for svc-inference-gw and ArenaSvcUser)
$pwsh = (Get-Command pwsh).Source
$script = (Join-Path $env:USERPROFILE 'PycharmProjects\Inference Gateway\release\inference-gateway-0.3.0\scripts\Set-ObservationPublicationAcl.ps1')
$gwAcct = ConvertTo-LabResolvableAccount '.\Administrator'; $arena = @(ConvertTo-LabResolvableAccount '.\CodexSandboxOffline')
$base = "& '$script' -PublishDirectory '$data\observation' -StateDirectory '$data\observation-state' -LogDirectory '$data\logs' -LedgerDirectory '$data\state' -GatewayAccount '$gwAcct' -ArenaAccount " + (($arena | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ',')
Show 'publication ACL (PlanOnly)' (Invoke-LabGatewayCommand -FileName $pwsh -Arguments @('-NoProfile', '-NonInteractive', '-Command', ($base + ' -PlanOnly')) -Label 'publication-acl-plan')

# the runtime itself with this ledger (console mode, 127.0.0.1), then the operator status
$L = Join-Path $S 'releases\0.3.0\inference_gateway\service_launcher.py'; $C = Join-Path $data 'config\gateway-service.json'
$p = Start-Process -FilePath $py -ArgumentList @('-I', ('"{0}"' -f $L), 'console', '--config', ('"{0}"' -f $C)) -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $S 'console.out') -RedirectStandardError (Join-Path $S 'console.err')
Start-Sleep -Seconds 12
$obs = Join-Path $data 'observation\shared-inference-observation.json'
try { $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'https://127.0.0.1:18443/health' -ErrorAction Stop; Write-Host "health: $($h.StatusCode)" } catch { Write-Host "health over TLS (self-signed test cert, not trusted here): $($_.Exception.Message)" }
Write-Host ("observation published: {0}" -f (Test-Path $obs))
Show 'status' (& $launch 'status' @('status', '--config', $C, '--observation-file', $obs, '--publisher-state', $pubState))
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Write-Host '--- console output'; Get-Content (Join-Path $S 'console.out') -ErrorAction SilentlyContinue | Select-Object -First 5; Get-Content (Join-Path $S 'console.err') -ErrorAction SilentlyContinue | Select-Object -Last 5
$results | Format-Table -AutoSize | Out-String -Width 250
