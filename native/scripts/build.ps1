param(
    [ValidateSet("Debug", "Release", "All")]
    [string]$Configuration = "All"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OpenSslRoot = Join-Path $RepoRoot "build\vcpkg_installed\x64-windows"
$ArtifactRoot = Join-Path $RepoRoot "artifacts"

if (-not (Test-Path (Join-Path $OpenSslRoot "include\openssl\ssl.h"))) {
    throw "Project-local OpenSSL 3.6 installation was not found at $OpenSslRoot."
}
$RustVersion = (& rustc --version)
if ($LASTEXITCODE -ne 0 -or $RustVersion -notmatch '^rustc 1\.97\.1 ') {
    throw "Rust stable 1.97.1 x86_64-pc-windows-msvc is required; found '$RustVersion'."
}

$PreviousOpenSslDir = $env:OPENSSL_DIR
$PreviousOpenSslLibDir = $env:OPENSSL_LIB_DIR

function Build-Configuration {
    param([string]$Name)

    $IsDebug = $Name -eq "Debug"
    $env:OPENSSL_DIR = $OpenSslRoot
    $env:OPENSSL_LIB_DIR = if ($IsDebug) {
        Join-Path $OpenSslRoot "debug\lib"
    } else {
        Join-Path $OpenSslRoot "lib"
    }

    Push-Location $RepoRoot
    try {
        if ($IsDebug) {
            & cargo build --locked -p cng-provider -p mtls-bridge
            $CargoOutput = Join-Path $RepoRoot "target\debug"
        } else {
            & cargo build --locked --release -p cng-provider -p mtls-bridge
            $CargoOutput = Join-Path $RepoRoot "target\release"
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Cargo $Name build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }

    $CMake = Get-Command cmake -ErrorAction SilentlyContinue
    if ($null -eq $CMake) {
        $CMakePath = "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    } else {
        $CMakePath = $CMake.Source
    }
    if (-not (Test-Path -LiteralPath $CMakePath)) {
        throw "CMake was not found. Configure the native test harness before building."
    }
    & $CMakePath --build (Join-Path $RepoRoot "build") --config $Name `
        --target native_provider_test native_test_pki native_mtls_test_server native_integration_test microsoft_identity_native_example `
        --parallel
    if ($LASTEXITCODE -ne 0) {
        throw "Native $Name test-harness build failed with exit code $LASTEXITCODE."
    }

    $Destination = Join-Path $ArtifactRoot $Name
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item `
        (Join-Path $CargoOutput "microsoft_identity_native_provider.dll"), `
        (Join-Path $CargoOutput "microsoft_identity_native_primitives.dll") `
        -Destination $Destination -Force

    $NativeReference = Join-Path $RepoRoot "build\$Name"
    foreach ($File in @(
        "native_provider_test.exe",
        "native_test_pki.exe",
        "native_mtls_test_server.exe",
        "native_integration_test.exe",
        "microsoft_identity_native_example.exe",
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll"
    )) {
        $Source = Join-Path $NativeReference $File
        if (-not (Test-Path $Source)) {
            throw "Required reference test/runtime artifact is missing: $Source"
        }
        Copy-Item $Source -Destination $Destination -Force
    }
    $Hashes = [ordered]@{}
    foreach ($File in @(
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll",
        "microsoft_identity_native_provider.dll",
        "microsoft_identity_native_primitives.dll"
    )) {
        $Hashes[$File] = (Get-FileHash -LiteralPath (Join-Path $Destination $File) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $Revision = (& git -C $RepoRoot rev-parse HEAD)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to identify the native source revision."
    }
    [ordered]@{
        schema_version = 1
        abi_version = 1
        development = $true
        build_id = "$Revision-$Name-local"
        files = $Hashes
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $Destination "native-manifest.json") -Encoding utf8
    Write-Host "Staged Rust $Name artifacts at $Destination"
}

try {
    $Configurations = if ($Configuration -eq "All") {
        @("Debug", "Release")
    } else {
        @($Configuration)
    }
    foreach ($Name in $Configurations) {
        Build-Configuration $Name
    }
}
finally {
    $env:OPENSSL_DIR = $PreviousOpenSslDir
    $env:OPENSSL_LIB_DIR = $PreviousOpenSslLibDir
}
