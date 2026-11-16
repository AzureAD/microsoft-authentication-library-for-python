param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OpenSslRoot = Join-Path $RepoRoot "build\vcpkg_installed\x64-windows"
$PreviousOpenSslDir = $env:OPENSSL_DIR
$PreviousOpenSslLibDir = $env:OPENSSL_LIB_DIR
$PreviousPath = $env:PATH

function Set-OpenSslConfiguration {
    param([string]$Name)

    $env:OPENSSL_DIR = $OpenSslRoot
    $env:OPENSSL_LIB_DIR = if ($Name -eq "Debug") {
        Join-Path $OpenSslRoot "debug\lib"
    } else {
        Join-Path $OpenSslRoot "lib"
    }
    $NativePath = Join-Path $RepoRoot "build\$Name"
    $env:PATH = "$NativePath;$PreviousPath"
}

function Invoke-NativeTests {
    param([string]$Name)

    $Directory = Join-Path $RepoRoot "artifacts\$Name"
    Write-Host "Running native tests against Rust $Name DLLs..."
    & (Join-Path $Directory "native_provider_test.exe") `
        (Join-Path $Directory "microsoft_identity_native_provider.dll")
    if ($LASTEXITCODE -ne 0) {
        throw "Rust $Name provider signing test failed with exit code $LASTEXITCODE."
    }
    & (Join-Path $Directory "native_integration_test.exe") `
        (Join-Path $Directory "microsoft_identity_native_provider.dll") `
        (Join-Path $Directory "native_provider_test.exe") `
        (Join-Path $Directory "native_test_pki.exe") `
        (Join-Path $Directory "native_mtls_test_server.exe") `
        (Join-Path $Directory "test-artifacts")
    if ($LASTEXITCODE -ne 0) {
        throw "Rust $Name native integration failed with exit code $LASTEXITCODE."
    }
}

try {
    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot "build.ps1") -Configuration All
    }
    Set-OpenSslConfiguration "Debug"
    Push-Location $RepoRoot
    try {
        & cargo fmt --all -- --check
        if ($LASTEXITCODE -ne 0) {
            throw "cargo fmt --check failed."
        }
        & cargo check --locked --workspace --all-targets --all-features
        if ($LASTEXITCODE -ne 0) {
            throw "cargo check failed."
        }
        & cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
        if ($LASTEXITCODE -ne 0) {
            throw "cargo clippy failed."
        }
        & cargo test --locked --workspace --all-features
        if ($LASTEXITCODE -ne 0) {
            throw "Debug cargo test failed."
        }
    }
    finally {
        Pop-Location
    }

    Set-OpenSslConfiguration "Release"
    Push-Location $RepoRoot
    try {
        & cargo test --locked --release --workspace --all-features
        if ($LASTEXITCODE -ne 0) {
            throw "Release cargo test failed."
        }
    }
    finally {
        Pop-Location
    }

    foreach ($Name in @("Debug", "Release")) {
        Invoke-NativeTests $Name
    }

    Set-OpenSslConfiguration "Debug"
    Push-Location $RepoRoot
    try {
        & cargo run --locked -p rust-tests --bin key-audit
        if ($LASTEXITCODE -ne 0) {
            throw "Project-specific CNG key audit failed."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:OPENSSL_DIR = $PreviousOpenSslDir
    $env:OPENSSL_LIB_DIR = $PreviousOpenSslLibDir
    $env:PATH = $PreviousPath
}
