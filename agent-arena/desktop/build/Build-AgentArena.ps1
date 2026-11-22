<#
  Builds the Agent Arena desktop package with the in-box .NET Framework compiler.
  Output: dist\Agent-Arena-<version>\ and Agent-Arena-<version>.zip (+ SHA-256 manifest).
  Inputs are all local: source, the approved Arena frontend files, and the
  Microsoft-signed WebView2 SDK assemblies in third_party\.
#>
param([ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = '2.1.0', [string]$OutRoot = '')   # verification builds go elsewhere, never over a shipped package
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSScriptRoot
$repo = Resolve-Path (Join-Path $here '..')
$static = Join-Path $repo 'frontend'
$wv = Join-Path $here 'third_party\webview2\1.0.4314.35'
$outputRoot = [IO.Path]::GetFullPath($(if ($OutRoot) { $OutRoot } else { Join-Path $repo 'dist' }))
$out = [IO.Path]::GetFullPath((Join-Path $outputRoot "Agent-Arena-$Version"))
if (-not $out.StartsWith($outputRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Package output must be inside its output root' }
$app = Join-Path $out 'app'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$fx = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'

foreach ($dll in 'Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll') {
    $sig = Get-AuthenticodeSignature (Join-Path $wv $dll)
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw "$dll is not validly signed by Microsoft" }
}

# The current versioned rig is the art baseline. Legacy art remains historical.
$tpl = '(?s)<template id="actor-template">.*?</template>'
$desktop = [regex]::Match((Get-Content (Join-Path $static 'agent_arena_desktop.html') -Raw), $tpl).Value
# Line endings don't count: git checks the HTML out with CRLF on Windows and the SVG with LF.
$approved = (Get-Content (Join-Path $static 'assets\classic-arcade\classic-refined.svg') -Raw).Replace("`r`n", "`n").Trim()
$embedded = [regex]::Match($desktop, '(?s)<svg\b.*?</svg>').Value.Replace("`r`n", "`n").Trim()
if (-not $embedded -or $approved -ne $embedded) { throw 'The embedded character must match the classic-refined.svg art baseline' }
foreach ($part in 'head', 'torso', 'left-arm', 'right-arm', 'left-leg', 'right-leg', 'gaze') {
    if ($approved -notmatch ('class="[^"]*\b' + $part + '\b')) { throw "Classic Refined rig missing part: $part" }
}

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $app 'web\css'), (Join-Path $app 'web\js'), (Join-Path $out 'docs') | Out-Null

$refs = 'System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Management.dll','System.Security.dll','System.Web.Extensions.dll','Microsoft.CSharp.dll' |
    ForEach-Object { '/reference:' + (Join-Path $fx $_) }
$refs += ('/reference:' + (Join-Path $wv 'Microsoft.Web.WebView2.Core.dll'))
$refs += ('/reference:' + (Join-Path $wv 'Microsoft.Web.WebView2.WinForms.dll'))
$sources = @(Get-ChildItem (Join-Path $here 'src') -Filter *.cs | ForEach-Object FullName)
# -Version is the only source of the program's version: the exe, setup, diagnostics and package always agree.
$buildInfo = Join-Path ([IO.Path]::GetTempPath()) "AgentArena-BuildInfo-$Version-$PID.g.cs"
@"
[assembly: System.Reflection.AssemblyVersion("$Version.0")]
[assembly: System.Reflection.AssemblyFileVersion("$Version.0")]
[assembly: System.Reflection.AssemblyInformationalVersion("$Version")]
namespace AgentArena { static class BuildInfo { public const string Version = "$Version"; } }
"@ | Set-Content $buildInfo -Encoding UTF8
$sources += $buildInfo
$icon = Join-Path $here 'build\arena.ico'
& (Join-Path $here 'build\New-ArenaIcon.ps1') -Out $icon | Out-Null
$manifest = Join-Path $here 'build\AgentArena.exe.manifest'
& $csc /nologo /target:winexe /platform:x64 /optimize+ /langversion:5 "/out:$(Join-Path $app 'AgentArena.exe')" "/win32icon:$icon" "/win32manifest:$manifest" @refs @sources
$cscExit = $LASTEXITCODE
Remove-Item $buildInfo -ErrorAction SilentlyContinue
if ($cscExit -ne 0) { throw "csc failed with exit code $cscExit" }

Copy-Item (Join-Path $wv '*.dll') $app
Copy-Item (Join-Path $wv 'LICENSE.txt') (Join-Path $app 'WebView2-LICENSE.txt')
Copy-Item (Join-Path $here 'arena-deployment.json') $app
Copy-Item (Join-Path $here 'build\AgentArena.exe.config') $app      # per-monitor DPI awareness
Copy-Item (Join-Path $static 'agent_arena_desktop.html') (Join-Path $app 'web')
Copy-Item (Join-Path $static 'css\*.css') (Join-Path $app 'web\css')
Copy-Item (Join-Path $static 'js\*.js'), (Join-Path $static 'js\*.mjs') (Join-Path $app 'web\js')
New-Item -ItemType Directory -Force (Join-Path $app 'web\assets') | Out-Null
Copy-Item (Join-Path $static 'assets\classic-arcade') (Join-Path $app 'web\assets') -Recurse
# Live rooms: the room page and exactly the assets it can load. tools\room_assets.mjs writes the
# list from the kits' catalogs (run it after changing rooms or kits); the kits' sources stay out.
Copy-Item (Join-Path $static 'arena-room.html') (Join-Path $app 'web')
foreach ($rel in Get-Content (Join-Path $static 'assets\room-assets.txt') | Where-Object { $_ -and -not $_.StartsWith('#') }) {
    # .NET Framework keeps the 260-character path limit; leave room for the install folder.
    if ($rel.Length -gt 100) { throw "Room asset path too long for an install under the user profile: $rel" }
    $src = Join-Path $static ('assets\' + $rel.Replace('/', '\'))
    if (-not (Test-Path $src)) { throw "Room asset missing: $rel (run node tools\room_assets.mjs)" }
    if ((Get-Item $src).Length -lt 1024 -and (Get-Content $src -TotalCount 1) -like 'version https://git-lfs*') { throw "Room asset is a Git LFS pointer, not the file: $rel (run git lfs pull)" }
    $dst = Join-Path $app ('web\assets\' + $rel.Replace('/', '\'))
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $src $dst -Force
}

$files = [ordered]@{}
Get-ChildItem $app -Recurse -File | Sort-Object FullName | ForEach-Object {
    $files[$_.FullName.Substring($app.Length + 1).Replace('\', '/')] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
}
[ordered]@{ product = 'Agent Arena'; version = $Version; built_at = (Get-Date).ToString('o'); files = $files } |
    ConvertTo-Json -Depth 4 | Set-Content (Join-Path $app 'app-manifest.json') -Encoding UTF8

Copy-Item (Join-Path $here 'package\*') $out -Recurse
# cmd.exe needs CRLF line endings for reliable labels and goto.
Get-ChildItem $out -File | Where-Object { $_.Extension -in '.cmd', '.txt' } | ForEach-Object {
    $text = [IO.File]::ReadAllText($_.FullName) -replace "`r?`n", "`r`n"
    [IO.File]::WriteAllText($_.FullName, $text, (New-Object Text.UTF8Encoding $false))
}
Copy-Item (Join-Path $here 'docs\*') (Join-Path $out 'docs') -Recurse
Get-ChildItem $out -Recurse -File | Where-Object Name -ne 'SHA256SUMS.txt' | Sort-Object FullName | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash $_.FullName).Hash, $_.FullName.Substring($out.Length + 1)
} | Set-Content (Join-Path $out 'SHA256SUMS.txt') -Encoding UTF8

$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $out -DestinationPath $zip
$hash = (Get-FileHash $zip).Hash
[ordered]@{ package = Split-Path $zip -Leaf; version = $Version; sha256 = $hash; bytes = (Get-Item $zip).Length; built_at = (Get-Date).ToString('o') } |
    ConvertTo-Json | Set-Content "$out.manifest.json" -Encoding UTF8
"built $zip  sha256 $hash"
