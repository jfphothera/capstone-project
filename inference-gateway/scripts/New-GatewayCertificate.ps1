<#
Issues the Gateway's TLS server certificate from the Gateway's own lab CA (PowerShell 7+, .NET only).

- CA: ECDSA P-384, 5 years, in -CaDirectory (ca.pem public, ca.key private). The CA
  folder is restricted to SYSTEM and Administrators; move ca.key offline after use.
- Server: ECDSA P-256, at most 397 days and never beyond the CA, SAN = -IpAddress
  (+ -DnsName), EKU serverAuth. Written to -OutputDirectory as server.pem/server.key,
  with inference-gateway-ca.pem (public, for clients).
- The output folder ACL is set before any key is written: SYSTEM and Administrators
  full; -KeyReaderAccount (the Gateway service account) read. Arena and Users get nothing.
- Nothing is added to a Windows certificate store; no network or service is touched.
Prints one JSON object. Exit 0 success, 1 failed check, 2 bad input.
#>
param(
    [Parameter(Mandatory=$true)][string]$CaDirectory,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string[]]$IpAddress = @('192.0.2.254'),
    [string[]]$DnsName = @(),
    [ValidateRange(1, 397)][int]$ValidDays = 397,
    [string]$KeyReaderAccount,
    [string]$AdministratorAccount,
    [switch]$NewCa,
    [switch]$Renew,
    [switch]$PlanOnly
)
$ErrorActionPreference = 'Stop'
function Out-Result($Object, [int]$Code) { $Object | ConvertTo-Json -Depth 5 -Compress; exit $Code }
try {
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7 or later is required' }
    foreach ($path in $CaDirectory, $OutputDirectory) { if (-not [System.IO.Path]::IsPathRooted($path)) { throw 'Use absolute folders' } }
    # -File passes "a,b" as one string; accept both forms.
    $ips = @($IpAddress | ForEach-Object { $_ -split ',' } | Where-Object { $_ } | ForEach-Object { [System.Net.IPAddress]::Parse($_.Trim()) })
    $DnsName = @($DnsName | ForEach-Object { $_ -split ',' } | Where-Object { $_ } | ForEach-Object { $_.Trim() })
    if (-not $ips.Count) { throw 'At least one IP SAN is required' }
} catch { Out-Result ([ordered]@{ ok = $false; error = "$_" }) 2 }
$X = 'System.Security.Cryptography.X509Certificates'
$caCert = Join-Path $CaDirectory 'ca.pem'; $caKey = Join-Path $CaDirectory 'ca.key'
$serverCert = Join-Path $OutputDirectory 'server.pem'; $serverKey = Join-Path $OutputDirectory 'server.key'
$clientCa = Join-Path $OutputDirectory 'inference-gateway-ca.pem'
$san = @($ips | ForEach-Object { "IP:$_" }) + @($DnsName | ForEach-Object { "DNS:$_" })
$plan = [ordered]@{ ca_directory = $CaDirectory; output_directory = $OutputDirectory; san = $san; valid_days = $ValidDays
    create_ca = -not (Test-Path -LiteralPath $caCert); files = @($serverCert, $serverKey, $clientCa) }
if ($PlanOnly) { Out-Result ([ordered]@{ ok = $true; applied = $false; plan = $plan }) 0 }
try {
    if ((Test-Path -LiteralPath $serverCert) -and -not $Renew) { throw 'A server certificate exists; pass -Renew to replace it' }
    if (-not (Test-Path -LiteralPath $caCert) -and -not $NewCa) { throw 'No CA in -CaDirectory; pass -NewCa to create one' }
    $admin = if ($AdministratorAccount) { $AdministratorAccount } else { '*S-1-5-32-544' }
    function Protect([string]$Folder) {
        New-Item -ItemType Directory -Force -Path $Folder | Out-Null
        & icacls $Folder /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' "${admin}:(OI)(CI)F" /Q | Out-Null
        if ($LASTEXITCODE) { throw "icacls failed for $Folder" }
    }
    function Write-Pem([string]$Path, [string]$Label, [byte[]]$Bytes) {
        [System.IO.File]::WriteAllText($Path, (-join [System.Security.Cryptography.PemEncoding]::Write($Label, $Bytes)) + "`n")
    }
    Protect $CaDirectory
    Protect $OutputDirectory
    $now = [DateTimeOffset]::UtcNow
    if (-not (Test-Path -LiteralPath $caCert)) {
        $caEc = [System.Security.Cryptography.ECDsa]::Create([System.Security.Cryptography.ECCurve+NamedCurves]::nistP384)
        $request = New-Object "$X.CertificateRequest" 'CN=Inference Gateway Lab CA', $caEc, ([System.Security.Cryptography.HashAlgorithmName]::SHA384)
        $request.CertificateExtensions.Add((New-Object "$X.X509BasicConstraintsExtension" $true, $true, 0, $true))
        $request.CertificateExtensions.Add((New-Object "$X.X509KeyUsageExtension" ([System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]'KeyCertSign, CrlSign'), $true))
        $request.CertificateExtensions.Add((New-Object "$X.X509SubjectKeyIdentifierExtension" $request.PublicKey, $false))
        $ca = $request.CreateSelfSigned($now.AddHours(-1), $now.AddDays(1825))
        Write-Pem $caKey 'PRIVATE KEY' $caEc.ExportPkcs8PrivateKey()
        Write-Pem $caCert 'CERTIFICATE' $ca.RawData
        $created = $true
    } else { $created = $false }
    $ca = [System.Security.Cryptography.X509Certificates.X509Certificate2]::CreateFromPemFile($caCert, $caKey)
    $ec = [System.Security.Cryptography.ECDsa]::Create([System.Security.Cryptography.ECCurve+NamedCurves]::nistP256)
    $request = New-Object "$X.CertificateRequest" "CN=$($ips[0])", $ec, ([System.Security.Cryptography.HashAlgorithmName]::SHA256)
    $names = New-Object "$X.SubjectAlternativeNameBuilder"
    foreach ($ip in $ips) { $names.AddIpAddress($ip) }
    foreach ($dns in $DnsName) { $names.AddDnsName($dns) }
    $request.CertificateExtensions.Add($names.Build($false))
    $request.CertificateExtensions.Add((New-Object "$X.X509BasicConstraintsExtension" $false, $false, 0, $true))
    $request.CertificateExtensions.Add((New-Object "$X.X509KeyUsageExtension" ([System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]'DigitalSignature'), $true))
    $usage = New-Object System.Security.Cryptography.OidCollection; [void]$usage.Add((New-Object System.Security.Cryptography.Oid '1.3.6.1.5.5.7.3.1'))
    $request.CertificateExtensions.Add((New-Object "$X.X509EnhancedKeyUsageExtension" $usage, $false))
    $request.CertificateExtensions.Add((New-Object "$X.X509SubjectKeyIdentifierExtension" $request.PublicKey, $false))
    $notAfter = $now.AddDays($ValidDays); if ($notAfter -gt $ca.NotAfter.ToUniversalTime()) { $notAfter = [DateTimeOffset]$ca.NotAfter.ToUniversalTime() }
    $serial = [byte[]]::new(16); [System.Security.Cryptography.RandomNumberGenerator]::Fill($serial); $serial[0] = $serial[0] -band 0x7F
    $server = $request.Create($ca, $now.AddHours(-1), $notAfter, $serial)
    Write-Pem $serverKey 'PRIVATE KEY' $ec.ExportPkcs8PrivateKey()
    Write-Pem $serverCert 'CERTIFICATE' $server.RawData
    Copy-Item -LiteralPath $caCert -Destination $clientCa -Force
    if ($KeyReaderAccount) {
        # The service account reads its certificate and key; it never gets the CA key.
        foreach ($file in $serverCert, $serverKey) { & icacls $file /grant "${KeyReaderAccount}:R" /Q | Out-Null; if ($LASTEXITCODE) { throw "icacls failed for $file" } }
    }
    Out-Result ([ordered]@{ ok = $true; applied = $true; ca_created = $created; san = $san
        server_certificate = $serverCert; server_key = $serverKey; ca_file_for_clients = $clientCa
        not_after = $server.NotAfter.ToUniversalTime().ToString('o'); ca_not_after = $ca.NotAfter.ToUniversalTime().ToString('o')
        ca_sha256 = [System.Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($ca.RawData)).ToLower() }) 0
} catch { Out-Result ([ordered]@{ ok = $false; error = "$_" }) 1 }
