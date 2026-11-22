<#
    A deliberately small test harness.

    Pester is not assumed to be present: a freshly imaged Windows 11 head has
    no module installed beyond the in-box set, and the tests have to be
    runnable on the same machine the package is delivered to. This provides
    only what the suite needs.
#>

$script:Results = @()
$script:CurrentGroup = '(ungrouped)'

function Reset-TestRun {
    $script:Results = @()
    $script:CurrentGroup = '(ungrouped)'
}

function Start-TestGroup {
    param([Parameter(Mandatory)][string]$Name)
    $script:CurrentGroup = $Name
    Write-Host ''
    Write-Host ('  {0}' -f $Name) -ForegroundColor Cyan
}

function Add-TestResult {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $script:Results += [pscustomobject]@{
        Group = $script:CurrentGroup; Name = $Name; Passed = $Passed; Detail = $Detail
    }
    if ($Passed) {
        Write-Host ('    PASS  {0}' -f $Name) -ForegroundColor Green
    } else {
        Write-Host ('    FAIL  {0}' -f $Name) -ForegroundColor Red
        if ($Detail) { Write-Host ('          {0}' -f $Detail) -ForegroundColor Red }
    }
}

function Test-Case {
    <#
        Runs one test body. An exception inside the body is a failure, not a
        crash, so one broken test cannot hide the rest of the suite.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body
    )
    try {
        & $Body
        Add-TestResult -Name $Name -Passed $true -Detail ''
    } catch {
        Add-TestResult -Name $Name -Passed $false -Detail $_.Exception.Message
    }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Because = '')
    $expectedText = if ($null -eq $Expected) { '<null>' } else { [string]$Expected }
    $actualText = if ($null -eq $Actual) { '<null>' } else { [string]$Actual }
    if ($expectedText -cne $actualText) {
        throw ('expected "{0}" but got "{1}"{2}' -f $expectedText, $actualText, $(if ($Because) { ' - ' + $Because } else { '' }))
    }
}

function Assert-True {
    param([bool]$Condition, [string]$Because = 'expected a true condition')
    if (-not $Condition) { throw $Because }
}

function Assert-False {
    param([bool]$Condition, [string]$Because = 'expected a false condition')
    if ($Condition) { throw $Because }
}

function Assert-Match {
    param([string]$Pattern, [string]$Actual, [string]$Because = '')
    if ($Actual -notmatch $Pattern) {
        throw ('expected text matching /{0}/ but got "{1}"{2}' -f $Pattern, $Actual, $(if ($Because) { ' - ' + $Because } else { '' }))
    }
}

function Get-TestSummary {
    $passed = @($script:Results | Where-Object { $_.Passed }).Count
    $failed = @($script:Results | Where-Object { -not $_.Passed }).Count
    return [pscustomobject]@{
        Total = $script:Results.Count
        Passed = $passed
        Failed = $failed
        Results = $script:Results
        Failures = @($script:Results | Where-Object { -not $_.Passed })
    }
}

function Write-TestSummary {
    param([string]$Title = 'TEST SUMMARY')
    $summary = Get-TestSummary
    Write-Host ''
    Write-Host ('  ==== {0} ====' -f $Title) -ForegroundColor Cyan
    Write-Host ('   total  {0}' -f $summary.Total)
    Write-Host ('   passed {0}' -f $summary.Passed) -ForegroundColor Green
    if ($summary.Failed -gt 0) {
        Write-Host ('   failed {0}' -f $summary.Failed) -ForegroundColor Red
        foreach ($failure in $summary.Failures) {
            Write-Host ('     - [{0}] {1}: {2}' -f $failure.Group, $failure.Name, $failure.Detail) -ForegroundColor Red
        }
    } else {
        Write-Host '   failed 0' -ForegroundColor Green
    }
    Write-Host ''
    return $summary
}

Export-ModuleMember -Function @(
    'Reset-TestRun', 'Start-TestGroup', 'Add-TestResult', 'Test-Case',
    'Assert-Equal', 'Assert-True', 'Assert-False', 'Assert-Match',
    'Get-TestSummary', 'Write-TestSummary'
)
