# MSI v2 mTLS End-to-End in Python — Summary

## Problem

MSAL Python's MSI v2 flow acquires an `mtls_pop` token bound to a KeyGuard-protected certificate. After token acquisition, the calling app needs to present the **same certificate** over mTLS when calling the resource (e.g., Azure Key Vault).

In .NET, this is transparent — `X509Certificate2` wraps both the cert and the CNG key reference, and `HttpClient` + SChannel handles mTLS natively. The caller just does:

```csharp
var handler = new HttpClientHandler {
    ClientCertificates = { result.BindingCertificate }
};
var client = new HttpClient(handler);
```

**Python cannot do this.** Python's HTTP libraries (`requests`, `httpx`, `urllib3`) all use **OpenSSL** for TLS, which requires the private key as exportable bytes. A KeyGuard key is **non-exportable by design** — OpenSSL cannot access it.

## Solution

MSAL Python provides `mtls_http_request()` — a helper that uses **WinHTTP/SChannel** (the Windows-native TLS stack) via ctypes to make mTLS resource calls. SChannel can access the non-exportable KeyGuard key natively, just like .NET does.

```python
from msal.msi_v2 import mtls_http_request

result = client.acquire_token_for_client(
    resource="https://vault.azure.net",
    mtls_proof_of_possession=True,
    with_attestation_support=True,
)

cert_der = base64.b64decode(result["cert_der_b64"])
resp = mtls_http_request(
    "GET",
    "https://tokenbinding.vault.azure.net/secrets/boundsecret/?api-version=2015-06-01",
    cert_der,
    headers={
        "Authorization": f"{result['token_type']} {result['access_token']}",
        "Accept": "application/json",
        "x-ms-tokenboundauth": "true",
    },
)
```

## Why Python Needs a Helper (and .NET Doesn't)

| | .NET | Python |
|---|---|---|
| TLS stack | SChannel (Windows native) | OpenSSL |
| Can access non-exportable CNG keys | ✅ via `X509Certificate2` | ❌ Not possible |
| Standard HTTP client works for mTLS | ✅ `HttpClient` + `ClientCertificates` | ❌ `requests`/`httpx` cannot use KeyGuard keys |
| Solution | Platform gives it for free | `mtls_http_request()` bridges the gap via WinHTTP/SChannel |

## Key Technical Findings

During development, three requirements were discovered for mTLS resource calls:

1. **`x-ms-tokenboundauth: true` header** — Required by Azure Key Vault. This header triggers the server to request the client certificate via TLS renegotiation. Without it, the server never asks for the cert. Discovered from the [.NET E2E test](https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/main/tests/Microsoft.Identity.Test.E2e/ManagedIdentityImdsV2Tests.cs).

2. **HTTP/1.1 (not HTTP/2)** — TLS renegotiation (needed for the server to request the client cert after seeing the header) is forbidden in HTTP/2. HTTP/1.1 must be used for the resource call.

3. **TLS 1.2** — TLS renegotiation for client certificates works reliably in TLS 1.2. TLS 1.3 uses post-handshake authentication which WinHTTP may not fully support.

## Auth Result Fields

```python
{
    "access_token": "eyJ...",
    "token_type": "mtls_pop",
    "expires_in": 86399,
    "cert_pem": "-----BEGIN CERTIFICATE-----\n...",
    "cert_der_b64": "MIID...",
    "cert_thumbprint_sha256": "abc123...",
    "key_name": "MsalMsiV2Key_caf87a12-..."
}
```

## Architecture Comparison

```
.NET E2E Flow:
  MSAL.NET → mtls_pop token + X509Certificate2 (wraps CNG key)
       ↓
  HttpClient + SChannel → mTLS to Key Vault ← platform-native, no helper needed

Python E2E Flow:
  MSAL Python → mtls_pop token + cert PEM/DER (no private key access)
       ↓
  mtls_http_request() → WinHTTP/SChannel via ctypes → mTLS to Key Vault
  (helper needed because OpenSSL cannot access non-exportable KeyGuard keys)
```

## Status

- ✅ Token acquisition works (mtls_pop token with KeyGuard attestation)
- ✅ Certificate binding verified (cnf.x5t#S256 matches)
- ✅ mTLS resource call presents certificate (confirmed by matching .NET's error)
- ⏳ AKV end-to-end blocked on service-side issue ("Certificate import or verification failed" — same error in both .NET and Python)
