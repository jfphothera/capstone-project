AGENT ARENA 2.0.0 - INSTALL ON AN INFERENCE HEAD
================================================

Which machine is which
----------------------
  192.0.2.1        Coordinator. Runs Capstone ("Ticketing") and the Arena Event
                   Service. Do NOT install the head viewer here unless you were
                   asked to set up the coordinator's own orchestrator display.
  192.0.2.2-.10    Inference heads (Ollama). Install Agent Arena HERE.

        [ Head 3  192.0.2.3 ]--+
        [ Head 4  192.0.2.4 ]--+--- isolated switch ---[ Coordinator 192.0.2.1 ]
        [  ...              ]--+                        Capstone + Arena Event Service :8790
        [ Head 10 192.0.2.10]--+

Before you start
----------------
Ask the coordinator administrator for this head's ONE-TIME ENROLLMENT CODE
(it looks like ARENA-XXXX-XXXX-XXXX-XXXX). Each head has its own code.

Install (about 2 minutes)
-------------------------
  1. Copy this ZIP from the USB drive and extract ALL of it to a folder
     (for example C:\Agent-Arena-2.0.0).
  2. Double-click  INSTALL AGENT ARENA
  3. Check that "Detected identity" shows this head (for Head 3: head-192-0-2-3).
  4. Type the enrollment code. Click Install. Wait for SUCCESS.
  5. Click Launch Arena.

You never type IP addresses, ports or PowerShell commands.

Something wrong?
----------------
  Double-click  AGENT ARENA DIAGNOSTICS   (works even before installing).
  Click "Export report..." and send the file. It contains no secrets.

Repair, upgrade, uninstall: run INSTALL AGENT ARENA again, or use
Start menu > Agent Arena > Agent Arena Setup.

More detail: docs\OPERATOR-GUIDE.md and docs\TROUBLESHOOTING.md
