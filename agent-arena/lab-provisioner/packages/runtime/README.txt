Official runtime installers for the Inference Gateway host (deployment-manifest.json "runtime").

python-3.12.10-amd64.exe     Python Software Foundation; MD5 as published at https://www.python.org/downloads/release/python-31210/
PowerShell-7.4.22-win-x64.msi  Microsoft; SHA-256 as published at https://github.com/PowerShell/PowerShell/releases/tag/v7.4.22

The provisioner checks each file against those published hashes and its Authenticode signer before it runs it, and installs it machine-wide with the arguments the Gateway manifest states. The values are in config\lab-network.json "runtime".
