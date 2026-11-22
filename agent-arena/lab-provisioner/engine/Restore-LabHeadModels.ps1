# Restore-LabHeadModels.ps1 - on the hub (RESTORE-HEAD-MODELS.cmd at the stick's root): puts a head's models back.
# Written after head 7 (2026-11-10): FIX-LAB-HEAD 1.6.1 deleted the models its Ollama serves from. This reads the head's
# last model list from its evidence on this stick (reports\<computer>\evidence-*\facts.json, the newest that lists
# models), leaves out what the head has now, and hands the rest to the hub's own Fleet Commit helper as one request:
# each model is downloaded once to the hub's library (hub Wi-Fi), copied to the head over the lab cable, checked by
# digest, and tested by the Gateway. The helper shows the list and does nothing until APPROVE is typed. Nothing is
# deleted anywhere.
param([string]$StickRoot = '', [int]$Head = 0)
$ErrorActionPreference = 'Stop'
if (-not $StickRoot) { $StickRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$cfg = Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'config\lab-network.json') | ConvertFrom-Json
$prefix = [string]$cfg.prefix; $hub = [string]$cfg.hub
$python = 'C:\Program Files\Python312\python.exe'; $helper = 'C:\Program Files\AgentArenaLab\GatewayModels\fleet_commit.py'
$requests = 'C:\ProgramData\AgentArenaLab\fleet\requests'; $published = 'C:\ProgramData\AgentArenaLab\fleet\published\fleet-state.json'
$out = Join-Path $StickRoot ('reports\{0}\restore-models-{1:yyyyMMdd-HHmmss}' -f $env:COMPUTERNAME, (Get-Date))
New-Item -ItemType Directory -Path $out -Force | Out-Null
Start-Transcript -LiteralPath (Join-Path $out 'RESTORE-LOG.txt') | Out-Null
function Stop-Here([string]$why) { Write-Host ''; Write-Host "  $why" -ForegroundColor Red; Write-Host '  Nothing was changed.'; Stop-Transcript | Out-Null; exit 1 }
try {
    Write-Host ''; Write-Host "  Agent Arena Lab: put a head's models back (run on the hub, $hub)" -ForegroundColor Green
    $mine = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object IPAddress)
    if ($mine -notcontains $hub) { Stop-Here "This is not the hub: run RESTORE-HEAD-MODELS.cmd on the hub ($hub), on the lab cable." }
    foreach ($p in $python, $helper) { if (-not (Test-Path -LiteralPath $p)) { Stop-Here "$p is missing: the Fleet Commit helper is installed by START-LAB-PROVISIONER > hub." } }
    if (-not $Head) { $Head = [int](Read-Host '  Which head''s models should be put back? Type its number (for head 7 type 7) and press Enter') }
    $address = "$prefix.$Head"
    $inv = @($cfg.head_inventory | Where-Object { $_.address -eq $address }) | Select-Object -First 1
    if (-not $inv) { Stop-Here "$address is not a head in the lab's head inventory." }
    $computer = [string]$inv.computer
    # The newest evidence for this head that lists models: what it served before it lost them.
    $facts = @(Get-ChildItem -LiteralPath (Join-Path $StickRoot "reports\$computer") -Directory -Filter 'evidence-*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending |
        ForEach-Object { $f = Join-Path $_.FullName 'facts.json'; if (Test-Path -LiteralPath $f) { [pscustomobject]@{ Folder = $_.Name; Facts = (Get-Content -Raw -LiteralPath $f | ConvertFrom-Json) } } } |
        Where-Object { $_.Facts.ollama -and @($_.Facts.ollama.models).Count })
    if (-not $facts.Count) { Stop-Here "No evidence on this stick lists head $Head's models (reports\$computer\evidence-*)." }
    $was = @($facts[0].Facts.ollama.models | ForEach-Object { [string]$_ } | Where-Object { $_ } | Select-Object -Unique)
    Write-Host ("  Head {0} ({1}, {2}) had {3} model(s) on {4}." -f $Head, $address, $computer, $was.Count, ($facts[0].Folder -replace '^evidence-', ''))
    $now = @()
    try { $now = @((Invoke-RestMethod -Uri "http://${address}:11434/api/tags" -TimeoutSec 15).models | ForEach-Object { [string]$_.name }) }
    catch { Stop-Here "Head $Head's Ollama does not answer the hub at http://${address}:11434 ($($_.Exception.Message)). Is head $Head on and on the lab cable?" }
    $missing = @($was | Where-Object { $now -notcontains $_ })
    Write-Host ("  It has {0} now; {1} to put back." -f $now.Count, $missing.Count)
    if (-not $missing.Count) { Write-Host '  Nothing to put back.' -ForegroundColor Green; Stop-Transcript | Out-Null; exit 0 }
    # The models the Gateway lets projects use go first, so the head is useful again as early as possible.
    $allowed = @()
    if (Test-Path -LiteralPath $published) { $allowed = @((Get-Content -Raw -LiteralPath $published | ConvertFrom-Json).applications | ForEach-Object { $_.models } | ForEach-Object { [string]$_ } | Select-Object -Unique) }
    $order = @($missing | Where-Object { $allowed -contains $_ }) + @($missing | Where-Object { $allowed -notcontains $_ })
    # Downloads come from the internet through the hub's Wi-Fi; the copy to the head goes over the lab cable.
    $net = $false; try { $c = New-Object Net.Sockets.TcpClient; $net = $c.ConnectAsync('registry.ollama.ai', 443).Wait(5000) -and $c.Connected; $c.Dispose() } catch { }
    if (-not $net) { Stop-Here "The hub cannot reach the internet (registry.ollama.ai). Connect the hub's Wi-Fi to the internet, keep the lab cable in, and run this again." }
    $id = -join ((1..16) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
    $request = [ordered]@{ schema = 'agent-arena.fleet-request/1'; created_by = "RESTORE-HEAD-MODELS (stick) for head $Head, as $env:USERNAME"
        changes = @($order | ForEach-Object { [ordered]@{ op = 'install'; address = $address; model = $_ } }) }
    New-Item -ItemType Directory -Path $requests -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $requests "$id.json"), ($request | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
    Copy-Item -LiteralPath (Join-Path $requests "$id.json") -Destination (Join-Path $out "request-$id.json")
    Write-Host ''
    Write-Host '  The hub''s Fleet Commit helper lists them next. Type APPROVE there to start. Downloads and copies take a long' -ForegroundColor Yellow
    Write-Host '  time (about 54 GB for head 7); keep the hub on its Wi-Fi and the lab cable, and this window open, until it says DONE.' -ForegroundColor Yellow
    & $python -I $helper --commit $id
    $code = $LASTEXITCODE
    Write-Host ''
    $after = @(); try { $after = @((Invoke-RestMethod -Uri "http://${address}:11434/api/tags" -TimeoutSec 15).models | ForEach-Object { [string]$_.name }) } catch { }
    $still = @($was | Where-Object { $after -notcontains $_ })
    Write-Host ("  Head {0} now has {1} of its {2} model(s){3}" -f $Head, ($was.Count - $still.Count), $was.Count, $(if ($still.Count) { '; still missing: ' + ($still -join ', ') } else { '.' })) $(if ($still.Count) { 'Yellow' } else { 'Green' })
    Stop-Transcript | Out-Null
    exit $code
} catch { Write-Host "  STOPPED: $($_.Exception.Message)" -ForegroundColor Red; try { Stop-Transcript | Out-Null } catch { }; exit 1 }
