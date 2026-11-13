[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$NativeArtifacts,
    [string]$AttestationLibrary,
    [Parameter(Mandatory)][string]$WheelDirectory
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$artifacts = (Resolve-Path -LiteralPath $NativeArtifacts).Path
$manifestPath = Join-Path $artifacts "native-manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$nativeFiles = @("libcrypto-3-x64.dll", "libssl-3-x64.dll",
    "microsoft_identity_native_provider.dll", "microsoft_identity_native_primitives.dll")
if ($manifest.schema_version -ne 1 -or $manifest.abi_version -ne 1 -or
    $manifest.development -ne $true -or -not $manifest.build_id -or
    @(Compare-Object @($manifest.files.psobject.Properties.Name) $nativeFiles).Count -ne 0) {
    throw "Only compatible, explicitly development-marked native candidates can be bundled here."
}
foreach ($name in $nativeFiles) {
    $hash = (Get-FileHash -LiteralPath (Join-Path $artifacts $name) -Algorithm SHA256).Hash
    if ($hash -ne $manifest.files.$name) { throw "Native candidate integrity check failed for $name." }
}
$projects = @("msal-native-primitives")
if ($AttestationLibrary) {
    $attestation = (Resolve-Path -LiteralPath $AttestationLibrary).Path
    $expected = "1c3cd187a44ca5d2f48aea30e7142aa93435de45ed47a60fd3eaad9107f93973"
    if ((Get-FileHash -LiteralPath $attestation -Algorithm SHA256).Hash -ne $expected) {
        throw "The attestation DLL must match the pinned KeyGuardAttestation 1.1.8 test dependency."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $attestation
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
        throw "The pinned attestation DLL must have a valid Microsoft Authenticode signature."
    }
    $projects += "msal-key-attestation"
}
$nativeDestination = Join-Path $root "packages\msal-native-primitives\msal_native_primitives\lib"
$attestationDestination = Join-Path $root "packages\msal-key-attestation\msal_key_attestation\lib"
New-Item -ItemType Directory -Force -Path $nativeDestination, $attestationDestination, $WheelDirectory | Out-Null
foreach ($name in $nativeFiles + "native-manifest.json") {
    Copy-Item -LiteralPath (Join-Path $artifacts $name) -Destination $nativeDestination -Force
}
if ($AttestationLibrary) {
    Copy-Item -LiteralPath $attestation -Destination (Join-Path $attestationDestination "AttestationClientLib.dll") -Force
    [ordered]@{
        schema_version = 1
        abi_version = 1
        development = $true
        build_id = "KeyGuardAttestation-1.1.8-$expected"
        files = @{ "AttestationClientLib.dll" = $expected }
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (
        Join-Path $attestationDestination "attestation-manifest.json") -Encoding utf8
}
foreach ($project in $projects) {
    & python -m pip wheel --no-deps --no-build-isolation --wheel-dir $WheelDirectory (Join-Path $root "packages\$project")
    if ($LASTEXITCODE -ne 0) { throw "Building the $project local wheel failed." }
}
