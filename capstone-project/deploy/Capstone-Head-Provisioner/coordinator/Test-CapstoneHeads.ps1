<#
    Test-CapstoneHeads
    Coordinator-side, read-only verification of every Capstone head.

    Run this on the coordinator (192.0.2.1). It is the only place the last two
    verification levels in spec section 21 can honestly be established, because
    a head cannot prove that the coordinator reached it.

    It is read-only with respect to configuration: it queries APIs and asks one
    model for a few tokens. It never installs, downloads, removes, restarts or
    reconfigures anything on any head.

    This exists so that four heads can be checked from one keyboard.
#>
[CmdletBinding()]
param(
    # 2 is the GPU workstation; 3 to 15 are CPU heads. Nodes that are not built
    # yet simply report UNREACHABLE, so the full sweep is harmless.
    #
    # Declared as a STRING, not [int[]]. When this script is launched with
    # powershell.exe -File (which is how the .cmd launcher runs it) every
    # argument arrives as a string, and "-Nodes 2,3,4" bound to an [int[]]
    # collapses to the single node 234. Parsing it here makes
    #     COORDINATOR-CHECK-ALL-HEADS.cmd -Nodes 3,4,5
    # work as written.
    [string]$Nodes = '',
    [int]$Port = 11434,
    [int]$ConnectTimeoutMs = 2000,
    [int]$InferenceTimeoutSeconds = 180,
    [switch]$SkipInference,
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# This utility is standalone by design - it must run on the coordinator from a
# copied folder with no modules imported - so the HTTP fix is inlined here.
# Without it, Windows PowerShell 5.1 spends about 15 seconds on WPAD proxy
# discovery before the FIRST request, which makes the first head queried look
# unreachable even when it is serving normally.
try {
    [System.Net.ServicePointManager]::SecurityProtocol =
        [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls11
    [System.Net.ServicePointManager]::Expect100Continue = $false
    $proxyConfigured = $false
    try {
        $internetSettings = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        $proxyEnabled = 0
        if ($null -ne $internetSettings.ProxyEnable) { $proxyEnabled = [int]$internetSettings.ProxyEnable }
        if (($proxyEnabled -eq 1 -and $internetSettings.ProxyServer) -or $internetSettings.AutoConfigURL) {
            $proxyConfigured = $true
        }
    } catch { }
    if (-not $proxyConfigured) {
        [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy
    }
} catch { }

$nodeList = @(2..15)
if (-not [string]::IsNullOrWhiteSpace($Nodes)) {
    $parsed = @()
    foreach ($piece in ($Nodes -split '[,;\s]+')) {
        if ([string]::IsNullOrWhiteSpace($piece)) { continue }
        $value = 0
        if ([int]::TryParse($piece.Trim(), [ref]$value) -and $value -ge 1 -and $value -le 254) {
            $parsed += $value
        } else {
            Write-Host ('  Ignoring "{0}": not a node number between 1 and 254.' -f $piece) -ForegroundColor Yellow
        }
    }
    if ($parsed.Count -gt 0) { $nodeList = @($parsed | Select-Object -Unique) }
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $base = $env:ProgramData
    if ([string]::IsNullOrWhiteSpace($base)) { $base = 'C:\ProgramData' }
    $OutputDirectory = Join-Path $base 'CapstoneProvisioner\reports'
}
if (-not (Test-Path -LiteralPath $OutputDirectory)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

function Test-TcpPort {
    param([string]$Address, [int]$TcpPort, [int]$TimeoutMs)
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($Address, $TcpPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $client.Connected
    } catch {
        return $false
    } finally {
        if ($client) { try { $client.Close() } catch { } }
    }
}

function Select-ProbeModel {
    <#
        Prefers the smallest generation model, so the end-to-end check stays
        quick even on a CPU-only head. Embedding models are excluded: sending a
        generation prompt to one is not a valid test.
    #>
    param([array]$Models)
    $generation = @($Models | Where-Object { $_.name -notmatch '(?i)embed|minilm' })
    if ($generation.Count -eq 0) { return $null }
    return (@($generation | Sort-Object { [int64]$_.size })[0])
}

Write-Host ''
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host '  CAPSTONE COORDINATOR - HEAD VERIFICATION (read-only)' -ForegroundColor Cyan
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host ('  Started {0} from {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $env:COMPUTERNAME)
Write-Host ''

$results = @()

foreach ($node in $nodeList) {
    $address = ('192.0.2.{0}' -f $node)
    $baseUri = ('http://{0}:{1}' -f $address, $Port)

    $record = [ordered]@{
        node = $node
        address = $address
        endpoint = $baseUri
        icmp_replied = $false
        tcp_open = $false
        api_healthy = $false
        ollama_version = ''
        model_count = 0
        models = @()
        inference_attempted = $false
        inference_model = ''
        inference_ok = $false
        inference_ms = 0
        inference_note = ''
        verification_level = 'UNREACHABLE'
        error = ''
        checked_at = (Get-Date).ToString('o')
    }

    Write-Host ('-- node {0} ({1})' -f $node, $address) -ForegroundColor White

    try { $record.icmp_replied = [bool](Test-Connection -ComputerName $address -Count 1 -Quiet -ErrorAction SilentlyContinue) } catch { }
    $record.tcp_open = Test-TcpPort -Address $address -TcpPort $Port -TimeoutMs $ConnectTimeoutMs

    if (-not $record.tcp_open) {
        # ICMP is blocked by default on these machines, so a ping failure here
        # is not the finding; the closed TCP port is.
        $record.error = ('No TCP connection to {0}. The head may be powered off, not yet provisioned, or not serving on the private LAN.' -f $baseUri)
        $record.verification_level = 'UNREACHABLE'
        Write-Host ('   unreachable (ping={0}, tcp {1}=closed)' -f $record.icmp_replied, $Port) -ForegroundColor DarkGray
        $results += [pscustomobject]$record
        continue
    }

    try {
        $version = Invoke-RestMethod -Uri ('{0}/api/version' -f $baseUri) -Method Get -TimeoutSec 15 -ErrorAction Stop
        $record.api_healthy = $true
        if ($version -and $version.version) { $record.ollama_version = [string]$version.version }
    } catch {
        $record.error = ('TCP {0} accepted a connection but the Ollama API did not answer: {1}' -f $Port, $_.Exception.Message)
        $record.verification_level = 'PORT_OPEN_API_SILENT'
        Write-Host ('   port open, API silent: {0}' -f $_.Exception.Message) -ForegroundColor Yellow
        $results += [pscustomobject]$record
        continue
    }

    $models = @()
    try {
        $tags = Invoke-RestMethod -Uri ('{0}/api/tags' -f $baseUri) -Method Get -TimeoutSec 30 -ErrorAction Stop
        if ($tags -and $tags.models) { $models = @($tags.models) }
    } catch {
        $record.error = ('Model inventory query failed: {0}' -f $_.Exception.Message)
    }

    $record.model_count = $models.Count
    $record.models = @($models | ForEach-Object {
        [ordered]@{
            tag = [string]$_.name
            size_bytes = $(if ($_.size) { [int64]$_.size } else { 0 })
            size_display = $(if ($_.size) { '{0:N2} GB' -f ([int64]$_.size / 1GB) } else { 'unknown' })
            digest = [string]$_.digest
        }
    })
    $record.verification_level = 'COORDINATOR_REACHABLE'

    Write-Host ('   API healthy, Ollama {0}, {1} model(s)' -f $record.ollama_version, $record.model_count) -ForegroundColor Green

    if ($SkipInference) {
        $record.inference_note = 'Inference check skipped by request.'
        $results += [pscustomobject]$record
        continue
    }
    if ($models.Count -eq 0) {
        $record.inference_note = 'No models are installed on this head, so remote inference could not be proven. The server itself is healthy.'
        Write-Host '   no models installed; remote inference not proven' -ForegroundColor Yellow
        $results += [pscustomobject]$record
        continue
    }

    $probe = Select-ProbeModel -Models $models
    if ($null -eq $probe) {
        $record.inference_note = 'Only embedding models are installed; a generation probe would be invalid. Embedding models are not exercised from here.'
        $results += [pscustomobject]$record
        continue
    }

    $record.inference_attempted = $true
    $record.inference_model = [string]$probe.name
    Write-Host ('   asking {0} for a short reply ...' -f $probe.name) -ForegroundColor White

    $started = Get-Date
    try {
        # 96 tokens, not 16. A reasoning model such as Qwen3 spends a small
        # budget entirely on `thinking` and returns an empty `response`, which
        # is a working model, not a failure. Reasoning output is accepted
        # below for the same reason.
        $body = [pscustomobject]@{
            model = [string]$probe.name
            prompt = 'Reply with the single word READY.'
            stream = $false
            options = [pscustomobject]@{ num_predict = 96; temperature = 0; seed = 42 }
        } | ConvertTo-Json -Compress -Depth 5

        $reply = Invoke-RestMethod -Uri ('{0}/api/generate' -f $baseUri) -Method Post -ContentType 'application/json' -Body $body -TimeoutSec $InferenceTimeoutSeconds -ErrorAction Stop
        $record.inference_ms = [int]((Get-Date) - $started).TotalMilliseconds

        $answerText = ''
        $reasoningText = ''
        $tokenCount = 0
        if ($reply) {
            if ($reply.PSObject.Properties.Name -contains 'response' -and $reply.response) { $answerText = [string]$reply.response }
            if ($reply.PSObject.Properties.Name -contains 'thinking' -and $reply.thinking) { $reasoningText = [string]$reply.thinking }
            if ($reply.PSObject.Properties.Name -contains 'eval_count' -and $reply.eval_count) { $tokenCount = [int]$reply.eval_count }
        }

        $generated = $answerText
        $generatedKind = 'answer'
        if ([string]::IsNullOrWhiteSpace($generated) -and -not [string]::IsNullOrWhiteSpace($reasoningText)) {
            $generated = $reasoningText
            $generatedKind = 'reasoning'
        }

        if ($reply -and $reply.done -eq $true -and (-not [string]::IsNullOrWhiteSpace($generated) -or $tokenCount -gt 0)) {
            $record.inference_ok = $true
            $preview = ($generated.Trim() -replace '\s+', ' ')
            if ($preview.Length -gt 50) { $preview = $preview.Substring(0, 50) + '...' }
            $record.inference_note = ('Generated {0} token(s) of {1} in {2} ms: "{3}"' -f $tokenCount, $generatedKind, $record.inference_ms, $preview)
            $record.verification_level = 'REMOTE_INFERENCE_PROVEN'
            Write-Host ('   remote inference PROVEN in {0} ms ({1} {2} tokens)' -f $record.inference_ms, $tokenCount, $generatedKind) -ForegroundColor Green
        } else {
            $record.inference_note = 'The head accepted the request but did not return a completed generation.'
            Write-Host '   remote inference NOT proven: incomplete response' -ForegroundColor Red
        }
    } catch {
        $record.inference_ms = [int]((Get-Date) - $started).TotalMilliseconds
        $record.inference_note = ('Remote generation failed: {0}' -f $_.Exception.Message)
        Write-Host ('   remote inference FAILED: {0}' -f $_.Exception.Message) -ForegroundColor Red
    }

    $results += [pscustomobject]$record
}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

$lines = @()
$lines += '================================================================'
$lines += '  CAPSTONE COORDINATOR - HEAD VERIFICATION REPORT'
$lines += '================================================================'
$lines += ('  Generated : {0}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
$lines += ('  From      : {0} (coordinator)' -f $env:COMPUTERNAME)
$lines += '  Scope     : read-only. No head was modified.'
$lines += ''
$lines += '  Node  Address      API    Models  Remote inference'
$lines += '  ----  -----------  -----  ------  ----------------------------------'

foreach ($result in $results) {
    $api = 'down'
    if ($result.api_healthy) { $api = 'up' }
    $inference = 'not proven'
    if ($result.inference_ok) { $inference = ('PROVEN ({0} in {1} ms)' -f $result.inference_model, $result.inference_ms) }
    elseif (-not $result.tcp_open) { $inference = 'host unreachable' }
    elseif ($result.model_count -eq 0) { $inference = 'no models installed' }
    $lines += ('  {0,-4}  {1,-11}  {2,-5}  {3,-6}  {4}' -f $result.node, $result.address, $api, $result.model_count, $inference)
}

$lines += ''
$lines += '----------------------------------------------------------------'
$lines += '  PER-HEAD DETAIL'
$lines += '----------------------------------------------------------------'
foreach ($result in $results) {
    $lines += ''
    $lines += ('  Node {0} - {1}' -f $result.node, $result.endpoint)
    $lines += ('    Verification level : {0}' -f $result.verification_level)
    $lines += ('    ICMP / TCP {0}      : {1} / {2}' -f $Port, $result.icmp_replied, $result.tcp_open)
    $lines += ('    Ollama version     : {0}' -f $result.ollama_version)
    $lines += ('    Models installed   : {0}' -f $result.model_count)
    foreach ($model in @($result.models)) {
        $lines += ('      - {0,-26} {1}' -f $model.tag, $model.size_display)
    }
    if (-not [string]::IsNullOrWhiteSpace($result.inference_note)) {
        $lines += ('    Inference          : {0}' -f $result.inference_note)
    }
    if (-not [string]::IsNullOrWhiteSpace($result.error)) {
        $lines += ('    Note               : {0}' -f $result.error)
    }
}

$proven = @($results | Where-Object { $_.inference_ok }).Count
$reachable = @($results | Where-Object { $_.api_healthy }).Count

$lines += ''
$lines += '----------------------------------------------------------------'
$lines += '  SUMMARY'
$lines += '----------------------------------------------------------------'
$lines += ('  Heads queried                     : {0}' -f $results.Count)
$lines += ('  Ollama API reachable              : {0}' -f $reachable)
$lines += ('  Remote inference proven           : {0}' -f $proven)
$lines += ''
$lines += '  A head listed as REMOTE_INFERENCE_PROVEN has completed all four'
$lines += '  verification levels. Its own local report may still say'
$lines += '  REMOTE_COORDINATOR_VERIFICATION_PENDING, because the head itself'
$lines += '  has no way to observe this test. This report is the authority for'
$lines += '  the last two levels.'
$lines += ''
$lines += '================================================================'

$text = ($lines -join [Environment]::NewLine)
Write-Host ''
Write-Host $text

$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$textPath = Join-Path $OutputDirectory 'COORDINATOR-HEAD-REPORT.txt'
$jsonPath = Join-Path $OutputDirectory 'COORDINATOR-HEAD-REPORT.json'
$encoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($textPath, $text, $encoding)
[System.IO.File]::WriteAllText((Join-Path $OutputDirectory ('coordinator-head-report-{0}.txt' -f $stamp)), $text, $encoding)
[System.IO.File]::WriteAllText($jsonPath, (([pscustomobject]@{
    schema_version = 1
    record_type = 'capstone-coordinator-head-report'
    generated_at = (Get-Date).ToString('o')
    coordinator = $env:COMPUTERNAME
    heads_queried = $results.Count
    api_reachable = $reachable
    remote_inference_proven = $proven
    heads = $results
}) | ConvertTo-Json -Depth 8), $encoding)

Write-Host ''
Write-Host ('  Saved: {0}' -f $textPath) -ForegroundColor Cyan
Write-Host ('  Saved: {0}' -f $jsonPath) -ForegroundColor Cyan
Write-Host ''

if ($reachable -eq 0) { exit 20 }
if ($proven -lt $reachable) { exit 10 }
exit 0
