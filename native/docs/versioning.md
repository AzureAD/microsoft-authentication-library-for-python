# Versioning

Microsoft.Identity.Native.Primitives has three related but distinct version
dimensions:

1. the product/package version;
2. the public native ABI version; and
3. the supported OpenSSL/provider compatibility range.

Changing one dimension does not automatically change the others.

## Product version

The workspace, CMake project, vcpkg manifest, and provider-reported product
version use [Semantic Versioning](https://semver.org/).

### Major

Increment the SemVer major version for an incompatible change to a supported
public product contract, including removal of supported functionality,
incompatible behavior changes, or an ABI break after a stable release.

During the pre-1.0 period, incompatible changes may use a minor version
increment, but they must still be identified under `Breaking` in
[CHANGELOG.md](../CHANGELOG.md) and reviewed explicitly.

### Minor

Increment the SemVer minor version for backward-compatible additions such as:

- a new native identity primitive;
- a new optional ABI entry point or versioned structure extension;
- support for an additional reviewed backend or platform;
- a new supported cryptographic capability; or
- a substantial backward-compatible behavior or diagnostic improvement.

### Patch

Increment the SemVer patch version for backward-compatible bug, security,
documentation, packaging, and servicing fixes that do not add a public
capability or break an existing contract.

Version changes must be applied consistently wherever the product version is
published or reported.

## Rust dependency versions

All crates inherit their product version and external dependency requirements from
`native/Cargo.toml`. Product version `0.2.0` is independent of the MSAL Python
package version.

Workspace dependencies use explicit compatible ranges, not exact version pins.
For example, `>=0.2.177, <0.3` permits compatible `libc` updates but excludes the
next potentially breaking `0.x` minor version. These ranges are equivalent to
the original Cargo caret requirements and do not broaden the compatibility
contract.

`native/Cargo.lock` records the exact direct and transitive versions validated
together. Keep it committed and use `--locked` for builds and tests. Dependabot
monitors the `/native` Cargo workspace; an update is reviewed and validated
before changing the lockfile. For a targeted manual update, run from `native/`:

```powershell
cargo update -p libc
```

Review the manifest and lockfile diff, run the complete Rust/native matrix and
dependency-policy checks, and commit the reviewed update. Changing an upper
bound requires an explicit compatibility review. Do not replace reviewed
ranges with wildcard dependencies or discard the lockfile.

## Native ABI version

The native ABI version is exposed by
`microsoft_identity_native_abi_version()` and
`MICROSOFT_IDENTITY_NATIVE_ABI_VERSION`. It is independent of the product
version.

Increment `MICROSOFT_IDENTITY_NATIVE_ABI_VERSION` before releasing an
ABI-incompatible change. ABI-breaking changes include:

- changing a public structure's field order, layout, size, or alignment;
- changing a field's type, width, signedness, or interpretation;
- changing public enum numeric values;
- removing or renaming an exported function;
- changing a calling convention;
- changing ownership, lifetime, mutability, or thread-safety rules;
- changing the allocation/free contract or allocator boundary;
- changing whether pointers may be null or whether buffers may alias;
- changing the meaning of an existing flag, error, field, or return value; or
- making previously valid input invalid without an explicitly versioned
  compatibility mechanism.

Adding a field directly to an existing fixed-layout structure is breaking even
when the field is appended. A compatible extension requires a reviewed
versioning design, such as a new structure size/version contract that old
consumers can safely use.

An ABI version increment does not by itself make a change safe. The release
must also document migration, reject incompatible structures
deterministically, update C conformance tests, and verify the exported symbol
table and layouts.

## OpenSSL/provider compatibility

OpenSSL compatibility is a runtime and provider contract, not the native ABI
version.

- Pin and review the development baseline through vcpkg.
- Document the OpenSSL branches and servicing releases validated for each
  published product release.
- Treat provider callback, property-query, STORE, key-management, signature,
  and TLS API compatibility as release criteria.
- Do not infer compatibility with a new OpenSSL major or minor branch from a
  successful compile alone.

Changing the supported OpenSSL range may require a product minor or major
version depending on whether existing supported deployments remain compatible.
It does not require a native ABI increment unless the C contract also changes.

See [ABI](abi.md), [Building and testing](building-and-testing.md), and
[Release process](releasing.md).
