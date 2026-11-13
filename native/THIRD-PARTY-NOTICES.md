# Third-party notices

Microsoft.Identity.Native.Primitives uses third-party software through Cargo
and vcpkg. The authoritative dependency versions are recorded in the committed
Cargo lockfile and vcpkg manifest.

Dependencies and licenses are reviewed with `cargo deny`; known Rust
vulnerabilities are reviewed with `cargo audit`. These checks supplement, but
do not replace, the notice and source-distribution obligations for released
artifacts.

## OpenSSL

OpenSSL is licensed under the Apache License 2.0. Source, license text, and
notices are available from <https://www.openssl.org/source/license.html>.

Redistributors must preserve all licenses and notices required by the exact
dependency set included with their binaries. Release packaging should generate
an SBOM and a complete machine-derived license report rather than treating this
summary as exhaustive.

## Release artifacts

Release owners must review the exact Rust crates, vcpkg ports, OpenSSL runtime,
licenses, notices, and source-offer requirements associated with each release.
See [Release process](docs/releasing.md).
