# msal-schannel-transport

Windows SChannel/WinHTTP HTTP transport for downstream mTLS calls using
platform-backed certificates with non-exportable private keys.

## Why

Python's `ssl` module uses OpenSSL, which requires raw private key bytes.
KeyGuard/TPM keys are non-exportable by design. This package uses Windows
native WinHTTP/SChannel which works directly with NCRYPT_KEY_HANDLEs.

## Installation

```bash
pip install msal-schannel-transport
```

## Usage

```python
from msal_schannel_transport import SchannelSession

# cert is a WindowsCertificate from MSAL auth result
session = SchannelSession(client_certificate=cert)
response = session.get(
    "https://my-api.example.com/resource",
    headers={"Authorization": f"{token_type} {access_token}"},
)
print(response.status_code, response.text)
```

## Requirements

- Windows (uses WinHTTP.dll)
- Python 3.9+
