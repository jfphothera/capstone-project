EXISTING HEADS (192.0.2.2-.10) - BRING THEM IN LINE WITH THE HUB
================================================================

The heads were set up by the Capstone Head Provisioner 1.0.3. Three things need changing
for the hub at 192.0.2.254; nothing is reinstalled.

On each head:
  1. Run FIX-EXISTING-HEAD.cmd -CheckOnly to see what would change (optional).
  2. Run FIX-EXISTING-HEAD.cmd and approve the administrator prompt.
  3. Read the report in the window, or in results\ next to this file.

What it changes:
  - Ollama firewall rule (port 11434): it allowed only 192.0.2.1. It now allows the
    Inference Gateway on the hub (192.0.2.254) and the dev laptop (192.0.2.1).
  - Time: Eastern time zone, and Windows Time syncing from the hub. The cluster has no
    internet time, and the Arena refuses clocks more than 2 minutes off.
  - Staying reachable: no sleep on AC power; Ethernet power saving and Energy-Efficient
    Ethernet off.

The same manual commands, for one head, in PowerShell as administrator:

  Get-NetFirewallRule -DisplayName 'Capstone Ollama Head*' | Set-NetFirewallRule -RemoteAddress 192.0.2.1,192.0.2.254
  Set-TimeZone -Id 'Eastern Standard Time'
  Set-Service w32time -StartupType Automatic; Start-Service w32time
  w32tm /config /manualpeerlist:"192.0.2.254,0x8" /syncfromflags:manual /reliable:no /update
  Restart-Service w32time; w32tm /resync /nowait
  powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
  Disable-NetAdapterPowerManagement -Name 'Ethernet'

Re-running the old provisioner 1.0.3 would put the firewall rule back to 192.0.2.1 only.
New heads come from the new provisioner, which sets all of this itself.
