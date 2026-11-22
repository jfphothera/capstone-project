<#
    Runs every test suite and writes machine-readable results.

    Usable on the coordinator and on a head. The integration suite is
    non-destructive by design, so this is safe to run anywhere.
#>
[CmdletBinding()]
param(
    [string]$PackageRoot = '',
    [string]$ResultDirectory = '',
    [switch]$SkipIntegration,
    [switch]$SkipNetworkDependent
)

$ErrorActionPreference = 'Continue'

$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($PackageRoot)) { $PackageRoot = Split-Path -Parent $testRoot }
if ([string]::IsNullOrWhiteSpace($ResultDirectory)) { $ResultDirectory = Join-Path $PackageRoot 'docs\test-results' }
if (-not (Test-Path -LiteralPath $ResultDirectory)) { New-Item -ItemType Directory -Path $ResultDirectory -Force | Out-Null }

$powerShell51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$interpreter = $powerShell51
if (-not (Test-Path -LiteralPath $powerShell51)) { $interpreter = (Get-Process -Id $PID).Path }

$suites = @(
    @{ Name = 'static';      Script = 'Invoke-StaticChecks.ps1';     Extra = @() }
    @{ Name = 'unit';        Script = 'Invoke-UnitTests.ps1';        Extra = @() }
)
if (-not $SkipIntegration) {
    $extra = @()
    if ($SkipNetworkDependent) { $extra += '-SkipNetworkDependent' }
    $suites += @{ Name = 'integration'; Script = 'Invoke-IntegrationTests.ps1'; Extra = $extra }
}

Write-Host ''
Write-Host '########################################################################' -ForegroundColor Cyan
Write-Host '#  CAPSTONE HEAD PROVISIONER - FULL TEST RUN' -ForegroundColor Cyan
Write-Host '########################################################################' -ForegroundColor Cyan
Write-Host ('   Package   : {0}' -f $PackageRoot)
Write-Host ('   Results   : {0}' -f $ResultDirectory)
Write-Host ('   Engine host: Windows PowerShell {0}' -f (& $interpreter -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'))

$outcomes = @()
foreach ($suite in $suites) {
    $script = Join-Path $testRoot $suite.Script
    $resultPath = Join-Path $ResultDirectory ('{0}-results.json' -f $suite.Name)
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script, '-PackageRoot', $PackageRoot, '-ResultPath', $resultPath) + $suite.Extra

    & $interpreter @arguments
    $code = $LASTEXITCODE

    $detail = $null
    if (Test-Path -LiteralPath $resultPath) {
        try { $detail = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json } catch { }
    }
    $outcomes += [pscustomobject]@{
        Suite = $suite.Name
        ExitCode = $code
        Total = $(if ($detail) { $detail.total } else { 0 })
        Passed = $(if ($detail) { $detail.passed } else { 0 })
        Failed = $(if ($detail) { $detail.failed } else { 0 })
        ResultPath = $resultPath
    }
}

Write-Host ''
Write-Host '########################################################################' -ForegroundColor Cyan
Write-Host '#  COMBINED RESULT' -ForegroundColor Cyan
Write-Host '########################################################################' -ForegroundColor Cyan
Write-Host ''
Write-Host '   suite         total  passed  failed'
Write-Host '   -----------   -----  ------  ------'
foreach ($outcome in $outcomes) {
    $colour = 'Green'
    if ($outcome.Failed -gt 0 -or $outcome.ExitCode -ne 0) { $colour = 'Red' }
    Write-Host ('   {0,-11}   {1,5}  {2,6}  {3,6}' -f $outcome.Suite, $outcome.Total, $outcome.Passed, $outcome.Failed) -ForegroundColor $colour
}

$totalFailed = ($outcomes | Measure-Object -Property Failed -Sum).Sum
$totalTests = ($outcomes | Measure-Object -Property Total -Sum).Sum
$totalPassed = ($outcomes | Measure-Object -Property Passed -Sum).Sum

Write-Host ''
if ($totalFailed -eq 0 -and @($outcomes | Where-Object { $_.ExitCode -ne 0 }).Count -eq 0) {
    Write-Host ('   ALL {0} CHECKS PASSED' -f $totalTests) -ForegroundColor Green
} else {
    Write-Host ('   {0} OF {1} CHECKS FAILED' -f $totalFailed, $totalTests) -ForegroundColor Red
}
Write-Host ''

$summaryPath = Join-Path $ResultDirectory 'all-results.json'
$payload = ([pscustomobject]@{
    generated_at = (Get-Date).ToString('o')
    machine = $env:COMPUTERNAME
    total = $totalTests; passed = $totalPassed; failed = $totalFailed
    suites = $outcomes
}) | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($summaryPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ('   Combined results: {0}' -f $summaryPath) -ForegroundColor Cyan
Write-Host ''

if ($totalFailed -gt 0) { exit 1 }
exit 0
