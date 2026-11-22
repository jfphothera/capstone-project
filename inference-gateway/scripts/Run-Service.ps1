param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Database,
    [Parameter(Mandatory=$true)][int]$Port,
    [string]$BindAddress = '127.0.0.1',
    [string]$ServiceId = 'shared-inference',
    [string]$Certificate, [string]$Key,
    [string]$UpstreamCa, [string]$UpstreamCertificate, [string]$UpstreamKey,
    [int]$MaxConnections = 64, [int]$MaxPending = 128
)
$ErrorActionPreference = 'Stop'
if (-not [System.IO.Path]::IsPathRooted($Python) -or -not [System.IO.Path]::IsPathRooted($Database)) { throw 'Use absolute interpreter and ledger paths.' }
if ($Port -lt 1 -or $Port -gt 65535) { throw 'An explicitly approved service port is required.' }
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) { throw 'Bootstrap the operator-owned ledger first.' }
$arguments = @('-m', 'inference_gateway.capacity_gateway_server', '--database', $Database, '--host', $BindAddress, '--port', $Port, '--service-id', $ServiceId, '--max-connections', $MaxConnections, '--max-pending', $MaxPending)
if ($Certificate) { $arguments += @('--certificate', $Certificate, '--key', $Key) }
if ($UpstreamCa) { $arguments += @('--upstream-ca', $UpstreamCa) }
if ($UpstreamCertificate) { $arguments += @('--upstream-certificate', $UpstreamCertificate, '--upstream-key', $UpstreamKey) }
& $Python @arguments
exit $LASTEXITCODE
