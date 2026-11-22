param(
    [switch]$CheckOnly,
    [switch]$Locked,
    [string]$BasePython,
    [string]$EnvironmentName = '.venv-py312'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot "$EnvironmentName/Scripts/python.exe"
if (!(Test-Path -LiteralPath $projectPython)) {
    if (!$BasePython) { throw 'Supply -BasePython pointing to a supported Python 3.12 interpreter.' }
    $version = & $BasePython -c "import sys; print('.'.join(map(str,sys.version_info[:2])))"
    if ($LASTEXITCODE -or $version -ne '3.12') { throw 'Capstone requires Python 3.12 for the verified runtime lock.' }
    & $BasePython -m venv (Join-Path $projectRoot $EnvironmentName)
    if ($LASTEXITCODE) { throw 'Virtual-environment creation failed.' }
}
$requirementsFile = if ($Locked) { 'requirements.runtime.lock' } else { 'requirements.txt' }
if (!$CheckOnly) { & $projectPython -m pip install -r (Join-Path $projectRoot $requirementsFile); if ($LASTEXITCODE) { throw 'Dependency synchronization failed' } }
& $projectPython (Join-Path $projectRoot 'scripts/check_environment.py')
if ($LASTEXITCODE) { throw 'Project runtime checks failed' }
