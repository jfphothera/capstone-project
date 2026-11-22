<#
  Prepares an Agent Arena install USB on the coordinator (field defect D1: one versioned
  toolset per USB, nothing else at the root).

    New-ArenaUsb.ps1 -Drive D: [-Version 2.1.0] [-Enroll head-192-0-2-3,head-192-0-2-6] [-Hours 72] [-Wipe]
    New-ArenaUsb.ps1 -Drive D: -Version 2.1.2 -ServiceId aes-...   while the hub is not answering yet

  - Copies AGENT-ARENA-<version>\ with the launchers, tools, the built package from dist\ and a
    usb-profile.json naming the version, the event service and its service_id. No head is named.
  - -Enroll issues one-time codes (enroll\<display>.code.txt). Issuing a code replaces that
    display's earlier unused code. Codes are secrets: they exist only on the USB.
  - -Wipe first backs up everything at the USB root to %LOCALAPPDATA%\AgentArenaService\usb-backups\
    and then removes it. Without -Wipe the script refuses a USB that holds anything else.
    The admin hub and Ticketing adapter folders (AGENT-ARENA-HUB, AGENT-ARENA-TICKETING) and the
    existing-heads fix (FIX-EXISTING-HEADS) may sit beside it and are never wiped: still one head
    toolset per USB.
  - -ServiceId names the service the heads will enroll with when it is not answering yet (the
    hub before it is installed; with moved data it keeps the current service's ID). If it does
    answer, its ID must match.
#>
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z]:\\?$')][string]$Drive,
      [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = '2.1.0',
      [string[]]$Enroll = @(), [double]$Hours = 72, [switch]$Wipe, [switch]$Force, [string]$ServiceId = '')
$ErrorActionPreference = 'Stop'
# From cmd.exe (PREPARE-USB.cmd) "-Enroll a,b,c" arrives as one string.
$Enroll = @($Enroll | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$repo =Resolve-Path (Join-Path $PSScriptRoot '..\..')
$usbSource = $PSScriptRoot
$root = $Drive.TrimEnd('\') + '\'
$py = Join-Path $repo '.venv\Scripts\python.exe'; if (-not (Test-Path $py)) { $py = 'python' }
$serviceData = Join-Path $env:LOCALAPPDATA 'AgentArenaService'
$cfg = Join-Path $serviceData 'aes.config.json'
$pkgName = "Agent-Arena-$Version"
$pkg = Join-Path $repo "dist\$pkgName"

$volume = Get-Volume -DriveLetter $Drive.Substring(0, 1) -ErrorAction Stop
if ($volume.DriveType -ne 'Removable') { throw "$root is not a removable drive ($($volume.DriveType)); refusing" }
if (-not (Test-Path (Join-Path $pkg 'app\AgentArena.exe'))) { throw "Build the package first: desktop\build\Build-AgentArena.ps1 -Version $Version" }

# The package must be intact and really be this version.
foreach ($line in Get-Content (Join-Path $pkg 'SHA256SUMS.txt')) {
    if ($line -match '^([0-9A-F]{64})\s+(.+)$' -and (Get-FileHash -LiteralPath (Join-Path $pkg $Matches[2])).Hash -ne $Matches[1]) { throw "dist package damaged: $($Matches[2])" }
}
$manifestVersion = (Get-Content (Join-Path $pkg 'app\app-manifest.json') -Raw | ConvertFrom-Json).version
if ($manifestVersion -ne $Version) { throw "dist\$pkgName is version $manifestVersion" }

# The event service the package points at must be the one whose database issues the codes.
$deploy = Get-Content (Join-Path $pkg 'app\arena-deployment.json') -Raw | ConvertFrom-Json
$endpoints = @($deploy.event_service_endpoints)
try {
    $req = [Net.HttpWebRequest][Net.WebRequest]::Create($endpoints[0].TrimEnd('/') + '/v2/health'); $req.Proxy = $null; $req.Timeout = 10000
    $resp = $req.GetResponse(); $health = (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd() | ConvertFrom-Json; $resp.Close()
} catch {
    if (-not $ServiceId) { throw "The event service at $($endpoints[0]) does not answer ($($_.Exception.Message)). If it is the hub before its install, give -ServiceId." }
    $health = [pscustomobject]@{ service_id = $ServiceId; version = '(not answering yet)' }
    Write-Host "The event service at $($endpoints[0]) does not answer yet; the heads will expect service $ServiceId." -ForegroundColor Yellow
}
if ($ServiceId -and $health.service_id -ne $ServiceId) { throw "The service at $($endpoints[0]) is $($health.service_id), not $ServiceId" }
if ($Enroll.Count) {   # codes come from this computer's database, so it must be the service the package points at
    $localId = & $py -E -s -c "import json,sys; from arena_service.store import Store; print(Store(json.load(open(sys.argv[1]))['db_path']).meta('service_id'))" $cfg
    if ($health.service_id -ne $localId) { throw "The service at $($endpoints[0]) is $($health.service_id) but this computer's database is $localId; issue codes in the hub's admin console instead" }
}
Write-Host "Event service $($endpoints[0]): $($health.service_id), version $($health.version)"

# Nothing but this toolset at the root.
$companions = 'System Volume Information', 'AGENT-ARENA-HUB', 'AGENT-ARENA-TICKETING', 'FIX-EXISTING-HEADS', 'START-HERE.txt'
$existing = @(Get-ChildItem $root -Force | Where-Object { $companions -notcontains $_.Name })
if ($existing.Count) {
    if (-not $Wipe) { throw "$root is not empty ($(($existing | ForEach-Object Name) -join ', ')). Re-run with -Wipe to back it up and clear it." }
    Write-Host "`nThe USB root holds:" -ForegroundColor Yellow; $existing | ForEach-Object { Write-Host "  $($_.Name)" }
    if (-not $Force -and (Read-Host "Type WIPE to back these up to $serviceData\usb-backups and remove them") -ne 'WIPE') { throw 'Stopped; the USB was not changed' }
    $backup = Join-Path $serviceData ("usb-backups\" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-" + $volume.FileSystemLabel.Replace(' ', '_'))
    New-Item -ItemType Directory -Force $backup | Out-Null
    foreach ($e in $existing) { Copy-Item -LiteralPath $e.FullName -Destination $backup -Recurse -Force }
    $copied = @(Get-ChildItem $backup -Recurse -File -Force).Count; $source = @($existing | ForEach-Object { if ($_.PSIsContainer) { Get-ChildItem $_.FullName -Recurse -File -Force } else { $_ } }).Count
    if ($copied -ne $source) { throw "Backup incomplete ($copied of $source files); the USB was not cleared" }
    foreach ($e in $existing) { Remove-Item -LiteralPath $e.FullName -Recurse -Force }
    Write-Host "Backed up $copied file(s) to $backup and cleared the USB root."
}

$target = Join-Path $root "AGENT-ARENA-$Version"
New-Item -ItemType Directory -Force (Join-Path $target 'tools'), (Join-Path $target 'package'), (Join-Path $target 'enroll'), (Join-Path $target 'evidence') | Out-Null
Copy-Item (Join-Path $usbSource 'tools\*.ps1') (Join-Path $target 'tools')
Copy-Item (Join-Path $usbSource '*.cmd') $target
$readme = (Get-Content (Join-Path $usbSource 'README.txt') -Raw).Replace('__VERSION__', $Version).Replace('__SERVICE__', $health.service_id).Replace('__ENDPOINTS__', ($endpoints -join ', '))
Set-Content (Join-Path $target 'README.txt') $readme -Encoding UTF8
Get-ChildItem $target -File | Where-Object { $_.Extension -in '.cmd', '.txt' } | ForEach-Object {   # cmd.exe needs CRLF
    [IO.File]::WriteAllText($_.FullName, ([IO.File]::ReadAllText($_.FullName) -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false))
}
[ordered]@{ toolset_version = $Version; package = $pkgName; version = $Version; service_id = $health.service_id
            event_service_endpoints = $endpoints; display_networks = @($deploy.display_networks)
            prepared_at = (Get-Date).ToString('s'); prepared_on = $env:COMPUTERNAME } |
    ConvertTo-Json | Set-Content (Join-Path $target 'tools\usb-profile.json') -Encoding UTF8
Copy-Item $pkg (Join-Path $target 'package') -Recurse

foreach ($display in $Enroll) {
    $out = Join-Path $target "enroll\$display.code.txt"
    & $py -E -s -m arena_service.admin --config $cfg enroll $display --hours $Hours --out $out | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "could not issue a code for $display" }
    Write-Host "Enrollment code for $display written (valid $Hours h; replaces any earlier unused code)."
}

$bad = @(Get-Content (Join-Path $target "package\$pkgName\SHA256SUMS.txt") | Where-Object { $_ -match '^([0-9A-F]{64})\s+(.+)$' -and (Get-FileHash -LiteralPath (Join-Path $target "package\$pkgName\$($Matches[2])")).Hash -ne $Matches[1] })
if ($bad.Count) { throw "USB copy failed verification: $($bad.Count) file(s)" }
$rootNow = @(Get-ChildItem $root -Force | Where-Object { $_.Name -ne 'System Volume Information' } | ForEach-Object Name)
Write-Host "`nUSB ready: $target (root holds only: $($rootNow -join ', ')); package verified." -ForegroundColor Green
