# MSI v2 (mTLS Proof-of-Possession) — Setup & Usage Guide

## Overview

MSI v2 enables Managed Identity token acquisition using mTLS Proof-of-Possession
on Windows Azure VMs with Credential Guard / KeyGuard.

The implementation is split into two packages mirroring the MSAL .NET architecture:

| Package | .NET Equivalent | What |
|---|---|---|
| `msal` | `Microsoft.Identity.Client` | Core mTLS PoP flow (KeyGuard key, CSR, IMDS, WinHTTP) |
| `msal-key-attestation` | `Microsoft.Identity.Client.KeyAttestation` | AttestationClientLib.dll native bindings |

## Prerequisites

1. **Windows Azure VM** with:
   - Credential Guard / KeyGuard enabled (VBS)
   - System-assigned or user-assigned managed identity
   - Network access to IMDS (169.254.169.254)

2. **AttestationClientLib.dll** — place in one of:
   - Current working directory
   - Same directory as `python.exe`
   - Directory of the `msal_key_attestation` package
   - Path specified by `ATTESTATION_CLIENTLIB_PATH` env var

## Installation

```bash
# Core MSAL package (includes MSI v2 flow)
pip install msal

# Attestation support (loads AttestationClientLib.dll)
pip install msal-key-attestation
```

For development (from this repo):

```bash
# Install msal in editable mode
pip install -e .

# Install msal-key-attestation in editable mode
pip install -e msal-key-attestation/
```

## Quick Start

```python
import msal
import requests

client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
)

result = client.acquire_token_for_client(
    resource="https://graph.microsoft.com",
    mtls_proof_of_possession=True,
    with_attestation_support=True,
)

if "access_token" in result:
    print(f"Token type: {result['token_type']}")        # mtls_pop
    print(f"Expires in: {result['expires_in']}s")
    print(f"Thumbprint: {result['cert_thumbprint_sha256']}")
else:
    print(f"Error: {result}")
```

## API Reference

### `acquire_token_for_client()` — New Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mtls_proof_of_possession` | `bool` | `False` | Enable MSI v2 mTLS PoP flow |
| `with_attestation_support` | `bool` | `False` | Enable KeyGuard attestation (requires `msal-key-attestation`) |

**Behavior matrix:**

| `mtls_proof_of_possession` | `with_attestation_support` | Result |
|---|---|---|
| `False` | `False` | MSI v1 (default, unchanged) |
| `True` | `False` | MSI v1 fallthrough (PoP alone = no-op) |
| `False` | `True` | Raises `ManagedIdentityError` |
| `True` | `True` | **MSI v2** — KeyGuard + attestation + mTLS PoP |

### Response

When MSI v2 succeeds, the response dict includes extra fields:

```python
{
    "access_token": "eyJ...",
    "expires_in": 3600,
    "token_type": "mtls_pop",
    "resource": "https://graph.microsoft.com",
    # Additional MSI v2 fields:
    "cert_pem": "-----BEGIN CERTIFICATE-----\n...",
    "cert_der_b64": "MIID...",
    "cert_thumbprint_sha256": "abc123...",
}
```

### Errors

| Error Class | When |
|---|---|
| `ManagedIdentityError` | `with_attestation_support=True` without `mtls_proof_of_possession` |
| `MsiV2Error` | Any MSI v2 flow failure (no fallback to v1) |
| `MsiV2Error` | `msal-key-attestation` package not installed |

### Verification

```python
from msal.msi_v2 import verify_cnf_binding

bound = verify_cnf_binding(result["access_token"], result["cert_pem"])
assert bound, "Token is not bound to the certificate"
```

## Flow Diagram

```
App                    MSAL Python              IMDS              ESTS (mTLS)
 |                        |                      |                    |
 |-- acquire_token ------>|                      |                    |
 |   (mtls_pop=True,      |                      |                    |
 |    attestation=True)   |                      |                    |
 |                        |                      |                    |
 |                  [1] NCrypt: KeyGuard key      |                    |
 |                  [2] GET /getplatformmetadata ->|                    |
 |                        |<-- clientId, tenantId,|                    |
 |                        |    cuId, attestEP     |                    |
 |                  [3] Build CSR (RSA-PSS/SHA256) |                    |
 |                  [4] AttestationClientLib.dll   |                    |
 |                        |--- MAA attest -------->|                    |
 |                        |<-- attestation JWT     |                    |
 |                  [5] POST /issuecredential ---->|                    |
 |                        |<-- certificate, endpoint                   |
 |                  [6] Crypt32: bind cert to key  |                    |
 |                  [7] WinHTTP: POST /token ------|--------->|         |
 |                        |                        |          |         |
 |                        |<-- mtls_pop token ----|---------|          |
 |<-- result -------------|                      |                    |
```

## Environment Variables

| Variable | Description |
|---|---|
| `AZURE_POD_IDENTITY_AUTHORITY_HOST` | Override IMDS base URL |
| `MSAL_MSI_V2_KEY_NAME` | Override per-boot key name |
| `ATTESTATION_CLIENTLIB_PATH` | Full path to AttestationClientLib.dll |
| `MSAL_MSI_V2_ATTESTATION_CACHE` | `"0"` to disable MAA JWT caching |

## Running the Sample

```bash
cd sample/
python msi_v2_sample.py

# With verbose logging:
MSI_V2_VERBOSE=1 python msi_v2_sample.py

# Custom resource:
RESOURCE=https://vault.azure.net python msi_v2_sample.py
```

## Running Tests

```bash
# Core MSI v2 tests (no Windows/KeyGuard dependency)
pytest tests/test_msi_v2.py -v

# Attestation package tests
cd msal-key-attestation/
pytest tests/test_attestation.py -v
```
