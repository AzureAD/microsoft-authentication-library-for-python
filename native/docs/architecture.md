# Architecture

## Objective

Microsoft.Identity.Native.Primitives provides Rust-native building blocks for
identity scenarios that require platform-protected, non-exportable
cryptographic keys.

The first set of primitives lets a native caller make an mTLS-authenticated
HTTPS request using a persisted Windows CNG RSA key without exporting the
key's private bytes. It is compatible with opaque keys backed by Windows CNG,
including appropriately provisioned KeyGuard keys.

The security-critical operation is the complete outbound transaction.
Returning a token while expecting each language runtime to reproduce the
matching client-certificate transport would make correct use dependent on
every caller. The current primitives instead own provider loading, key lookup,
the TLS handshake, HTTP request construction, and response framing behind one
native ABI.

Future primitives may support other native cryptographic backends or opaque-key
mechanisms. They must be implemented and reviewed explicitly; this native project
does not provide placeholder backends or a generic software-key abstraction.

## Runtime flow

```text
Caller
  |
  | MICROSOFT_IDENTITY_NATIVE_REQUEST
  v
microsoft_identity_native_primitives.dll
  |- validate all ABI inputs
  |- create private OSSL_LIB_CTX
  |- load microsoft_identity_native_provider.dll from an explicit path
  |- load CNG key via OSSL_STORE
  |- load certificate and CA bundle
  |- resolve host and connect socket
  |- configure SNI and certificate verification
  |- run TLS client-auth handshake
  |- write one HTTP/1.1 request
  |- parse one bounded response
  v
MICROSOFT_IDENTITY_NATIVE_RESPONSE
```

## Rust crates

### `cng-core`

Owns NCrypt and BCrypt handles through Rust RAII. It validates persisted key
policy, exports only public RSA parameters, hashes messages, and invokes
`NCryptSignHash`. Signing operations on a shared key are serialized.

### `cng-provider`

Implements the OpenSSL 3 provider callbacks needed for RSA:

- KEYMGMT for public parameters, validation, duplication, and non-exportable
  private-key references;
- SIGNATURE for SHA-256/384/512 with PKCS#1 and PSS; and
- STORE for strict `cng://` URI loading.

OpenSSL receives an opaque provider-backed key. Private-key parameters are
never exported.

### `mtls-bridge`

Exports the C ABI and owns the network transaction. It uses a private OpenSSL
library context so provider selection and certificate parsing do not depend on
process-global OpenSSL properties.

### `openssl-provider-sys`

Defines the small set of OpenSSL provider structures, constants, callbacks,
STORE APIs, and TLS functions not exposed by the normal Rust OpenSSL bindings
used by the project.

## Why there is a C ABI

The C ABI is the language-neutral boundary, not a C implementation. Any
runtime capable of calling a Windows DLL can use the same Rust transaction
without receiving the private key or reimplementing the TLS behavior.

The ABI is versioned and structure-sized so future versions can add fields
while preserving deterministic validation.

## Why C tests remain

Files under `tests/native` compile as independent C programs. They verify:

- exported symbols and calling convention;
- structure layouts as seen by a C compiler;
- allocation and free ownership;
- OpenSSL provider loading from an external consumer;
- certificate/key matching;
- successful and failing mTLS handshakes; and
- strict HTTP framing.

They are test clients for the Rust DLLs, not product code.

## Related documentation

- [Security model](security-model.md)
- [Security review](security-review.md)
- [C ABI](abi.md)
- [Versioning](versioning.md)
- [Development](development.md)
