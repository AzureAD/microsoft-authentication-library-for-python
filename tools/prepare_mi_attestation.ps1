[CmdletBinding()]
param([Parameter(Mandatory)][string]$DestinationDirectory)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$destination = [IO.Path]::GetFullPath($DestinationDirectory)
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$package = "microsoft.azure.security.keyguardattestation"
$version = "1.1.8"
$archive = Join-Path $destination "$package.$version.nupkg"
$dll = Join-Path $destination "AttestationClientLib.dll"
Invoke-WebRequest -Uri "https://www.nuget.org/api/v2/package/$package/$version" -OutFile $archive
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archive)
try {
    $entry = $zip.GetEntry("runtimes/win-x64/native/AttestationClientLib.dll")
    if ($null -eq $entry) { throw "The pinned package lacks its Windows x64 attestation DLL." }
    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dll, $true)
} finally {
    $zip.Dispose()
}
$hash = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash
if ($hash -ne "1c3cd187a44ca5d2f48aea30e7142aa93435de45ed47a60fd3eaad9107f93973") {
    throw "The pinned KeyGuardAttestation 1.1.8 DLL failed SHA256 verification."
}
$signature = Get-AuthenticodeSignature -LiteralPath $dll
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
    throw "The pinned attestation DLL must have a valid Microsoft Authenticode signature."
}
Write-Output $dll
