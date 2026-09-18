# msal-key-attestation

KeyGuard attestation support for MSAL Python MSI v2.

Provides `create_attestation_provider()` which returns a callable that mints
MAA (Microsoft Azure Attestation) JWTs for KeyGuard-protected keys using
`AttestationClientLib.dll`.

## Installation

```bash
pip install msal-key-attestation
```

## Requirements

- Windows with Credential Guard / KeyGuard enabled
- `AttestationClientLib.dll` available (from `Microsoft.Azure.Security.KeyGuardAttestation` NuGet v1.1.5)

## Usage

```python
from msal_key_attestation import create_attestation_provider

provider = create_attestation_provider()
# provider(endpoint, key_handle, client_id, cache_key) -> str (JWT)
```

This package is automatically discovered by MSAL Python when
`with_attestation_support=True` is passed to `acquire_token_for_client()`.
