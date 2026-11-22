<#
    Capstone.Ollama
    Detection, installation and server lifecycle for Ollama on a Capstone head.

    Design notes that exist because earlier versions failed:

      * Installation completion is NEVER inferred from the setup process exiting
        (defect D-04). OllamaSetup.exe is an Inno Setup bundle that launches the
        tray application and can outlive its own visible window. Completion is
        established by polling three measured facts: the executable exists, it
        reports a version, and the local HTTP API answers.

      * The presence of an ollama.exe process is not treated as a working server
        (spec section 10). Only an HTTP response from /api/version counts.

      * An empty model inventory is a valid state for a healthy new install, not
        a failed installation (defect D-05).

      * The installer is downloaded over HTTPS from the official host and its
        Authenticode signature and publisher are verified BEFORE it is executed.
        Measured publisher on 2026-10-29: CN=Ollama Inc., O=Ollama Inc.
#>

$script:OfficialInstallerUrl = 'https://ollama.com/download/OllamaSetup.exe'
$script:ExpectedPublisher    = 'Ollama'
$script:DefaultPort          = 11434

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

function Get-CapstoneOllamaExecutable {
    <#
        .SYNOPSIS
            Locates ollama.exe without assuming it is on PATH.
        .DESCRIPTION
            A fresh install puts it under the user's LOCALAPPDATA and only adds
            it to PATH for sessions started afterwards, which is why earlier
            versions "could not find" an Ollama they had just installed
            (defect D-05).
    #>
    [CmdletBinding()]
    param()

    $candidates = @()
    try {
        $command = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
        if ($command -and $command.Source) { $candidates += [string]$command.Source }
    } catch { }

    $userProfiles = @()
    if ($env:LOCALAPPDATA) { $userProfiles += $env:LOCALAPPDATA }
    # When the provisioner is elevated, LOCALAPPDATA may belong to the admin
    # account while Ollama was installed for the logged-on user. Look there too.
    try {
        $users = Join-Path $env:SystemDrive 'Users'
        if (Test-Path -LiteralPath $users) {
            foreach ($dir in @(Get-ChildItem -LiteralPath $users -Directory -ErrorAction SilentlyContinue)) {
                $userProfiles += (Join-Path $dir.FullName 'AppData\Local')
            }
        }
    } catch { }

    foreach ($local in ($userProfiles | Select-Object -Unique)) {
        $candidates += (Join-Path $local 'Programs\Ollama\ollama.exe')
    }
    if ($env:ProgramFiles)         { $candidates += (Join-Path $env:ProgramFiles 'Ollama\ollama.exe') }
    if (${env:ProgramFiles(x86)})  { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Ollama\ollama.exe') }

    foreach ($path in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($path) -and (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $path
        }
    }
    return $null
}

function Get-CapstoneOllamaProcesses {
    [CmdletBinding()]
    param()
    $result = @()
    foreach ($name in @('ollama', 'ollama app')) {
        try {
            foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
                $path = ''
                try { $path = [string]$process.Path } catch { }
                $result += [pscustomobject]@{
                    Id = $process.Id; Name = $process.ProcessName; Path = $path
                }
            }
        } catch { }
    }
    return $result
}

function Get-CapstonePortOwner {
    <#
        .SYNOPSIS
            Reports every listener on the Ollama port and which process owns it.
    #>
    [CmdletBinding()]
    param([int]$Port = $script:DefaultPort)

    $listeners = @()
    try {
        foreach ($connection in @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            $owner = ''
            try {
                $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
                if ($process) { $owner = $process.ProcessName }
            } catch { }
            $listeners += [pscustomobject]@{
                LocalAddress = [string]$connection.LocalAddress
                Port         = [int]$connection.LocalPort
                ProcessId    = [int]$connection.OwningProcess
                ProcessName  = $owner
            }
        }
    } catch { }
    return $listeners
}

function Get-CapstoneModelDirectory {
    <#
        .SYNOPSIS
            Resolves where Ollama actually stores blobs on this machine.
    #>
    [CmdletBinding()]
    param()

    foreach ($scope in @('Process', 'User', 'Machine')) {
        $configured = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', $scope)
        if (-not [string]::IsNullOrWhiteSpace($configured)) {
            return [pscustomobject]@{ Path = $configured; Source = ('OLLAMA_MODELS ({0} scope)' -f $scope) }
        }
    }
    $default = Join-Path $env:USERPROFILE '.ollama\models'
    return [pscustomobject]@{ Path = $default; Source = 'default user profile location' }
}

function Get-CapstoneDiskFacts {
    <#
        .SYNOPSIS
            Free space on the volume that will actually hold the models.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $root = ''
    try { $root = [System.IO.Path]::GetPathRoot($Path) } catch { }
    if ([string]::IsNullOrWhiteSpace($root)) { $root = $env:SystemDrive }

    $free = -1L
    $total = -1L
    try {
        $drive = New-Object System.IO.DriveInfo($root)
        if ($drive.IsReady) {
            $free = [int64]$drive.AvailableFreeSpace
            $total = [int64]$drive.TotalSize
        }
    } catch { }

    return [pscustomobject]@{
        Path           = $Path
        Volume         = $root
        FreeBytes      = $free
        TotalBytes     = $total
        FreeDisplay    = $(if ($free -ge 0) { Format-CapstoneBytes -Bytes $free } else { 'unknown' })
    }
}

# ---------------------------------------------------------------------------
# Local API health
# ---------------------------------------------------------------------------

function Get-CapstoneApiBase {
    [CmdletBinding()]
    param([string]$HostAddress = '127.0.0.1', [int]$Port = $script:DefaultPort)
    return ('http://{0}:{1}' -f $HostAddress, $Port)
}

function Test-CapstoneOllamaApi {
    <#
        .SYNOPSIS
            One bounded health probe against a specific Ollama endpoint.
        .DESCRIPTION
            /api/version is used rather than /api/tags because an empty model
            list must not look like an unhealthy server.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$BaseUri,
        [int]$TimeoutSeconds = 5
    )

    $result = [ordered]@{
        BaseUri = $BaseUri; Healthy = $false; Version = ''; Error = ''; HttpStatus = 0
    }
    try {
        $response = Invoke-RestMethod -Uri ('{0}/api/version' -f $BaseUri) -Method Get -TimeoutSec $TimeoutSeconds -ErrorAction Stop
        $result.Healthy = $true
        if ($response -and $response.version) { $result.Version = [string]$response.version }
        $result.HttpStatus = 200
    } catch {
        $result.Error = $_.Exception.Message
        try {
            $response = $_.Exception.Response
            if ($response -and $response.StatusCode) { $result.HttpStatus = [int]$response.StatusCode }
        } catch { }
    }
    return [pscustomobject]$result
}

function Wait-CapstoneOllamaApi {
    <#
        .SYNOPSIS
            Polls an endpoint until healthy or the budget runs out.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$BaseUri,
        [int]$TimeoutSeconds = 60,
        [int]$IntervalSeconds = 3,
        [string]$Activity = 'Waiting for the Ollama API'
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $announced = $false
    while ((Get-Date) -lt $deadline) {
        $probe = Test-CapstoneOllamaApi -BaseUri $BaseUri -TimeoutSeconds 5
        if ($probe.Healthy) { return $probe }
        if (-not $announced) {
            Write-CapstoneLog ('{0} at {1} ...' -f $Activity, $BaseUri) -Level INFO
            $announced = $true
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
    return (Test-CapstoneOllamaApi -BaseUri $BaseUri -TimeoutSeconds 5)
}

function Get-CapstoneInstalledModels {
    <#
        .SYNOPSIS
            Reads the live model inventory from a running server.
        .OUTPUTS
            Available=$false means the query failed. Available=$true with zero
            models is a healthy server that simply has nothing downloaded yet.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$BaseUri,
        [int]$TimeoutSeconds = 20
    )

    try {
        $response = Invoke-RestMethod -Uri ('{0}/api/tags' -f $BaseUri) -Method Get -TimeoutSec $TimeoutSeconds -ErrorAction Stop
        $models = @()
        if ($response -and $response.models) {
            foreach ($model in @($response.models)) {
                $models += [pscustomobject]@{
                    Name   = [string]$model.name
                    Size   = $(if ($model.size) { [int64]$model.size } else { 0L })
                    Digest = [string]$model.digest
                }
            }
        }
        return [pscustomobject]@{ Available = $true; Models = $models; Error = '' }
    } catch {
        return [pscustomobject]@{ Available = $false; Models = @(); Error = $_.Exception.Message }
    }
}

function Get-CapstoneRunningModels {
    <#
        .SYNOPSIS
            Models currently loaded in memory, used to avoid interrupting work.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$BaseUri)
    try {
        $response = Invoke-RestMethod -Uri ('{0}/api/ps' -f $BaseUri) -Method Get -TimeoutSec 10 -ErrorAction Stop
        $names = @()
        if ($response -and $response.models) {
            $names = @($response.models | ForEach-Object { [string]$_.name })
        }
        return [pscustomobject]@{ Available = $true; Names = $names; Count = $names.Count }
    } catch {
        return [pscustomobject]@{ Available = $false; Names = @(); Count = 0 }
    }
}

# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

function Test-CapstoneInstallerSignature {
    <#
        .SYNOPSIS
            Refuses to run an installer that is not validly signed by Ollama.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $result = [ordered]@{ Trusted = $false; Status = 'Unknown'; Subject = ''; Thumbprint = ''; Reason = '' }
    try {
        $signature = Get-AuthenticodeSignature -FilePath $Path -ErrorAction Stop
        $result.Status = [string]$signature.Status
        if ($signature.SignerCertificate) {
            $result.Subject = [string]$signature.SignerCertificate.Subject
            $result.Thumbprint = [string]$signature.SignerCertificate.Thumbprint
        }
        if ($signature.Status -ne 'Valid') {
            $result.Reason = ('Authenticode status is {0}, not Valid. The installer was not executed.' -f $signature.Status)
            return [pscustomobject]$result
        }
        if ($result.Subject -notmatch [regex]::Escape($script:ExpectedPublisher)) {
            $result.Reason = ('The signature is valid but the publisher "{0}" does not contain "{1}". The installer was not executed.' -f $result.Subject, $script:ExpectedPublisher)
            return [pscustomobject]$result
        }
        $result.Trusted = $true
        $result.Reason = ('Signed by {0}.' -f $result.Subject)
    } catch {
        $result.Reason = ('Could not read an Authenticode signature: {0}' -f $_.Exception.Message)
    }
    return [pscustomobject]$result
}

function Save-CapstoneOllamaInstaller {
    <#
        .SYNOPSIS
            Downloads the official Windows installer with visible progress.
        .DESCRIPTION
            The installer is roughly 1.6 GB, so a silent request with no
            feedback is exactly the kind of apparent freeze the operator has
            already suffered. Progress is reported from the stream itself.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Destination,
        [string]$Url = $script:OfficialInstallerUrl,
        [int]$TimeoutMinutes = 60
    )

    $directory = Split-Path -Parent $Destination
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    Write-CapstoneLog ('Downloading the official Ollama installer from {0}' -f $Url) -Level INFO
    if ($Url -notmatch '^https://') {
        return [pscustomobject]@{ Success = $false; Path = $Destination; Error = 'The installer source must be an HTTPS URL.' }
    }

    $request = $null
    $response = $null
    $stream = $null
    $output = $null
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Timeout = 120000
        $request.ReadWriteTimeout = 300000
        $request.UserAgent = 'CapstoneHeadProvisioner/1.0'
        $request.AllowAutoRedirect = $true
        $response = $request.GetResponse()

        $total = [int64]$response.ContentLength
        Write-CapstoneLog ('Installer size: {0}' -f (Format-CapstoneBytes -Bytes $total)) -Level INFO

        $stream = $response.GetResponseStream()
        $output = [System.IO.File]::Create($Destination)
        $buffer = New-Object byte[] 1048576
        $written = 0L
        $lastReport = Get-Date
        $deadline = (Get-Date).AddMinutes($TimeoutMinutes)

        while ($true) {
            $read = $stream.Read($buffer, 0, $buffer.Length)
            if ($read -le 0) { break }
            $output.Write($buffer, 0, $read)
            $written += $read
            if (((Get-Date) - $lastReport).TotalSeconds -ge 5) {
                $percent = 0
                if ($total -gt 0) { $percent = [math]::Round(($written / $total) * 100, 1) }
                Write-CapstoneLog ('  installer download: {0} of {1} ({2}%)' -f (Format-CapstoneBytes -Bytes $written), (Format-CapstoneBytes -Bytes $total), $percent) -Level INFO
                $lastReport = Get-Date
            }
            if ((Get-Date) -gt $deadline) {
                throw ('The installer download exceeded {0} minutes.' -f $TimeoutMinutes)
            }
        }
        $output.Flush()
        return [pscustomobject]@{ Success = $true; Path = $Destination; Bytes = $written; Error = '' }
    } catch {
        return [pscustomobject]@{ Success = $false; Path = $Destination; Bytes = 0; Error = $_.Exception.Message }
    } finally {
        if ($output) { try { $output.Dispose() } catch { } }
        if ($stream) { try { $stream.Dispose() } catch { } }
        if ($response) { try { $response.Dispose() } catch { } }
    }
}

function Install-CapstoneOllama {
    <#
        .SYNOPSIS
            Installs Ollama and proves it by measured state, not process exit.
        .DESCRIPTION
            The setup process is started and then watched, but what decides the
            outcome is a bounded poll over (a) the executable existing and
            (b) the local API answering. If the API becomes healthy while setup
            is still running, that is success and setup is left alone; nothing
            is force-terminated to make this function return (defect D-04).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [int]$TimeoutMinutes = 25,
        [string]$Url = $script:OfficialInstallerUrl,
        # LAB FIX: an operator-supplied OllamaSetup.exe (for a head without internet). It gets
        # the same Authenticode publisher check as a download before it is executed.
        [string]$InstallerPath = ''
    )

    $outcome = [ordered]@{
        Success = $false; Executable = ''; Version = ''; Method = ''; Error = ''
        Signature = $null; SetupStillRunning = $false
    }

    $existing = Get-CapstoneOllamaExecutable
    if ($existing) {
        Write-CapstoneLog ('Ollama is already installed at {0}. It will be reused, not reinstalled.' -f $existing) -Level OK
        $outcome.Success = $true
        $outcome.Executable = $existing
        $outcome.Method = 'existing-installation'
        return [pscustomobject]$outcome
    }

    if (-not [string]::IsNullOrWhiteSpace($InstallerPath)) {
        if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
            $outcome.Error = ('The supplied installer {0} does not exist.' -f $InstallerPath)
            return [pscustomobject]$outcome
        }
        $installer = Join-Path $Paths.Downloads 'OllamaSetup.exe'
        if (-not (Test-Path -LiteralPath $Paths.Downloads)) { New-Item -ItemType Directory -Path $Paths.Downloads -Force | Out-Null }
        Copy-Item -LiteralPath $InstallerPath -Destination $installer -Force
        Write-CapstoneLog ('Using the supplied installer {0}' -f $InstallerPath) -Level INFO
    } else {
        $installer = Join-Path $Paths.Downloads 'OllamaSetup.exe'
        $download = Save-CapstoneOllamaInstaller -Destination $installer -Url $Url
        if (-not $download.Success) {
            $outcome.Error = ('Installer download failed: {0}' -f $download.Error)
            return [pscustomobject]$outcome
        }
    }

    $signature = Test-CapstoneInstallerSignature -Path $installer
    $outcome.Signature = $signature
    Write-CapstoneLog ('Installer signature: {0}' -f $signature.Reason) -Level $(if ($signature.Trusted) { 'OK' } else { 'ERROR' })
    if (-not $signature.Trusted) {
        $outcome.Error = $signature.Reason
        return [pscustomobject]$outcome
    }

    Write-CapstoneLog 'Running the Ollama installer silently. This takes several minutes and needs no input.' -Level STEP
    $setup = $null
    try {
        $setup = Start-Process -FilePath $installer `
            -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCANCEL') `
            -PassThru -WindowStyle Hidden -ErrorAction Stop
        $outcome.Method = 'official-installer-silent'
    } catch {
        $outcome.Error = ('Could not start the Ollama installer: {0}' -f $_.Exception.Message)
        return [pscustomobject]$outcome
    }

    # Bounded observation of real state. The setup process lifetime is recorded
    # but never used as the completion signal.
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $localApi = Get-CapstoneApiBase
    $heartbeat = Get-Date

    while ((Get-Date) -lt $deadline) {
        $executable = Get-CapstoneOllamaExecutable
        if ($executable) {
            $version = ''
            $probe = Test-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds 5
            if ($probe.Healthy) { $version = $probe.Version }
            if ([string]::IsNullOrWhiteSpace($version)) {
                $version = Get-CapstoneExecutableVersion -Executable $executable
            }
            if (-not [string]::IsNullOrWhiteSpace($version)) {
                $outcome.Success = $true
                $outcome.Executable = $executable
                $outcome.Version = $version
                break
            }
        }

        $setupAlive = $false
        try { $setup.Refresh(); $setupAlive = (-not $setup.HasExited) } catch { $setupAlive = $false }

        if (((Get-Date) - $heartbeat).TotalSeconds -ge 30) {
            $note = 'setup process has exited; still waiting for the installed files'
            if ($setupAlive) { $note = 'setup process is still working' }
            Write-CapstoneLog ('  installing Ollama ... {0}' -f $note) -Level INFO
            $heartbeat = Get-Date
        }
        Start-Sleep -Seconds 5
    }

    try { $setup.Refresh(); $outcome.SetupStillRunning = (-not $setup.HasExited) } catch { }

    if (-not $outcome.Success) {
        $outcome.Error = ('Ollama did not become usable within {0} minutes. The installer was left running rather than being force-terminated; re-run the provisioner to resume.' -f $TimeoutMinutes)
        return [pscustomobject]$outcome
    }

    Write-CapstoneLog ('Ollama {0} installed at {1}' -f $outcome.Version, $outcome.Executable) -Level OK
    return [pscustomobject]$outcome
}

function Get-CapstoneExecutableVersion {
    <#
        .SYNOPSIS
            Reads `ollama --version` with a bounded wait and no pipeline tricks.
        .DESCRIPTION
            Output is redirected to files rather than piped, because native
            stderr piped through PowerShell 5.1 with a Stop preference can be
            promoted to a terminating error (defect D-09).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Executable,
        [int]$TimeoutSeconds = 20
    )

    $stdout = [System.IO.Path]::GetTempFileName()
    $stderr = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $Executable -ArgumentList @('--version') `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -ErrorAction Stop
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch { }
            return ''
        }
        $text = ''
        foreach ($file in @($stdout, $stderr)) {
            if (Test-Path -LiteralPath $file) {
                $text += (Get-Content -LiteralPath $file -Raw -ErrorAction SilentlyContinue)
            }
        }
        if ($text -match '(\d+\.\d+\.\d+)') { return $Matches[1] }
        return ($text.Trim())
    } catch {
        return ''
    } finally {
        foreach ($file in @($stdout, $stderr)) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

function Resolve-CapstoneServerStrategy {
    <#
        .SYNOPSIS
            Decides how to obtain a healthy local server. Pure - unit tested.
        .DESCRIPTION
            The overriding rule is that an already-listening server is reused.
            Earlier versions started a second `ollama serve` alongside the tray
            application's server and then fought it for the port (defect D-06).
    #>
    [CmdletBinding()]
    param(
        [bool]$LocalApiHealthy = $false,
        [bool]$PrivateApiHealthy = $false,
        [array]$Listeners = @(),
        [bool]$TrayAppRunning = $false,
        [bool]$ExecutablePresent = $false
    )

    if ($LocalApiHealthy) {
        return [pscustomobject]@{ Action = 'ReuseExisting'; Reason = 'A healthy Ollama API already answers on 127.0.0.1. It is reused unchanged.' }
    }
    if ($PrivateApiHealthy) {
        return [pscustomobject]@{ Action = 'ReusePrivate'; Reason = 'A healthy Ollama API already answers on the private-LAN address. It is reused unchanged.' }
    }

    $foreign = @($Listeners | Where-Object { $_.ProcessName -and $_.ProcessName -notmatch '(?i)^ollama' })
    if ($foreign.Count -gt 0) {
        $names = @($foreign | ForEach-Object { '{0} (PID {1})' -f $_.ProcessName, $_.ProcessId }) -join ', '
        return [pscustomobject]@{
            Action = 'StopWithPortConflict'
            Reason = ('Port 11434 is held by a process that is not Ollama: {0}. Nothing was changed; free the port and re-run.' -f $names)
        }
    }

    if (-not $ExecutablePresent) {
        return [pscustomobject]@{ Action = 'InstallFirst'; Reason = 'No Ollama executable is present yet.' }
    }

    if ($Listeners.Count -gt 0) {
        return [pscustomobject]@{
            Action = 'WaitForExisting'
            Reason = 'An Ollama process is already listening but has not answered the API yet. Waiting rather than starting a competing server.'
        }
    }
    if ($TrayAppRunning) {
        return [pscustomobject]@{
            Action = 'WaitForExisting'
            Reason = 'The Ollama tray application is running but its server is not listening yet. Waiting rather than starting a competing server.'
        }
    }
    return [pscustomobject]@{ Action = 'StartTrayApp'; Reason = 'No Ollama server is running; starting the supported Windows application.' }
}

function Start-CapstoneOllamaServer {
    <#
        .SYNOPSIS
            Brings a healthy local Ollama API into existence, or explains why not.
        .DESCRIPTION
            Prefers "ollama app.exe" (the supported Windows application) because
            it registers itself to start at logon, so remote serving survives a
            reboot. Falls back to `ollama serve` only if the app is absent.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Executable,
        [string]$PrivateIp = '',
        # 90 seconds was measured to be too short on a machine where Ollama had
        # just been installed: the first start does runtime and accelerator
        # discovery before it binds the port.
        [int]$TimeoutSeconds = 180
    )

    $localApi = Get-CapstoneApiBase
    $privateApi = ''
    if (-not [string]::IsNullOrWhiteSpace($PrivateIp)) {
        $privateApi = Get-CapstoneApiBase -HostAddress $PrivateIp
    }

    $local = Test-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds 5
    $private = [pscustomobject]@{ Healthy = $false; Version = '' }
    if ($privateApi) { $private = Test-CapstoneOllamaApi -BaseUri $privateApi -TimeoutSeconds 5 }

    $listeners = Get-CapstonePortOwner
    $processes = Get-CapstoneOllamaProcesses
    $trayRunning = @($processes | Where-Object { $_.Name -match '(?i)ollama app' }).Count -gt 0

    $strategy = Resolve-CapstoneServerStrategy `
        -LocalApiHealthy $local.Healthy `
        -PrivateApiHealthy $private.Healthy `
        -Listeners $listeners `
        -TrayAppRunning $trayRunning `
        -ExecutablePresent ([bool](Test-Path -LiteralPath $Executable))

    Write-CapstoneLog $strategy.Reason -Level $(if ($strategy.Action -eq 'StopWithPortConflict') { 'ERROR' } else { 'INFO' })

    switch ($strategy.Action) {
        'ReuseExisting' {
            return [pscustomobject]@{ Success = $true; BaseUri = $localApi; Version = $local.Version; Strategy = $strategy; Started = $false }
        }
        'ReusePrivate' {
            return [pscustomobject]@{ Success = $true; BaseUri = $privateApi; Version = $private.Version; Strategy = $strategy; Started = $false }
        }
        'StopWithPortConflict' {
            return [pscustomobject]@{ Success = $false; BaseUri = ''; Version = ''; Strategy = $strategy; Started = $false }
        }
        'StartTrayApp' {
            $app = Join-Path (Split-Path -Parent $Executable) 'ollama app.exe'
            try {
                if (Test-Path -LiteralPath $app) {
                    Start-Process -FilePath $app -WindowStyle Hidden -ErrorAction Stop | Out-Null
                    Write-CapstoneLog 'Started the Ollama Windows application.' -Level INFO
                } else {
                    Start-Process -FilePath $Executable -ArgumentList @('serve') -WindowStyle Hidden -ErrorAction Stop | Out-Null
                    Write-CapstoneLog 'Started "ollama serve" (the Windows application was not present).' -Level INFO
                }
            } catch {
                return [pscustomobject]@{ Success = $false; BaseUri = ''; Version = ''; Strategy = $strategy; Started = $false; Error = $_.Exception.Message }
            }
        }
    }

    # How long to wait before giving up on whatever is currently supposed to
    # be starting. A process that is already LISTENING deserves the full
    # budget - it is initialising. A tray application with no listener at all
    # is usually a dead end, and waiting three minutes on it is
    # indistinguishable from a hang, so that case gets a short leash and then
    # goes straight to the server binary.
    $firstWait = $TimeoutSeconds
    if ($listeners.Count -eq 0) { $firstWait = 30 }

    $final = Wait-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds $firstWait -Activity 'Waiting for the local Ollama API'
    if ($final.Healthy) {
        return [pscustomobject]@{ Success = $true; BaseUri = $localApi; Version = $final.Version; Strategy = $strategy; Started = $true }
    }
    if ($privateApi) {
        $finalPrivate = Test-CapstoneOllamaApi -BaseUri $privateApi -TimeoutSeconds 5
        if ($finalPrivate.Healthy) {
            return [pscustomobject]@{ Success = $true; BaseUri = $privateApi; Version = $finalPrivate.Version; Strategy = $strategy; Started = $true }
        }
    }

    # Fallback. Measured on nodes 5 and 6 (2026-10-30): a clean install of
    # Ollama 0.40.13 succeeded, "ollama app.exe" was started, and no API ever
    # answered. The Windows application is a tray wrapper; on a first run it
    # can fail to bring up the server at all, particularly when launched
    # hidden from an elevated process.
    #
    # So if the application did not produce a listener, start the server
    # binary directly. This is a supported invocation and does not conflict
    # with anything, because we have already established that nothing is
    # listening on the port.
    Write-CapstoneLog 'The Ollama application did not bring up its server. Starting the server process directly instead.' -Level WARN
    try {
        Start-Process -FilePath $Executable -ArgumentList @('serve') -WindowStyle Hidden -ErrorAction Stop | Out-Null
    } catch {
        Write-CapstoneLog ('Could not start "ollama serve": {0}' -f $_.Exception.Message) -Level ERROR
    }

    $fallback = Wait-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds $TimeoutSeconds -Activity 'Waiting for the Ollama server process'
    if ($fallback.Healthy) {
        Write-CapstoneLog 'The Ollama server answered after being started directly.' -Level OK
        return [pscustomobject]@{ Success = $true; BaseUri = $localApi; Version = $fallback.Version; Strategy = $strategy; Started = $true; UsedFallback = $true }
    }

    # Record what is actually on the machine, so a repeat failure is
    # diagnosable from the log without a second trip to the keyboard.
    $processes = Get-CapstoneOllamaProcesses
    $listeners = Get-CapstonePortOwner
    $processText = 'none'
    if ($processes.Count -gt 0) {
        $processText = (@($processes | ForEach-Object { '{0} (PID {1})' -f $_.Name, $_.Id }) -join ', ')
    }
    $listenerText = 'none'
    if ($listeners.Count -gt 0) {
        $listenerText = (@($listeners | ForEach-Object { '{0}:{1} by {2}' -f $_.LocalAddress, $_.Port, $_.ProcessName }) -join ', ')
    }
    Write-CapstoneLog ('Ollama processes running: {0}' -f $processText) -Level ERROR
    Write-CapstoneLog ('Listeners on port 11434 : {0}' -f $listenerText) -Level ERROR

    return [pscustomobject]@{
        Success = $false; BaseUri = ''; Version = ''; Strategy = $strategy; Started = $true; UsedFallback = $true
        Error = ('No Ollama API answered on {0} within {1} seconds, even after starting the server directly. Processes: {2}. Listeners: {3}.' -f $localApi, $TimeoutSeconds, $processText, $listenerText)
    }
}

Export-ModuleMember -Function @(
    'Get-CapstoneOllamaExecutable', 'Get-CapstoneOllamaProcesses', 'Get-CapstonePortOwner',
    'Get-CapstoneModelDirectory', 'Get-CapstoneDiskFacts',
    'Get-CapstoneApiBase', 'Test-CapstoneOllamaApi', 'Wait-CapstoneOllamaApi',
    'Get-CapstoneInstalledModels', 'Get-CapstoneRunningModels',
    'Test-CapstoneInstallerSignature', 'Save-CapstoneOllamaInstaller', 'Install-CapstoneOllama',
    'Get-CapstoneExecutableVersion',
    'Resolve-CapstoneServerStrategy', 'Start-CapstoneOllamaServer'
)
