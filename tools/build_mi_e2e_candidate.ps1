[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$WheelDirectory
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path $PSScriptRoot -Parent
$native = Join-Path $root "native"
$wheels = [IO.Path]::GetFullPath($WheelDirectory)
$tools = Join-Path $wheels "tools"
New-Item -ItemType Directory -Force -Path $wheels, $tools | Out-Null

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -latest -products * -version "[17.0,18.0)" `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $vs) {
    throw "The MI runner requires Visual Studio 2022 with the C++ build tools."
}
$cmake = Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if (-not (Test-Path -LiteralPath $cmake -PathType Leaf)) {
    throw "The MI runner requires the Visual Studio CMake tools."
}

$previousPath = $env:PATH
$previousCargoHome = $env:CARGO_HOME
$previousRustupHome = $env:RUSTUP_HOME
$previousToolchain = $env:RUSTUP_TOOLCHAIN
try {
    # Keep bootstrap state separate from other SDK jobs on the shared runner.
    $env:CARGO_HOME = Join-Path $tools "cargo"
    $env:RUSTUP_HOME = Join-Path $tools "rustup"
    $env:RUSTUP_TOOLCHAIN = "1.97.1-x86_64-pc-windows-msvc"
    $installer = Join-Path $tools "rustup-init.exe"
    $installerUrl = "https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-pc-windows-msvc/rustup-init.exe"
    Invoke-WebRequest -Uri $installerUrl -OutFile $installer
    $expectedHash = ((Invoke-RestMethod "$installerUrl.sha256") -split "\s+")[0]
    if ($expectedHash -notmatch "^[0-9a-fA-F]{64}$" -or
        (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $expectedHash) {
        throw "The Rust installer failed SHA256 verification."
    }
    & $installer -y --no-modify-path --profile minimal --default-toolchain $env:RUSTUP_TOOLCHAIN
    if ($LASTEXITCODE -ne 0) { throw "Installing the pinned Rust toolchain failed." }
    $env:PATH = "$(Join-Path $env:CARGO_HOME 'bin');$(Split-Path $cmake -Parent);$previousPath"

    $baseline = (Get-Content (Join-Path $native "vcpkg.json") -Raw | ConvertFrom-Json).'builtin-baseline'
    if ($baseline -notmatch "^[0-9a-f]{40}$") { throw "The native vcpkg baseline is not a commit ID." }
    $vcpkg = Join-Path $tools "vcpkg"
    & git init --quiet $vcpkg
    if ($LASTEXITCODE -ne 0) { throw "Initializing the candidate vcpkg checkout failed." }
    & git -C $vcpkg remote add origin https://github.com/microsoft/vcpkg.git
    if ($LASTEXITCODE -ne 0) { throw "Configuring the candidate vcpkg remote failed." }
    & git -C $vcpkg fetch --quiet --depth=1 origin $baseline
    if ($LASTEXITCODE -ne 0) { throw "Fetching the pinned vcpkg baseline failed." }
    & git -C $vcpkg checkout --quiet --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw "Checking out the pinned vcpkg baseline failed." }

    & $cmake -S $native -B (Join-Path $native "build") -G "Visual Studio 17 2022" -A x64 `
        "-DCMAKE_TOOLCHAIN_FILE=$(Join-Path $vcpkg 'scripts\buildsystems\vcpkg.cmake')" `
        -DVCPKG_TARGET_TRIPLET=x64-windows
    if ($LASTEXITCODE -ne 0) { throw "Configuring the native candidate failed." }
    & (Join-Path $native "scripts\build.ps1") -Configuration Release

    & python -m pip install setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "Installing Python wheel-build dependencies failed." }
    $attestation = & (Join-Path $PSScriptRoot "prepare_mi_attestation.ps1") `
        -DestinationDirectory (Join-Path $tools "attestation")
    & (Join-Path $PSScriptRoot "build_mi_native_wheels.ps1") `
        -NativeArtifacts (Join-Path $native "artifacts\Release") `
        -AttestationLibrary $attestation -WheelDirectory $wheels
    & python -m pip wheel --no-deps --no-build-isolation --wheel-dir $wheels $root
    if ($LASTEXITCODE -ne 0) { throw "Building the MSAL candidate wheel failed." }
}
finally {
    $env:PATH = $previousPath
    $env:CARGO_HOME = $previousCargoHome
    $env:RUSTUP_HOME = $previousRustupHome
    $env:RUSTUP_TOOLCHAIN = $previousToolchain
}
