<#
    Capstone.Preflight
    Read-only machine discovery.

    Nothing in this module changes anything. The complete inventory is written
    to disk before the engine is allowed to make its first change, so there is
    always a record of what the machine looked like beforehand.
#>

function Get-CapstoneOperatingSystemFacts {
    [CmdletBinding()]
    param()

    $facts = [ordered]@{
        Caption = ''; Version = ''; BuildNumber = ''; Architecture = ''
        InstallationType = ''; ComputerName = $env:COMPUTERNAME
        PowerShellVersion = $PSVersionTable.PSVersion.ToString()
        PowerShellEdition = [string]$PSVersionTable.PSEdition
        UserName = ''; Elevated = (Test-CapstoneElevated)
        PendingReboot = $false; Supported = $false; SupportNote = ''
    }

    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $facts.Caption = [string]$os.Caption
        $facts.Version = [string]$os.Version
        $facts.BuildNumber = [string]$os.BuildNumber
        $facts.Architecture = [string]$os.OSArchitecture
    } catch { }

    try {
        $facts.InstallationType = [string](Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name InstallationType -ErrorAction Stop).InstallationType
    } catch { }

    try { $facts.UserName = [Security.Principal.WindowsIdentity]::GetCurrent().Name } catch { }

    foreach ($key in @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    )) {
        if (Test-Path -LiteralPath $key) { $facts.PendingReboot = $true }
    }

    $build = 0
    [void][int]::TryParse($facts.BuildNumber, [ref]$build)
    if ($build -ge 22000) {
        $facts.Supported = $true
        $facts.SupportNote = 'Windows 11 or later.'
    } elseif ($build -ge 10240) {
        $facts.Supported = $true
        $facts.SupportNote = 'Windows 10. Supported, but the Capstone heads are specified as Windows 11.'
    } else {
        $facts.SupportNote = ('Build {0} is older than this provisioner supports.' -f $facts.BuildNumber)
    }

    if ($facts.Architecture -and $facts.Architecture -notmatch '64') {
        $facts.Supported = $false
        $facts.SupportNote = 'Ollama for Windows requires a 64-bit operating system.'
    }

    return [pscustomobject]$facts
}

function Get-CapstoneHardwareFacts {
    [CmdletBinding()]
    param()

    $facts = [ordered]@{
        CpuName = ''; CpuManufacturer = ''; PhysicalCores = 0; LogicalCores = 0
        TotalMemoryBytes = 0L; TotalMemoryGB = 0.0; AvailableMemoryBytes = 0L
        Gpus = @(); NvidiaVramMB = 0; NvidiaDriver = ''; Volumes = @()
    }

    try {
        $cpus = @(Get-CimInstance Win32_Processor -ErrorAction Stop)
        if ($cpus.Count -gt 0) {
            $facts.CpuName = [string]$cpus[0].Name
            $facts.CpuManufacturer = [string]$cpus[0].Manufacturer
            foreach ($cpu in $cpus) {
                $facts.PhysicalCores += [int]$cpu.NumberOfCores
                $facts.LogicalCores += [int]$cpu.NumberOfLogicalProcessors
            }
        }
    } catch { }

    try {
        $system = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        $facts.TotalMemoryBytes = [int64]$system.TotalPhysicalMemory
        $facts.TotalMemoryGB = [math]::Round($facts.TotalMemoryBytes / 1GB, 1)
    } catch { }

    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $facts.AvailableMemoryBytes = [int64]$os.FreePhysicalMemory * 1024
    } catch { }

    try {
        foreach ($controller in @(Get-CimInstance Win32_VideoController -ErrorAction Stop)) {
            $facts.Gpus += [pscustomobject]@{
                Name = [string]$controller.Name
                DriverVersion = [string]$controller.DriverVersion
                AdapterRamBytes = $(if ($controller.AdapterRAM) { [int64]$controller.AdapterRAM } else { 0L })
            }
        }
    } catch { }

    # nvidia-smi is authoritative for VRAM; Win32_VideoController under-reports
    # anything above 4 GB because AdapterRAM is a 32-bit field.
    try {
        $smi = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
        if (-not $smi) {
            $fallback = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
            if (Test-Path -LiteralPath $fallback) { $smi = [pscustomobject]@{ Source = $fallback } }
        }
        if ($smi) {
            $stdout = [System.IO.Path]::GetTempFileName()
            $process = Start-Process -FilePath $smi.Source -ArgumentList @('--query-gpu=memory.total,driver_version', '--format=csv,noheader,nounits') `
                -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -ErrorAction Stop
            if ($process.WaitForExit(20000)) {
                $line = (Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue | Select-Object -First 1)
                if ($line -and $line -match '^\s*(\d+)\s*,\s*(.+?)\s*$') {
                    $facts.NvidiaVramMB = [int]$Matches[1]
                    $facts.NvidiaDriver = $Matches[2]
                }
            } else { try { $process.Kill() } catch { } }
            Remove-Item -LiteralPath $stdout -Force -ErrorAction SilentlyContinue
        }
    } catch { }

    try {
        foreach ($volume in @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 3' -ErrorAction Stop)) {
            $facts.Volumes += [pscustomobject]@{
                Drive = [string]$volume.DeviceID
                SizeBytes = [int64]$volume.Size
                FreeBytes = [int64]$volume.FreeSpace
                FreeDisplay = (Format-CapstoneBytes -Bytes ([int64]$volume.FreeSpace))
            }
        }
    } catch { }

    return [pscustomobject]$facts
}

function Get-CapstoneOllamaFacts {
    <#
        .SYNOPSIS
            Everything observable about Ollama, without touching it.
    #>
    [CmdletBinding()]
    param([string]$PrivateIp = '')

    $executable = Get-CapstoneOllamaExecutable
    $modelDirectory = Get-CapstoneModelDirectory
    $localApi = Get-CapstoneApiBase
    $localProbe = Test-CapstoneOllamaApi -BaseUri $localApi -TimeoutSeconds 5

    $privateProbe = $null
    if (-not [string]::IsNullOrWhiteSpace($PrivateIp)) {
        $privateProbe = Test-CapstoneOllamaApi -BaseUri (Get-CapstoneApiBase -HostAddress $PrivateIp) -TimeoutSeconds 5
    }

    $inventory = [pscustomobject]@{ Available = $false; Models = @(); Error = 'No healthy local API to query.' }
    $running = [pscustomobject]@{ Available = $false; Names = @(); Count = 0 }
    if ($localProbe.Healthy) {
        $inventory = Get-CapstoneInstalledModels -BaseUri $localApi
        $running = Get-CapstoneRunningModels -BaseUri $localApi
    } elseif ($privateProbe -and $privateProbe.Healthy) {
        $base = Get-CapstoneApiBase -HostAddress $PrivateIp
        $inventory = Get-CapstoneInstalledModels -BaseUri $base
        $running = Get-CapstoneRunningModels -BaseUri $base
    }

    $version = ''
    if ($localProbe.Healthy) { $version = $localProbe.Version }
    elseif ($privateProbe -and $privateProbe.Healthy) { $version = $privateProbe.Version }
    elseif ($executable) { $version = Get-CapstoneExecutableVersion -Executable $executable }

    $environment = [ordered]@{}
    foreach ($name in @('OLLAMA_HOST', 'OLLAMA_MODELS', 'OLLAMA_ORIGINS', 'OLLAMA_KEEP_ALIVE', 'OLLAMA_NUM_PARALLEL', 'OLLAMA_MAX_LOADED_MODELS')) {
        $environment[$name] = [ordered]@{
            Machine = [Environment]::GetEnvironmentVariable($name, 'Machine')
            User    = [Environment]::GetEnvironmentVariable($name, 'User')
        }
    }

    return [pscustomobject]@{
        Installed        = [bool]$executable
        Executable       = [string]$executable
        Version          = $version
        Processes        = (Get-CapstoneOllamaProcesses)
        Listeners        = (Get-CapstonePortOwner)
        LocalApi         = $localProbe
        PrivateApi       = $privateProbe
        ModelDirectory   = $modelDirectory
        Disk             = (Get-CapstoneDiskFacts -Path $modelDirectory.Path)
        InventoryAvailable = $inventory.Available
        Models           = $inventory.Models
        ModelCount       = @($inventory.Models).Count
        RunningModels    = $running
        Environment      = [pscustomobject]$environment
        Firewall         = (Get-CapstoneFirewallRuleFacts)
    }
}

function Get-CapstoneCapstoneServiceFacts {
    <#
        .SYNOPSIS
            Notes any other Capstone-related listener already on this machine.
        .DESCRIPTION
            Purely informational. The provisioner does not stop, start or
            reconfigure anything it finds here.
    #>
    [CmdletBinding()]
    param([int[]]$Ports = @(8000, 8080, 8501, 5000, 7860, 11435))

    $found = @()
    foreach ($port in $Ports) {
        try {
            foreach ($connection in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
                $owner = ''
                try {
                    $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
                    if ($process) { $owner = $process.ProcessName }
                } catch { }
                $found += [pscustomobject]@{
                    Port = $port; LocalAddress = [string]$connection.LocalAddress
                    ProcessId = [int]$connection.OwningProcess; ProcessName = $owner
                }
            }
        } catch { }
    }
    return $found
}

function New-CapstonePreflightReport {
    <#
        .SYNOPSIS
            Collects and persists the complete read-only inventory.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Paths,
        [string]$PrivateIp = '',
        [int]$NodeNumber = 0,
        [string]$ProfileName = ''
    )

    Write-CapstoneLog 'Collecting the read-only machine inventory before any change.' -Level STEP

    $report = [ordered]@{
        schema_version   = 1
        generated_at     = (Get-Date).ToString('o')
        provisioner      = '1.0.0'
        requested_node   = $NodeNumber
        requested_profile = $ProfileName
        operating_system = (Get-CapstoneOperatingSystemFacts)
        hardware         = (Get-CapstoneHardwareFacts)
        adapters         = (Get-CapstoneAdapterInventory)
        coordinator      = (Test-CapstoneCoordinatorReachable)
        internet         = (Test-CapstoneInternetAvailable)
        ollama           = (Get-CapstoneOllamaFacts -PrivateIp $PrivateIp)
        other_services   = (Get-CapstoneCapstoneServiceFacts)
    }

    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $path = Join-Path $Paths.Diagnostics ('preflight-{0}.json' -f $stamp)
    Write-CapstoneJson -InputObject ([pscustomobject]$report) -Path $path
    Write-CapstoneJson -InputObject ([pscustomobject]$report) -Path (Join-Path $Paths.Diagnostics 'preflight-LATEST.json')
    Write-CapstoneLog ('Preflight inventory saved to {0}' -f $path) -Level OK

    return [pscustomobject]@{ Report = [pscustomobject]$report; Path = $path }
}

function Test-CapstoneInternetAvailable {
    <#
        .SYNOPSIS
            Confirms the machine can actually reach the sources it needs.
        .DESCRIPTION
            Checks DNS resolution and a real HTTPS response from the Ollama
            hosts, not merely "a default route exists".
    #>
    [CmdletBinding()]
    param()

    $result = [ordered]@{
        DnsResolves = $false; RegistryReachable = $false; DownloadHostReachable = $false
        Available = $false; Note = ''
    }

    try {
        $resolved = [System.Net.Dns]::GetHostEntry('registry.ollama.ai')
        $result.DnsResolves = ($null -ne $resolved -and @($resolved.AddressList).Count -gt 0)
    } catch { }

    # Any HTTP status at all - including 401, 403, 404 and 500 - proves the
    # host was reached, because a status code can only come from the server.
    # Only a transport failure (DNS, no route, refused, timeout) means
    # unreachable. HEAD is deliberately NOT used: measured on node 3, a HEAD
    # probe reported both hosts unreachable on a machine that then downloaded
    # three models successfully, because some paths along the way do not answer
    # HEAD. A ranged GET behaves like the real download.
    foreach ($pair in @(
        @{ Name = 'RegistryReachable';     Uri = 'https://registry.ollama.ai/v2/' },
        @{ Name = 'DownloadHostReachable'; Uri = 'https://ollama.com/' }
    )) {
        $request = $null
        $response = $null
        try {
            [System.Net.ServicePointManager]::SecurityProtocol =
                [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls11
            $request = [System.Net.HttpWebRequest]::Create($pair.Uri)
            $request.Method = 'GET'
            $request.Timeout = 25000
            $request.ReadWriteTimeout = 25000
            $request.UserAgent = 'CapstoneHeadProvisioner/1.0'
            $request.AllowAutoRedirect = $true
            # Ask for only the first byte so this stays cheap on a slow link.
            $request.AddRange(0, 0)
            $response = $request.GetResponse()
            $result[$pair.Name] = $true
        } catch [System.Net.WebException] {
            # A protocol error carries a response, so the host answered.
            if ($_.Exception.Response) {
                $result[$pair.Name] = $true
            } elseif ($_.Exception.Status -eq [System.Net.WebExceptionStatus]::ProtocolError) {
                $result[$pair.Name] = $true
            }
        } catch {
            # Leave as unreachable.
        } finally {
            if ($response) { try { $response.Dispose() } catch { } }
        }
    }

    $result.Available = ($result.RegistryReachable -or $result.DownloadHostReachable)
    if ($result.Available) {
        $result.Note = 'Internet access to the official Ollama hosts was confirmed.'
    } elseif ($result.DnsResolves) {
        $result.Note = 'DNS resolves but neither official Ollama host answered this probe. This is advisory only: downloads are still attempted, and the download itself reports the real result.'
    } else {
        $result.Note = 'DNS lookup for the official Ollama hosts failed. Downloads are still attempted, but if there is genuinely no internet they will fail with a transport error. Check the Wi-Fi connection.'
    }
    return [pscustomobject]$result
}

Export-ModuleMember -Function @(
    'Get-CapstoneOperatingSystemFacts', 'Get-CapstoneHardwareFacts', 'Get-CapstoneOllamaFacts',
    'Get-CapstoneCapstoneServiceFacts', 'Test-CapstoneInternetAvailable', 'New-CapstonePreflightReport'
)
