param([string]$Python = 'py')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    if ($Python -eq 'py') { & $Python -3.12 -m venv .venv }
    else { & $Python -m venv .venv }
    if ($LASTEXITCODE) { throw 'Python 3.12 environment creation failed.' }
    & ./.venv/Scripts/python.exe -m pip install -r requirements-dev.lock
    if ($LASTEXITCODE) { throw 'Development dependency installation failed.' }
    & ./.venv/Scripts/python.exe -m pip install --no-build-isolation --no-deps -e .
    if ($LASTEXITCODE) { throw 'Gateway package installation failed.' }
    & ./.venv/Scripts/python.exe -m pytest
    if ($LASTEXITCODE) { throw 'Gateway verification failed.' }
} finally { Pop-Location }
