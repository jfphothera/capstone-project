<#
    Capstone.Models
    The model download engine and local inference verification.

    Design notes that exist because earlier versions failed:

      * Downloads go through the local HTTP API with a genuinely streamed
        response (POST /api/pull, stream:true, read line by line). Earlier
        versions either buffered the whole transfer as one request, which the
        server eventually cut ("An existing connection was forcibly closed by
        the remote host", defect D-08), or shelled out to `ollama pull` and
        piped its output through PowerShell, where native stderr under a Stop
        preference becomes a terminating error (defect D-09).

      * Stalls are distinguished from slow progress by watching the byte
        counters the server itself reports, so the window never looks frozen
        and a genuinely dead transfer is still cut off.

      * A model counts as installed only when its exact tag comes back from
        /api/tags afterwards, and counts as verified only when it answers a
        real inference or embedding request (spec section 19).

      * A failed tag is never silently replaced with a different model.
#>

$script:StallMinutes = 12

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

function Get-CapstoneCatalog {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $catalog = Read-CapstoneJson -Path $Path
    if ($null -eq $catalog) {
        throw ('The model catalog could not be read from {0}.' -f $Path)
    }
    return $catalog
}

function Get-CapstoneProfileNames {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Catalog)
    return @($Catalog.profiles.PSObject.Properties | ForEach-Object { $_.Name })
}

function Get-CapstoneProfileModels {
    <#
        .SYNOPSIS
            Returns the catalog entries for one hardware profile.
        .PARAMETER ProfileName
            CPU8, CPU16, CPU32, GPU or EMBEDDINGS. Deliberately not named
            "Profile": $Profile is an automatic PowerShell variable and binding
            a parameter over it is how defect D-10's family of parser and scope
            problems started.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Catalog,
        [Parameter(Mandatory)][string]$ProfileName,
        [switch]$IncludeEmbeddings
    )

    $entry = $Catalog.profiles.PSObject.Properties | Where-Object { $_.Name -eq $ProfileName }
    if (-not $entry) {
        throw ('Profile "{0}" is not defined in the model catalog. Defined profiles: {1}' -f $ProfileName, ((Get-CapstoneProfileNames -Catalog $Catalog) -join ', '))
    }

    $tags = @($entry.Value.models)
    if ($IncludeEmbeddings -and $entry.Value.PSObject.Properties.Name -contains 'embeddings') {
        $tags += @($entry.Value.embeddings)
    }

    $byTag = @{}
    foreach ($model in @($Catalog.models)) { $byTag[$model.tag] = $model }

    $result = @()
    foreach ($tag in ($tags | Select-Object -Unique)) {
        if ($byTag.ContainsKey($tag)) {
            $result += $byTag[$tag]
        } else {
            # A tag listed in a profile but absent from the model table is a
            # packaging defect, surfaced rather than skipped.
            $result += [pscustomobject]@{
                tag = $tag; kind = 'generate'; bytes = 0; family = 'unknown'
                role = 'unspecified'; verified = $false
                note = 'This tag is listed in the profile but has no entry in the catalog model table.'
            }
        }
    }
    return $result
}

# ---------------------------------------------------------------------------
# Name handling  (pure - unit tested)
# ---------------------------------------------------------------------------

function Resolve-CapstoneModelKey {
    <#
        .SYNOPSIS
            Canonicalises a model reference for comparison.
        .DESCRIPTION
            /api/tags reports "nomic-embed-text:latest" for a model requested as
            "nomic-embed-text". Comparing the raw strings marks a model that is
            present as missing, which then fails the whole profile. Both sides
            of every comparison go through this function.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Reference)

    $value = $Reference.Trim()
    if ([string]::IsNullOrWhiteSpace($value)) { return '' }
    if ($value -notmatch ':') { $value = $value + ':latest' }
    return $value.ToLowerInvariant()
}

function Test-CapstoneModelPresent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Tag,
        [Parameter(Mandatory)][AllowEmptyCollection()][array]$InstalledNames
    )
    $wanted = Resolve-CapstoneModelKey -Reference $Tag
    foreach ($name in $InstalledNames) {
        if ((Resolve-CapstoneModelKey -Reference ([string]$name)) -eq $wanted) { return $true }
    }
    return $false
}

# ---------------------------------------------------------------------------
# Planning  (pure - unit tested)
# ---------------------------------------------------------------------------

function Get-CapstoneModelPlan {
    <#
        .SYNOPSIS
            Builds the persisted download queue before anything is downloaded.
        .DESCRIPTION
            Disk is checked against what is actually still to be fetched, not
            against the whole profile, because already-present models cost
            nothing. Shared layers mean the estimate is an upper bound; that is
            stated rather than silently assumed away.

            RAM capacity never gates disk: spec section 18 is explicit that the
            two are independent, and an earlier version started a multi-tens-of-
            gigabyte batch purely because the machine had enough memory.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][array]$CatalogModels,
        [Parameter(Mandatory)][AllowEmptyCollection()][array]$InstalledNames,
        [int64]$FreeBytes = -1,
        [int64]$ReserveBytes = 10737418240
    )

    $items = @()
    $toDownloadBytes = 0L

    foreach ($model in $CatalogModels) {
        $present = Test-CapstoneModelPresent -Tag $model.tag -InstalledNames $InstalledNames
        $bytes = 0L
        if ($model.PSObject.Properties.Name -contains 'bytes' -and $model.bytes) { $bytes = [int64]$model.bytes }

        $status = 'Pending'
        if ($present) { $status = 'AlreadyInstalled' } else { $toDownloadBytes += $bytes }

        $kind = 'generate'
        if ($model.PSObject.Properties.Name -contains 'kind' -and $model.kind) { $kind = [string]$model.kind }

        $items += [pscustomobject][ordered]@{
            Tag              = [string]$model.tag
            Kind             = $kind
            Family           = $(if ($model.PSObject.Properties.Name -contains 'family') { [string]$model.family } else { '' })
            Role             = $(if ($model.PSObject.Properties.Name -contains 'role') { [string]$model.role } else { '' })
            ExpectedBytes    = $bytes
            Status           = $status
            Attempts         = 0
            StartedAt        = ''
            CompletedAt      = ''
            LastError        = ''
            FailureCategory  = ''
            Digest           = ''
            ObservedBytes    = 0L
            InferenceOk      = $false
            InferenceMs      = 0
            InferenceNote    = ''
        }
    }

    $required = $toDownloadBytes + $ReserveBytes
    $diskOk = $true
    $diskNote = 'Free space was not measured.'

    if ($FreeBytes -ge 0) {
        $diskOk = ($FreeBytes -ge $required)
        $diskNote = ('{0} free; about {1} still to download plus a {2} reserve.' -f (Format-CapstoneBytes -Bytes $FreeBytes), (Format-CapstoneBytes -Bytes $toDownloadBytes), (Format-CapstoneBytes -Bytes $ReserveBytes))
        if (-not $diskOk) {
            $diskNote += ' Not enough space. No existing model is deleted to make room.'
            foreach ($item in $items) {
                if ($item.Status -eq 'Pending') { $item.Status = 'InsufficientDisk' }
            }
        }
    }

    return [pscustomobject]@{
        Items              = $items
        TotalCount         = $items.Count
        AlreadyInstalled   = @($items | Where-Object { $_.Status -eq 'AlreadyInstalled' }).Count
        PendingCount       = @($items | Where-Object { $_.Status -eq 'Pending' }).Count
        ToDownloadBytes    = $toDownloadBytes
        RequiredBytes      = $required
        FreeBytes          = $FreeBytes
        DiskSufficient     = $diskOk
        DiskNote           = $diskNote
    }
}

function Get-CapstoneQueueSummary {
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][array]$Items)

    $byStatus = @{}
    foreach ($item in $Items) {
        $key = [string]$item.Status
        if (-not $byStatus.ContainsKey($key)) { $byStatus[$key] = 0 }
        $byStatus[$key] = $byStatus[$key] + 1
    }
    $installed = @($Items | Where-Object { $_.Status -in @('Verified', 'AlreadyInstalled', 'Downloaded') }).Count
    $failed = @($Items | Where-Object { $_.Status -in @('Failed', 'InsufficientDisk', 'Unsupported') }).Count

    return [pscustomobject]@{
        Total          = $Items.Count
        Installed      = $installed
        Failed         = $failed
        FailedTags     = @($Items | Where-Object { $_.Status -in @('Failed', 'InsufficientDisk', 'Unsupported') } | ForEach-Object { $_.Tag })
        ByStatus       = $byStatus
        InferenceOk    = @($Items | Where-Object { $_.InferenceOk }).Count
    }
}

# ---------------------------------------------------------------------------
# Streaming pull
# ---------------------------------------------------------------------------

function Invoke-CapstoneModelPull {
    <#
        .SYNOPSIS
            Pulls one model through a genuinely streamed local API request.
        .DESCRIPTION
            Reads the newline-delimited JSON progress stream as it arrives.
            Progress is reported from the server's own completed/total counters,
            which also provides the stall detection: if the byte count does not
            advance for StallMinutes the transfer is abandoned as interrupted
            rather than waited on forever.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$BaseUri,
        [Parameter(Mandatory)][string]$Tag,
        [int]$StallMinutes = 0,
        [int]$ProgressIntervalSeconds = 10
    )

    if ($StallMinutes -le 0) { $StallMinutes = $script:StallMinutes }

    $outcome = [ordered]@{
        Tag = $Tag; Success = $false; Error = ''; HttpStatus = 0
        ObservedBytes = 0L; TotalBytes = 0L; LastStatus = ''; Stalled = $false
    }

    $request = $null
    $response = $null
    $reader = $null
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        $request = [System.Net.HttpWebRequest]::Create(('{0}/api/pull' -f $BaseUri))
        $request.Method = 'POST'
        $request.ContentType = 'application/json'
        $request.UserAgent = 'CapstoneHeadProvisioner/1.0'
        # The target is always this machine's own Ollama daemon, so no proxy can
        # ever be correct here, and leaving the default in place risks a WPAD
        # discovery stall on the first request of the process.
        $request.Proxy = $null
        # No overall timeout on the response: the transfer legitimately takes
        # a long time. Liveness is enforced by the stall detector below and by
        # ReadWriteTimeout on individual socket reads.
        $request.Timeout = 60000
        $request.ReadWriteTimeout = 600000
        $request.KeepAlive = $true

        $payload = [System.Text.Encoding]::UTF8.GetBytes((
            [pscustomobject]@{ model = $Tag; stream = $true } | ConvertTo-Json -Compress
        ))
        $request.ContentLength = $payload.Length
        $requestStream = $request.GetRequestStream()
        $requestStream.Write($payload, 0, $payload.Length)
        $requestStream.Dispose()

        $response = $request.GetResponse()
        $outcome.HttpStatus = 200
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())

        $lastProgressAt = Get-Date
        $lastReportAt = Get-Date
        $lastBytes = 0L

        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }

            $message = $null
            try { $message = $line | ConvertFrom-Json -ErrorAction Stop } catch { continue }

            if ($message.PSObject.Properties.Name -contains 'error' -and $message.error) {
                $outcome.Error = [string]$message.error
                return [pscustomobject]$outcome
            }

            if ($message.PSObject.Properties.Name -contains 'status' -and $message.status) {
                $outcome.LastStatus = [string]$message.status
            }
            if ($message.PSObject.Properties.Name -contains 'total' -and $message.total) {
                $outcome.TotalBytes = [int64]$message.total
            }
            if ($message.PSObject.Properties.Name -contains 'completed' -and $message.completed) {
                $outcome.ObservedBytes = [int64]$message.completed
            }

            if ($outcome.ObservedBytes -gt $lastBytes) {
                $lastBytes = $outcome.ObservedBytes
                $lastProgressAt = Get-Date
            }

            if (((Get-Date) - $lastReportAt).TotalSeconds -ge $ProgressIntervalSeconds) {
                $percent = 0
                if ($outcome.TotalBytes -gt 0) {
                    $percent = [math]::Round(($outcome.ObservedBytes / $outcome.TotalBytes) * 100, 1)
                }
                Write-CapstoneLog ('    {0}: {1} - {2} of {3} ({4}%)' -f $Tag, $outcome.LastStatus, (Format-CapstoneBytes -Bytes $outcome.ObservedBytes), (Format-CapstoneBytes -Bytes $outcome.TotalBytes), $percent) -Level INFO
                $lastReportAt = Get-Date
            }

            if (((Get-Date) - $lastProgressAt).TotalMinutes -ge $StallMinutes) {
                $outcome.Stalled = $true
                $outcome.Error = ('No download progress for {0} minutes (last reported status: {1}). The transfer was abandoned; completed layers are kept and a re-run resumes.' -f $StallMinutes, $outcome.LastStatus)
                return [pscustomobject]$outcome
            }

            if ($outcome.LastStatus -eq 'success') {
                $outcome.Success = $true
            }
        }

        if (-not $outcome.Success) {
            # The stream ended without an explicit success record. Do not treat
            # that as completion; the caller re-reads /api/tags to decide.
            $outcome.Error = ('The pull stream ended after "{0}" without reporting success.' -f $outcome.LastStatus)
        }
        return [pscustomobject]$outcome
    } catch [System.Net.WebException] {
        # $_ is rebound by every nested catch, so the original transport message
        # is captured here before anything else can run.
        $transportMessage = $_.Exception.Message
        $outcome.Error = $transportMessage
        try {
            $webResponse = $_.Exception.Response
            if ($webResponse) {
                $outcome.HttpStatus = [int]$webResponse.StatusCode
                $errorReader = New-Object System.IO.StreamReader($webResponse.GetResponseStream())
                $body = $errorReader.ReadToEnd()
                $errorReader.Dispose()
                if (-not [string]::IsNullOrWhiteSpace($body)) {
                    $reported = ''
                    try {
                        $parsed = $body | ConvertFrom-Json -ErrorAction Stop
                        if ($parsed.error) { $reported = [string]$parsed.error }
                    } catch { $reported = '' }
                    if (-not [string]::IsNullOrWhiteSpace($reported)) {
                        $outcome.Error = ('{0} (HTTP {1})' -f $reported, $outcome.HttpStatus)
                    } else {
                        $outcome.Error = ('{0} (HTTP {1}): {2}' -f $transportMessage, $outcome.HttpStatus, $body.Trim())
                    }
                }
            }
        } catch { }
        return [pscustomobject]$outcome
    } catch {
        $outcome.Error = $_.Exception.Message
        return [pscustomobject]$outcome
    } finally {
        if ($reader) { try { $reader.Dispose() } catch { } }
        if ($response) { try { $response.Dispose() } catch { } }
    }
}

# ---------------------------------------------------------------------------
# Local inference verification
# ---------------------------------------------------------------------------

function Test-CapstoneGenerationResponse {
    <#
        .SYNOPSIS
            Decides whether a /api/generate reply proves the model works.
        .DESCRIPTION
            Pure, so this rule is unit tested. It exists because of a real
            false negative measured against qwen3:4b on the GPU workstation:

                response     : (empty)
                thinking     : "Hmm, the user just asked me to reply with ..."
                done         : True
                done_reason  : length
                eval_count   : 16

            Qwen3 is a reasoning model. With a small num_predict budget every
            generated token goes into `thinking` and `response` is left empty.
            Requiring a non-empty `response` therefore fails every Qwen3 model -
            nine of the tags in this catalog, including the whole backbone of
            the CPU16 and CPU32 profiles - even though each one loaded and
            generated correctly.

            What actually needs proving at this stage is that the model loads
            and emits tokens. Reasoning output counts: it is still generated
            text. The distinction is preserved in the note so a truncated
            reasoning reply is visible rather than hidden.
    #>
    [CmdletBinding()]
    param(
        [AllowEmptyString()][AllowNull()][string]$ResponseText = '',
        [AllowEmptyString()][AllowNull()][string]$ThinkingText = '',
        [bool]$Done = $false,
        [string]$DoneReason = '',
        [int]$EvalCount = 0
    )

    if ($null -eq $ResponseText) { $ResponseText = '' }
    if ($null -eq $ThinkingText) { $ThinkingText = '' }

    $hasResponse = -not [string]::IsNullOrWhiteSpace($ResponseText)
    $hasThinking = -not [string]::IsNullOrWhiteSpace($ThinkingText)

    if (-not $Done) {
        return [pscustomobject]@{
            Success = $false
            Note = 'The model did not report a completed generation.'
            Kind = 'incomplete'
        }
    }
    if (-not $hasResponse -and -not $hasThinking -and $EvalCount -le 0) {
        return [pscustomobject]@{
            Success = $false
            Note = 'The model completed but generated no tokens at all.'
            Kind = 'empty'
        }
    }

    $sample = $ResponseText
    $kind = 'response'
    if (-not $hasResponse -and $hasThinking) {
        $sample = $ThinkingText
        $kind = 'thinking'
    }
    $preview = $sample.Trim() -replace '\s+', ' '
    if ($preview.Length -gt 60) { $preview = $preview.Substring(0, 60) + '...' }

    $note = ('Generated {0} token(s); {1} text: "{2}"' -f $EvalCount, $kind, $preview)
    if ($kind -eq 'thinking') {
        $note = ('Generated {0} reasoning token(s) and no final answer (done_reason: {1}). This model thinks before answering, so the token budget was spent reasoning. It loaded and generated correctly. Reasoning text: "{2}"' -f $EvalCount, $DoneReason, $preview)
    }
    if (-not $hasResponse -and -not $hasThinking -and $EvalCount -gt 0) {
        $note = ('Generated {0} token(s) with no text returned (done_reason: {1}).' -f $EvalCount, $DoneReason)
        $kind = 'tokens-only'
    }

    return [pscustomobject]@{ Success = $true; Note = $note; Kind = $kind }
}

function Test-CapstoneModelInference {
    <#
        .SYNOPSIS
            Proves one model actually loads and answers.
        .DESCRIPTION
            Generation models get a short deterministic prompt; embedding models
            get the embedding API and their vector shape is checked. A
            generation prompt is never sent to an embedding-only model
            (spec section 19).

            The model is unloaded afterwards so a long profile does not hold
            many models resident at once.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$BaseUri,
        [Parameter(Mandatory)][string]$Tag,
        [string]$Kind = 'generate',
        [int]$TimeoutSeconds = 300
    )

    $result = [ordered]@{
        Tag = $Tag; Kind = $Kind; Success = $false; ElapsedMs = 0
        Note = ''; Error = ''; Endpoint = $BaseUri
    }
    $started = Get-Date

    try {
        if ($Kind -eq 'embedding') {
            $body = [pscustomobject]@{ model = $Tag; input = 'Capstone provisioning verification probe.' } | ConvertTo-Json -Compress
            $dimensions = 0
            try {
                $response = Invoke-RestMethod -Uri ('{0}/api/embed' -f $BaseUri) -Method Post -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSeconds -ErrorAction Stop
                if ($response -and $response.embeddings) {
                    $first = @($response.embeddings)[0]
                    $dimensions = @($first).Count
                }
            } catch {
                # Older daemons expose /api/embeddings with a "prompt" field.
                $legacyBody = [pscustomobject]@{ model = $Tag; prompt = 'Capstone provisioning verification probe.' } | ConvertTo-Json -Compress
                $response = Invoke-RestMethod -Uri ('{0}/api/embeddings' -f $BaseUri) -Method Post -ContentType 'application/json' -Body $legacyBody -TimeoutSec $TimeoutSeconds -ErrorAction Stop
                if ($response -and $response.embedding) { $dimensions = @($response.embedding).Count }
            }
            if ($dimensions -gt 0) {
                $result.Success = $true
                $result.Note = ('Returned an embedding vector of {0} dimensions.' -f $dimensions)
            } else {
                $result.Error = 'The embedding API returned no vector.'
            }
        } else {
            # 96 tokens, not 16: a reasoning model needs room to think AND
            # answer. See Test-CapstoneGenerationResponse for why this matters.
            $body = [pscustomobject]@{
                model  = $Tag
                prompt = 'Reply with the single word READY.'
                stream = $false
                options = [pscustomobject]@{ num_predict = 96; temperature = 0; seed = 42 }
            } | ConvertTo-Json -Compress -Depth 5

            $response = Invoke-RestMethod -Uri ('{0}/api/generate' -f $BaseUri) -Method Post -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSeconds -ErrorAction Stop

            $responseText = ''
            $thinkingText = ''
            $doneReason = ''
            $evalCount = 0
            $done = $false
            if ($response) {
                if ($response.PSObject.Properties.Name -contains 'response' -and $response.response) { $responseText = [string]$response.response }
                if ($response.PSObject.Properties.Name -contains 'thinking' -and $response.thinking) { $thinkingText = [string]$response.thinking }
                if ($response.PSObject.Properties.Name -contains 'done_reason' -and $response.done_reason) { $doneReason = [string]$response.done_reason }
                if ($response.PSObject.Properties.Name -contains 'eval_count' -and $response.eval_count) { $evalCount = [int]$response.eval_count }
                $done = ($response.done -eq $true)
            }

            $verdict = Test-CapstoneGenerationResponse -ResponseText $responseText -ThinkingText $thinkingText `
                -Done $done -DoneReason $doneReason -EvalCount $evalCount
            $result.Success = $verdict.Success
            if ($verdict.Success) { $result.Note = $verdict.Note } else { $result.Error = $verdict.Note }
        }
    } catch {
        $result.Error = $_.Exception.Message
    } finally {
        $result.ElapsedMs = [int]((Get-Date) - $started).TotalMilliseconds
        try {
            $unload = [pscustomobject]@{ model = $Tag; keep_alive = 0 } | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri ('{0}/api/generate' -f $BaseUri) -Method Post -ContentType 'application/json' -Body $unload -TimeoutSec 30 -ErrorAction SilentlyContinue | Out-Null
        } catch { }
    }

    return [pscustomobject]$result
}

# ---------------------------------------------------------------------------
# Queue execution
# ---------------------------------------------------------------------------

function Invoke-CapstoneModelQueue {
    <#
        .SYNOPSIS
            Works the download queue to completion with bounded retries.
        .DESCRIPTION
            Every item ends in a recorded terminal status. A model is only
            marked Verified after its exact tag reappears in /api/tags AND it
            answers an inference probe. Nothing here can report a profile as
            complete while an item is still Failed (defect D-11).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [Parameter(Mandatory)][string]$BaseUri,
        [Parameter(Mandatory)]$Plan,
        [int]$MaxAttempts = 3,
        [switch]$SkipInference
    )

    $items = @($Plan.Items)
    $total = $items.Count
    $index = 0

    foreach ($item in $items) {
        $index++

        if ($item.Status -eq 'InsufficientDisk') {
            Write-CapstoneLog ('[{0}/{1}] {2}: skipped, not enough disk space.' -f $index, $total, $item.Tag) -Level WARN
            continue
        }

        $installed = Get-CapstoneInstalledModels -BaseUri $BaseUri
        $names = @($installed.Models | ForEach-Object { $_.Name })

        if (Test-CapstoneModelPresent -Tag $item.Tag -InstalledNames $names) {
            if ($item.Status -eq 'Pending') { $item.Status = 'AlreadyInstalled' }
            Write-CapstoneLog ('[{0}/{1}] {2}: already present, not downloaded again.' -f $index, $total, $item.Tag) -Level OK
        } else {
            $item.StartedAt = (Get-Date).ToString('o')
            $succeeded = $false

            for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
                $item.Attempts = $attempt
                $item.Status = 'Downloading'
                Write-CapstoneLog ('[{0}/{1}] {2}: downloading (attempt {3} of {4}, about {5})' -f $index, $total, $item.Tag, $attempt, $MaxAttempts, (Format-CapstoneBytes -Bytes $item.ExpectedBytes)) -Level STEP
                Write-CapstoneJournal -Paths $Paths -Stage 'Models' -Event 'pull-start' -Detail ([pscustomobject]@{ tag = $item.Tag; attempt = $attempt })

                $pull = Invoke-CapstoneModelPull -BaseUri $BaseUri -Tag $item.Tag
                $item.ObservedBytes = $pull.ObservedBytes

                # Presence in the inventory is the decisive check, not the
                # stream's own report: a transfer can be cut after the final
                # layer has already landed.
                $after = Get-CapstoneInstalledModels -BaseUri $BaseUri
                $afterNames = @($after.Models | ForEach-Object { $_.Name })
                if (Test-CapstoneModelPresent -Tag $item.Tag -InstalledNames $afterNames) {
                    $match = @($after.Models | Where-Object { (Resolve-CapstoneModelKey -Reference $_.Name) -eq (Resolve-CapstoneModelKey -Reference $item.Tag) })
                    if ($match.Count -gt 0) { $item.Digest = [string]$match[0].Digest }
                    $succeeded = $true
                    break
                }

                $category = Get-CapstoneFailureCategory -Message $pull.Error -HttpStatus $pull.HttpStatus -Stage 'Models'
                $item.LastError = $pull.Error
                $item.FailureCategory = $category.Category
                Write-CapstoneLog ('    {0} failed: [{1}] {2}' -f $item.Tag, $category.Category, $pull.Error) -Level WARN
                Write-CapstoneJournal -Paths $Paths -Stage 'Models' -Event 'pull-failed' -Detail ([pscustomobject]@{ tag = $item.Tag; attempt = $attempt; category = $category.Category; error = $pull.Error })

                if (-not $category.Retryable) {
                    Write-CapstoneLog ('    {0} will not be retried: {1}' -f $item.Tag, $category.Recovery) -Level WARN
                    break
                }
                if ($attempt -lt $MaxAttempts) {
                    $delay = Get-CapstoneRetryDelaySeconds -Attempt $attempt
                    Write-CapstoneLog ('    retrying {0} in {1} seconds' -f $item.Tag, $delay) -Level INFO
                    $item.Status = 'RetryPending'
                    Start-Sleep -Seconds $delay
                }
            }

            if (-not $succeeded) {
                $item.Status = 'Failed'
                $item.CompletedAt = (Get-Date).ToString('o')
                Write-CapstoneLog ('[{0}/{1}] {2}: FAILED after {3} attempt(s). No substitute model was installed.' -f $index, $total, $item.Tag, $item.Attempts) -Level ERROR
                continue
            }
            $item.Status = 'Downloaded'
        }

        if ($SkipInference) {
            $item.InferenceNote = 'Inference check skipped for this run.'
            $item.CompletedAt = (Get-Date).ToString('o')
            continue
        }

        $item.Status = 'Verifying'
        $inference = Test-CapstoneModelInference -BaseUri $BaseUri -Tag $item.Tag -Kind $item.Kind
        $item.InferenceOk = $inference.Success
        $item.InferenceMs = $inference.ElapsedMs
        if ($inference.Success) {
            $item.InferenceNote = $inference.Note
            $item.Status = 'Verified'
            Write-CapstoneLog ('[{0}/{1}] {2}: verified in {3} ms. {4}' -f $index, $total, $item.Tag, $inference.ElapsedMs, $inference.Note) -Level OK
        } else {
            $item.InferenceNote = $inference.Error
            $item.LastError = $inference.Error
            $item.FailureCategory = 'MODEL_VERIFICATION_FAILURE'
            $item.Status = 'Failed'
            Write-CapstoneLog ('[{0}/{1}] {2}: downloaded but FAILED its inference check: {3}' -f $index, $total, $item.Tag, $inference.Error) -Level ERROR
        }
        $item.CompletedAt = (Get-Date).ToString('o')

        Write-CapstoneJson -InputObject ([pscustomobject]@{ updated = (Get-Date).ToString('o'); items = $items }) -Path (Join-Path $Paths.State 'model-queue.json')
    }

    $summary = Get-CapstoneQueueSummary -Items $items
    Write-CapstoneJson -InputObject ([pscustomobject]@{ updated = (Get-Date).ToString('o'); items = $items; summary = $summary }) -Path (Join-Path $Paths.State 'model-queue.json')

    return [pscustomobject]@{ Items = $items; Summary = $summary }
}

Export-ModuleMember -Function @(
    'Get-CapstoneCatalog', 'Get-CapstoneProfileNames', 'Get-CapstoneProfileModels',
    'Resolve-CapstoneModelKey', 'Test-CapstoneModelPresent',
    'Get-CapstoneModelPlan', 'Get-CapstoneQueueSummary',
    'Test-CapstoneGenerationResponse',
    'Invoke-CapstoneModelPull', 'Test-CapstoneModelInference', 'Invoke-CapstoneModelQueue'
)
