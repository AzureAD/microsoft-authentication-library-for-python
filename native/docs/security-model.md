# Security model

## Assets

The current Microsoft.Identity.Native.Primitives CNG/OpenSSL primitives protect:

- the persisted CNG private-key capability;
- certificate-bound access tokens supplied by the caller;
- request bodies and response bodies copied through native memory;
- TLS peer identity; and
- the integrity of HTTP request and response framing.

## Trusted computing base

The trusted computing base includes:

- the calling process;
- `microsoft_identity_native_primitives.dll`;
- `microsoft_identity_native_provider.dll`;
- the selected OpenSSL runtime;
- Windows CNG and the selected KSP;
- the supplied certificate and CA files; and
- operating-system access controls protecting those files and the persisted
  key.

## Enforced properties

The current implementation enforces:

- bounded ABI inputs;
- strict header allowlisting;
- `mtls_pop` authorization scheme validation;
- no redirects, proxy selection, arbitrary Host header, or transfer encoding;
- verified CA trust and hostname/IP identity;
- opaque CNG signing with public-key export only;
- rejection of persisted keys with nonzero private export policy;
- minimum RSA strength;
- exact Content-Length response framing; and
- panic containment across native boundaries.

## Important non-goals

### In-process caller isolation

The native primitive cannot identify which package, module, plugin, or other
component inside one process initiated a call. All in-process code runs with
the process identity. A malicious dependency that can call arbitrary native
APIs may be able to invoke the primitive or Windows CNG directly.

Use an out-of-process broker with OS-authenticated IPC when separate workloads
must receive different authorization.

### Token acquisition

Microsoft.Identity.Native.Primitives does not acquire OAuth tokens and does not
validate token audience, tenant, scopes, or claims. The caller is responsible
for supplying the correct certificate-bound token for the selected destination.

### Destination authorization

The low-level ABI accepts a destination from the caller. A higher-level trusted
application must restrict keys, hosts, methods, paths, tenants, and operations
before invoking the ABI.

### Managed-runtime zeroization

Native copies are redacted or zeroized where practical. Original strings or
byte objects owned by a managed runtime remain subject to that runtime's memory
behavior.

Copied request paths, STORE URIs, header values, and bodies use zeroizing
storage, including on validation failures.

### Response-buffer reservation

Each in-flight request reserves the bounded 1 MiB native response capacity
before reading response plaintext. This avoids reallocating plaintext through
an old allocation that can no longer be zeroized, at the cost of approximately
1 MiB of reserved response memory per concurrent request. Hosts must bound
concurrency according to their memory budget.

### Attestation policy

Successfully using a VBS/KeyGuard key does not, by itself, prove an attestation
policy. A deployment that requires hardware or virtual-isolation guarantees
must validate the expected KSP, key properties, and attestation evidence as
part of its provisioning and authorization design.

## Deployment recommendations

- Install the primitives DLL, provider, OpenSSL dependencies, certificates,
  and policy in a directory not writable by the application identity.
- Restrict the persisted key's ACL to the intended service or broker identity.
- Pin or verify the provider and OpenSSL binaries used by production.
- Allowlist destination hosts and operations outside the raw ABI.
- Avoid logging tokens, STORE URIs, certificate paths, request bodies, or
  response bodies.
- Use bounded concurrency at the host level.
- Treat an aborted non-idempotent request as having an unknown remote outcome.

Use [Security review](security-review.md) when changing any enforced property
or trusted-computing-base component. Public-contract changes must also follow
[Versioning](versioning.md).
