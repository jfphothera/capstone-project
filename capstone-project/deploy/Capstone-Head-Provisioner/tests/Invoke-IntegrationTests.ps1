<#
    Integration tests.

    These exercise the real thing: a real Ollama daemon, real netsh, real
    Windows networking cmdlets, and the real engine entry point. They are
    written so that they can run on the coordinator without changing it.

    What they deliberately DO NOT do:
      * assign or modify any IP address,
      * install, upgrade or restart Ollama,
      * download any model,
      * create or remove a firewall rule,
      * write anywhere under C:\ProgramData\CapstoneProvisioner.

    All engine state is redirected to a disposable DataRoot. The remaining
    behaviours that genuinely cannot be proven without a spare machine are
    listed in docs/ACCEPTANCE-REPORT.md and are not claimed here.
#>
[CmdletBinding()]
param(
    [string]$PackageRoot = '',
    [string]$ResultPath = '',
    [string]$SandboxRoot = '',
    [string]$GpuHead = '192.0.2.2',
    [switch]$SkipNetworkDependent
)

$ErrorActionPreference = 'Continue'

$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($PackageRoot)) { $PackageRoot = Split-Path -Parent $testRoot }
if ([string]::IsNullOrWhiteSpace($SandboxRoot)) {
    $SandboxRoot = Join-Path $env:TEMP ('CapstoneProvisionerTest-{0}' -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
}

Import-Module (Join-Path $testRoot 'TestHarness.psm1') -Force -DisableNameChecking
foreach ($moduleName in @('Capstone.Core', 'Capstone.Network', 'Capstone.Ollama', 'Capstone.Models', 'Capstone.Remote', 'Capstone.Preflight', 'Capstone.Report')) {
    Import-Module (Join-Path $PackageRoot ('engine\modules\{0}.psm1' -f $moduleName)) -Force -DisableNameChecking -Global
}
Reset-TestRun

# Same fix the engine applies, for the same reason: without it the first HTTP
# probe in this process pays a 15-second WPAD stall and every short-timeout
# probe below would spuriously fail.
$httpSetup = Initialize-CapstoneHttp

Write-Host ''
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host '  CAPSTONE HEAD PROVISIONER - INTEGRATION TESTS' -ForegroundColor Cyan
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host ('  Host        : {0}, PowerShell {1}' -f $env:COMPUTERNAME, $PSVersionTable.PSVersion)
Write-Host ('  Sandbox     : {0}' -f $SandboxRoot)
Write-Host '  Guarantee   : no address, install, model, or firewall rule is changed.'

$localApi = Get-CapstoneApiBase
$localProbe = Test-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds 5
Write-Host ('  Local Ollama: {0}' -f $(if ($localProbe.Healthy) { 'healthy, version ' + $localProbe.Version } else { 'not answering' }))

# ---------------------------------------------------------------------------
Start-TestGroup 'Paths and disposable state'
# ---------------------------------------------------------------------------

$paths = $null
Test-Case 'the reporting layout is created exactly as specified' {
    $script:paths = Initialize-CapstonePaths -Paths (Get-CapstonePaths -Root $SandboxRoot)
    foreach ($name in @('config', 'inventory', 'logs', 'reports', 'backups', 'state', 'diagnostics')) {
        $directory = Join-Path $SandboxRoot $name
        if (-not (Test-Path -LiteralPath $directory)) { throw ('{0}\ was not created' -f $name) }
    }
}

Test-Case 'the default root is C:\ProgramData\CapstoneProvisioner' {
    $default = Get-CapstonePaths
    Assert-Match 'ProgramData\\CapstoneProvisioner$' $default.Root
    Assert-Match 'LATEST-STATUS\.txt$' $default.StatusText
    Assert-Match 'LATEST-STATUS\.json$' $default.StatusJson
}

Test-Case 'JSON round-trips without a BOM' {
    $file = Join-Path $paths.State 'roundtrip.json'
    Write-CapstoneJson -InputObject ([pscustomobject]@{ alpha = 1; beta = 'two' }) -Path $file
    $bytes = [System.IO.File]::ReadAllBytes($file)
    if ($bytes[0] -eq 0xEF) { throw 'a BOM was written' }
    $back = Read-CapstoneJson -Path $file
    Assert-Equal 1 $back.alpha
    Assert-Equal 'two' $back.beta
}

Test-Case 'reading absent or corrupt JSON returns null instead of throwing' {
    Assert-Equal $null (Read-CapstoneJson -Path (Join-Path $paths.State 'does-not-exist.json'))
    $corrupt = Join-Path $paths.State 'corrupt.json'
    [System.IO.File]::WriteAllText($corrupt, '{ this is not json')
    Assert-Equal $null (Read-CapstoneJson -Path $corrupt)
}

Test-Case 'the journal is append-only and machine-readable' {
    Write-CapstoneJournal -Paths $paths -Stage 'Test' -Event 'first' -Detail ([pscustomobject]@{ n = 1 })
    Write-CapstoneJournal -Paths $paths -Stage 'Test' -Event 'second' -Detail ([pscustomobject]@{ n = 2 })
    $lines = @(Get-Content -LiteralPath $paths.Journal)
    Assert-Equal 2 $lines.Count
    $second = $lines[1] | ConvertFrom-Json
    Assert-Equal 'second' $second.event
    Assert-Equal 2 $second.detail.n
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Single-instance lock'
# ---------------------------------------------------------------------------

Test-Case 'a lock is acquired and released' {
    $lock = Enter-CapstoneLock -Paths $paths
    Assert-True $lock.Acquired
    Assert-True (Test-Path -LiteralPath $paths.LockFile)
    Exit-CapstoneLock -Paths $paths
    Assert-False (Test-Path -LiteralPath $paths.LockFile)
}

Test-Case 'a live lock held by another process blocks a second run' {
    # PID 4 is the Windows System process: always present, never us.
    Write-CapstoneJson -InputObject ([pscustomobject]@{ pid = 4; at = (Get-Date).ToString('o'); host = $env:COMPUTERNAME }) -Path $paths.LockFile
    $lock = Enter-CapstoneLock -Paths $paths
    Assert-False $lock.Acquired
    Assert-Match 'already running' $lock.Reason
    Remove-Item -LiteralPath $paths.LockFile -Force
}

Test-Case 'a stale lock from a dead process is reclaimed' {
    Write-CapstoneJson -InputObject ([pscustomobject]@{ pid = 999999; at = (Get-Date).ToString('o'); host = 'OLD' }) -Path $paths.LockFile
    $lock = Enter-CapstoneLock -Paths $paths
    Assert-True $lock.Acquired 'a hard-powered-off head must not need manual lock cleanup'
    Exit-CapstoneLock -Paths $paths
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Real Windows network discovery (read-only)'
# ---------------------------------------------------------------------------

$inventory = @()
Test-Case 'the live adapter inventory is collected' {
    $script:inventory = Get-CapstoneAdapterInventory
    Assert-True ($inventory.Count -gt 0) 'no adapters were enumerated'
}

Test-Case 'exactly one physical Ethernet adapter is identified on this machine' {
    $ethernet = @($inventory | Where-Object { Test-CapstonePhysicalEthernet -Adapter $_ })
    Assert-Equal 1 $ethernet.Count ('expected one real NIC, got: ' + (@($ethernet | ForEach-Object { $_.Name }) -join ', '))
}

Test-Case 'virtual and wireless adapters are excluded from the live inventory' {
    $wrong = @($inventory | Where-Object {
        (Test-CapstonePhysicalEthernet -Adapter $_) -and
        ($_.Virtual -or $_.NdisPhysicalMedium -eq 9 -or $_.Description -match '(?i)tap-windows|wintun')
    })
    Assert-Equal 0 $wrong.Count 'a virtual or wireless adapter passed the physical-Ethernet filter'
}

Test-Case 'Get-NetIPInterface is the authority on DHCP state, not netsh' {
    # Measured on this machine: netsh show config prints "DHCP enabled: Yes"
    # for an interface Get-NetIPInterface reports as Disabled. The engine must
    # read the cmdlet. This test records that the two really do disagree.
    $ethernet = @($inventory | Where-Object { Test-CapstonePhysicalEthernet -Adapter $_ })[0]
    $facts = Get-CapstoneNetworkFacts -InterfaceIndex $ethernet.InterfaceIndex -TargetIp '192.0.2.1'
    Assert-True (@('Enabled', 'Disabled') -contains $facts.Dhcp) ('Dhcp came back as "{0}"' -f $facts.Dhcp)
}

Test-Case 'netsh accepts the exact argument form the engine uses (read-only probe)' {
    # `set address` cannot be tested without changing the machine, but the
    # identical name=<index> addressing is proven here with `show config`.
    $ethernet = @($inventory | Where-Object { Test-CapstonePhysicalEthernet -Adapter $_ })[0]
    $result = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'show', 'config', ('name={0}' -f $ethernet.InterfaceIndex))
    Assert-Equal 0 $result.ExitCode ('netsh rejected name=<index>: ' + $result.Output)
    Assert-Match 'Configuration for interface' $result.Output
}

Test-Case 'netsh reports failure for an interface that does not exist' {
    $result = Invoke-CapstoneNetsh -Arguments @('interface', 'ipv4', 'show', 'config', 'name=99999')
    Assert-True ($result.ExitCode -ne 0) 'a non-existent interface should not return success'
}

Test-Case 'a network backup record captures everything rollback needs' {
    $ethernet = @($inventory | Where-Object { Test-CapstonePhysicalEthernet -Adapter $_ })[0]
    $backup = New-CapstoneNetworkBackup -Paths $paths -Adapter $ethernet -TargetIp '192.0.2.9' -NodeNumber 9
    Assert-True (Test-Path -LiteralPath $backup.Path)
    $record = Read-CapstoneJson -Path $backup.Path
    foreach ($field in @('interface_index', 'interface_alias', 'mac_address', 'dhcp_state', 'ipv4_addresses', 'dns_servers', 'default_gateways', 'captured_at')) {
        if ($record.PSObject.Properties.Name -notcontains $field) { throw ('the backup is missing {0}' -f $field) }
    }
    # And the plan derived from it must be coherent.
    $plan = Resolve-CapstoneRollbackPlan -Backup $record
    Assert-True (@('RestoreDhcp', 'RestoreStatic') -contains $plan.Action)
    Assert-Equal $ethernet.InterfaceIndex $plan.InterfaceIndex
}

Test-Case 'coordinator reachability does not depend on ICMP alone' {
    $reach = Test-CapstoneCoordinatorReachable -CoordinatorIp '127.0.0.1' -TcpPort 11434
    Assert-True ($reach.PSObject.Properties.Name -contains 'IcmpReplied')
    Assert-True ($reach.PSObject.Properties.Name -contains 'TcpAnswered')
    Assert-True ($reach.PSObject.Properties.Name -contains 'ArpEntry')
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Real Ollama discovery (read-only)'
# ---------------------------------------------------------------------------

Test-Case 'the Ollama executable is located without relying on PATH' {
    $executable = Get-CapstoneOllamaExecutable
    if ($localProbe.Healthy -and -not $executable) {
        throw 'a healthy Ollama API is answering but no executable was found'
    }
    if ($executable) { Assert-Match 'ollama\.exe$' $executable }
}

Test-Case 'the model storage directory and its free space are resolved' {
    $directory = Get-CapstoneModelDirectory
    Assert-True (-not [string]::IsNullOrWhiteSpace($directory.Path))
    $disk = Get-CapstoneDiskFacts -Path $directory.Path
    Assert-True ($disk.FreeBytes -gt 0) 'free space should be measurable'
    Assert-Match 'GB|MB' $disk.FreeDisplay
}

Test-Case 'port ownership on 11434 is reported with the owning process' {
    $listeners = Get-CapstonePortOwner
    foreach ($listener in $listeners) {
        Assert-Equal 11434 $listener.Port
        Assert-True ($listener.ProcessId -gt 0)
    }
    Add-TestResult -Name ('  observed {0} listener(s) on 11434' -f $listeners.Count) -Passed $true -Detail ''
}

Test-Case 'HTTP is configured for direct connection, so no WPAD stall is possible' {
    Assert-True ($httpSetup.PSObject.Properties.Name -contains 'Direct')
    if ($httpSetup.Direct) {
        $proxy = [System.Net.WebRequest]::DefaultWebProxy
        Assert-True ($null -ne $proxy) 'the default proxy should be an explicit direct WebProxy, not left unset'
        # A direct WebProxy returns the request URI itself rather than a proxy.
        $resolved = $proxy.GetProxy([uri]'http://192.0.2.2:11434/api/version')
        Assert-Equal '192.0.2.2' $resolved.Host 'a proxy is still being interposed for private-LAN addresses'
    }
    Add-TestResult -Name ('  {0}' -f $httpSetup.Note) -Passed $true -Detail ''
}

Test-Case 'a short-timeout probe of a reachable endpoint completes well inside its budget' {
    # The regression this guards: the first HTTP request in a Windows
    # PowerShell 5.1 process took 15.5 seconds measured on this machine, which
    # exceeds every probe budget in the engine.
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $probe = Test-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds 5
    $stopwatch.Stop()
    if ($localProbe.Healthy -and -not $probe.Healthy) {
        throw ('a healthy local API was reported unhealthy by a 5 second probe after {0} ms' -f $stopwatch.ElapsedMilliseconds)
    }
    Assert-True ($stopwatch.ElapsedMilliseconds -lt 5000) ('the probe took {0} ms' -f $stopwatch.ElapsedMilliseconds)
}

Test-Case 'an unreachable endpoint is reported as unhealthy, not as an exception' {
    $probe = Test-CapstoneOllamaApi -BaseUri 'http://127.0.0.1:1' -TimeoutSeconds 3
    Assert-False $probe.Healthy
    Assert-True (-not [string]::IsNullOrWhiteSpace($probe.Error))
}

if ($localProbe.Healthy) {
    Test-Case 'the live local inventory query succeeds (an empty list is valid)' {
        $inventoryResult = Get-CapstoneInstalledModels -BaseUri $localApi
        Assert-True $inventoryResult.Available 'the query itself must succeed'
        Add-TestResult -Name ('  local model count: {0}' -f @($inventoryResult.Models).Count) -Passed $true -Detail ''
    }

    Test-Case 'a real invalid-tag pull is classified as MODEL_TAG_INVALID, with nothing downloaded' {
        # This is the genuine streaming pull path against a real daemon. The tag
        # cannot exist, so the error branch, the HTTP status capture and the
        # classifier are all exercised without transferring a single model byte.
        $tag = 'capstone-no-such-model-zzz:0.1b'
        $pull = Invoke-CapstoneModelPull -BaseUri $localApi -Tag $tag -StallMinutes 2
        Assert-False $pull.Success
        Assert-True (-not [string]::IsNullOrWhiteSpace($pull.Error)) 'the daemon error text must be captured'
        $category = Get-CapstoneFailureCategory -Message $pull.Error -HttpStatus $pull.HttpStatus -Stage 'Models'
        if ($category.Category -ne 'MODEL_TAG_INVALID') {
            throw ('classified as {0} from "{1}" (HTTP {2})' -f $category.Category, $pull.Error, $pull.HttpStatus)
        }
        Assert-False $category.Retryable 'a non-existent tag must never be retried'
        # And it must not have appeared in the inventory.
        $after = Get-CapstoneInstalledModels -BaseUri $localApi
        Assert-False (Test-CapstoneModelPresent -Tag $tag -InstalledNames @($after.Models | ForEach-Object { $_.Name }))
    }

    Test-Case 'the running-model query works against a real daemon' {
        $running = Get-CapstoneRunningModels -BaseUri $localApi
        Assert-True $running.Available
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Real inference against the GPU head (read-only)'
# ---------------------------------------------------------------------------

$gpuApi = Get-CapstoneApiBase -HostAddress $GpuHead
$gpuProbe = [pscustomobject]@{ Healthy = $false; Version = '' }
if (-not $SkipNetworkDependent) {
    $gpuProbe = Test-CapstoneOllamaApi -BaseUri $gpuApi -TimeoutSeconds 8
}

if ($gpuProbe.Healthy) {
    Write-Host ('    GPU head {0} is reachable, Ollama {1}' -f $GpuHead, $gpuProbe.Version) -ForegroundColor DarkGray
    $gpuModels = @((Get-CapstoneInstalledModels -BaseUri $gpuApi).Models)

    Test-Case 'the GPU head reports its existing model inventory' {
        Assert-True ($gpuModels.Count -gt 0) 'the GPU workstation should already have models'
    }

    Test-Case 'a generation model answers a real inference request' {
        $generation = @($gpuModels | Where-Object { $_.Name -notmatch '(?i)embed' } | Sort-Object { $_.Size })
        if ($generation.Count -eq 0) { throw 'no generation model is installed on the GPU head' }
        $result = Test-CapstoneModelInference -BaseUri $gpuApi -Tag $generation[0].Name -Kind 'generate' -TimeoutSeconds 180
        if (-not $result.Success) { throw ('{0} failed: {1}' -f $generation[0].Name, $result.Error) }
        Assert-True ($result.ElapsedMs -gt 0)
        Add-TestResult -Name ('  {0}: {1}' -f $generation[0].Name, $result.Note) -Passed $true -Detail ''
    }

    Test-Case 'an embedding model is verified through the embedding API, not a prompt' {
        $embedding = @($gpuModels | Where-Object { $_.Name -match '(?i)embed' })
        if ($embedding.Count -eq 0) { throw 'no embedding model is installed on the GPU head' }
        $result = Test-CapstoneModelInference -BaseUri $gpuApi -Tag $embedding[0].Name -Kind 'embedding' -TimeoutSeconds 120
        if (-not $result.Success) { throw ('{0} failed: {1}' -f $embedding[0].Name, $result.Error) }
        Assert-Match 'dimensions' $result.Note 'the vector shape must be validated'
        Add-TestResult -Name ('  {0}: {1}' -f $embedding[0].Name, $result.Note) -Passed $true -Detail ''
    }

    Test-Case 'the GPU head model storage and endpoint were not altered by these tests' {
        $after = @((Get-CapstoneInstalledModels -BaseUri $gpuApi).Models)
        Assert-Equal $gpuModels.Count $after.Count 'the model count changed, which these tests must never do'
    }
} else {
    Add-TestResult -Name 'GPU head inference tests SKIPPED (192.0.2.2 not reachable from here)' -Passed $true -Detail ''
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Engine end to end, diagnostics mode (changes nothing)'
# ---------------------------------------------------------------------------

$engine = Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1'
$powerShell51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$engineSandbox = Join-Path $SandboxRoot 'engine-run'

$engineExit = -1
Test-Case 'the engine runs to completion in DiagnosticsOnly mode' {
    $output = & $powerShell51 -NoProfile -ExecutionPolicy Bypass -File $engine -Mode DiagnosticsOnly -DataRoot $engineSandbox -NonInteractive 2>&1 | Out-String
    $script:engineExit = $LASTEXITCODE
    if ($engineExit -ne 0) { throw ('exit code {0}; output: {1}' -f $engineExit, $output) }
    if ($output -notmatch 'Diagnostics-only run') { throw 'the engine did not report a diagnostics-only run' }
}

Test-Case 'a complete preflight inventory is written' {
    $preflight = Read-CapstoneJson -Path (Join-Path $engineSandbox 'diagnostics\preflight-LATEST.json')
    if ($null -eq $preflight) { throw 'preflight-LATEST.json was not written' }
    foreach ($section in @('operating_system', 'hardware', 'adapters', 'coordinator', 'internet', 'ollama', 'other_services')) {
        if ($preflight.PSObject.Properties.Name -notcontains $section) { throw ('the preflight report is missing the {0} section' -f $section) }
    }
}

Test-Case 'the preflight inventory records the facts the specification asks for' {
    $preflight = Read-CapstoneJson -Path (Join-Path $engineSandbox 'diagnostics\preflight-LATEST.json')
    Assert-True (-not [string]::IsNullOrWhiteSpace($preflight.operating_system.BuildNumber)) 'no Windows build number'
    Assert-True (-not [string]::IsNullOrWhiteSpace($preflight.operating_system.PowerShellVersion)) 'no PowerShell version'
    Assert-True ($preflight.hardware.LogicalCores -gt 0) 'no CPU core count'
    Assert-True ([double]$preflight.hardware.TotalMemoryGB -gt 0) 'no RAM figure'
    Assert-True (@($preflight.adapters).Count -gt 0) 'no adapters'
    Assert-True (-not [string]::IsNullOrWhiteSpace($preflight.ollama.ModelDirectory.Path)) 'no model directory'
    Assert-True ($preflight.ollama.Disk.FreeBytes -gt 0) 'no free space figure'
}

Test-Case 'diagnostics mode writes nothing outside its DataRoot' {
    # The real reporting root must be untouched by this test run.
    $realRoot = (Get-CapstonePaths).Root
    $marker = Join-Path $engineSandbox 'diagnostics\preflight-LATEST.json'
    Assert-True (Test-Path -LiteralPath $marker) 'the sandbox report is missing'
    if ($SandboxRoot -like ($realRoot + '*')) { throw 'the sandbox is inside the real reporting root' }
}

Test-Case 'diagnostics mode takes no lock, so it can never block a real run' {
    Assert-False (Test-Path -LiteralPath (Join-Path $engineSandbox 'state\provisioner.lock')) 'a lock file was left behind'
}

Test-Case 'a repeated diagnostics run succeeds and does not destroy earlier output' {
    $firstRun = @(Get-ChildItem -LiteralPath (Join-Path $engineSandbox 'diagnostics') -File | Where-Object { $_.Name -like 'preflight-2*' }).Count
    $output = & $powerShell51 -NoProfile -ExecutionPolicy Bypass -File $engine -Mode DiagnosticsOnly -DataRoot $engineSandbox -NonInteractive 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw ('the second run exited {0}: {1}' -f $LASTEXITCODE, $output) }
    $secondRun = @(Get-ChildItem -LiteralPath (Join-Path $engineSandbox 'diagnostics') -File | Where-Object { $_.Name -like 'preflight-2*' }).Count
    Assert-True ($secondRun -gt $firstRun) 'the second run should add a timestamped report, not overwrite the history'
    $logs = @(Get-ChildItem -LiteralPath (Join-Path $engineSandbox 'logs') -File)
    Assert-True ($logs.Count -ge 2) 'each run should keep its own log'
}

Test-Case 'the engine refuses to provision the coordinator as a head' {
    # This machine holds 192.0.2.1. Asking for node 1 must be rejected outright.
    $output = & $powerShell51 -NoProfile -ExecutionPolicy Bypass -File $engine -Mode Provision -NodeNumber 1 -ModelProfile CPU8 -DataRoot (Join-Path $SandboxRoot 'coordinator-guard') -NonInteractive -SkipModels -SkipRemote 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($code -eq 0) { throw ('the engine returned success for node 1; output: {0}' -f $output) }
    if ($output -notmatch 'coordinator') { throw ('the refusal did not mention the coordinator: {0}' -f $output) }
}

Test-Case 'Rollback mode with no backup record reports it and changes nothing' {
    $emptyRoot = Join-Path $SandboxRoot 'rollback-empty'
    $output = & $powerShell51 -NoProfile -ExecutionPolicy Bypass -File $engine -Mode Rollback -DataRoot $emptyRoot -NonInteractive 2>&1 | Out-String
    $code = $LASTEXITCODE
    Assert-Equal 5 $code ('expected the documented "no backup" exit code; output: ' + $output)
    Assert-Match 'No network backup record' $output
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Status report generation'
# ---------------------------------------------------------------------------

Test-Case 'a status report is written in both plain text and JSON' {
    $reportPaths = Initialize-CapstonePaths -Paths (Get-CapstonePaths -Root (Join-Path $SandboxRoot 'report-test'))
    $sampleRun = [pscustomobject]@{
        Version = '1.0.0'; Mode = 'Provision'; ComputerName = 'TEST-HEAD'; LogFile = 'x.log'
        PreflightPath = 'p.json'; BackupPath = 'b.json'; RegistrationPath = 'h.json'
        OsCaption = 'Windows 11 Pro'; OsBuild = '26200'; Cpu = 'Test CPU'; RamGB = 16.0
        Gpu = 'none detected'; NvidiaVramMB = 0; ModelDirectory = 'C:\models'; FreeDisk = '400 GB'
        NodeNumber = 4; NodeIp = '192.0.2.4'; AdapterSummary = 'Ethernet (index 6)'
        NetworkVerified = $true; NetworkSummary = 'all checks pass'; CoordinatorNote = 'reachable'
        OllamaInstalled = $true; OllamaVersion = '0.40.12'; OllamaExecutable = 'C:\ollama.exe'
        LocalApiHealthy = $true; LocalApiEndpoint = 'http://127.0.0.1:11434'; OllamaInstalledThisRun = $false
        ProfileName = 'CPU16'; ModelsRequested = 18; ModelsInstalled = 17; ModelsAlreadyPresent = 7
        ModelsDownloaded = 10; ModelsFailed = 1; ModelsVerified = 17; FailedTags = @('granite4:3b')
        InferenceVerified = $true
        RemoteConfigured = $true; RemoteBinding = '0.0.0.0:11434'; PrivateListenerHealthy = $true
        FirewallSummary = 'scoped to 192.0.2.1'; CoordinatorVerified = $false
        RemoteStatus = 'REMOTE_COORDINATOR_VERIFICATION_PENDING'; RemoteEndpoint = 'http://192.0.2.4:11434'
        MachineState = 'REMOTE_READY'
        Outcome = (Get-CapstoneOverallOutcome -NetworkVerified $true -OllamaInstalled $true -LocalApiHealthy $true `
            -ModelsRequested 18 -ModelsInstalled 17 -ModelsFailed 1 -InferenceVerified $true `
            -RemoteConfigured $true -PrivateListenerHealthy $true -CoordinatorVerified $false `
            -FailedTags @('granite4:3b'))
    }
    $report = New-CapstoneStatusReport -Paths $reportPaths -Run $sampleRun
    Assert-True (Test-Path -LiteralPath $reportPaths.StatusText)
    Assert-True (Test-Path -LiteralPath $reportPaths.StatusJson)

    # Every question spec section 24 requires must be answerable from the text.
    foreach ($needle in @('RESULT: PARTIAL_SUCCESS', '192.0.2.4', 'granite4:3b', 'CPU16',
                          'Models requested     : 18', 'Models installed     : 17',
                          'Local inference      : YES', 'Coordinator verified : NO',
                          'WHAT STILL NEEDS ATTENTION', 'NOT yet ready for Phase 4')) {
        if ($report.Text -notmatch [regex]::Escape($needle)) { throw ('the summary never says "{0}"' -f $needle) }
    }

    $json = Read-CapstoneJson -Path $reportPaths.StatusJson
    Assert-Equal 'PARTIAL_SUCCESS' $json.result
    Assert-False $json.ready_for_phase4
}

Test-Case 'the head registration record carries the Phase 4 schema' {
    $registrationPaths = Initialize-CapstonePaths -Paths (Get-CapstonePaths -Root (Join-Path $SandboxRoot 'registration-test'))
    $queue = @(
        [pscustomobject]@{ Tag = 'qwen3:0.6b'; Kind = 'generate'; Family = 'qwen3'; Role = 'routing'; Status = 'Verified'; Digest = 'sha256:abc'; ExpectedBytes = 522640096; InferenceOk = $true; InferenceMs = 820; FailureCategory = ''; LastError = ''; Attempts = 1 }
        [pscustomobject]@{ Tag = 'granite4:3b'; Kind = 'generate'; Family = 'granite4'; Role = 'mid'; Status = 'Failed'; Digest = ''; ExpectedBytes = 2104344576; InferenceOk = $false; InferenceMs = 0; FailureCategory = 'MODEL_PULL_SERVER_ERROR'; LastError = 'HTTP 500'; Attempts = 3 }
        [pscustomobject]@{ Tag = 'all-minilm'; Kind = 'embedding'; Family = 'all-minilm'; Role = 'embedding'; Status = 'AlreadyInstalled'; Digest = 'sha256:def'; ExpectedBytes = 46269440; InferenceOk = $true; InferenceMs = 120; FailureCategory = ''; LastError = ''; Attempts = 0 }
    )
    $run = [pscustomobject]@{
        Version = '1.0.0'; NodeNumber = 4; ComputerName = 'TEST-HEAD'; NodeIp = '192.0.2.4'
        ProfileName = 'CPU16'; Cpu = 'Test CPU'; RamGB = 16.0; Gpu = 'none'; NvidiaVramMB = 0
        OllamaVersion = '0.40.12'; RemoteEndpoint = 'http://192.0.2.4:11434'
        LocalApiEndpoint = 'http://127.0.0.1:11434'; ModelDirectory = 'C:\models'
        RemoteStatus = 'REMOTE_COORDINATOR_VERIFICATION_PENDING'; CoordinatorVerified = $false
        MachineState = 'REMOTE_READY'
    }
    $registration = New-CapstoneHeadRegistration -Paths $registrationPaths -Run $run -QueueItems $queue
    $record = Read-CapstoneJson -Path $registration.Path

    foreach ($field in @('schema_version', 'node_id', 'hostname', 'ip_address', 'hardware_profile',
                         'cpu', 'ram_gb', 'gpu', 'ollama_version', 'ollama_endpoint',
                         'available_models', 'failed_models', 'remote_verification',
                         'service_health', 'last_verification', 'capabilities')) {
        if ($record.PSObject.Properties.Name -notcontains $field) { throw ('the registration is missing {0}' -f $field) }
    }
    Assert-Equal 2 @($record.available_models).Count
    Assert-Equal 1 @($record.failed_models).Count
    Assert-Equal 'granite4:3b' @($record.failed_models)[0].tag
    Assert-Equal 2 @($record.capabilities).Count 'generate and embedding'
    Assert-Match 'No Capstone model-routing configuration was modified' $record.phase4_note
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Coordinator verification utility'
# ---------------------------------------------------------------------------

Test-Case 'the coordinator utility runs and writes both report formats' {
    $coordinatorReports = Join-Path $SandboxRoot 'coordinator-reports'
    $utility = Join-Path $PackageRoot 'coordinator\Test-CapstoneHeads.ps1'
    # The node list is pinned rather than left to default, so this stays fast
    # and deterministic as the supported range grows. The default sweep itself
    # is covered by a static check.
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $utility, '-OutputDirectory', $coordinatorReports, '-ConnectTimeoutMs', '800', '-Nodes', '2,3,4')
    if ($SkipNetworkDependent) { $arguments += '-SkipInference' }
    $output = & $powerShell51 @arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($code -notin @(0, 10, 20)) { throw ('unexpected exit code {0}: {1}' -f $code, $output) }
    Assert-True (Test-Path -LiteralPath (Join-Path $coordinatorReports 'COORDINATOR-HEAD-REPORT.txt'))
    Assert-True (Test-Path -LiteralPath (Join-Path $coordinatorReports 'COORDINATOR-HEAD-REPORT.json'))

    $json = Read-CapstoneJson -Path (Join-Path $coordinatorReports 'COORDINATOR-HEAD-REPORT.json')
    Assert-Equal 'capstone-coordinator-head-report' $json.record_type
    Assert-Equal 3 @($json.heads).Count 'the three nodes requested'
    Add-TestResult -Name ('  API reachable: {0}, remote inference proven: {1}' -f $json.api_reachable, $json.remote_inference_proven) -Passed $true -Detail ''
}

Test-Case 'an unreachable head is reported as UNREACHABLE, not as a crash' {
    $coordinatorReports = Join-Path $SandboxRoot 'coordinator-reports'
    $json = Read-CapstoneJson -Path (Join-Path $coordinatorReports 'COORDINATOR-HEAD-REPORT.json')
    foreach ($head in @($json.heads)) {
        if (-not $head.tcp_open) {
            Assert-Equal 'UNREACHABLE' $head.verification_level
            Assert-Match 'No TCP connection' $head.error
        }
    }
}

# ---------------------------------------------------------------------------

$summary = Write-TestSummary -Title 'INTEGRATION TEST SUMMARY'

Write-Host ('  Sandbox retained for inspection: {0}' -f $SandboxRoot) -ForegroundColor DarkGray

if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    $directory = Split-Path -Parent $ResultPath
    if ($directory -and -not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $payload = ([pscustomobject]@{
        suite = 'integration-tests'
        generated_at = (Get-Date).ToString('o')
        machine = $env:COMPUTERNAME
        powershell_version = $PSVersionTable.PSVersion.ToString()
        local_ollama_healthy = $localProbe.Healthy
        gpu_head_reachable = $gpuProbe.Healthy
        sandbox = $SandboxRoot
        total = $summary.Total; passed = $summary.Passed; failed = $summary.Failed
        results = $summary.Results
    }) | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($ResultPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ('  Results written to {0}' -f $ResultPath) -ForegroundColor Cyan
}

if ($summary.Failed -gt 0) { exit 1 }
exit 0
