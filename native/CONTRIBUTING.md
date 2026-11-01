# Contributing to Microsoft.Identity.Native.Primitives

Microsoft.Identity.Native.Primitives welcomes contributions. This project
contains security-sensitive Rust, native ABI, Windows CNG, OpenSSL provider,
TLS, and HTTP code. Contributions are reviewed for correctness, security,
compatibility, and fit with the project direction.

These requirements apply to the standalone project under `native/`. Follow the
MSAL Python repository's [contributing guide](../contributing.md) for branches,
ownership, and the common contribution process. Run the native commands in this
document from `native/`, not the repository root.

## Contributor License Agreement

Most contributions require you to agree to a Contributor License Agreement
(CLA) declaring that you have the right to, and actually do, grant us the
rights to use your contribution. For details, visit
<https://cla.opensource.microsoft.com>.

When you submit a pull request, a CLA bot will automatically determine whether
you need to provide a CLA and decorate the pull request appropriately. Follow
the instructions provided by the bot. You will only need to do this once
across all repositories using the Microsoft CLA.

Contributions cannot be accepted until the required Microsoft CLA is complete.

## Before writing code

Small bug fixes and documentation corrections may proceed normally. Open an
issue before starting work when the expected behavior or scope is unclear.

Submit a
[design proposal issue](https://github.com/AzureAD/microsoft-authentication-library-for-python/issues/new)
and agree on the high-level design with the maintainers before investing heavily in a
substantial change involving any of the following:

- the public native ABI or its ownership/versioning contract;
- Windows CNG behavior, key creation, key policy, KSP selection, or
  opaque/non-exportable key behavior;
- OpenSSL provider behavior, algorithms, properties, or loading;
- cryptographic algorithms, parameters, or certificate/private-key binding;
- TLS configuration, chain validation, hostname/IP verification, or peer
  verification;
- HTTP parsing, framing, redirects, proxies, or destination controls;
- native memory ownership, FFI design, unsafe Rust, threading, concurrency, or
  `Send`/`Sync` assumptions;
- platform support or dependencies that enter the trusted computing base;
- a new native identity primitive; or
- a new key backend, KSP, HSM, or opaque-key abstraction.

Early design agreement prevents contributors from spending significant effort
on an approach that does not fit the end-to-end security model or roadmap.
Substantial pull requests should reference an approved design-proposal issue.

## Development prerequisites

The supported development environment is:

- Windows x64;
- Visual Studio 2022 native C/C++ build tools;
- Rust 1.97.1 for `x86_64-pc-windows-msvc`;
- CMake;
- vcpkg; and
- the project-pinned OpenSSL baseline. The current development baseline is
  OpenSSL 3.6.3; production release requirements are documented separately.

See [Development](docs/development.md) and
[Building and testing](docs/building-and-testing.md) for setup instructions.

## Required validation

Before opening a pull request, run:

```powershell
cargo fmt --all -- --check
cargo check --locked --workspace --all-targets --all-features
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo test --locked --workspace --all-features
cargo test --locked --release --workspace --all-features
```

Also run:

- Debug native provider tests;
- Release native provider tests;
- Debug native ABI/mTLS integration tests;
- Release native ABI/mTLS integration tests;
- the project-specific CNG test-key audit;
- `cargo audit`; and
- `cargo deny check`.

The native project scripts run the Rust and native Debug/Release matrix:

```powershell
.\scripts\test.ps1
cargo audit
cargo deny check
```

## Engineering and security bar

The following invariants are part of the native project contract:

- The production implementation is Rust.
- Do not introduce production Python, Node.js, or C++ code.
- C is allowed only for external ABI/conformance tests and minimal native
  examples.
- This native project does not implement MSAL, token acquisition, Azure Identity,
  or Azure SDK authentication flows.
- Private-key bytes must never leave the CNG/KSP boundary.
- Private-key export must remain disabled.
- Every unsafe block must include a precise `SAFETY:` comment explaining the
  invariant that makes the operation valid.
- Native handle ownership must be explicit.
- FFI allocation/free pairing and caller-owned versus callee-owned memory must
  be documented.
- Input lengths must be bounded before traversal or allocation.
- Tokens, secret request bodies, secret responses, private keys, and sensitive
  key identifiers must not be logged.
- TLS peer verification must not be weakened.
- Arbitrary redirects, proxies, Host-header overrides, ambiguous HTTP framing,
  and transfer-encoding support require an approved design.
- Public ABI changes require explicit compatibility and versioning analysis.
- Bug fixes require regression tests.
- Security-sensitive changes require negative or adversarial tests where
  practical.

Review [Security model](docs/security-model.md),
[ABI contract](docs/abi.md), [Versioning](docs/versioning.md), and
[Security review](docs/security-review.md) before changing a sensitive area.

## Pull requests

Pull requests must be:

- focused on one problem;
- covered by appropriate tests;
- warning-clean;
- documented when behavior or contracts change; and
- free of unrelated refactoring.

Describe security, unsafe/FFI, native ownership, ABI, cryptographic/CNG, TLS/HTTP,
dependency, compatibility, performance, and memory impacts even when the answer
is "none."

Maintainers may ask for a large change to be split into independently
reviewable increments. Changes are merged only after required reviews,
validation, and CLA checks succeed.

## Reporting bugs and requesting features

Use the MSAL Python repository's
[GitHub Issues](https://github.com/AzureAD/microsoft-authentication-library-for-python/issues)
for native bugs, feature requests, design proposals, and documentation issues.
Identify the affected `native/` component in the issue.

Do not report security vulnerabilities through GitHub Issues. Follow
[SECURITY.md](SECURITY.md).

## Code of Conduct

This project follows the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
