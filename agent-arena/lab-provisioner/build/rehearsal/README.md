# Rehearsals: run the real code before a field trip

These scripts run F:'s real code against the other projects' real code on the development laptop.
They exist because mocks hid four field failures on 2026-11-05.

**`Rehearse-Flow.ps1`** runs the whole `Invoke-LabGatewayFlow`, intake to cutover, in Windows PowerShell 5.1.
- **Real:**
  - the release intake from F:;
  - the head probe and inventory against the live heads (needs 192.0.2.1 on the laptop);
  - every Gateway launcher command against a scratch Gateway release and a fresh ledger;
  - real qualification on the heads.
- **Stood in:** what would change the laptop, which is the account, rights, Windows service, ACL script and firewall rule.

**`Rehearse-AfterInstall.ps1`** runs the same commands one by one and prints each Gateway result.

**Setup**, in the scripts' folder:
1. `python -I -m pip install --no-index --no-deps --no-compile --target releases\0.3.0 <Gateway wheel>`
2. `data\tls\server.pem` and `server.key` from openssl, for the console runtime.
3. `data\config\gateway-service.json` from `deployment.service_config`.

**Results** are recorded in `docs/test-results/rehearsal-gateway-*.md`.
