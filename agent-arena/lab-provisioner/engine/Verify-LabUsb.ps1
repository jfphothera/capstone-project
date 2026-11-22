<#
    VERIFY-USB: checks every file on this USB against SHA256SUMS.txt at its root. Read-only.
#>
$ErrorActionPreference = 'Stop'
$release = Split-Path -Parent $PSScriptRoot
$usb = Split-Path -Parent $release
$sums = Join-Path $usb 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $sums)) { Write-Host "No SHA256SUMS.txt at $usb" -ForegroundColor Red; exit 2 }
$expected = @{}
foreach ($line in Get-Content -LiteralPath $sums) { if ($line -match '^([0-9a-f]{64})\s+\*?(.+)$') { $expected[$Matches[2]] = $Matches[1] } }
$bad = @(); $missing = @(); $n = 0
foreach ($rel in ($expected.Keys | Sort-Object)) {
    $n++; $path = Join-Path $usb ($rel -replace '/', '\')
    if ($n % 200 -eq 0) { Write-Host ("  {0} of {1} files checked" -f $n, $expected.Count) }
    if (-not (Test-Path -LiteralPath $path)) { $missing += $rel; continue }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected[$rel]) { $bad += $rel }
}
$known = @{}; foreach ($k in $expected.Keys) { $known[$k.ToLowerInvariant()] = $true }
$extra = @(Get-ChildItem -LiteralPath $usb -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\System Volume Information\\|\\reports\\|\\results\\|\\evidence\\' -and $_.FullName -ne $sums } |
    ForEach-Object { $_.FullName.Substring($usb.TrimEnd('\').Length + 1) -replace '\\', '/' } | Where-Object { -not $known[$_.ToLowerInvariant()] })
$manifest = Get-Content -LiteralPath (Join-Path $release 'release-manifest.json') -Raw | ConvertFrom-Json
Write-Host ''
Write-Host ("  Release {0} ({1} files listed)" -f $manifest.release_id, $expected.Count)
if ($missing.Count) { Write-Host ('  MISSING: ' + ($missing -join ', ')) -ForegroundColor Red }
if ($bad.Count) { Write-Host ('  CHANGED: ' + ($bad -join ', ')) -ForegroundColor Red }
if ($extra.Count) { Write-Host ('  Not part of the release (left alone): ' + ($extra -join ', ')) -ForegroundColor Yellow }
if (-not $missing.Count -and -not $bad.Count) { Write-Host '  USB VERIFIED: every file matches its SHA-256.' -ForegroundColor Green; exit 0 }
Write-Host '  USB DAMAGED: copy the release again from the Agent Arena repository.' -ForegroundColor Red
exit 1
