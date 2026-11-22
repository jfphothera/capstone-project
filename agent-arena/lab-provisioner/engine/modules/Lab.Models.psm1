<#
    Lab.Models - model selection and installation, separate from base provisioning.

    The catalog is provisioner 1.0.3's (engine\head-engine\catalog\model-catalog.json): profiles
    CPU8, CPU16, CPU32, GPU and EMBEDDINGS, each entry with its exact tag, kind, size and the
    registry's config digest. Nothing is bundled on the USB; models are pulled through the
    head's own Ollama API (/api/pull) with the 1.0.3 queue (retries, stall detection, disk
    reserve), whose failure classification is fixed so that no internet is never reported as
    an invalid tag. Every model ends with six separate facts:

        selected  ->  download attempted  ->  download completed  ->  installed locally
        (in /api/tags, with its config digest compared to the catalog)  ->  verified by a real
        generate or embed request  ->  qualified by the Gateway (never claimed here: only the
        Inference Gateway qualifies models, from the hub).
#>

function Get-LabCatalog {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-CapstoneCatalog -Path $Path)
}

function Get-LabProfileProposal {
    <#  Same thresholds as 1.0.3 (engine lines 84-91), with the reason shown.  Pure.  #>
    param([double]$RamGB = 0, [int]$VramMB = 0)
    if ($VramMB -ge 8000) { return [pscustomobject]@{ Profile = 'GPU'; Reason = ('NVIDIA GPU with {0} MB VRAM' -f $VramMB) } }
    if ($RamGB -lt 11) { return [pscustomobject]@{ Profile = 'CPU8'; Reason = ('{0} GB RAM, no qualifying GPU' -f $RamGB) } }
    if ($RamGB -lt 24) { return [pscustomobject]@{ Profile = 'CPU16'; Reason = ('{0} GB RAM, no qualifying GPU' -f $RamGB) } }
    return [pscustomobject]@{ Profile = 'CPU32'; Reason = ('{0} GB RAM, no qualifying GPU' -f $RamGB) }
}

function Select-LabModels {
    <#  The models to install: the chosen profiles' entries, plus individually named tags that are
        in the catalog, minus exclusions. A tag outside the catalog is refused (no substitutes).  Pure.  #>
    param([Parameter(Mandatory)]$Catalog, [AllowEmptyCollection()][string[]]$Profiles = @(), [AllowEmptyCollection()][string[]]$Add = @(), [AllowEmptyCollection()][string[]]$Exclude = @(), [switch]$NoEmbeddings)
    $byTag = @{}; foreach ($m in @($Catalog.models)) { $byTag[(Resolve-CapstoneModelKey -Reference $m.tag)] = $m }
    $chosen = New-Object System.Collections.ArrayList; $seen = @{}; $refused = @()
    foreach ($p in $Profiles) {
        if (-not ($Catalog.profiles.PSObject.Properties.Name -contains $p)) { $refused += ("profile $p is not in the catalog"); continue }
        $prof = $Catalog.profiles.$p
        $tags = @($prof.models); if (-not $NoEmbeddings) { $tags += @($prof.embeddings) }
        foreach ($t in $tags) { $k = Resolve-CapstoneModelKey -Reference $t; if (-not $seen[$k] -and $byTag[$k]) { $seen[$k] = $true; [void]$chosen.Add($byTag[$k]) } }
    }
    foreach ($t in $Add) {
        $k = Resolve-CapstoneModelKey -Reference $t
        if (-not $byTag[$k]) { $refused += ("$t is not in the approved catalog"); continue }
        if (-not $seen[$k]) { $seen[$k] = $true; [void]$chosen.Add($byTag[$k]) }
    }
    $ex = @($Exclude | ForEach-Object { Resolve-CapstoneModelKey -Reference $_ })
    $final = @($chosen | Where-Object { $ex -notcontains (Resolve-CapstoneModelKey -Reference $_.tag) })
    $bytes = 0L; foreach ($m in $final) { if ($m.bytes) { $bytes += [int64]$m.bytes } }
    return [pscustomobject]@{ Models = $final; Refused = $refused; TotalBytes = $bytes }
}

function Test-LabInternet {
    <#  Can this machine reach the Ollama model registry? DNS first, then TCP 443.  #>
    param([string]$HostName = 'registry.ollama.ai', [int]$TimeoutMs = 5000)
    try { $addr = @([Net.Dns]::GetHostAddresses($HostName)) } catch { return [pscustomobject]@{ Online = $false; Category = 'NO_INTERNET_CONNECTION'; Detail = ("DNS cannot resolve {0}: {1}" -f $HostName, $_.Exception.InnerException.Message) } }
    if ($addr.Count -eq 0) { return [pscustomobject]@{ Online = $false; Category = 'NO_INTERNET_CONNECTION'; Detail = "DNS returned no address for $HostName" } }
    if (-not (Test-LabTcp -HostName $HostName -Port 443 -TimeoutMs $TimeoutMs)) { return [pscustomobject]@{ Online = $false; Category = 'NO_INTERNET_CONNECTION'; Detail = "$HostName resolves but TCP 443 does not connect" } }
    return [pscustomobject]@{ Online = $true; Category = ''; Detail = "$HostName reachable on TCP 443" }
}

function Get-LabManifestPath {
    param([Parameter(Mandatory)][string]$ModelDirectory, [Parameter(Mandatory)][string]$Tag)
    $key = Resolve-CapstoneModelKey -Reference $Tag
    $name, $version = $key.Split(':', 2)
    $parts = @($name.Split('/'))
    if ($parts.Count -eq 1) { $parts = @('library') + $parts }
    return (Join-Path $ModelDirectory ('manifests\registry.ollama.ai\' + ($parts -join '\') + '\' + $version))
}

function Get-LabDigestCheck {
    <#  The installed model's config digest (from its local manifest) against the catalog's.  #>
    param([Parameter(Mandatory)][string]$ModelDirectory, [Parameter(Mandatory)]$CatalogEntry)
    $expected = [string]$CatalogEntry.config_digest
    if (-not $expected) { return [pscustomobject]@{ Result = 'no catalog digest'; Observed = '' } }
    $path = Get-LabManifestPath -ModelDirectory $ModelDirectory -Tag $CatalogEntry.tag
    try { $m = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json } catch { return [pscustomobject]@{ Result = 'unreadable'; Observed = '' } }
    $observed = [string]$m.config.digest
    if ($observed -eq $expected) { return [pscustomobject]@{ Result = 'match'; Observed = $observed } }
    return [pscustomobject]@{ Result = 'differs'; Observed = $observed }
}

function ConvertTo-LabModelRecord {
    <#  Maps one 1.0.3 queue item onto the six states.  Pure.  #>
    param([Parameter(Mandatory)]$Item, [bool]$Selected = $true, [string]$Digest = 'not checked', [string]$Offline = '')
    $attempted = ([int]$Item.Attempts -gt 0)
    $present = ($Item.Status -in 'AlreadyInstalled', 'Downloaded', 'Verified') -or ($Item.Status -eq 'Failed' -and $Item.FailureCategory -eq 'MODEL_VERIFICATION_FAILURE')
    $category = [string]$Item.FailureCategory
    if (-not $present -and -not $attempted -and $Offline) { $category = 'NO_INTERNET_CONNECTION' }
    return [pscustomobject][ordered]@{
        tag = $Item.Tag; kind = $Item.Kind; selected = $Selected
        download_attempted = $attempted; attempts = [int]$Item.Attempts
        download_completed = ($attempted -and $present)
        installed_locally = $present; already_present = ($Item.Status -eq 'AlreadyInstalled' -or ($present -and -not $attempted))
        digest = $Digest
        verified_by_inference = [bool]$Item.InferenceOk; inference_note = [string]$Item.InferenceNote
        gateway_qualified = 'not claimed: only the Inference Gateway qualifies models'
        failure_category = $category; last_error = [string]$Item.LastError
        final = $(if ($Item.InferenceOk) { 'VERIFIED' } elseif ($present) { 'INSTALLED_UNVERIFIED' } elseif ($category -eq 'NO_INTERNET_CONNECTION') { 'NOT_DOWNLOADED_OFFLINE' } elseif ($Item.Status -eq 'InsufficientDisk') { 'NOT_DOWNLOADED_DISK' } else { 'FAILED' })
    }
}

function Invoke-LabModelInstall {
    param([Parameter(Mandatory)]$Run, [Parameter(Mandatory)][string]$BaseUri, [Parameter(Mandatory)][AllowEmptyCollection()][array]$Models,
          [Parameter(Mandatory)][string]$ModelDirectory, [switch]$SkipInference)
    $paths = Get-LabEnginePaths -Run $Run
    $installed = Get-CapstoneInstalledModels -BaseUri $BaseUri
    if (-not $installed.Available) { throw ('Ollama does not answer on {0}: {1}' -f $BaseUri, $installed.Error) }
    $names = @($installed.Models | ForEach-Object { $_.Name })
    $disk = Get-CapstoneDiskFacts -Path $ModelDirectory
    $plan = Get-CapstoneModelPlan -CatalogModels $Models -InstalledNames $names -FreeBytes $disk.FreeBytes
    Write-LabLog ('  {0} model(s) selected, {1} already present; {2}' -f $plan.TotalCount, $plan.AlreadyInstalled, $plan.DiskNote)
    $net = [pscustomobject]@{ Online = $true; Detail = '' }
    if ($plan.PendingCount -gt 0) { $net = Test-LabInternet }
    if (-not $net.Online) {
        Add-LabWarning ('No internet path to the model registry ({0}). Models not yet present were NOT attempted and are reported as not downloaded (offline), not as invalid tags.' -f $net.Detail)
        foreach ($i in @($plan.Items | Where-Object { $_.Status -eq 'Pending' })) { $i.Status = 'NotAttempted'; $i.FailureCategory = 'NO_INTERNET_CONNECTION' }
    }
    $queuePlan = [pscustomobject]@{ Items = @($plan.Items | Where-Object { $_.Status -ne 'NotAttempted' }) }
    $result = Invoke-CapstoneModelQueue -Paths $paths -BaseUri $BaseUri -Plan $queuePlan -SkipInference:$SkipInference
    $records = @()
    $byTag = @{}; foreach ($m in $Models) { $byTag[(Resolve-CapstoneModelKey -Reference $m.tag)] = $m }
    foreach ($i in @($plan.Items)) {
        $entry = $byTag[(Resolve-CapstoneModelKey -Reference $i.Tag)]
        $d = 'not installed'
        if ($i.Status -in 'AlreadyInstalled', 'Downloaded', 'Verified' -or $i.FailureCategory -eq 'MODEL_VERIFICATION_FAILURE') { $d = (Get-LabDigestCheck -ModelDirectory $ModelDirectory -CatalogEntry $entry).Result }
        $records += ConvertTo-LabModelRecord -Item $i -Digest $d -Offline $(if ($net.Online) { '' } else { $net.Detail })
    }
    return [pscustomobject]@{ Records = $records; Online = $net.Online; Internet = $net.Detail; Disk = $plan.DiskNote }
}

Export-ModuleMember -Function @(
    'Get-LabCatalog', 'Get-LabProfileProposal', 'Select-LabModels', 'Test-LabInternet', 'Get-LabManifestPath', 'Get-LabDigestCheck',
    'ConvertTo-LabModelRecord', 'Invoke-LabModelInstall'
)
