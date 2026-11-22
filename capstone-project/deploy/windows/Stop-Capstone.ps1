param(
 [string]$TaskName='Capstone Application',
 [string]$InstallRoot='C:\Program Files\Capstone',
 [string]$DataRoot='C:\ProgramData\Capstone',
 [int]$TimeoutSeconds=30
)
$ErrorActionPreference='Stop'
$task=Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if($task){Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue}

# Stop the scheduled PowerShell launcher first. The application Python process
# can outlive that launcher on Windows, so this is followed by an executable-
# path sweep rooted strictly inside InstallRoot.
$launcherPath=Join-Path $InstallRoot 'tools\Start-Capstone.ps1'
Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -and $_.CommandLine.IndexOf($launcherPath,[StringComparison]::OrdinalIgnoreCase) -ge 0} | ForEach-Object {
 if([int]$_.ProcessId -ne $PID){Stop-Process -Id ([int]$_.ProcessId) -Force -ErrorAction SilentlyContinue}
}

$resolvedInstall=[IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$installPrefix=$resolvedInstall+'\'
$deadline=(Get-Date).AddSeconds([Math]::Max(1,$TimeoutSeconds))
do {
 $owned=@(Get-CimInstance Win32_Process | Where-Object {
  $_.ExecutablePath -and [int]$_.ProcessId -ne $PID -and
  ([IO.Path]::GetFullPath([string]$_.ExecutablePath)).StartsWith($installPrefix,[StringComparison]::OrdinalIgnoreCase)
 })
 foreach($process in $owned){Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue}
 if($owned.Count){Start-Sleep -Milliseconds 250}
} while($owned.Count -and (Get-Date) -lt $deadline)

$settingsPath=Join-Path $DataRoot 'config\appsettings.json'
$listenPort=$null
if(Test-Path -LiteralPath $settingsPath){
 $settings=Get-Content -Raw -LiteralPath $settingsPath|ConvertFrom-Json
 $listenPort=[int]$settings.listen_port
 while((Get-Date) -lt $deadline -and (Get-NetTCPConnection -LocalPort $listenPort -State Listen -ErrorAction SilentlyContinue)){
  Start-Sleep -Milliseconds 250
 }
}
$remaining=@(Get-CimInstance Win32_Process | Where-Object {
 $_.ExecutablePath -and [int]$_.ProcessId -ne $PID -and
 ([IO.Path]::GetFullPath([string]$_.ExecutablePath)).StartsWith($installPrefix,[StringComparison]::OrdinalIgnoreCase)
})
if($remaining.Count){throw "Capstone processes under $resolvedInstall did not stop: $($remaining.ProcessId -join ', ')"}
if($listenPort -and (Get-NetTCPConnection -LocalPort $listenPort -State Listen -ErrorAction SilentlyContinue)){
 throw "Capstone port $listenPort is still listening after $TimeoutSeconds seconds"
}
