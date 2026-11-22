<#
    Copies a built lab-provisioner release to a USB drive, only after the destination is
    positively identified, and then proves the copy against SHA256SUMS.txt.

        Publish-LabUsb.ps1 -Drive F: -Release dist\lab-provisioner-1.0.0 -ExpectSerial REDACTED1 [-CheckOnly]
        Publish-LabUsb.ps1 -Drive D: -Release dist\lab-provisioner-1.6.11 -ExpectSerial 0000000000000000 -ExpectModel 'INTEL SS DSC2BW120H6' -RefuseDrives E: -Update

    Refuses (and writes nothing) unless ALL hold:
      * the letter is not the system drive and not one of -RefuseDrives (the E: and D: sources);
      * the volume is removable, on a USB disk that is neither the system nor the boot disk;
        1.6.11: or a fixed volume on a USB disk (an SSD in a USB enclosure) whose model is -ExpectModel;
      * the disk serial is not one of -RefuseSerials (the preserved original sticks and the CA key stick),
        whatever letter they have today;
      * that disk is not the disk of the system drive or of any refused drive;
      * the disk serial number is the one given with -ExpectSerial (when given); a serial of only zeros
        (USB enclosures that hide it) identifies nothing, so it also needs -ExpectModel to match;
      * the root holds nothing but "System Volume Information" (with -Update: only an earlier
        copy of this same product, which is then replaced);
      * there is room, and no file exceeds the FAT32 4 GB limit.
    It never formats, repartitions or erases anything else.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z]:\\?$')][string]$Drive,
    [Parameter(Mandatory)][string]$Release,
    [string]$ExpectSerial = '',
    [string]$ExpectModel = '',
    [string[]]$RefuseDrives = @('D:', 'E:'),
    # The two preserved original sticks and the CA key stick, by serial: refused whatever their letter is.
    [string[]]$RefuseSerials = @('REDACTED2', 'REDACTED3', 'REDACTED4'),
    [switch]$Update,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
# From cmd.exe or "powershell -File", "-RefuseDrives D:,E:" arrives as one string.
$RefuseDrives = @($RefuseDrives | ForEach-Object { $_ -split '[,;\s]+' } | Where-Object { $_ -match '^[A-Za-z]:?\\?$' })
$letter = $Drive.Substring(0, 1).ToUpperInvariant()
$root = "${letter}:\"
$release = (Resolve-Path $Release).Path
$checks = New-Object System.Collections.ArrayList
function Check([string]$Name, [bool]$Ok, [string]$Detail) { [void]$checks.Add([pscustomobject]@{ Check = $Name; Ok = $Ok; Detail = $Detail }); Write-Host ('  {0}  {1}: {2}' -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Name, $Detail) -ForegroundColor $(if ($Ok) { 'Green' } else { 'Red' }) }

Write-Host "`n  Destination checks for $root (nothing is written until all pass)`n" -ForegroundColor Cyan
$system = $env:SystemDrive.Substring(0, 1).ToUpperInvariant()
$refused = @($RefuseDrives | ForEach-Object { $_.Substring(0, 1).ToUpperInvariant() })
Check 'not the system drive' ($letter -ne $system) "system drive is ${system}:"
Check 'not a source drive' ($refused -notcontains $letter) ("refused: " + (($refused | ForEach-Object { "${_}:" }) -join ', '))

$vol = Get-Volume -DriveLetter $letter -ErrorAction SilentlyContinue
Check 'volume present' ([bool]$vol) $(if ($vol) { "label '$($vol.FileSystemLabel)', $($vol.FileSystem), $([math]::Round($vol.Size / 1GB, 2)) GB, $([math]::Round($vol.SizeRemaining / 1GB, 2)) GB free, health $($vol.HealthStatus)" } else { 'no volume' })
if (-not $vol) { exit 2 }
$part = Get-Partition -DriveLetter $letter -ErrorAction SilentlyContinue
$disk = $null; if ($part) { $disk = Get-Disk -Number $part.DiskNumber -ErrorAction SilentlyContinue }
$model = $(if ($disk) { ([string]$disk.FriendlyName).Trim() } else { '' })
$modelOk = [bool]($ExpectModel -and $model -eq $ExpectModel.Trim())
# 1.6.11: an SSD in a USB enclosure is a fixed volume; it is accepted only on the USB bus and when its model is named.
$usbFixed = ($vol.DriveType -eq 'Fixed' -and $disk -and $disk.BusType -eq 'USB' -and $modelOk)
Check 'removable volume, or a USB disk of the expected model' ($vol.DriveType -eq 'Removable' -or $usbFixed) ('{0}{1}' -f [string]$vol.DriveType, $(if ($vol.DriveType -eq 'Fixed') { "; model '$model', expected '$ExpectModel'" } else { '' }))
Check 'USB disk' ($disk -and $disk.BusType -eq 'USB') $(if ($disk) { "disk $($disk.Number) '$($disk.FriendlyName)' serial $($disk.SerialNumber) bus $($disk.BusType) $([math]::Round($disk.Size / 1GB, 2)) GB" } else { 'disk not found' })
if (-not $disk) { exit 2 }
Check 'not a system or boot disk' (-not $disk.IsSystem -and -not $disk.IsBoot) "IsSystem=$($disk.IsSystem) IsBoot=$($disk.IsBoot)"
$otherDisks = @()
foreach ($l in @($refused + $system)) { $p = Get-Partition -DriveLetter $l -ErrorAction SilentlyContinue; if ($p) { $otherDisks += [pscustomobject]@{ Letter = $l; Disk = $p.DiskNumber; Serial = (Get-Disk -Number $p.DiskNumber).SerialNumber } } }
Check 'a different physical disk from the system and source drives' (@($otherDisks | Where-Object { $_.Disk -eq $disk.Number }).Count -eq 0) ((@($otherDisks | ForEach-Object { '{0}: disk {1} ({2})' -f $_.Letter, $_.Disk, ([string]$_.Serial).Trim() })) -join '; ')
$serial = ([string]$disk.SerialNumber).Trim()
Check 'not a protected stick' ($RefuseSerials -notcontains $serial) ("serial '$serial'; refused: " + ($RefuseSerials -join ', '))
$blankSerial = ($serial -eq '' -or $serial -match '^0+$')
if ($ExpectSerial -and $blankSerial) { Check 'expected serial number' ($serial -eq $ExpectSerial.Trim() -and $modelOk) ("found '$serial' (the enclosure hides the real one), expected '$ExpectSerial'; model '$model', expected '$ExpectModel'") }
elseif ($ExpectSerial) { Check 'expected serial number' ($serial -eq $ExpectSerial.Trim()) ("found '$serial', expected '$ExpectSerial'") }
else { Check 'expected serial number' $false 'give -ExpectSerial so the destination is identified unambiguously' }

$manifest = Get-Content -LiteralPath (Get-ChildItem -LiteralPath $release -Directory | Where-Object Name -like 'AGENT-ARENA-LAB-*' | ForEach-Object { Join-Path $_.FullName 'release-manifest.json' } | Select-Object -First 1) -Raw | ConvertFrom-Json
# Field data the publisher keeps on -Update: reports from the machines, the Gateway's public CA
# (Administration hub > 2 writes gateway-ca; the Capstone role reads it), and the QMS/ERP tokens that heads 3 and 4
# leave in handoff for the Capstone laptop (1.5.0; never read or copied here).
$kept = @('reports', 'gateway-ca', 'handoff')
$ours = @((Get-ChildItem -LiteralPath $release -Force | ForEach-Object Name) + $kept)
# 1.6.11: NTFS drives also carry the hidden recycle bin; both Windows folders are never ours and never touched.
$present = @(Get-ChildItem -LiteralPath $root -Force | Where-Object { @('System Volume Information', '$RECYCLE.BIN') -notcontains $_.Name })
$unexpected = @($present | Where-Object { $ours -notcontains $_.Name -and $_.Name -notlike 'AGENT-ARENA-LAB-*' })
if ($present.Count -eq 0) { Check 'destination is empty' $true 'only System Volume Information' }
elseif ($Update -and $unexpected.Count -eq 0) {
    $old = @($present | Where-Object Name -like 'AGENT-ARENA-LAB-*' | ForEach-Object { Join-Path $_.FullName 'release-manifest.json' } | Where-Object { Test-Path $_ } | ForEach-Object { Get-Content $_ -Raw | ConvertFrom-Json })
    Check 'destination holds only an earlier copy of this product' (@($old | Where-Object product -eq $manifest.product).Count -ge 1) ('present: ' + (($present | ForEach-Object Name) -join ', '))
} else { Check 'destination is empty' $false ('it holds: ' + (($present | ForEach-Object Name) -join ', ') + '. Stopped without writing.') }

$files = @(Get-ChildItem -LiteralPath $release -Recurse -File -Force)
$bytes = ($files | Measure-Object Length -Sum).Sum
$reclaim = 0; if ($Update) { $reclaim = (@($present | Where-Object { $kept -notcontains $_.Name } | ForEach-Object { if ($_.PSIsContainer) { Get-ChildItem $_.FullName -Recurse -File -Force } else { $_ } }) | Measure-Object Length -Sum).Sum }
Check 'enough free space' (($vol.SizeRemaining + $reclaim) -gt ($bytes * 1.05)) ('{0:N0} MB needed, {1:N0} MB free' -f ($bytes / 1MB), (($vol.SizeRemaining + $reclaim) / 1MB))
$big = @($files | Where-Object { $_.Length -ge 4GB })
Check 'no file over the FAT32 4 GB limit' ($big.Count -eq 0 -or $vol.FileSystem -ne 'FAT32') ($vol.FileSystem)

$failed = @($checks | Where-Object { -not $_.Ok })
$log = [ordered]@{ at = (Get-Date).ToString('o'); computer = $env:COMPUTERNAME; drive = $root; release = $manifest.release_id; disk = [ordered]@{ number = $disk.Number; serial = ([string]$disk.SerialNumber).Trim(); model = $disk.FriendlyName; bus = [string]$disk.BusType; size = $disk.Size }
                   volume = [ordered]@{ label = $vol.FileSystemLabel; filesystem = $vol.FileSystem; size = $vol.Size; free = $vol.SizeRemaining; health = [string]$vol.HealthStatus }; checks = @($checks); copied = $false; verified = $false }
$logPath = Join-Path $release ('..\publish-' + $letter + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
if ($failed.Count) { Write-Host "`n  STOPPED: $($failed.Count) check(s) failed. Nothing was written to $root." -ForegroundColor Red; $log | ConvertTo-Json -Depth 6 | Set-Content $logPath -Encoding UTF8; exit 1 }
if ($CheckOnly) { Write-Host "`n  All checks passed. -CheckOnly: nothing was written." -ForegroundColor Green; $log | ConvertTo-Json -Depth 6 | Set-Content $logPath -Encoding UTF8; exit 0 }

if ($Update) { foreach ($p in $present | Where-Object { $kept -notcontains $_.Name }) { Remove-Item -LiteralPath $p.FullName -Recurse -Force } }
Write-Host "`n  Copying $($files.Count) files ($([math]::Round($bytes / 1MB)) MB) to $root ..." -ForegroundColor Cyan
foreach ($item in Get-ChildItem -LiteralPath $release -Force) { Copy-Item -LiteralPath $item.FullName -Destination $root -Recurse -Force }
$log.copied = $true
foreach ($empty in Get-ChildItem -LiteralPath $release -Recurse -Directory | Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force) }) {
    $target = Join-Path $root $empty.FullName.Substring($release.Length + 1); if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
}

Write-Host '  Verifying every file on the USB against SHA256SUMS.txt ...' -ForegroundColor Cyan
$bad = @(); $n = 0
foreach ($line in Get-Content -LiteralPath (Join-Path $root 'SHA256SUMS.txt')) {
    if ($line -notmatch '^([0-9a-f]{64})\s+(.+)$') { continue }
    $n++; $f = Join-Path $root ($Matches[2] -replace '/', '\')
    if (-not (Test-Path -LiteralPath $f) -or (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Matches[1]) { $bad += $Matches[2] }
}
$innerManifest = Join-Path $root ((Get-ChildItem $root -Directory | Where-Object Name -like 'AGENT-ARENA-LAB-*' | Select-Object -First 1).Name + '\release-manifest.json')
$m2 = Get-Content $innerManifest -Raw | ConvertFrom-Json
$manifestBad = @($m2.files.PSObject.Properties | Where-Object { $p = Join-Path (Split-Path $innerManifest) ($_.Name -replace '/', '\'); -not (Test-Path -LiteralPath $p) -or (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -ne $_.Value } | ForEach-Object Name)
$log.verified = ($bad.Count -eq 0 -and $manifestBad.Count -eq 0); $log.files_checked = $n; $log.mismatches = @($bad + $manifestBad)
$log | ConvertTo-Json -Depth 6 | Set-Content $logPath -Encoding UTF8
if (-not $log.verified) { Write-Host ("  VERIFY FAILED: {0}" -f (($bad + $manifestBad) -join ', ')) -ForegroundColor Red; exit 1 }
Write-Host ("`n  USB READY: {0} files on {1} match SHA256SUMS.txt and release-manifest.json ({2})." -f $n, $root, $m2.release_id) -ForegroundColor Green
Write-Host "  Publish record: $((Resolve-Path $logPath).Path)"
exit 0
