# Changelog

All user-visible changes and security fixes must be recorded in this file.
This project follows [Semantic Versioning](docs/versioning.md) for its product
version and versions its native ABI separately.

## Unreleased

### Added

- Microsoft contribution, code-of-conduct, support, maintainer, ownership, and
  release-governance documentation.
- Structured bug, feature, design-proposal, documentation, and pull-request
  templates.
- Native ABI versioning, development, release, and security-review guides.
- Cargo and GitHub Actions Dependabot monitoring and repository-specific
  Copilot instructions.

### Changed

- Moved the standalone native project under `native/` in the MSAL Python
  repository, preserving the Rust implementation, native ABI, and lockfile.
- Expressed workspace dependency requirements as explicit compatible ranges
  and documented reviewed dependency updates.
- Scoped native CI and Dependabot to the new location without integrating
  native sources or binaries into the Python package.
- Adopted Microsoft Security Response Center vulnerability reporting and
  Microsoft repository legal/trademark language.

### Fixed

- Preserve available public material when OpenSSL duplicates a public-only RSA
  key with a selection that also includes unavailable private material.
- Resolve the example's provider DLL path before passing it to the ABI, which
  requires absolute paths.
- Configure the pinned OpenSSL installation and native test harness before the
  first quick-start build command.
- Reject digest updates that conflict with retained explicit PSS MGF1 state
  before changing the signature context.
- Include IPv6 Host brackets in HTTP header/request limits and the reservation
  made before copying authorization or body plaintext.
- Wipe copied request paths, STORE URIs, header values, and bodies on validation
  failures by retaining them in zeroizing storage immediately after copying.
- Accept valid HTTP response token field names and surrounding Content-Length
  whitespace without permitting duplicate or ambiguous framing.

### Security

- Documented mandatory review areas and evidence for unsafe Rust, FFI, CNG,
  OpenSSL provider, TLS, HTTP framing, secret handling, and ABI changes.

### Breaking

## 0.2.0 - Pre-release

### Added

- Rust OpenSSL 3 provider for persisted Windows CNG RSA signing keys.
- Opaque CNG key loading through a strict `cng://` STORE URI.
- Bounded Rust TLS/HTTP transport for certificate-bound identity requests.
- Language-neutral C ABI and external C conformance tests.
- Debug and Release Windows validation, dependency auditing, and security
  documentation.

### Changed

- Renamed the pre-release project and public native surface to
  Microsoft.Identity.Native.Primitives.
- Aligned the Rust crates, CMake/vcpkg metadata, provider identity, public DLL
  names, header, exported functions, and public ABI prefix at version 0.2.0.
- Clarified that token acquisition, MSAL, Azure SDK integration, and
  language-specific SDKs are outside the repository scope.

### Fixed

- Hardened FFI string ownership, native allocation failures, signature state
  updates, CNG key-policy checks, provider loading, DNS cancellation, socket
  inheritance, TLS IP verification, HTTP framing, and plaintext zeroization.

### Security

- Enforced non-exportable private-key policy and bounded native inputs and
  outputs.
- Added explicit security-boundary, deployment, release, and dependency-policy
  requirements.

### Breaking

- Replaced the former pre-release product and ABI names without compatibility
  aliases. No stable public release preceded this rename.
