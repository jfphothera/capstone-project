<#
  Brings an already-provisioned inference head in line with the hub at 192.0.2.254.
  Run on the head, in PowerShell as administrator:   .\Fix-ExistingHead.ps1            (changes)
                                                      .\Fix-ExistingHead.ps1 -CheckOnly (reports only)
  1. Ollama firewall: the Capstone Head Provisioner 1.0.3 rule accepts port 11434 only from 192.0.2.1.
     It is widened to the Inference Gateway on the hub (192.0.2.254) and the dev laptop (192.0.2.1).
  2. Time: Eastern time zone, and Windows Time syncing from the hub (the cluster has no internet time;
     the Arena refuses clocks more than 2 minutes off). Until the hub serves time, syncs simply retry.
  3. Stay reachable: no sleep on AC power, Ethernet power saving and Energy-Efficient Ethernet off
     (Head 3 dropped off the LAN while idle).
  It changes nothing else: the address, Ollama, its models and the Arena viewer stay as they are.
  A report is written next to this script, in results\<computer>-<time>.txt.
#>
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$Hub = '192.0.2.254'; $Dev = '192.0.2.1'; $Zone = 'Eastern Standard Time'
$lines = New-Object System.Collections.Generic.List[string]
function Say([string]$text) { Write-Host $text; $lines.Add($text) }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this in PowerShell as administrator.'; exit 2
}
$addr = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -match '^192\.0\.2\.(\d{1,3})$' -and $_.PrefixLength -eq 24 } | Select-Object -First 1
if (-not $addr) { Write-Host 'This computer has no 192.0.2.x/24 address: it is not a provisioned head. Nothing was changed.'; exit 2 }
$n = [int]($addr.IPAddress.Split('.')[3])
if ($n -lt 2 -or $n -gt 199) { Write-Host "$($addr.IPAddress) is not a head address (.2-.199). Nothing was changed."; exit 2 }
$nic = $addr.InterfaceAlias
Say "Head $n ($($addr.IPAddress)) on '$nic'  $(if ($CheckOnly) { '- CHECK ONLY, nothing is changed' })"

# 1. Ollama firewall
$rules = @(Get-NetFirewallRule -DisplayName 'Capstone Ollama Head*' -ErrorAction SilentlyContinue)
if (-not $rules) { Say '  Ollama firewall: no "Capstone Ollama Head" rule here (the GPU workstation keeps its own setup). Check that the hub can reach port 11434.' }
foreach ($rule in $rules) {
    $now = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
    if (($now -contains $Hub) -and ($now -contains $Dev)) { Say "  Ollama firewall: '$($rule.DisplayName)' already allows $Dev and $Hub." }
    elseif ($CheckOnly) { Say "  Ollama firewall: '$($rule.DisplayName)' allows only $($now -join ', '): would allow $Dev and $Hub." }
    else { $rule | Set-NetFirewallRule -RemoteAddress $Dev, $Hub; Say "  Ollama firewall: '$($rule.DisplayName)' now allows $Dev and $Hub (was $($now -join ', '))." }
}

# 2. Time zone and time source
$tz = (Get-TimeZone).Id
if ($tz -eq $Zone) { Say "  Time zone: $Zone." }
elseif ($CheckOnly) { Say "  Time zone: $tz, would become $Zone." }
else { Set-TimeZone -Id $Zone; Say "  Time zone: $Zone (was $tz)." }
$peers = (w32tm /query /configuration 2>$null | Select-String 'NtpServer:' | Select-Object -First 1)
if ($CheckOnly) { Say "  Time source: $(if ($peers) { $peers.ToString().Trim() } else { 'not configured' }); would sync from $Hub." }
else {
    Set-Service w32time -StartupType Automatic; Start-Service w32time -ErrorAction SilentlyContinue
    w32tm /config /manualpeerlist:"$Hub,0x8" /syncfromflags:manual /reliable:no /update | Out-Null
    Restart-Service w32time; w32tm /resync /nowait | Out-Null
    Say "  Time source: $Hub (Windows Time starts automatically; it retries until the hub serves time)."
}

# 3. Stay reachable
if ($CheckOnly) { Say '  Power: would set no sleep on AC power and turn off Ethernet power saving and Energy-Efficient Ethernet.' }
else {
    powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
    try { Disable-NetAdapterPowerManagement -Name $nic -ErrorAction Stop; Say "  Power: '$nic' may no longer be turned off to save power." }
    catch { Say "  Power: could not change power management on '$nic' ($($_.Exception.Message)); set it in Device Manager." }
    $eee = Get-NetAdapterAdvancedProperty -Name $nic -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Energy.Efficient' }
    foreach ($p in $eee) { try { Set-NetAdapterAdvancedProperty -Name $nic -DisplayName $p.DisplayName -DisplayValue 'Off' -ErrorAction Stop; Say "  Power: '$($p.DisplayName)' off." } catch { Say "  Power: '$($p.DisplayName)' could not be set to Off." } }
    Say '  Power: no sleep or hibernate on AC power.'
}

$out = Join-Path $PSScriptRoot 'results'
New-Item -ItemType Directory -Force $out | Out-Null
$file = Join-Path $out ('{0}-{1}{2}.txt' -f $env:COMPUTERNAME, (Get-Date -Format 'yyyyMMdd-HHmmss'), $(if ($CheckOnly) { '-check' } else { '' }))
$lines | Set-Content -Path $file -Encoding UTF8
Write-Host ''; Write-Host "Report saved: $file"
