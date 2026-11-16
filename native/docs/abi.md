# C ABI

The Microsoft.Identity.Native.Primitives native contract is declared in
`include/microsoft_identity_native_primitives.h`.

## Request ownership

The caller owns the request structure and every referenced string, header
array, and body buffer. They must remain readable until
`microsoft_identity_native_request` returns. All strings are UTF-8; HTTP
protocol fields are restricted to ASCII.

Provider, client-certificate, and CA-bundle paths must be absolute. The target
must be origin-form, beginning with `/`.

Each header name is limited to
`MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_NAME_BYTES`, each value to
`MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_VALUE_BYTES`, and the combined generated
request headers to `MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_BYTES`.

The header allowlist is `Accept`, `Authorization`, `Content-Type`, `User-Agent`,
`X-Request-ID`, `client-request-id`, `x-ms-client-request-id`, and
`x-ms-tokenboundauth`, matched case-insensitively. Duplicate names, control
characters, and unrelated headers remain invalid. `X-Request-ID` is not a
replacement for either client correlation header.

A caller can send a bounded form POST over mTLS without an `Authorization`
header. If supplied, `Authorization` still requires the `mtls_pop` scheme.
The native library does not construct OAuth requests or interpret tokens;
the host owns form contents, destination authorization, and token validation.
This uses the existing version-1 request layout and allocation contract.

## Optional protected-key entry points

The additive `microsoft_identity_native_key_*` exports do not change the
existing request/response layouts or ABI version. Consumers requiring these
exports must check their presence as well as ABI version 1 before using them.

`key_open_protected` accepts only `microsoft-identity-native-vbs-*` names.
It opens or creates a user-scoped, per-boot, non-exportable RSA-2048 key,
restricted to signing and key attestation. It verifies `Virtual Iso` and
performs a real SHA-256/PSS signing probe with a 32-byte salt. An owned
generation that opens but fails isolation or liveness validation is deleted
and recreated once under the same per-name cross-process lock; there is no
software fallback. Initial open and key-policy errors propagate without
deleting the container. Successful open returns
one owned opaque object. Closing that object does not delete the persisted
key.

`key_public` copies only the public modulus, exponent, and bit length into
caller-owned `MICROSOFT_IDENTITY_NATIVE_PUBLIC_KEY` storage. Initialize its
size and ABI version. `key_sign_sha256_pss` signs an exactly 32-byte digest
into a distinct caller-owned buffer of at most 2,048 bytes. `key_borrow_handle`
borrows the NCrypt handle for an independent native attestation library; it
does not transfer ownership or attest anything itself.

Retain the opaque object throughout all operations and every borrowed-handle
use. No operation may race `key_free`. Free each successfully opened object
exactly once; copied, forged, previously freed, or concurrently freed objects
are invalid. A null object is accepted only by `key_free`. Private-key bytes
and private RSA parameters never cross this interface.

## Response ownership

Pass fresh or zeroed `MICROSOFT_IDENTITY_NATIVE_RESPONSE` storage. On success,
the returned structure owns native header and body allocations.

Response header names follow the HTTP token grammar. Content-Length permits
surrounding spaces and tabs, but duplicate lengths, internal whitespace,
nondecimal lengths, and transfer encoding remain rejected.

The response is move-only:

- Do not copy the structure.
- Do not alter its pointers or lengths.
- Call `microsoft_identity_native_response_free` exactly once on the original
  structure.
- A repeated free on that reset original structure is harmless. Freeing a
  copied structure is invalid.

## Errors

`microsoft_identity_native_request` returns nonzero on success and zero on
failure. `MICROSOFT_IDENTITY_NATIVE_ERROR` contains a stable category, optional
system/OpenSSL code, and a bounded message. Failures do not return a partially
owned response.

Rust panics are caught at the ABI boundary and converted to an I/O error.

## Versioning

Call `microsoft_identity_native_abi_version` before use and compare it with
`MICROSOFT_IDENTITY_NATIVE_ABI_VERSION`. The request begins with structure size
and ABI version fields so incompatible inputs are rejected deterministically.

ABI compatibility rules and version increments are defined in
[Versioning](versioning.md). Changes to this contract also require the review
described in [Security review](security-review.md).
