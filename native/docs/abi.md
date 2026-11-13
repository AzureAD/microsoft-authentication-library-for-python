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
