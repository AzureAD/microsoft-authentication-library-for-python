# Guidance for Higher-Level SDKs to Consume MSAL mTLS PoP

> **Prerequisites:** This document describes the API surface introduced by
> [PR #931](https://github.com/AzureAD/microsoft-authentication-library-for-python/pull/931).
> The types and parameters referenced below (`WindowsCertificate`, `MsiV2Error`,
> `mtls_proof_of_possession`, `with_attestation_support`, and the extended return
> value contract) are available once that PR is merged.
>
> This file is standalone design guidance for SDK authors and is not part of the
> Sphinx-rendered documentation site (`source_suffix = '.rst'`).

## Purpose

This document explains how higher-level SDKs (e.g., `azure-identity`, `azure-sdk-for-python`)
can consume MSAL Python's MSI v2 mTLS Proof-of-Possession API to provide seamless mTLS
token-bound authentication to end users.

## What MSAL Python Exposes

### Public API Surface

```python
import msal
import requests

# 1. Create ManagedIdentityClient (existing API)
client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
)

# 2. Acquire mTLS-bound token (new parameters)
result = client.acquire_token_for_client(
    resource="https://vault.azure.net",
    mtls_proof_of_possession=True,       # Always routes to MSI v2 (hard fail if unsupported)
    with_attestation_support=True,       # KeyGuard tier (attested); False = Software tier
)
```

### Return Value Contract

```python
{
    "access_token": "eyJ0eXAi...",                    # mTLS-bound PoP token
    "token_type": "mtls_pop",                         # Token type (use in auth header)
    "expires_in": 86399,                              # Seconds until expiry
    "binding_certificate": WindowsCertificate(...),   # Opaque cert+key handle
    "cert_thumbprint_sha256": "buc7x...",             # Base64url SHA-256 thumbprint
    "cert_pem": "-----BEGIN CERTIFICATE-----\n...",   # Public cert (PEM)
    "cert_der_b64": "MIIC...",                        # Public cert (Base64 DER)
}
```

### Key Objects

#### `WindowsCertificate`

Python equivalent of .NET's `X509Certificate2`. Wraps a non-exportable private key
(NCRYPT_KEY_HANDLE) and the associated X.509 certificate.

```python
from msal import WindowsCertificate

cert: WindowsCertificate = result["binding_certificate"]

# Properties
cert.thumbprint_sha256       # str: hex SHA-256 thumbprint (uppercase, 64 chars)
cert.x5t_s256               # str: base64url SHA-256 (matches cnf.x5t#S256 in JWT)
cert.public_certificate_pem  # str: PEM-encoded public certificate
cert.public_certificate_der  # bytes: DER-encoded public certificate
cert.has_private_key         # bool: True if key handle is live
cert.key_name               # str: CNG key name
cert.store_path             # str: cert store path

# Methods
cert.create_cert_context()  # -> PCCERT_CONTEXT (for WinHTTP/SChannel)
cert.close()               # Free native handles (or use as context manager)
```

#### `MsiV2Error`

```python
from msal import MsiV2Error

try:
    result = client.acquire_token_for_client(...)
except MsiV2Error as e:
    # MSI v2 specific error (attestation failure, IMDS error, etc.)
    # No silent fallback to v1 — matches MSAL .NET behavior.
    pass
```

---

## Integration Pattern for Azure SDK

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Your SDK (e.g., azure-identity)                             │
│                                                              │
│  ManagedIdentityCredential                                   │
│    ├── calls MSAL acquire_token_for_client(mtls_pop=True)    │
│    ├── receives binding_certificate + access_token           │
│    └── returns AccessToken + metadata to pipeline            │
├──────────────────────────────────────────────────────────────┤
│  Transport Layer (e.g., azure-core)                          │
│                                                              │
│  MtlsTransport (new, SChannel-based)                         │
│    ├── receives WindowsCertificate from credential           │
│    ├── calls cert.create_cert_context() for TLS handshake    │
│    └── presents cert during client-auth in mTLS              │
├──────────────────────────────────────────────────────────────┤
│  Resource SDK (e.g., azure-keyvault-secrets)                 │
│    └── unaware of mTLS — just uses credential + transport    │
└──────────────────────────────────────────────────────────────┘
```

### Step-by-Step Integration

#### Step 1: Token Acquisition (in your Credential class)

```python
# Inside azure-identity ManagedIdentityCredential
import msal

class ManagedIdentityCredential:
    def __init__(self, *, mtls_pop: bool = False, **kwargs):
        self._mtls_pop = mtls_pop
        self._msal_client = msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=self._http_client,
        )
        self._binding_certificate = None

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        if self._mtls_pop:
            result = self._msal_client.acquire_token_for_client(
                resource=scopes[0].removesuffix("/.default"),
                mtls_proof_of_possession=True,
                with_attestation_support=True,
            )
            # Store the binding certificate for the transport layer
            self._binding_certificate = result["binding_certificate"]
            # Store token_type for the auth policy (AccessToken has no token_type field)
            self._token_type = result["token_type"]  # "mtls_pop"

            return AccessToken(
                token=result["access_token"],
                expires_on=int(time.time()) + result["expires_in"],
            )
        else:
            # Standard MSI v1 path
            ...

    @property
    def binding_certificate(self):
        """Transport layer reads this to configure mTLS."""
        return self._binding_certificate
```

#### Step 2: Authorization Header Construction

The token type dictates the auth header format. Since `azure-core`'s `AccessToken`
does not carry a `token_type` field, read it from the credential:

```python
# In your BearerTokenPolicy or equivalent:
def on_request(self, request):
    token = self._credential.get_token(self._scopes)

    # Read token_type from credential (not from AccessToken which lacks it)
    token_type = getattr(self._credential, "_token_type", "Bearer")
    request.headers["Authorization"] = f"{token_type} {token.token}"

    # For mTLS PoP to token-binding-aware services:
    if token_type == "mtls_pop":
        request.headers["x-ms-tokenboundauth"] = "true"
```

#### Step 3: mTLS Transport (presenting the certificate)

Standard Python `requests` **cannot** use non-exportable keys. You need a
SChannel/WinHTTP transport.

`SchannelSession` takes the certificate in its constructor (not per-request):

```python
# Using msal-schannel-transport
from msal_schannel_transport import SchannelSession

# Certificate is bound at session construction time
session = SchannelSession(client_certificate=credential.binding_certificate)

# Then make requests — certificate is presented automatically
response = session.get(url, headers=headers)
response = session.post(url, headers=headers, body=data)
```

For azure-core integration, build a custom `HttpTransport`:

```python
from azure.core.pipeline.transport import HttpTransport

class SchannelTransport(HttpTransport):
    """WinHTTP/SChannel transport for mTLS with non-exportable keys."""

    def __init__(self, credential):
        self._credential = credential

    def send(self, request, **kwargs):
        cert = self._credential.binding_certificate
        if cert and cert.has_private_key:
            # Create SchannelSession with the certificate
            session = SchannelSession(client_certificate=cert)
            response = session._request(
                request.method, request.url,
                headers=dict(request.headers), body=request.body)
            return self._wrap_response(response)
        else:
            # Fall back to standard requests transport
            ...
```

#### Step 4: End-User Experience (the goal)

```python
from azure.identity import ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

# One line to enable mTLS PoP
credential = ManagedIdentityCredential(mtls_pop=True)

# Standard SDK usage — no mTLS awareness needed by the developer
client = SecretClient(
    vault_url="https://tokenbinding.vault.azure.net",
    credential=credential,
)
secret = client.get_secret("boundsecret")
print(secret.value)  # "secretme"
```

---

## Key Design Decisions

### 1. MSAL Only Acquires Tokens

MSAL never makes downstream API calls. It returns the `binding_certificate` object
and the access token. The higher-level SDK is responsible for:
- Constructing the authorization header
- Presenting the certificate during TLS handshake
- Managing the transport layer

### 2. `WindowsCertificate` is the Bridge

The `WindowsCertificate` object is the contract between MSAL (token acquisition)
and the transport layer (mTLS presentation). It:
- Holds the live NCRYPT_KEY_HANDLE (non-exportable private key)
- Creates thread-safe CERT_CONTEXT instances for concurrent requests
- Manages handle lifecycle (reference counting, cleanup)

### 3. No Silent Fallback

`mtls_proof_of_possession=True` always routes to MSI v2. If the host does not
support MSI v2 (e.g., not on a KeyGuard-enabled VM), MSAL raises `MsiV2Error`.
This matches MSAL .NET's behavior (`MtlsPopTokenNotSupportedinImdsV1`).

A developer who asks for mTLS PoP will never silently receive a Bearer token.

### 4. Transport is Pluggable

Higher-level SDKs can choose:
- **`msal-schannel-transport`** — ready-to-use WinHTTP session
- **Custom transport** — build your own using `cert.create_cert_context()`
- **Future: OpenSSL provider** — when available, standard `requests` will work

### 5. Certificate Lifecycle

```python
# WindowsCertificate is valid for the lifetime of the issued certificate
# (typically 8 hours for IMDS-issued certs). MSAL caches internally.

# Pattern: acquire once, reuse for multiple requests
result = client.acquire_token_for_client(...)
cert = result["binding_certificate"]

# Make many requests with the same cert
with SchannelSession(client_certificate=cert) as session:
    for url in urls:
        session.get(url, headers=headers)

# When done (optional — GC handles this too):
cert.close()
```

---

## Why Standard `requests` Doesn't Work

```python
# This FAILS with non-exportable keys:
requests.get(url, cert=("cert.pem", "key.pem"))
#                                    ^^^^^^^^
#                  KeyGuard/TPM keys cannot be exported to a file!
```

Python's `ssl` module → OpenSSL → requires raw private key bytes.
KeyGuard/VBS keys are hardware-isolated and never leave the security boundary.

**Solution:** Use WinHTTP/SChannel which integrates natively with Windows
certificate stores and NCRYPT_KEY_HANDLEs.

---

## Comparison with .NET

| Concept | .NET | Python |
|---------|------|--------|
| Token acquisition | `.WithMtlsProofOfPossession()` | `mtls_proof_of_possession=True` |
| Attestation opt-in | `.WithAttestationSupport(provider)` | `with_attestation_support=True` |
| Certificate object | `X509Certificate2` | `WindowsCertificate` |
| Downstream mTLS | `HttpClientHandler.ClientCertificates` | `SchannelSession(client_certificate=cert)` |
| Key isolation | Automatic (SChannel) | Automatic (WinHTTP/SChannel) |
| Auth header | `$"{tokenType} {token}"` | `f"{result['token_type']} {result['access_token']}"` |
| Fallback behavior | Hard fail (`MtlsPopTokenNotSupportedinImdsV1`) | Hard fail (`MsiV2Error`) |
| Capability discovery | `GetManagedIdentityCapabilitiesAsync()` | Future |

---

## Minimum Integration Example

For SDK authors who want the fastest path to integration:

```python
"""Minimal integration — full mTLS PoP."""
import msal
from msal_schannel_transport import SchannelSession

# Acquire token
client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=__import__("requests").Session(),
)
result = client.acquire_token_for_client(
    resource="https://vault.azure.net",
    mtls_proof_of_possession=True,
    with_attestation_support=True,
)

# Downstream call — cert goes in constructor
session = SchannelSession(client_certificate=result["binding_certificate"])
response = session.get(
    "https://tokenbinding.vault.azure.net/secrets/boundsecret/?api-version=7.5",
    headers={
        "Authorization": f"{result['token_type']} {result['access_token']}",
        "x-ms-tokenboundauth": "true",
    },
)
print(response.status_code, response.text)
```

---

## Package Dependencies

| Package | Role | Required? |
|---------|------|-----------|
| `msal` | Token acquisition + `WindowsCertificate` | Yes |
| `msal-key-attestation` | MAA attestation (KeyGuard proof) | Yes for `with_attestation_support=True` |
| `msal-schannel-transport` | WinHTTP-based downstream mTLS | Yes (or build your own) |
| `requests` | HTTP client for MSAL's IMDS calls | Yes |

---

## Future: OpenSSL 3 CNG Provider (Strategic Path)

When a Microsoft-supported OpenSSL 3 CNG Provider becomes available, the
transport layer simplifies to:

```python
from azure_identity_mtls import create_mtls_context
import requests

ctx = create_mtls_context(thumbprint=result["cert_thumbprint_sha256"])
response = requests.get(url, headers=headers)  # standard requests, no WinHTTP needed
```

This is a future investment. The current WinHTTP approach is production-ready
and provides identical security guarantees.
