# MSI v2 — mTLS Proof-of-Possession API Reference

## Overview

MSI v2 enables Managed Identity token acquisition with mTLS Proof-of-Possession
on Windows Azure VMs with Credential Guard (KeyGuard). Tokens are
cryptographically bound to a per-boot hardware-isolated key.

## Requirements

- Windows Azure VM with Credential Guard / KeyGuard enabled
- `AttestationClientLib.dll` available on the system
- Python 3.9+

## Installation

```bash
pip install msal msal-key-attestation requests
```

## Public API

### Token Acquisition

```python
import msal
import requests

client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
)

result = client.acquire_token_for_client(
    resource="https://graph.microsoft.com",
    mtls_proof_of_possession=True,    # Enable MSI v2 mTLS PoP
    with_attestation_support=True,    # Require msal-key-attestation
)
```

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `resource` | str | Yes | Resource URI (e.g., `https://graph.microsoft.com`) |
| `mtls_proof_of_possession` | bool | No | Enable mTLS PoP token binding |
| `with_attestation_support` | bool | No | Require MAA attestation (needs `msal-key-attestation`) |

#### Return Value

On success, returns a dict:

```python
{
    "access_token": "eyJ0eXAi...",        # The mTLS-bound access token
    "expires_in": 3599,                    # Token lifetime in seconds
    "token_type": "mtls_pop",             # Always "mtls_pop" for this flow
    "binding_certificate": WindowsCertificate(...),  # Use with SchannelSession
    "cert_der_b64": "MIIC...",            # Base64-encoded DER certificate
    "cert_pem": "-----BEGIN CERT...",     # PEM-encoded public certificate only
    "cert_thumbprint_sha256": "buc7x...", # Base64url SHA-256 thumbprint
}
```

On failure, raises `MsiV2Error`.

### User-Assigned Managed Identity

```python
# By client_id
client = msal.ManagedIdentityClient(
    msal.UserAssignedManagedIdentity(client_id="11111111-..."),
    http_client=requests.Session(),
)

# By object_id
client = msal.ManagedIdentityClient(
    msal.UserAssignedManagedIdentity(object_id="22222222-..."),
    http_client=requests.Session(),
)

# By resource_id
client = msal.ManagedIdentityClient(
    msal.UserAssignedManagedIdentity(
        resource_id="/subscriptions/.../providers/Microsoft.ManagedIdentity/..."),
    http_client=requests.Session(),
)
```

### Downstream mTLS Call

After acquiring the token, use the returned `binding_certificate` for
downstream mTLS API calls. The `cert_pem` in the result is the public
certificate bound to the token's `cnf.x5t#S256` claim, but it is not enough
to perform downstream mTLS by itself because the private key remains
platform-backed and non-exportable.

> **Note:** Standard Python `requests` cannot present a KeyGuard-bound
> certificate (the private key is non-exportable). Use WinHTTP/SChannel
> for production mTLS calls. Use `SchannelSession` with the returned
> `binding_certificate` to present the certificate during the TLS handshake.

```python
# Authorization header format:
headers = {
    "Authorization": f"mtls_pop {result['access_token']}"
}
```

### Helper Functions

```python
from msal.msi_v2 import get_cert_thumbprint_sha256, verify_cnf_binding

# Compute base64url SHA-256 thumbprint of a PEM certificate
thumbprint = get_cert_thumbprint_sha256(cert_pem)

# Verify that a JWT's cnf.x5t#S256 matches the certificate
is_bound = verify_cnf_binding(access_token, cert_pem)
```

## Error Handling

```python
from msal import MsiV2Error, ManagedIdentityError

try:
    result = client.acquire_token_for_client(...)
except MsiV2Error as e:
    # MSI v2 specific failure (no v1 fallback)
    print(f"MSI v2 failed: {e}")
except ManagedIdentityError as e:
    # General managed identity error
    print(f"MI error: {e}")
```

### Error Hierarchy

```
ValueError
└── ManagedIdentityError
    └── MsiV2Error
```

### Common Errors

| Error Message | Cause |
|---------------|-------|
| `KeyGuard + mTLS PoP is Windows-only` | Running on non-Windows |
| `with_attestation_support=True requires...` | `msal-key-attestation` not installed |
| `attestation_requires_pop` | `with_attestation_support=True` without `mtls_proof_of_possession=True` |
| `getplatformmetadata missing required fields` | VM doesn't support IMDS v2 |
| `attestationEndpoint missing` | VM doesn't have MAA configured |

## msal-key-attestation Package

Separate pip package providing Windows `AttestationClientLib.dll` bindings.

### API

```python
from msal_key_attestation import create_attestation_provider

provider = create_attestation_provider()
# provider(endpoint, key_handle, client_id, cache_key) -> str (JWT)
```

The provider is automatically discovered by MSAL when
`with_attestation_support=True` is set.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ATTESTATION_CLIENTLIB_PATH` | Absolute path to `AttestationClientLib.dll` |
| `MSAL_MSI_V2_ATTESTATION_CACHE` | Set to `"0"` to disable MAA token cache |

## Environment Variables (Core)

| Variable | Description |
|----------|-------------|
| `AZURE_POD_IDENTITY_AUTHORITY_HOST` | Override IMDS base URL (default: `http://169.254.169.254`) |
| `MSAL_MSI_V2_KEY_NAME` | Override per-boot key name |

## Flow Diagram

```
1. NCrypt      → Create/open KeyGuard RSA key (VBS-isolated, per-boot)
2. IMDS        → GET /getplatformmetadata → clientId, tenantId, cuId, attestationEndpoint
3. CSR         → Build PKCS#10 (RSA-PSS/SHA256 + cuId OID attribute)
4. Attestation → AttestationClientLib.dll → MAA JWT (proves key is KeyGuard-protected)
5. IMDS        → POST /issuecredential {csr, attestation_token} → X.509 certificate
6. Crypt32     → Bind certificate to NCrypt key handle (SChannel-ready)
7. WinHTTP     → POST /oauth2/v2.0/token via mTLS → mtls_pop access token
```

## Caching

- **Certificate cache**: In-memory, process-local. Evicts when remaining
  lifetime < 1 hour. Keyed by managed identity + attestation mode.
- **MAA token cache** (in `msal-key-attestation`): Refreshes at 90% of
  JWT lifetime, 10-second absolute guard before expiry.
