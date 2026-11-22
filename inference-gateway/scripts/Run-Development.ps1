param([Parameter(Mandatory=$true)][int]$Port)
$ErrorActionPreference = 'Stop'
if ($Port -lt 0 -or $Port -gt 65535) { throw 'Choose a valid port; 0 requests an ephemeral loopback port.' }
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
$state = Join-Path $root 'var'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$database = Join-Path $state 'development-capacity.db'
& $python -m inference_gateway.capacity_bootstrap --database $database --policy (Join-Path $root 'config/development-policy.json')
if ($LASTEXITCODE) { throw 'Development bootstrap failed.' }
& $python -m inference_gateway.capacity_gateway_server --database $database --host 127.0.0.1 --port $Port
if ($LASTEXITCODE) { throw 'Gateway stopped with an error.' }
