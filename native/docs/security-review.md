# Security review

This guide defines the minimum review evidence for changes to
Microsoft.Identity.Native.Primitives. It supplements, and does not replace,
Microsoft security review processes.

## When security review is required

Require explicit security review for changes involving:

- unsafe Rust, raw pointers, callback lifetimes, or FFI;
- C ABI layouts, exports, calling conventions, versioning, or ownership;
- native allocation, zeroization, error paths, or handle cleanup;
- CNG/BCrypt handles, key creation, key ACLs, key usage, export policy, KSP
  selection, KeyGuard/VBS behavior, or attestation;
- cryptographic algorithms, digest/padding/salt parameters, or signing;
- OpenSSL provider loading, dispatch, property selection, STORE, KEYMGMT, or
  SIGNATURE behavior;
- certificate parsing, chain construction, or certificate/private-key binding;
- DNS, sockets, TLS, trust anchors, SNI, hostname/IP verification, or peer
  verification;
- HTTP request construction, header validation, redirects, proxies, framing,
  limits, timeouts, or response parsing;
- tokens, request/response plaintext, diagnostics, logs, or sensitive key
  identifiers;
- threading, cancellation, concurrency, or `Send`/`Sync` assumptions; or
- dependencies that enter the trusted computing base.

## Review questions

### Key boundary

- Can private-key bytes, private parameters, or an exportable private blob
  leave the selected CNG/KSP boundary?
- Is private export policy rejected explicitly?
- Are key usage, size, provider, and certificate binding validated?
- Could a new path select or exercise a different key than intended?
- Does the change make claims about KeyGuard/VBS or attestation that the code
  does not enforce?

### Unsafe Rust and FFI

- Does every unsafe block state the exact `SAFETY:` invariant?
- Are pointer validity, alignment, lifetime, aliasing, and nullability checked?
- Are lengths bounded before pointer traversal, conversion, or allocation?
- Is panic containment preserved at every exported callback boundary?
- Are Rust allocations always released by the matching Rust free function?
- Are caller-owned and callee-owned objects unambiguous?

### Native ownership and concurrency

- Is each CNG, BCrypt, OpenSSL, socket, DNS, allocation, and OS handle owned
  exactly once?
- Are partial initialization and every failure path cleaned up correctly?
- Are cancellation races and timeouts safe?
- Are `Send` and `Sync` implementations or assumptions justified by the native
  API contract?

### TLS and HTTP

- Are certificate chain and hostname/IP checks still mandatory?
- Can the caller bypass trust, SNI, or peer verification?
- Are redirects, proxies, Host overrides, transfer encoding, duplicate
  framing, or ambiguous lengths introduced?
- Are request and response sizes and total deadlines still bounded?
- Could retry behavior duplicate a non-idempotent operation?

### Secrets and diagnostics

- Can tokens, keys, credentials, certificates with private material, request
  bodies, response bodies, or sensitive key identifiers reach logs or errors?
- Are native plaintext copies zeroized where the ownership model permits?
- Are errors useful without exposing secrets?

### Compatibility

- Does the change alter a public structure, enum value, exported symbol,
  calling convention, ownership rule, allocation/free pair, or existing field
  interpretation?
- Is the product and ABI version impact documented under
  [Versioning](versioning.md)?
- Are old callers rejected safely and deterministically where compatibility is
  not possible?

## Required evidence

Security-sensitive pull requests should include:

- a linked design proposal when required by [CONTRIBUTING.md](../CONTRIBUTING.md);
- a concise threat and security-impact analysis;
- regression tests;
- negative/adversarial tests where practical;
- Debug and Release Rust/native results;
- dependency-policy results when the trusted computing base changes;
- ABI layout/export evidence when the public boundary changes; and
- documentation and changelog updates.

Prefer no security finding over a speculative claim. Review comments should
identify a concrete violated invariant, reachable failure mode, or contract
regression.
