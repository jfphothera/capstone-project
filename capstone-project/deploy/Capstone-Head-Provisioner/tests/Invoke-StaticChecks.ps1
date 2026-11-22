<#
    Static checks over the whole package.

    This is the gate that the earlier versions did not have. Defects D-10 (a
    PowerShell parser error shipped to the operator) and D-03 (an unsupported
    cmdlet parameter) would both have been caught here before packaging.
#>
[CmdletBinding()]
param(
    [string]$PackageRoot = '',
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Continue'

# Windows PowerShell 5.1 does not populate $PSScriptRoot while param defaults
# are being evaluated, so the script's own location is resolved here instead.
$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($PackageRoot)) { $PackageRoot = Split-Path -Parent $testRoot }

Import-Module (Join-Path $testRoot 'TestHarness.psm1') -Force -DisableNameChecking
Reset-TestRun

Write-Host ''
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host '  CAPSTONE HEAD PROVISIONER - STATIC CHECKS' -ForegroundColor Cyan
Write-Host '========================================================================' -ForegroundColor Cyan
Write-Host ('  Package root : {0}' -f $PackageRoot)
Write-Host ('  Host         : PowerShell {0}' -f $PSVersionTable.PSVersion)

# -Include is silently ignored alongside -LiteralPath, so every category would
# otherwise receive every file. Enumerate once and filter by extension.
$allFiles = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -ErrorAction SilentlyContinue)
$powerShellFiles = @($allFiles | Where-Object { $_.Extension -in @('.ps1', '.psm1', '.psd1') })
$cmdFiles        = @($allFiles | Where-Object { $_.Extension -in @('.cmd', '.bat') })
$jsonFiles       = @($allFiles | Where-Object { $_.Extension -eq '.json' })

Write-Host ('  PowerShell   : {0} file(s)' -f $powerShellFiles.Count)
Write-Host ('  CMD          : {0} file(s)' -f $cmdFiles.Count)
Write-Host ('  JSON         : {0} file(s)' -f $jsonFiles.Count)

# ---------------------------------------------------------------------------
Start-TestGroup 'PowerShell parses cleanly'
# ---------------------------------------------------------------------------

foreach ($file in $powerShellFiles) {
    $relative = $file.FullName.Substring($PackageRoot.Length).TrimStart('\')
    Test-Case ('parses: {0}' -f $relative) {
        $tokens = $null
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
        if ($null -ne $errors -and $errors.Count -gt 0) {
            $first = $errors[0]
            throw ('{0} parser error(s); first at line {1}: {2}' -f $errors.Count, $first.Extent.StartLineNumber, $first.Message)
        }
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'PowerShell 5.1 compatibility'
# ---------------------------------------------------------------------------

# Syntax and cmdlet parameters that exist in PowerShell 7 but not in the 5.1
# host that a freshly imaged head actually runs.
$forbiddenPatterns = @(
    @{ Pattern = '\?\?'; Name = 'null-coalescing operator (??)' }
    @{ Pattern = '\?\.'; Name = 'null-conditional operator (?.)' }
    @{ Pattern = '-AsHashtable'; Name = 'ConvertFrom-Json -AsHashtable' }
    @{ Pattern = 'ForEach-Object\s+-Parallel'; Name = 'ForEach-Object -Parallel' }
    @{ Pattern = 'Set-DnsClientServerAddress[^\r\n]*-AddressFamily'; Name = 'Set-DnsClientServerAddress -AddressFamily (defect D-03: not a valid parameter)' }
)

# This script necessarily contains, as string literals, every pattern it
# searches for, so it is excluded from the content scans. It is still parsed
# and syntax-checked above like everything else.
$scanFiles = @($powerShellFiles | Where-Object { $_.Name -ne 'Invoke-StaticChecks.ps1' })

# Comment text must not be scanned: a module that documents "ConvertFrom-Json
# has no -AsHashtable on 5.1" is describing the rule, not breaking it. Naive
# line-prefix stripping misses <# block comments #>, so the real PowerShell
# tokenizer is used to separate code from commentary.
$codeOnly = @{}
foreach ($file in $scanFiles) {
    $fileTokens = $null
    $fileErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$fileTokens, [ref]$fileErrors)
    $codeOnly[$file.FullName] = (@($fileTokens | Where-Object { $_.Kind -ne 'Comment' } | ForEach-Object { $_.Text }) -join ' ')
}

foreach ($file in $scanFiles) {
    $relative = $file.FullName.Substring($PackageRoot.Length).TrimStart('\')
    $code = $codeOnly[$file.FullName]
    foreach ($rule in $forbiddenPatterns) {
        Test-Case ('no {0}: {1}' -f $rule.Name, $relative) {
            if ($code -match $rule.Pattern) {
                throw ('found {0}' -f $rule.Name)
            }
        }
    }
}

Test-Case 'no parameter is named -Profile (collides with the automatic $Profile)' {
    $offenders = @()
    foreach ($file in $scanFiles) {
        $code = $codeOnly[$file.FullName]
        if ($code -match '\[string\]\s*\$Profile\b' -or $code -match '\$Profile\s*=\s*[''"]CPU') {
            $offenders += $file.Name
        }
    }
    if ($offenders.Count -gt 0) { throw ('files binding $Profile: {0}' -f ($offenders -join ', ')) }
}

Test-Case 'no automatic variable is used as a local ($input, $args, $error)' {
    $offenders = @()
    foreach ($file in $scanFiles) {
        $code = $codeOnly[$file.FullName]
        if ($code -match '\$input\s*=[^=]' -or $code -match '\$args\s*=[^=]' -or $code -match '\$error\s*=[^=]') {
            $offenders += $file.Name
        }
    }
    if ($offenders.Count -gt 0) { throw ('files assigning an automatic variable: {0}' -f ($offenders -join ', ')) }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Colon-adjacent interpolation (defect D-10)'
# ---------------------------------------------------------------------------

Test-Case 'no "$var:" interpolation that the parser would read as a scope' {
    $offenders = @()
    foreach ($file in $scanFiles) {
        $fileTokens = $null
        $fileErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$fileTokens, [ref]$fileErrors)
        # Only expandable (double-quoted) strings can suffer this. Single-quoted
        # strings and comments cannot, so they are not examined.
        foreach ($token in @($fileTokens | Where-Object { $_.Kind -eq 'StringExpandable' })) {
            $lineNumber = $token.Extent.StartLineNumber
            $line = $token.Text
            # A bare $name immediately followed by a colon inside a double-quoted
            # string. ${name} and $env:/$script:/$global: are correct forms.
            if ($line -match '"[^"]*\$(?!env:|script:|global:|local:|using:|private:|\{)[A-Za-z_][A-Za-z0-9_]*:') {
                $offenders += ('{0}:{1}' -f $file.Name, $lineNumber)
            }
        }
    }
    if ($offenders.Count -gt 0) { throw ('colon-adjacent interpolation at {0}' -f ($offenders -join ', ')) }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Script-scope control flow'
# ---------------------------------------------------------------------------

Test-Case 'the engine has no script-scope "return", which would skip its final exit' {
    # Regression guard. A `return` outside any function returns from the whole
    # script, so the trailing `exit $exitCode` never runs and the process
    # reports 0 whatever actually happened. Rollback failures were invisible
    # because of exactly this.
    $enginePath = Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1'
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($enginePath, [ref]$tokens, [ref]$errors)

    $returns = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.ReturnStatementAst] }, $true))
    $offenders = @()
    foreach ($statement in $returns) {
        $parent = $statement.Parent
        $insideFunction = $false
        while ($null -ne $parent) {
            if ($parent -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
                $parent -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
                $insideFunction = $true
                break
            }
            $parent = $parent.Parent
        }
        if (-not $insideFunction) {
            $offenders += ('line {0}' -f $statement.Extent.StartLineNumber)
        }
    }
    if ($offenders.Count -gt 0) {
        throw ('script-scope return at {0}; use "exit $exitCode" instead' -f ($offenders -join ', '))
    }
}

Test-Case 'the engine configures HTTP before any request can be made' {
    # Regression guard. Windows PowerShell 5.1 spends about 15 seconds on WPAD
    # proxy discovery before the first HTTP request of a process, which is
    # longer than every health-probe budget in the engine.
    $engineCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    $setupIndex = $engineCode.IndexOf('Initialize-CapstoneHttp')
    if ($setupIndex -lt 0) { throw 'the engine never calls Initialize-CapstoneHttp' }
    foreach ($probe in @('New-CapstonePreflightReport', 'Test-CapstoneOllamaApi', 'Start-CapstoneOllamaServer')) {
        $probeIndex = $engineCode.IndexOf($probe)
        if ($probeIndex -ge 0 -and $probeIndex -lt $setupIndex) {
            throw ('{0} is called before Initialize-CapstoneHttp' -f $probe)
        }
    }
}

Test-Case 'the standalone coordinator utility applies the same HTTP fix' {
    # It must work when copied to the coordinator on its own, with no modules.
    $utilityCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'coordinator\Test-CapstoneHeads.ps1') -Raw
    if ($utilityCode -notmatch 'DefaultWebProxy') {
        throw 'the coordinator utility does not disable automatic proxy discovery'
    }
    $fixIndex = $utilityCode.IndexOf('DefaultWebProxy')
    $firstRequest = $utilityCode.IndexOf('Invoke-RestMethod')
    if ($firstRequest -ge 0 -and $firstRequest -lt $fixIndex) {
        throw 'the utility makes an HTTP request before applying the proxy fix'
    }
}

Test-Case 'the engine ends by exiting with its computed code' {
    $content = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    if ($content -notmatch 'exit \$exitCode\s*$') { throw 'the engine does not end with "exit $exitCode"' }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Module exports resolve'
# ---------------------------------------------------------------------------

$moduleDirectory = Join-Path $PackageRoot 'engine\modules'
$moduleFiles = @(Get-ChildItem -LiteralPath $moduleDirectory -Filter '*.psm1' -File -ErrorAction SilentlyContinue)

Test-Case 'all seven engine modules are present' {
    Assert-Equal 7 $moduleFiles.Count 'expected exactly the seven Capstone.* modules'
}

foreach ($file in $moduleFiles) {
    Test-Case ('every exported name is defined: {0}' -f $file.Name) {
        $content = Get-Content -LiteralPath $file.FullName -Raw
        $defined = @([regex]::Matches($content, '(?m)^\s*function\s+([A-Za-z0-9\-\.]+)') | ForEach-Object { $_.Groups[1].Value })

        $exportMatch = [regex]::Match($content, 'Export-ModuleMember\s+-Function\s+@\(([^)]*)\)', 'Singleline')
        if (-not $exportMatch.Success) { throw 'no Export-ModuleMember -Function block was found' }
        $exported = @([regex]::Matches($exportMatch.Groups[1].Value, "'([^']+)'") | ForEach-Object { $_.Groups[1].Value })
        if ($exported.Count -eq 0) { throw 'the export list is empty' }

        $missing = @($exported | Where-Object { $defined -notcontains $_ })
        if ($missing.Count -gt 0) { throw ('exported but not defined: {0}' -f ($missing -join ', ')) }
    }
}

Test-Case 'all engine modules import together without error' {
    foreach ($file in $moduleFiles) {
        Import-Module -Name $file.FullName -Force -DisableNameChecking -Global -ErrorAction Stop
    }
    foreach ($required in @('Write-CapstoneLog', 'Select-CapstoneEthernetAdapter', 'Resolve-CapstoneModelKey', 'Get-CapstoneOverallOutcome', 'Resolve-CapstoneRemoteBinding', 'New-CapstonePreflightReport')) {
        if (-not (Get-Command $required -ErrorAction SilentlyContinue)) {
            throw ('{0} is not available after import' -f $required)
        }
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'CMD launchers'
# ---------------------------------------------------------------------------

foreach ($file in $cmdFiles) {
    $relative = $file.FullName.Substring($PackageRoot.Length).TrimStart('\')
    $content = Get-Content -LiteralPath $file.FullName -Raw

    Test-Case ('resolves its own directory: {0}' -f $relative) {
        if ($content -notmatch '%~dp0') { throw 'does not use %~dp0, so it would break when run from another working directory' }
    }
    Test-Case ('quotes paths: {0}' -f $relative) {
        if ($content -match '(?m)^\s*call\s+%~dp0' -or $content -match '(?m)^\s*"?%PS51%"?\s+[^"]*-File\s+[^"]') {
            throw 'an unquoted path would break on a folder containing spaces'
        }
    }
    Test-Case ('stays open at the end: {0}' -f $relative) {
        if ($content -notmatch 'pause' -and $content -notmatch 'call\s+"') {
            throw 'neither pauses nor delegates to a launcher that pauses'
        }
    }
    Test-Case ('CRLF line endings: {0}' -f $relative) {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        for ($i = 0; $i -lt $bytes.Length; $i++) {
            if ($bytes[$i] -eq 10 -and ($i -eq 0 -or $bytes[$i - 1] -ne 13)) {
                throw ('bare LF at byte {0}; cmd.exe requires CRLF' -f $i)
            }
        }
    }
    Test-Case ('ASCII only: {0}' -f $relative) {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        $high = @($bytes | Where-Object { $_ -gt 127 })
        if ($high.Count -gt 0) { throw ('{0} non-ASCII byte(s); these render as mojibake in a console code page' -f $high.Count) }
    }
}

Test-Case 'every launcher reaches the engine through START-CAPSTONE-SETUP.cmd' {
    foreach ($file in $cmdFiles) {
        if ($file.Name -eq 'START-CAPSTONE-SETUP.cmd') { continue }
        if ($file.Name -eq 'COORDINATOR-CHECK-ALL-HEADS.cmd') { continue }
        if ($file.Name -eq 'RUN-ALL-TESTS.cmd') { continue }
        $content = Get-Content -LiteralPath $file.FullName -Raw
        if ($content -notmatch 'START-CAPSTONE-SETUP\.cmd') {
            throw ('{0} does not call the central launcher, which would fragment the install procedure' -f $file.Name)
        }
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Referenced files exist'
# ---------------------------------------------------------------------------

Test-Case 'the engine entry point exists' {
    Assert-True (Test-Path -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1')) 'engine\Invoke-CapstoneProvisioner.ps1 is missing'
}
Test-Case 'the model catalog exists' {
    Assert-True (Test-Path -LiteralPath (Join-Path $PackageRoot 'engine\catalog\model-catalog.json')) 'engine\catalog\model-catalog.json is missing'
}
Test-Case 'the coordinator utility exists' {
    Assert-True (Test-Path -LiteralPath (Join-Path $PackageRoot 'coordinator\Test-CapstoneHeads.ps1')) 'coordinator\Test-CapstoneHeads.ps1 is missing'
}

Test-Case 'every launcher target resolves on disk' {
    $broken = @()
    foreach ($file in $cmdFiles) {
        foreach ($line in (Get-Content -LiteralPath $file.FullName)) {
            $match = [regex]::Match($line, '%~dp0([A-Za-z0-9_\\\.\-]+\.(?:ps1|cmd))')
            if ($match.Success) {
                $target = Join-Path $file.DirectoryName $match.Groups[1].Value
                if (-not (Test-Path -LiteralPath $target)) {
                    $broken += ('{0} -> {1}' -f $file.Name, $match.Groups[1].Value)
                }
            }
        }
    }
    if ($broken.Count -gt 0) { throw ('unresolved launcher targets: {0}' -f ($broken -join ', ')) }
}

Test-Case 'the engine references only modules that exist' {
    $content = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    $match = [regex]::Match($content, "foreach \(\`$moduleName in @\(([^)]*)\)")
    if (-not $match.Success) { throw 'could not find the module list in the engine' }
    $names = @([regex]::Matches($match.Groups[1].Value, "'([^']+)'") | ForEach-Object { $_.Groups[1].Value })
    if ($names.Count -eq 0) { throw 'the module list is empty' }
    foreach ($name in $names) {
        $path = Join-Path $moduleDirectory ('{0}.psm1' -f $name)
        if (-not (Test-Path -LiteralPath $path)) { throw ('the engine imports {0} but {1} does not exist' -f $name, $path) }
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'JSON and catalog schema'
# ---------------------------------------------------------------------------

foreach ($file in $jsonFiles) {
    $relative = $file.FullName.Substring($PackageRoot.Length).TrimStart('\')
    Test-Case ('valid JSON: {0}' -f $relative) {
        $raw = Get-Content -LiteralPath $file.FullName -Raw
        [void]($raw | ConvertFrom-Json -ErrorAction Stop)
    }
    Test-Case ('no UTF-8 BOM: {0}' -f $relative) {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            throw 'starts with a UTF-8 BOM, which some JSON readers reject'
        }
    }
}

$catalog = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\catalog\model-catalog.json') -Raw | ConvertFrom-Json

Test-Case 'the catalog declares the four required profiles plus embeddings' {
    $names = @($catalog.profiles.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($required in @('CPU8', 'CPU16', 'CPU32', 'GPU', 'EMBEDDINGS')) {
        if ($names -notcontains $required) { throw ('profile {0} is missing' -f $required) }
    }
}

Test-Case 'every profile tag has a catalog model entry' {
    $tags = @($catalog.models | ForEach-Object { $_.tag })
    $missing = @()
    foreach ($property in $catalog.profiles.PSObject.Properties) {
        foreach ($listName in @('models', 'embeddings')) {
            foreach ($tag in @($property.Value.$listName)) {
                if ($tags -notcontains $tag) { $missing += ('{0}:{1}' -f $property.Name, $tag) }
            }
        }
    }
    if ($missing.Count -gt 0) { throw ('profile tags with no model entry: {0}' -f ($missing -join ', ')) }
}

Test-Case 'every catalog model carries a measured size and a config digest' {
    $bad = @()
    foreach ($model in @($catalog.models)) {
        if (-not $model.bytes -or [int64]$model.bytes -le 0) { $bad += ('{0} (no size)' -f $model.tag) }
        if ([string]::IsNullOrWhiteSpace([string]$model.config_digest)) { $bad += ('{0} (no digest)' -f $model.tag) }
    }
    if ($bad.Count -gt 0) { throw ('unvalidated catalog entries: {0}' -f ($bad -join ', ')) }
}

Test-Case 'every catalog model declares a kind the verifier understands' {
    foreach ($model in @($catalog.models)) {
        if (@('generate', 'embedding') -notcontains [string]$model.kind) {
            throw ('{0} has kind "{1}"' -f $model.tag, $model.kind)
        }
    }
}

Test-Case 'embedding models are never listed as generation models' {
    foreach ($property in $catalog.profiles.PSObject.Properties) {
        foreach ($tag in @($property.Value.models)) {
            $model = @($catalog.models | Where-Object { $_.tag -eq $tag })
            if ($model.Count -gt 0 -and $model[0].kind -eq 'embedding') {
                throw ('{0} lists the embedding model {1} as a generation model; a generation prompt would be sent to it' -f $property.Name, $tag)
            }
        }
    }
}

Test-Case 'the catalog records no unresolved tags' {
    $unresolved = @($catalog.unresolved_tags)
    if ($unresolved.Count -gt 0) { throw ('the catalog still lists unresolved tags: {0}' -f ($unresolved -join ', ')) }
}

Test-Case 'documented profile totals match the sum of their model sizes' {
    foreach ($property in $catalog.profiles.PSObject.Properties) {
        $sum = 0L
        foreach ($tag in (@($property.Value.models) + @($property.Value.embeddings))) {
            $model = @($catalog.models | Where-Object { $_.tag -eq $tag })
            if ($model.Count -gt 0) { $sum += [int64]$model[0].bytes }
        }
        if ([int64]$property.Value.total_bytes -ne $sum) {
            throw ('{0} declares {1} bytes but its models total {2}' -f $property.Name, $property.Value.total_bytes, $sum)
        }
    }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Operator experience guarantees'
# ---------------------------------------------------------------------------

Test-Case 'no typed confirmation prompts (defect D-15)' {
    # The earlier kits made the operator type "CONFIGURE 192.0.2.3" or
    # "APPLY 192.0.2.4" exactly, on every machine, for every stage.
    $offenders = @()
    foreach ($file in $scanFiles) {
        $code = $codeOnly[$file.FullName]
        if ($code -match '(?i)Read-Host[^\r\n]*(CONFIGURE|APPLY|Type exactly|INSTALL|DOWNLOAD)') {
            $offenders += $file.Name
        }
    }
    foreach ($file in $cmdFiles) {
        $content = Get-Content -LiteralPath $file.FullName -Raw
        if ($content -match '(?i)set\s*/p') { $offenders += $file.Name }
    }
    if ($offenders.Count -gt 0) { throw ('typed confirmation prompts in: {0}' -f ($offenders -join ', ')) }
}

Test-Case 'the operator is asked at most about node number and profile' {
    # Any other interactive question would reintroduce keyboard swapping.
    $engineCode = $codeOnly[(Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1')]
    $prompts = @([regex]::Matches($engineCode, "Read-Host\s+'([^']*)'") | ForEach-Object { $_.Groups[1].Value })
    $prompts += @([regex]::Matches($engineCode, "Read-Host\s+\(") | ForEach-Object { 'formatted-prompt' })
    if ($prompts.Count -gt 2) { throw ('{0} interactive prompts found: {1}' -f $prompts.Count, ($prompts -join ' | ')) }
    foreach ($prompt in $prompts) {
        if ($prompt -ne 'formatted-prompt' -and $prompt -notmatch '(?i)node number|profile') {
            throw ('unexpected interactive prompt: "{0}"' -f $prompt)
        }
    }
    # And no other module may prompt at all.
    foreach ($file in $scanFiles) {
        if ($file.Extension -ne '.psm1') { continue }
        if ($codeOnly[$file.FullName] -match 'Read-Host') {
            throw ('{0} prompts the operator; only the engine may ask anything' -f $file.Name)
        }
    }
}

Test-Case 'no fragile native pipeline carries Ollama progress output (defect D-09)' {
    # The download path must not shell out to `ollama pull` at all, and no
    # native command output may cross a PowerShell pipeline, where 5.1 can
    # promote stderr to a terminating error.
    # Scoped to shipped engine and coordinator code. Test scripts legitimately
    # capture a child powershell.exe transcript, and they run under
    # ErrorActionPreference = Continue where the hazard does not arise.
    $shipped = @($scanFiles | Where-Object { $_.FullName -notlike (Join-Path $PackageRoot 'tests\*') })
    $offenders = @()
    foreach ($file in $shipped) {
        $code = $codeOnly[$file.FullName]
        if ($code -match "(?i)'pull'" -and $file.Name -eq 'Capstone.Models.psm1') {
            $offenders += ('{0}: shells out to ollama pull' -f $file.Name)
        }
        if ($code -match '2>&1\s*\|') {
            $offenders += ('{0}: pipes native output through a pipeline' -f $file.Name)
        }
    }
    if ($offenders.Count -gt 0) { throw ($offenders -join '; ') }
}

Test-Case 'the netsh wrapper relaxes ErrorActionPreference around native stderr' {
    # Without this, netsh writing to stderr under the engine's Stop preference
    # throws instead of returning an inspectable result, and the NetTCPIP
    # fallback path can never run.
    $networkCode = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Network.psm1') -Raw
    $wrapperIndex = $networkCode.IndexOf('function Invoke-CapstoneNetsh')
    if ($wrapperIndex -lt 0) { throw 'Invoke-CapstoneNetsh was not found' }
    $wrapperBody = $networkCode.Substring($wrapperIndex, [math]::Min(2000, $networkCode.Length - $wrapperIndex))
    if ($wrapperBody -notmatch "ErrorActionPreference\s*=\s*'Continue'") {
        throw 'Invoke-CapstoneNetsh does not relax ErrorActionPreference before invoking netsh'
    }
    if ($wrapperBody -notmatch 'finally') {
        throw 'Invoke-CapstoneNetsh does not restore ErrorActionPreference'
    }
}

Test-Case 'native process output is captured by redirection, not by piping' {
    $ollamaCode = $codeOnly[(Join-Path $moduleDirectory 'Capstone.Ollama.psm1')]
    if ($ollamaCode -notmatch 'RedirectStandardOutput') {
        throw 'Get-CapstoneExecutableVersion should redirect native output to a file'
    }
    if ($ollamaCode -notmatch 'WaitForExit') {
        throw 'a native process is started without a bounded wait'
    }
}

Test-Case 'no installer is waited on with an unbounded Start-Process -Wait (defect D-04)' {
    $offenders = @()
    foreach ($file in $scanFiles) {
        $code = $codeOnly[$file.FullName]
        if ($code -match 'Start-Process[^\r\n]{0,200}-Wait\b') {
            $offenders += $file.Name
        }
    }
    if ($offenders.Count -gt 0) { throw ('unbounded Start-Process -Wait in: {0}' -f ($offenders -join ', ')) }
}

Test-Case 'network verification is polled, not read once (node 6 false OnLinkRoute failure)' {
    # Regression guard. Measured on node 6 (2026-10-30): two seconds after a
    # netsh apply that passed Address, DhcpDisabled, NoDefaultGateway and
    # NoIpv4Dns, the on-link route had not landed and OnLinkRoute read False.
    # The machine reached the coordinator eight seconds later, proving the
    # configuration was correct. A single early read turned a good result into
    # a failure, which also skipped remote serving.
    $networkCode = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Network.psm1') -Raw
    if ($networkCode -notmatch 'function Wait-CapstoneNetworkState') {
        throw 'Wait-CapstoneNetworkState is missing'
    }
    $applyIndex = $networkCode.IndexOf('function Set-CapstonePrivateAddress')
    if ($applyIndex -lt 0) { throw 'Set-CapstonePrivateAddress was not found' }
    $applyBody = $networkCode.Substring($applyIndex)
    if ($applyBody -notmatch 'Wait-CapstoneNetworkState') {
        throw 'Set-CapstonePrivateAddress still verifies with a single immediate read'
    }
}

Test-Case 'the Ollama server start falls back to the server binary (nodes 5 and 6)' {
    # Regression guard. Measured on nodes 5 and 6 (2026-10-30): a clean install
    # of 0.40.13 succeeded, "ollama app.exe" was started, and no API ever
    # answered within 90 seconds. The tray application alone is not sufficient
    # on a first run.
    $ollamaCode = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Ollama.psm1') -Raw
    $startIndex = $ollamaCode.IndexOf('function Start-CapstoneOllamaServer')
    if ($startIndex -lt 0) { throw 'Start-CapstoneOllamaServer was not found' }
    $startBody = $ollamaCode.Substring($startIndex)
    if ($startBody -notmatch 'Starting the server process directly instead') {
        throw 'there is no fallback to "ollama serve" when the application does not bind'
    }
    if ($startBody -notmatch 'Listeners on port 11434') {
        throw 'a failed start does not record the processes and listeners it observed'
    }
    # And the first-start budget must be generous enough.
    if ($ollamaCode -notmatch '\[int\]\$TimeoutSeconds = 1[0-9][0-9]') {
        throw 'the server start timeout is still under 100 seconds'
    }
}

Test-Case 'the server starts at boot without depending on the tray application' {
    # The core fix for nodes 5 and 6. The tray application only starts at
    # interactive logon and was measured running with no listener at all, so
    # the server must be registered to run at system startup instead.
    $remoteCode = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Remote.psm1') -Raw
    foreach ($required in @('Register-CapstoneOllamaService', 'Start-CapstoneOllamaService', 'Restore-CapstoneTrayAutostart')) {
        if ($remoteCode -notmatch ('function {0}' -f [regex]::Escape($required))) {
            throw ('{0} is missing' -f $required)
        }
    }
    if ($remoteCode -notmatch 'New-ScheduledTaskTrigger -AtStartup') {
        throw 'the task does not trigger at system startup'
    }
    if ($remoteCode -notmatch "UserId 'SYSTEM'") {
        throw 'the task does not run as SYSTEM, so it would still need a logon'
    }
    if ($remoteCode -notmatch "Argument 'serve'") {
        throw 'the task does not run the Ollama server binary'
    }
}

Test-Case 'registering the boot task is reversible if it does not work' {
    $engineCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    if ($engineCode -notmatch 'Restore-CapstoneTrayAutostart') {
        throw 'the engine never restores the tray autostart when the task fails'
    }
    $registerIndex = $engineCode.IndexOf('Register-CapstoneOllamaService')
    $restoreIndex = $engineCode.IndexOf('Restore-CapstoneTrayAutostart')
    if ($registerIndex -lt 0 -or $restoreIndex -lt 0) { throw 'the register/restore pair was not found' }
    if ($restoreIndex -lt $registerIndex) { throw 'the restore happens before the registration' }
}

Test-Case 'a tray app with no listener is not waited on for minutes' {
    # "Waiting rather than starting a competing server" is correct, but waiting
    # the full budget on a process that never binds looks like a hang.
    $ollamaCode = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Ollama.psm1') -Raw
    if ($ollamaCode -notmatch '\$firstWait\s*=\s*30') {
        throw 'there is no short leash for the case where nothing is listening'
    }
}

Test-Case 'launchers cover every supported node, 3 through 15' {
    $launcherDirectory = Join-Path $PackageRoot 'launchers'
    $missing = @()
    foreach ($node in 3..15) {
        foreach ($profileName in @('CPU8', 'CPU16', 'CPU32')) {
            $file = Join-Path $launcherDirectory ('SETUP-NODE-{0}-{1}.cmd' -f $node, $profileName)
            if (-not (Test-Path -LiteralPath $file)) { $missing += ('{0}/{1}' -f $node, $profileName) }
        }
    }
    if ($missing.Count -gt 0) { throw ('missing launchers: {0}' -f ($missing -join ', ')) }
    $actual = @(Get-ChildItem -LiteralPath $launcherDirectory -Filter 'SETUP-NODE-*.cmd' -File).Count
    Assert-Equal 39 $actual '13 nodes x 3 profiles'
}

Test-Case 'the engine accepts node numbers above 9 at its one prompt' {
    # A single-digit regex would silently reject node 10 and up.
    $engineCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    if ($engineCode -match "match '\^\\s\*\(\[3-7\]\)") {
        throw 'the node prompt still only accepts a single digit 3 to 7'
    }
    if ($engineCode -notmatch '3 to 15') { throw 'the node prompt does not offer the full 3 to 15 range' }
}

Test-Case 'the coordinator utility sweeps every supported node by default' {
    $utilityCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'coordinator\Test-CapstoneHeads.ps1') -Raw
    if ($utilityCode -notmatch '\$nodeList\s*=\s*@\(2\.\.15\)') {
        throw 'the default sweep is not nodes 2 through 15'
    }
}

Test-Case 'the coordinator -Nodes parameter survives powershell.exe -File binding' {
    # Regression guard. Declared as [int[]], "-Nodes 2,3,4" passed through
    # -File collapsed into the single node 234, because -File delivers every
    # argument as a string. It must be taken as a string and parsed.
    $utilityCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'coordinator\Test-CapstoneHeads.ps1') -Raw
    if ($utilityCode -match '\[int\[\]\]\$Nodes') {
        throw '-Nodes is still typed [int[]], which mis-binds under -File'
    }
    if ($utilityCode -notmatch '\[string\]\$Nodes') { throw '-Nodes is not declared as a string' }
    if ($utilityCode -notmatch "Nodes -split") { throw '-Nodes is never split into individual node numbers' }
}

Test-Case 'the internet probe is advisory and never gates the Ollama installation' {
    # Regression guard. Measured on node 3 (2026-10-30): the preflight probe
    # reported "could not be reached" on a machine that then downloaded three
    # models successfully. While the probe gated installation, that false
    # negative would have refused to install Ollama on a perfectly capable
    # machine. The download is now the authority.
    $engineCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    if ($engineCode -match 'elseif\s*\(-not\s+\$facts\.internet\.Available\)') {
        throw 'the internet probe is still used as a branch that skips Install-CapstoneOllama'
    }
    $installIndex = $engineCode.IndexOf('$install = Install-CapstoneOllama')
    if ($installIndex -lt 0) { throw 'the install call was not found' }
    # Everything between the "not installed" test and the install call must not
    # contain a guard on the probe.
    $notInstalledIndex = $engineCode.IndexOf('if (-not $run.OllamaInstalled)')
    if ($notInstalledIndex -lt 0) { throw 'the install branch was not found' }
    $between = $engineCode.Substring($notInstalledIndex, $installIndex - $notInstalledIndex)
    if ($between -match 'Cannot install Ollama without internet') {
        throw 'the engine still refuses to install when the probe says unreachable'
    }
}

Test-Case 'the internet probe treats any HTTP status as proof the host answered' {
    $preflightCode = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Preflight.psm1') -Raw
    if ($preflightCode -match "Method\s*=\s*'HEAD'" -or $preflightCode -match '-Method Head') {
        throw 'the probe still uses HEAD, which some paths do not answer'
    }
    if ($preflightCode -notmatch 'ProtocolError') {
        throw 'the probe does not treat a protocol error as proof of reachability'
    }
}

Test-Case 'remote serving is configured after model downloads, not before (defect D-06)' {
    $engineCode = Get-Content -LiteralPath (Join-Path $PackageRoot 'engine\Invoke-CapstoneProvisioner.ps1') -Raw
    $modelStage = $engineCode.IndexOf('Stage 5 of 7: model downloads')
    $remoteStage = $engineCode.IndexOf('Stage 6 of 7: remote serving')
    if ($modelStage -lt 0 -or $remoteStage -lt 0) { throw 'the stage markers were not found' }
    if ($remoteStage -lt $modelStage) { throw 'remote serving runs before model downloads' }
}

# ---------------------------------------------------------------------------
Start-TestGroup 'Security posture'
# ---------------------------------------------------------------------------

Test-Case 'nothing disables the firewall or antivirus' {
    $offenders = @()
    foreach ($file in ($scanFiles + $cmdFiles)) {
        $content = Get-Content -LiteralPath $file.FullName -Raw
        if ($content -match 'Set-NetFirewallProfile[^\r\n]*-Enabled\s+False' -or
            $content -match 'netsh\s+advfirewall\s+set[^\r\n]*state\s+off' -or
            $content -match 'Set-MpPreference[^\r\n]*DisableRealtimeMonitoring') {
            $offenders += $file.Name
        }
    }
    if ($offenders.Count -gt 0) { throw ('security controls disabled in: {0}' -f ($offenders -join ', ')) }
}

Test-Case 'every download URL is HTTPS and on an official Ollama host' {
    $bad = @()
    foreach ($file in $scanFiles) {
        $content = $codeOnly[$file.FullName]
        foreach ($match in [regex]::Matches($content, 'https?://[A-Za-z0-9\.\-]+')) {
            $url = $match.Value
            if ($url -match '^http://(127\.0\.0\.1|localhost|192\.0\.2\.|0\.0\.0\.0)') { continue }
            if ($url -match "^http://'" ) { continue }
            if ($url -notmatch '^https://') { $bad += ('{0}: {1}' -f $file.Name, $url); continue }
            if ($url -notmatch '^https://(ollama\.com|registry\.ollama\.ai|docs\.ollama\.com)$') {
                $bad += ('{0}: {1}' -f $file.Name, $url)
            }
        }
    }
    if ($bad.Count -gt 0) { throw ('non-official or non-HTTPS URLs: {0}' -f (($bad | Select-Object -Unique) -join '; ')) }
}

Test-Case 'the installer signature is verified before it is executed' {
    $content = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Ollama.psm1') -Raw
    $signatureIndex = $content.IndexOf('Test-CapstoneInstallerSignature -Path $installer')
    $startIndex = $content.IndexOf('Start-Process -FilePath $installer')
    if ($signatureIndex -lt 0) { throw 'the signature check was not found' }
    if ($startIndex -lt 0) { throw 'the installer invocation was not found' }
    if ($signatureIndex -gt $startIndex) { throw 'the installer is executed before its signature is verified' }
}

Test-Case 'no credential or cloud sign-in is embedded' {
    $offenders = @()
    foreach ($file in ($scanFiles + $cmdFiles)) {
        $content = Get-Content -LiteralPath $file.FullName -Raw
        if ($content -match '(?i)ollama\s+(signin|login)' -or $content -match '(?i)api[_-]?key\s*=\s*[''"][^''"]{8,}') {
            $offenders += $file.Name
        }
    }
    if ($offenders.Count -gt 0) { throw ('credential or cloud sign-in found in: {0}' -f ($offenders -join ', ')) }
}

Test-Case 'the firewall rule is scoped to the coordinator, not to any address' {
    $content = Get-Content -LiteralPath (Join-Path $moduleDirectory 'Capstone.Remote.psm1') -Raw
    if ($content -notmatch '-RemoteAddress \$CoordinatorIp') { throw 'New-NetFirewallRule does not restrict -RemoteAddress' }
    if ($content -match '-RemoteAddress\s+Any') { throw 'the firewall rule admits any remote address' }
}

# ---------------------------------------------------------------------------

$summary = Write-TestSummary -Title 'STATIC CHECK SUMMARY'

if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    $directory = Split-Path -Parent $ResultPath
    if ($directory -and -not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $payload = ([pscustomobject]@{
        suite = 'static-checks'
        generated_at = (Get-Date).ToString('o')
        powershell_version = $PSVersionTable.PSVersion.ToString()
        total = $summary.Total; passed = $summary.Passed; failed = $summary.Failed
        results = $summary.Results
    }) | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($ResultPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ('  Results written to {0}' -f $ResultPath) -ForegroundColor Cyan
}

if ($summary.Failed -gt 0) { exit 1 }
exit 0
