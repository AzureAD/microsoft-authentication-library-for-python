# Building and testing

These instructions build and validate the current
Microsoft.Identity.Native.Primitives Windows CNG/OpenSSL primitives.

Run the commands below from the repository's `native/` directory.

## Configure

Use Windows x64, Visual Studio 2022, Rust 1.97.1 MSVC, CMake, and vcpkg.

```powershell
$vcpkg = 'C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\vcpkg'
cmake -S . -B build -G 'Visual Studio 17 2022' -A x64 `
  -DCMAKE_TOOLCHAIN_FILE="$vcpkg\scripts\buildsystems\vcpkg.cmake" `
  -DVCPKG_TARGET_TRIPLET=x64-windows
```

The pinned vcpkg registry currently supplies OpenSSL 3.6.3.

## OpenSSL servicing plan

OpenSSL 3.6 reaches end of life on November 1, 2026. It is a temporary
development target, not the long-term production baseline.

The first supported Microsoft.Identity.Native.Primitives release will target
OpenSSL 3.5 LTS, which is supported through April 8, 2030. Before a production
release:

- pin a reviewed vcpkg version or override that supplies the selected OpenSSL
  3.5 servicing release;
- run the complete Debug and Release provider/ABI/mTLS matrix against it;
- retain OpenSSL 3.6 only as a compatibility lane until its end of life; and
- test OpenSSL 4.0 separately before claiming compatibility.

Do not publish production binaries validated only against OpenSSL 3.6. The
OpenSSL baseline, runtime DLLs, and test evidence must move together.

## Build

```powershell
.\scripts\build.ps1
```

Debug and Release artifacts are staged under `artifacts`.

## Test

```powershell
.\scripts\test.ps1
```

The script runs:

- `cargo fmt --check`
- workspace checks and Clippy with warnings denied
- Debug and Release Rust tests
- Debug and Release external C provider tests
- Debug and Release native ABI/mTLS integration tests
- project-owned CNG test-key audit

The C tests are external conformance tests, not product implementations. They
validate C compiler layouts, calling conventions, exports, allocation
ownership, provider behavior, and a complete local mTLS transaction against
the Rust DLLs.

See [Development](development.md) for contributor setup,
[Security review](security-review.md) for sensitive-change evidence, and
[Release process](releasing.md) for the complete release gate.
