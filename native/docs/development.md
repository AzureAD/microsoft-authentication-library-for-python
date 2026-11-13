# Development

## Supported environment

Development and validation currently target:

- Windows x64;
- Visual Studio 2022 native C/C++ build tools;
- Rust 1.97.1 for `x86_64-pc-windows-msvc`, including `rustfmt` and Clippy;
- CMake 3.21 or later;
- vcpkg; and
- the vcpkg-pinned OpenSSL development baseline, currently OpenSSL 3.6.3.

OpenSSL 3.6 is the current development target, not the long-term production
support commitment. See [Building and testing](building-and-testing.md) for
the OpenSSL servicing plan.

## Configure

Run native commands from the repository's `native/` directory.

Use a Visual Studio 2022 developer environment:

```powershell
$vcpkg = 'C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\vcpkg'

cmake -S . -B build -G 'Visual Studio 17 2022' -A x64 `
  -DCMAKE_TOOLCHAIN_FILE="$vcpkg\scripts\buildsystems\vcpkg.cmake" `
  -DVCPKG_TARGET_TRIPLET=x64-windows
```

If Visual Studio is installed in another edition or location, adjust the
vcpkg path without changing the pinned manifest baseline.

## Repository layout

| Path | Responsibility |
|---|---|
| `crates/cng-core` | Windows CNG/BCrypt handle ownership, key policy, hashing, signing |
| `crates/cng-provider` | Rust OpenSSL provider implementation |
| `crates/mtls-bridge` | Rust TLS/HTTP transport and public native ABI |
| `crates/openssl-provider-sys` | Minimal OpenSSL provider FFI definitions |
| `crates/rust-tests` | Rust integration utilities and CNG key audit |
| `include` | Public C ABI |
| `tests/native` | External C conformance and integration tests |
| `examples/c` | Minimal external native example |

Internal CNG-oriented crate and source names are intentional because CNG is the
current backend. They are not public product branding.

## Build and test

Build both configurations:

```powershell
.\scripts\build.ps1
```

Run the complete local matrix:

```powershell
.\scripts\test.ps1
cargo audit
cargo deny check
```

The test script establishes the project-local OpenSSL environment. Direct
Cargo commands that compile OpenSSL-dependent crates must set `OPENSSL_DIR` and
`OPENSSL_LIB_DIR` to the matching project-local vcpkg installation.

## Development rules

- Run formatting rather than hand-formatting Rust.
- Keep builds warning-clean.
- Use fallible allocation in provider-reachable FFI paths where allocation
  failure must become a structured native error.
- Add a precise `SAFETY:` comment to every unsafe block.
- Model native handles with explicit ownership and deterministic cleanup.
- Keep input traversal and allocation bounded.
- Preserve the public ABI's ownership and allocator boundary.
- Add regression tests for fixes and negative tests for security-sensitive
  behavior.
- Do not add production managed-language or C++ implementations.

Read [CONTRIBUTING.md](../CONTRIBUTING.md) before substantial work and use a
[design proposal issue](https://github.com/AzureAD/microsoft-authentication-library-for-python/issues/new) for changes
listed there.

## Generated and local files

`build`, `target`, `artifacts`, test PKI, logs, symbols, and IDE user settings
are local outputs and must not be committed.

The native integration tests create process-owned CNG keys. A successful test
run ends with:

```text
No project-specific CNG test keys remain.
```

If a test process is interrupted, rerun the key audit and remove only keys
identified as project-owned.
