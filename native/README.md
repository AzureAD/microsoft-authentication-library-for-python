# Microsoft.Identity.Native.Primitives

Rust-native building blocks for identity scenarios that require
platform-protected, non-exportable cryptographic keys.

The first implemented primitives bridge Windows CNG-backed keys into OpenSSL
and provide a bounded mTLS transport for certificate-bound identity requests
without exporting private-key material.

This standalone native project lives under `native/` in the MSAL Python
repository. It is not integrated into the `msal` package, and neither native
sources nor DLLs are included in MSAL Python's PyPI distributions.

## What this project is

- A Rust OpenSSL 3 provider for persisted Windows CNG RSA signing keys.
- A Rust DNS, TCP, TLS, HTTP/1.1 request, and response transport.
- A stable language-neutral C ABI exported by
  `microsoft_identity_native_primitives.dll`.
- Native conformance tests that consume the Rust DLLs from an external C
  compiler boundary.

## Why it exists

OpenSSL-based applications cannot ordinarily associate a TLS client
certificate with a non-exportable Windows CNG or KeyGuard private key.
Exporting that key would defeat its security model.

The current CNG/OpenSSL primitives let OpenSSL request RSA signatures from CNG
while private-key bytes remain inside the Windows key-storage provider. The
Rust transport owns the complete certificate-bound request so callers do not
need to reproduce the CNG/OpenSSL integration.

## Request flow

```text
caller already has an mtls_pop token
        |
        v
caller invokes the Rust DLL through the C ABI
        |
        v
Rust presents the matching opaque CNG key over mTLS
        |
        v
resource receives the token and matching client certificate
```

## What this project does not do

- No token acquisition.
- No MSAL dependency or configuration.
- No token cache.
- No Managed Identity orchestration.
- No Azure SDK integration.
- No Node.js, Python, or other language SDK.
- No language-specific authentication library.
- No authorization decision for caller-selected resources.
- No isolation between mutually untrusted code in the same process.

Language and identity-library integrations are outside this native project's
scope. Hosting it alongside MSAL Python does not add a Python binding or change
MSAL's token acquisition behavior.

## Repository boundary

| Path | Purpose |
|---|---|
| `crates/cng-core` | CNG/BCrypt ownership, key policy, hashing, signing |
| `crates/cng-provider` | OpenSSL KEYMGMT, SIGNATURE, and STORE provider |
| `crates/mtls-bridge` | Bounded TLS/HTTP transport and exported ABI |
| `crates/openssl-provider-sys` | Minimal OpenSSL provider FFI |
| `include/microsoft_identity_native_primitives.h` | Stable native contract |
| `examples/c` | Minimal caller with an already-acquired token |
| `tests/native` | External C ABI/provider/mTLS conformance tests |

There is no production C or C++ implementation. C remains only where an
external compiler boundary is the test or example being demonstrated.

## Quick start

Run the commands below from the native project directory:

```powershell
Set-Location native
```

Build the Release configuration first:

```powershell
.\scripts\build.ps1 -Configuration Release
```

The staged `artifacts\Release` directory contains the native programs and
libraries. A request needs:

- `microsoft_identity_native_primitives.dll` and
  `microsoft_identity_native_provider.dll`;
- the staged OpenSSL runtime (`libcrypto-3-x64.dll` and `libssl-3-x64.dll`);
- an existing persisted CNG RSA signing key;
- a PEM client-certificate chain whose leaf public key matches that CNG key;
- a PEM CA bundle used to verify the resource server; and
- an already-acquired `mtls_pop` token.

The STORE URI identifies the persisted key:

```text
cng://?key=<percent-encoded-key-name>&provider=<percent-encoded-KSP-name>
```

The `provider` field may be omitted to use the Microsoft Software Key Storage
Provider. Values use RFC 3986 percent encoding; `+` is not treated as a space.

The client-certificate file must contain complete `CERTIFICATE` PEM blocks,
with the leaf certificate first and any intermediates afterward. Its leaf
public key must match the selected CNG private key. The CA bundle verifies the
server and is not a substitute for the client-certificate chain.

The standalone C example reads its token from an environment variable only to
keep the demonstration simple:

```powershell
$env:NATIVE_PRIMITIVES_MTLS_POP_TOKEN = '<already-acquired-token>'

.\artifacts\Release\microsoft_identity_native_example.exe `
  .\artifacts\Release\microsoft_identity_native_provider.dll `
  'cng://?key=<encoded-key>&provider=<encoded-provider>' `
  'C:\absolute\path\client-chain.pem' `
  'C:\absolute\path\ca-bundle.pem' `
  '<resource-host>' `
  443 `
  '/resource/path'
```

Success prints the HTTP status followed by the bounded response body.
Production callers should pass the token directly from protected application
memory. Do not use environment variables, command-line arguments, logs, or
persisted files for production tokens; environment values remain in process
storage and may be inherited by child processes.

Common failures include a missing CNG key, a certificate/key mismatch, an
invalid DNS or IP host, an authorization value that is not `mtls_pop`, an
incorrect CA bundle, a response without exact `Content-Length` framing, and a
connection or total I/O timeout. The native request returns a structured error
code, system error, and bounded diagnostic message through
`MICROSOFT_IDENTITY_NATIVE_ERROR`.

## Build and test

Requirements:

- Windows x64
- Visual Studio 2022 C++ build tools
- Rust 1.97.1 `x86_64-pc-windows-msvc`
- CMake and vcpkg

Configure:

```powershell
$vcpkg = 'C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\vcpkg'
cmake -S . -B build -G 'Visual Studio 17 2022' -A x64 `
  -DCMAKE_TOOLCHAIN_FILE="$vcpkg\scripts\buildsystems\vcpkg.cmake" `
  -DVCPKG_TARGET_TRIPLET=x64-windows
```

Build and run the complete Rust/native matrix:

```powershell
.\scripts\build.ps1
.\scripts\test.ps1
```

## Documentation

- [Architecture](docs/architecture.md)
- [Security model](docs/security-model.md)
- [Security review](docs/security-review.md)
- [C ABI](docs/abi.md)
- [Versioning](docs/versioning.md)
- [Development](docs/development.md)
- [Building and testing](docs/building-and-testing.md)
- [Release process](docs/releasing.md)

The project is pre-release.

## Contributing

Contributions are welcome. Read the repository's
[contributing guide](../contributing.md) and the
[native requirements](CONTRIBUTING.md), complete the Microsoft CLA when
required, and use a design proposal before substantial ABI, CNG, OpenSSL,
cryptographic, TLS/HTTP, unsafe/FFI, platform, or new-primitive work.

## Support

See [SUPPORT.md](SUPPORT.md) for the current pre-release support policy and
[CHANGELOG.md](CHANGELOG.md) for user-visible changes.

## Security

Do not report suspected vulnerabilities through public GitHub Issues. Follow
the [Microsoft Security Response Center reporting process](SECURITY.md).

## Code of Conduct

This project has adopted the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).

## Trademarks

This project may contain trademarks or logos for projects, products, or
services. Authorized use of Microsoft trademarks or logos is subject to and
must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must
not cause confusion or imply Microsoft sponsorship. Any use of third-party
trademarks or logos is subject to those third parties' policies.

## License

Copyright (c) Microsoft Corporation. All rights reserved.

Licensed under the [MIT License](LICENSE).
