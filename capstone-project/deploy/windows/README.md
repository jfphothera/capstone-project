# Capstone dedicated-laptop release

This package installs Capstone as the application coordinator on the Windows
laptop whose planned address is `192.0.2.201`. It does not assign that address,
change a firewall, host an inference service, or install Agent Arena. The
independent Gateway and Arena remain on operator-managed infrastructure at
`192.0.2.254`.

## Layout and lifecycle

- Replaceable code: `C:\Program Files\Capstone\releases\<version>\application`
- Atomic active-version pointer: `C:\Program Files\Capstone\current.json`
- Managed Python: `C:\Program Files\Capstone\runtime`
- Bundled locked wheels and the approved local MiniLM embedding snapshot; runtime
  operation does not download models or packages.
- Bundled public CA trust files contain no private keys or credentials. Gateway
  trust remains separately provisioned through its private `ca_file` setting.
- Persistent state/config/logs/backups: `C:\ProgramData\Capstone`
- Startup: one Windows Scheduled Task under `LOCAL SERVICE`, independent of an
  interactive sign-in or browser. Experiment subprocesses are children of this
  coordinator and keep running when an administrator closes the browser.

From an elevated PowerShell prompt after extracting the ZIP:

```powershell
.\tools\Install-Capstone.ps1
.\tools\Configure-Capstone.ps1 `
  -OperationalGatewayBootstrap C:\AuthorizedProvisioning\inference-gateway.client.json `
  -ResearchGatewayBootstrap C:\AuthorizedProvisioning\inference-gateway.research-client.json `
  -DeveloperTokenFile C:\ProgramData\Capstone\secrets\developer.token `
  -VerifyLocalAddress
& 'C:\Program Files\Capstone\tools\Register-CapstoneTask.ps1'
& 'C:\Program Files\Capstone\tools\Test-CapstoneGateway.ps1'
& 'C:\Program Files\Capstone\tools\Test-CapstoneHealth.ps1'
```

Startup registration deliberately occurs last and fails closed while the local
developer secret, Gateway bootstrap, endpoint, or CA trust is incomplete.

The administrator must provision the Gateway CA and two independent tokens using
the Gateway owner's approved process. Each bootstrap must name a protected
`token_file` and `ca_file`; production configuration rejects `token_environment`
and never contains a token. The Agent Arena owner
must install/register its existing Ticketing adapter against the read-only
`C:\ProgramData\Capstone\data\operations.db` path and provision any
machine-specific adapter credential. No unverified Arena API or port is assumed.

Use `Start-ScheduledTask`, `tools\Stop-Capstone.ps1`, and
`tools\Test-CapstoneHealth.ps1` for lifecycle and diagnostics. Logs are under
`C:\ProgramData\Capstone\logs`.

Updates install into a new version directory, retain its compatibility manifest,
verify the current data schema, preserve ProgramData, back up both the operations and
AI-configuration databases, switch the version pointer, and health-check. Failure
restores the prior pointer and both databases. Old releases remain available for controlled
rollback until an administrator removes them under the retention policy.
`tools\Rollback-Capstone.ps1 -Version <installed-version>` performs a manual
schema-compatible application-pointer rollback while preserving the current ProgramData databases,
Testing Lab registry, experiment directories, exports, and recovery audit records.

The release manifest pins Python 3.12, minimum and maximum data schema 11, Gateway contract
`shared.inference/1`, and the existing Ticketing adapter boundary. Package file
hashes are verified before install/update. No secret is included in the ZIP.

## Lab qualification (rc.25)

`tools\Qualify-CapstoneLab.cmd` runs a small real-model request-compatibility
set from an elevated prompt on the Capstone laptop when no experiment is
running. It refuses if an experiment or a live worker is active, launches no
experiment and changes no setting. It covers:

- Gemma explicit mediated tool selection with its real payload;
- Granite/Qwen structured QMS and peer contracts at their declared budgets;
- native tool-argument schema checks;
- a two-fixture reviewer calibration.

Results (`qualification-<time>.json` and `.md`) are written to a
`qualification-results` folder beside the `.cmd`, so it can run from a drive.

