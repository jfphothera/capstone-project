Capstone Windows release, supplied by the Capstone project and carried UNCHANGED.

Only the approved archive is shipped: Capstone-6.0.0-rc.2-windows-x64.zip, SHA-256 f316d5a0b94087bce3aa1274f2db70d2a2a47052c87f7db36fdec72347e6b24f (config\lab-network.json, capstone.approved_release). The builder refuses any other archive. On the Capstone laptop, the provisioner checks the archive's SHA-256 before unpacking, then every file against Capstone's own package-manifest.json. It then runs Capstone's own tools, each only after approval:
- Install-Capstone.ps1, or Update-Capstone.ps1 over an older release;
- Configure-Capstone.ps1 with Capstone's own capstone and capstone-research bootstraps;
- Register-CapstoneTask.ps1;
- Test-CapstoneHealth.ps1 to verify.

Configure needs both token files issued on the hub, the Gateway CA, and the developer token file. Tokens are never read or stored by the provisioner.
