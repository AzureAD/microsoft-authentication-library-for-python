# MSAL Python — mTLS PoP with Non-Exportable Keys: Architecture Decision

## Problem

Python applications on Azure VMs with Credential Guard / KeyGuard need to:
1. Acquire an `mtls_pop` token bound to a non-exportable private key
2. Make downstream API calls presenting that same key via mTLS (TLS client certificate)

The private key lives inside Windows VBS (Virtualization-Based Security) and **cannot be exported as raw bytes**. This is by design — it's the entire security model of KeyGuard.

## Why Python Has a Gap

Every mainstream Python HTTP library uses OpenSSL for TLS:

| Library | TLS Backend | Can use non-exportable keys? |
|---------|-------------|------------------------------|
| `requests` / `urllib3` | OpenSSL | ❌ Requires `key_bytes` |
| `httpx` | OpenSSL | ❌ Requires `key_bytes` |
| `aiohttp` | OpenSSL | ❌ Requires `key_bytes` |
| `ssl` stdlib | OpenSSL | ❌ `SSLContext.load_cert_chain(keyfile=...)` needs a file/PEM |

OpenSSL needs the private key as raw bytes (PEM/DER) to perform the TLS `CertificateVerify` signature. A non-exportable CNG key handle (`NCRYPT_KEY_HANDLE`) cannot provide this.

**.NET does not have this problem** — `HttpClientHandler` uses Windows SChannel natively, which accepts `X509Certificate2` objects backed by non-exportable keys. The OS TLS stack performs the signature internally.

## Research: Could We Use an OpenSSL CNG Provider?

### RTI OpenSSL CNG Engine (2021)

- **Repo:** https://github.com/rticommunity/openssl-cng-engine
- **Docs:** https://openssl-cng-engine.readthedocs.io/en/latest/about.html
- **Author:** RTI (Real-Time Innovations), a DDS middleware company
- **License:** Apache 2.0

RTI built an OpenSSL **ENGINE** that bridges OpenSSL → Windows CNG. It provides:
- `engine-bcrypt.dll` — Crypto primitives (AES, RSA, ECDSA)
- `engine-ncrypt.dll` — Key Store access (reads certs/keys from Windows cert store)

### Why It Doesn't Work for Us

| Issue | Detail |
|-------|--------|
| **OpenSSL 1.1.1 only** | Python 3.12+ ships OpenSSL 3.x. The ENGINE API was deprecated. |
| **Not ported to OpenSSL 3.x PROVIDER** | OpenSSL 3.x replaced ENGINEs with PROVIDERs (completely different API). No port exists. |
| **No KeyGuard/VBS testing** | Built for regular CNG keys, not VBS-isolated keys. |
| **Python `ssl` module** | Doesn't expose `ENGINE_load()` or `OSSL_PROVIDER_load()` to user code. |
| **RSA-PSS limitation** | ENGINE API can't support RSA-PSS (needed for modern TLS 1.3). |
| **Unmaintained** | Last meaningful activity ~2021. |

### OpenSSL Mailing List Confirmation (July 2021)

From David von Oheimb (OpenSSL project):
> "Porting this to the new OpenSSL crypto **provider** interface will likely lift the limitation regarding RSA-PSS support, which lacks just due to the engine interface."

Source: https://mta.openssl.org/pipermail/openssl-users/2021-July/013944.html

**No one has built this provider.** Not RTI, not Microsoft, not the OpenSSL community.

## Our Solution: WinHTTP/SChannel via `msal-schannel-transport`

Instead of trying to make OpenSSL work with non-exportable keys, we bypass OpenSSL entirely and use Windows' native TLS stack (SChannel) via WinHTTP — the same approach .NET uses internally.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  App Developer Code                                              │
│                                                                  │
│  result = client.acquire_token_for_client(                       │
│      resource="https://vault.azure.net",                         │
│      mtls_proof_of_possession=True,                              │
│      with_attestation_support=True,                              │
│  )                                                               │
│                                                                  │
│  binding_cert = result["binding_certificate"]  # WindowsCert     │
│  auth_header = f"{result['token_type']} {result['access_token']}│
│                                                                  │
│  with SchannelSession(client_certificate=binding_cert) as s:     │
│      response = s.get(url, headers={"Authorization": auth_header})│
└──────────────┬──────────────────────────────────┬────────────────┘
               │                                  │
    ┌──────────▼──────────┐          ┌───────────▼────────────┐
    │  msal (pip package)  │          │ msal-schannel-transport │
    │                      │          │    (pip package)         │
    │ • Token acquisition  │          │ • WinHTTP/SChannel      │
    │ • KeyGuard key mgmt  │          │ • Uses NCRYPT_KEY_HANDLE│
    │ • Attestation (MAA)  │          │ • No OpenSSL dependency │
    │ • Returns cert handle│          │ • mTLS with non-export  │
    └──────────────────────┘          └─────────────────────────┘
```

### Why Separate Packages?

| Principle | Implementation |
|-----------|---------------|
| MSAL only acquires tokens | `msal` never makes downstream calls |
| App developer owns HTTP | `msal-schannel-transport` is a helper, not MSAL |
| Same pattern as .NET | .NET: MSAL → HttpClient. Python: MSAL → SchannelSession |
| Clear dependency boundary | Teams that don't need downstream mTLS don't install it |

### Comparison with Other Languages

| Language | Token Library | Downstream mTLS Transport | Notes |
|----------|--------------|---------------------------|-------|
| **.NET** | MSAL.NET | `HttpClientHandler` (built-in) | SChannel native, just works |
| **Go** | azure-sdk-for-go | `crypto/tls` + CNG bridge | Go has pluggable TLS |
| **Rust** | azure-sdk-for-rust | `schannel` crate | Native Windows TLS |
| **Python** | MSAL Python | `msal-schannel-transport` (ours) | OpenSSL can't, so we provide it |

## E2E Proof (June 2026)

Tested on `MSIV2` Azure VM (Windows, Credential Guard enabled):

```
✓ Token acquired: mtls_pop, 86399s expiry
✓ Binding certificate: WindowsCertificate (non-exportable, KeyGuard-backed)
✓ cnf.x5t#S256 binding: MATCH
✓ Downstream mTLS call: HTTP 200 from tokenbinding.vault.azure.net
  Secret value returned: "secretme"
```

## Future Alternatives (if landscape changes)

| If this happens... | We could... |
|---|---|
| Microsoft ships OpenSSL 3.x CNG Provider | Use `requests` normally with provider loaded |
| Python `ssl` exposes `OSSL_PROVIDER_load()` | Load CNG provider from Python |
| Python adds native SChannel support | Use stdlib directly |
| RTI ports to OpenSSL 3.x + supports KeyGuard | Evaluate as alternative |

**None of these exist today.** Our WinHTTP approach is the only working solution for Python + non-exportable keys + mTLS.
