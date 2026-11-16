# Release process

Microsoft.Identity.Native.Primitives is pre-release. This checklist defines
the expected release bar; it does not claim that Microsoft signing,
publication, or provenance infrastructure is already configured. Native release
authority belongs to the MSAL Python repository maintainers under
[CODEOWNERS](../../CODEOWNERS). A Python package release does not publish native
artifacts.

## Release preparation

- Confirm the intended release scope and approved design proposals.
- Complete the `Unreleased` section in [CHANGELOG.md](../CHANGELOG.md).
- Select the product SemVer and apply it consistently to Cargo, CMake, vcpkg,
  and provider-reported metadata.
- Decide whether `MICROSOFT_IDENTITY_NATIVE_ABI_VERSION` must change under
  [Versioning](versioning.md).
- Commit the final `Cargo.lock`.
- Review the pinned vcpkg baseline and exact OpenSSL version.
- Confirm the supported OpenSSL branch and servicing lifetime.
- Review every dependency entering the native trusted computing base.
- Confirm all user-facing, ABI, security-model, development, and support
  documentation is current.

## Validation

Run from a clean checkout:

```powershell
cargo fmt --all -- --check
cargo check --locked --workspace --all-targets --all-features
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo test --locked --workspace --all-features
cargo test --locked --release --workspace --all-features
.\scripts\test.ps1
cargo audit
cargo deny check
```

Record evidence for:

- Debug and Release Rust tests;
- Debug and Release native provider tests;
- Debug and Release native ABI/mTLS integration tests;
- project-specific CNG test-key cleanup audit;
- certificate/key mismatch and TLS verification failures;
- bounded HTTP framing and timeout failures;
- exported symbols in `microsoft_identity_native_primitives.dll`;
- the value returned by `microsoft_identity_native_abi_version()`; and
- public C structure sizes and constants.

## Security and legal review

- Review all changes to unsafe Rust, FFI, native ownership, CNG/key policy,
  cryptography, OpenSSL provider behavior, TLS, HTTP framing, and secret
  handling.
- Confirm negative/adversarial tests cover security-sensitive behavior.
- Review dependency licenses and third-party notice obligations.
- Generate an SBOM for the exact released dependency and binary set.
- Generate build provenance tied to the source commit and build environment.
- Verify that no token, key, certificate private material, credential, or
  sensitive production payload is included in artifacts or logs.

## Artifact preparation

- Build release artifacts from the reviewed commit.
- Include the required OpenSSL runtime and notices when distribution requires
  them.
- Generate SHA-256 checksums for every published artifact.
- Inspect DLL dependencies and the public export table.
- Sign the release commit or tag.
- Sign Windows DLLs when Microsoft signing infrastructure is available and
  configured for this repository.
- Verify signatures and checksums from a clean environment.

## Publication

- Create a signed, annotated release tag that matches the product version.
- Publish release notes derived from the changelog.
- Attach only reviewed, signed artifacts and their SHA-256 checksums.
- Attach or link the SBOM and provenance statement.
- Verify downloads, signatures, checksums, exports, ABI version, and a minimal
  native request from the published artifacts.
- Update [SUPPORT.md](../SUPPORT.md) when the release changes supported
  versions or servicing commitments.

Coordinate native release tags, artifact publication, and servicing commitments
with the repository maintainers; do not reuse the Python package release
pipeline for native artifacts.
