<#
  Prepares the admin computer for the Arena: run it ON THE ADMIN COMPUTER before INSTALL-ADMIN-HUB.cmd.
  SET-UP-THIS-COMPUTER.cmd starts it as administrator.   -CheckOnly reports and changes nothing.
  1. Address 192.0.2.254/24 on the cluster Ethernet adapter (you pick it; asks before changing).
  2. Python 3.12 for this Windows user (winget, when there is internet; otherwise tells you how).
  3. Stays awake: no sleep or hibernate on AC, lid closed does nothing, Ethernet power saving off.
  4. Time: Eastern time zone; this computer serves time to the cluster (UDP 123 from 192.0.2.0/24).
  5. Ping answered from the cluster; Remote Desktop allowed from the dev laptop (192.0.2.1) only.
  Every step checks first and is safe to run again. A report goes to results\ next to this script.
#>
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$Address = '192.0.2.254'; $Dev = '192.0.2.1'; $Cluster = '192.0.2.0/24'; $Zone = 'Eastern Standard Time'
$lines = New-Object System.Collections.Generic.List[string]
function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color; $lines.Add($text) }
function Rule([string]$name, [scriptblock]$make) {
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) { Say "  Firewall '$name': already present."; return }
    if ($CheckOnly) { Say "  Firewall '$name': would be added."; return }
    & $make | Out-Null; Say "  Firewall '$name': added." 'Green'
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this as administrator (use SET-UP-THIS-COMPUTER.cmd).'; exit 2
}
Say "ADMIN COMPUTER SETUP  $env:COMPUTERNAME  $(Get-Date -Format 's')$(if ($CheckOnly) { '  - CHECK ONLY, nothing is changed' })" 'Cyan'

# 1. Address
Say '1. Address'
$have = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $Address -ErrorAction SilentlyContinue
if ($have -and $have.PrefixLength -eq 24) { $nic = $have.InterfaceAlias; Say "  $Address/24 is already on '$nic'." 'Green' }
else {
    $wired = @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -and $_.MediaType -eq '802.3' })
    if (-not $wired) { Say '  No connected wired adapter: plug this computer into the cluster switch and run this again.' 'Red'; $nic = $null }
    else {
        for ($i = 0; $i -lt $wired.Count; $i++) {
            $ips = @(Get-NetIPAddress -InterfaceIndex $wired[$i].ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" }) -join ', '
            Write-Host ("  [{0}] {1}  ({2}, {3})  now: {4}" -f ($i + 1), $wired[$i].Name, $wired[$i].InterfaceDescription, $wired[$i].MacAddress, $(if ($ips) { $ips } else { 'no address' }))
        }
        $pick = if ($wired.Count -eq 1) { 1 } else { [int](Read-Host '  Which one is plugged into the cluster switch? Number') }
        $nic = $wired[$pick - 1].Name
        if ($CheckOnly) { Say "  Would set $Address/24 on '$nic' (no gateway)." }
        elseif ((Read-Host "  Set '$nic' to $Address/24, replacing its current IPv4 address? Type YES") -ne 'YES') { Say '  Address not changed (not confirmed).' 'Yellow' }
        else {
            Set-NetIPInterface -InterfaceAlias $nic -AddressFamily IPv4 -Dhcp Disabled
            Get-NetIPAddress -InterfaceAlias $nic -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
            New-NetIPAddress -InterfaceAlias $nic -IPAddress $Address -PrefixLength 24 | Out-Null
            Say "  '$nic' is now $Address/24." 'Green'
        }
    }
}
if ($nic -and -not $CheckOnly) {
    try { Set-NetConnectionProfile -InterfaceAlias $nic -NetworkCategory Private; Say "  '$nic' network profile: Private." } catch { Say "  Could not set the network profile on '$nic' (it may still be identifying)." 'Yellow' }
}

# 2. Python
Say '2. Python 3.12 or later'
$py = $null
foreach ($cmd in @(@('py', '-3'), @('python'))) {
    if (-not (Get-Command $cmd[0] -ErrorAction SilentlyContinue)) { continue }
    try {
        $v = & $cmd[0] @($cmd[1..9] | Where-Object { $_ }) -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>$null
        if ([int]$v -ge 312) { $py = "$($cmd -join ' ') ($([int]$v / 100 -as [int]).$([int]$v % 100))"; break }
    } catch { }
}
if ($py) { Say "  Found $py." 'Green' }
elseif ($CheckOnly) { Say '  Not found; would install it with winget.' }
elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Say '  Installing Python 3.12 with winget (needs internet, for example Wi-Fi)...'
    winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
    Say "  winget finished (code $LASTEXITCODE). Open a NEW window and check: py -3 --version" 'Yellow'
} else { Say '  Not found, and winget is not available: install Python 3.12 from python.org for this user (tick "Add to PATH").' 'Red' }

# 3. Stay awake
Say '3. Stay awake and reachable'
if ($CheckOnly) { Say '  Would set: no sleep/hibernate on AC, screen on, lid does nothing, Ethernet power saving off.' }
else {
    powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; powercfg /change monitor-timeout-ac 0
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0; powercfg /setactive SCHEME_CURRENT
    Say '  No sleep or hibernate on AC; screen stays on; closing the lid does nothing.' 'Green'
    if ($nic) {
        try { Disable-NetAdapterPowerManagement -Name $nic -ErrorAction Stop; Say "  '$nic' may no longer be turned off to save power." 'Green' }
        catch { Say "  Could not change power management on '$nic'; turn it off in Device Manager." 'Yellow' }
        foreach ($p in @(Get-NetAdapterAdvancedProperty -Name $nic -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Energy.Efficient' })) {
            try { Set-NetAdapterAdvancedProperty -Name $nic -DisplayName $p.DisplayName -DisplayValue 'Off' -ErrorAction Stop; Say "  '$($p.DisplayName)' off." 'Green' } catch { }
        }
    }
}

# 4. Time
Say '4. Time'
$tz = (Get-TimeZone).Id
if ($tz -eq $Zone) { Say "  Time zone: $Zone." } elseif ($CheckOnly) { Say "  Time zone $tz would become $Zone." } else { Set-TimeZone -Id $Zone; Say "  Time zone: $Zone (was $tz)." 'Green' }
if ($CheckOnly) { Say '  Would make this computer the cluster''s time server (it syncs from time.windows.com when it has internet).' }
else {
    Set-Service w32time -StartupType Automatic; Start-Service w32time -ErrorAction SilentlyContinue
    w32tm /config /manualpeerlist:"time.windows.com,0x8" /syncfromflags:manual /reliable:yes /update | Out-Null
    Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer -Name Enabled -Value 1
    Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Config -Name AnnounceFlags -Value 5
    Restart-Service w32time
    Say '  This computer now serves time to the cluster.' 'Green'
}
Rule 'Arena time server' { New-NetFirewallRule -DisplayName 'Arena time server' -Direction Inbound -Protocol UDP -LocalPort 123 -RemoteAddress $Cluster -Action Allow -Profile Any }

# 5. Ping and Remote Desktop
Say '5. Ping and Remote Desktop'
Rule 'Arena ping' { New-NetFirewallRule -DisplayName 'Arena ping' -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -RemoteAddress $Cluster -Action Allow -Profile Any }
if ($CheckOnly) { Say "  Would allow Remote Desktop from $Dev only." }
else {
    Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
    $rdp = @(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue)
    if ($rdp) { $rdp | Enable-NetFirewallRule; $rdp | Set-NetFirewallRule -RemoteAddress $Dev; Say "  Remote Desktop on, from $Dev only." 'Green' }
    else { Say '  Remote Desktop firewall rules not found (Windows Home has no Remote Desktop host).' 'Yellow' }
}

$out = Join-Path $PSScriptRoot 'results'
New-Item -ItemType Directory -Force $out | Out-Null
$file = Join-Path $out ('{0}-{1}-setup{2}.txt' -f $env:COMPUTERNAME, (Get-Date -Format 'yyyyMMdd-HHmmss'), $(if ($CheckOnly) { '-check' } else { '' }))
$lines | Set-Content -Path $file -Encoding UTF8
Write-Host ''
Write-Host "Report saved: $file"
Write-Host 'Next: close this window and run INSTALL-ADMIN-HUB.cmd as your normal user (not as administrator).' -ForegroundColor Cyan
