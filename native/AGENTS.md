# Microsoft.Identity.Native.Primitives instructions

These instructions apply to the standalone native project under `native/`,
not the `msal/` Python implementation. Python package behavior and packaging
must remain independent unless a separate integration change is approved.

## Review priorities

- Review bugs, security issues, ownership/lifetime errors, and ABI contract
  violations before style.
- Comment only on concrete, actionable issues introduced by the change.
- Prefer no review comment over a speculative security finding.
- Require regression tests for fixes and negative/adversarial tests for
  security-sensitive changes where practical.

## Native project boundary

- Production implementation is Rust.
- Never add production Python, Node.js, or C++ implementations.
- C is allowed only for external ABI/conformance tests and minimal native
  examples.
- Do not add MSAL, token acquisition, token caching, Managed Identity
  orchestration, Azure Identity, or Azure SDK authentication behavior.
- Language-specific integrations are outside this native project's scope.

## Key and cryptographic invariants

- Never suggest exporting a private key as a workaround.
- Preserve opaque, non-exportable key semantics.
- Private-key bytes and private RSA parameters must not leave the CNG/KSP
  boundary.
- Treat CNG key creation, key usage, export policy, KSP selection,
  KeyGuard/VBS, and attestation changes as security-sensitive.
- Do not claim hardware/VBS/attestation guarantees unless the implementation
  verifies the required policy and evidence.
- Treat algorithm, digest, padding, PSS salt, and certificate/key-binding
  changes as security-sensitive.

## Unsafe Rust, FFI, and ownership

- Treat all unsafe and FFI code as security-critical.
- Every unsafe block requires a precise `SAFETY:` comment describing pointer,
  lifetime, aliasing, threading, and ownership invariants as applicable.
- Check `Send` and `Sync` assumptions against the underlying native API.
- Check native handle ownership, partial initialization, cancellation, and
  every error-path cleanup.
- Keep allocation/free pairing explicit and within the same allocator family.
- Document caller-owned versus callee-owned memory.
- Bound lengths before pointer traversal, string scanning, or allocation.
- Do not add infallible allocation to provider callbacks where allocation
  failure is expected to become a structured native error.
- Preserve panic containment across exported ABI and OpenSSL callback
  boundaries.

## Native ABI

- Public ABI changes require compatibility and versioning review.
- Check structure size, order, alignment, field types, enum numeric values,
  calling convention, ownership, allocation/free rules, nullability, and field
  interpretation.
- Update the public header, Rust layouts, C conformance tests, documentation,
  changelog, exported-symbol checks, and
  `MICROSOFT_IDENTITY_NATIVE_ABI_VERSION` when required.
- Do not add compatibility aliases or silently reinterpret existing fields
  without an approved design.

## TLS, networking, and HTTP

- Treat TLS trust, SNI, certificate-chain, hostname, and IP verification
  changes as security-sensitive.
- Do not weaken peer verification or add success-shaped fallbacks.
- Treat DNS cancellation, socket inheritance, deadlines, and concurrency as
  native ownership/security concerns.
- Treat HTTP parsing and framing changes as security-sensitive.
- Do not add arbitrary redirects, proxy selection, Host-header overrides,
  transfer encoding, duplicate framing, or ambiguous message lengths without
  an approved design.
- Preserve bounded requests, responses, headers, and total I/O deadlines.
- Do not log tokens, private keys, credentials, secret bodies, secret
  responses, or sensitive key identifiers.

## Intentional patterns not to "fix"

- Internal crate names such as `cng-core`, `cng-provider`, and `mtls-bridge`
  intentionally describe the current implementation.
- C files under `tests/native` and `examples/c` intentionally exercise the
  external compiler/ABI boundary; they are not production implementations.
- The caller supplies an already-acquired certificate-bound token; token
  acquisition and token semantic validation are outside this native project.
- The raw ABI accepts a caller-selected destination; authorization belongs in
  a trusted higher-level host.
- The library does not isolate mutually untrusted callers in one process.
- Exact `Content-Length` framing, no redirects, no proxy selection, no
  transfer encoding, and strict header validation are intentional.
- Reserving the bounded response capacity before reading plaintext is an
  intentional zeroization tradeoff; optimize it only with an approved design
  that preserves plaintext cleanup guarantees.
- The explicit provider module path and private OpenSSL library context are
  intentional loading and isolation choices.

## Validation

- Follow `CONTRIBUTING.md`, `docs/security-review.md`, and
  `docs/versioning.md`.
- Run the complete Debug and Release Rust/native matrix.
- Keep formatting, Clippy, dependency audit, and dependency policy checks
  clean.
