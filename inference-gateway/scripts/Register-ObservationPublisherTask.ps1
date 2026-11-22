<#
Registers the Task Scheduler supervisor for the Arena observation publisher.

The task runs the long-lived publisher (one publication every 5 s) under the
gateway's infrastructure account, before any login. A boot trigger repeating
every minute with MultipleInstances=IgnoreNew restarts a stopped publisher
within a minute and never starts a second one. The publisher's own writes are
atomic, so a forced stop never leaves a partial publication.

-PlanOnly prints the task definition and changes nothing. Registration needs an
elevated session and either -Credential (local or domain account) or
-GroupManagedServiceAccount. Task Scheduler cannot run as an NT SERVICE virtual account.
#>
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Database,
    [Parameter(Mandatory=$true)][string]$PublishFile,
    [Parameter(Mandatory=$true)][string]$StateFile,
    [Parameter(Mandatory=$true)][string]$LogFile,
    [Parameter(Mandatory=$true)][string]$GatewayAccount,
    [string]$ServiceId = 'shared-inference',
    [string]$TaskName = 'InferenceGateway-ObservationPublisher',
    [ValidateRange(1, 30)][int]$IntervalSeconds = 5,
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$GroupManagedServiceAccount,
    [switch]$PlanOnly
)
$ErrorActionPreference = 'Stop'
foreach ($path in $Python, $Database, $PublishFile, $StateFile, $LogFile) {
    if (-not [System.IO.Path]::IsPathRooted($path)) { throw 'Use absolute interpreter, ledger, publication, state and log paths.' }
}
$folders = @($Database, $StateFile, $LogFile) | ForEach-Object { [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($_)) }
if ([System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($PublishFile)) -in $folders) {
    throw 'Publish into a dedicated folder, separate from ledger, state and logs.'
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Interpreter not found.' }
if ($ServiceId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$') { throw 'Invalid service ID.' }
function Quote([string]$Value) { '"' + $Value + '"' }
$arguments = @('-I', '-m', 'inference_gateway.observation_publisher', 'run',
    '--database', (Quote $Database), '--state', (Quote $StateFile), '--output', (Quote $PublishFile),
    '--service-id', $ServiceId, '--interval', $IntervalSeconds, '--log-file', (Quote $LogFile)) -join ' '
$e = { param($v) [System.Security.SecurityElement]::Escape([string]$v) }
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Inference Gateway: publish the sanitized shared.inference.observation/1 document for Agent Arena every $IntervalSeconds s.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(& $e $GatewayAccount)</UserId>
      <LogonType>Password</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(& $e $Python)</Command>
      <Arguments>$(& $e $arguments)</Arguments>
      <WorkingDirectory>$(& $e ([System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($StateFile))))</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
if ($PlanOnly) { $xml; exit 0 }
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) { throw 'Registration requires an elevated session.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw "Task '$TaskName' already exists; unregister it deliberately before replacing it." }
if ($Credential) {
    Register-ScheduledTask -TaskName $TaskName -Xml $xml -User $GatewayAccount -Password $Credential.GetNetworkCredential().Password | Out-Null
} elseif ($GroupManagedServiceAccount) {
    if (-not $GatewayAccount.EndsWith('$')) { throw 'A group managed service account name ends with $.' }
    Register-ScheduledTask -TaskName $TaskName -Xml $xml | Out-Null
} else { throw 'Pass -Credential for the gateway account, or -GroupManagedServiceAccount.' }
"Registered $TaskName. It starts at the next boot; run Start-ScheduledTask -TaskName '$TaskName' to start it now."
