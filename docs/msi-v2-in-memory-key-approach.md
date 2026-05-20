# MSI v2 In-Memory Key Approach — Design Document

## Goal

Implement an MSI v2 path using an **in-memory software RSA key** (no KeyGuard).
The private key is exportable, so standard Python HTTP libraries (`requests`) work
for both token acquisition and resource calls. **No MSAL helper needed.**

This matches .NET's `InMemoryManagedIdentityKeyProvider` — the lowest tier in the
key hierarchy.

---

## .NET Reference Implementation

From [`InMemoryManagedIdentityKeyProvider.cs`](https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/main/src/client/Microsoft.Identity.Client/ManagedIdentity/KeyProviders/InMemoryManagedIdentityKeyProvider.cs):

```csharp
// Portable (non-Windows): pure in-memory RSA
private static RSA CreatePortableRsa()
{
    var rsa = RSA.Create();
    rsa.KeySize = 2048;
    return rsa;
}

// Windows: persisted CNG key with AllowExport
private static RSA CreateWindowsPersistedRsa()
{
    var creation = new CngKeyCreationParameters
    {
        ExportPolicy = CngExportPolicies.AllowExport,  // ← EXPORTABLE
        Provider = CngProvider.MicrosoftSoftwareKeyStorageProvider
    };
    string keyName = "MSAL-MTLS-" + Guid.NewGuid().ToString("N");
    var key = CngKey.Create(CngAlgorithm.Rsa, keyName, creation);
    return new RSACng(key);
}
```

Key points:
- **`AllowExport`** — the key CAN be extracted as bytes
- No VBS/KeyGuard flags — purely software key
- No attestation — MAA not called
- Named + persisted so SChannel can use it (Windows only)

---

## Python Implementation Design

### Key Generation

```python
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Generate exportable RSA-2048 key
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Export as PEM — this is possible because key is in-memory (not KeyGuard)
key_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
```

### CSR Building

```python
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.asymmetric.padding import PSS, MGF1

# Build CSR using cryptography library (no manual DER needed)
csr = (
    x509.CertificateSigningRequestBuilder()
    .subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, client_id),
        x509.NameAttribute(NameOID.DOMAIN_COMPONENT, tenant_id),
    ]))
    .add_attribute(cu_id_oid, cu_id_value)
    .sign(private_key, SHA256(), padding=PSS(mgf=MGF1(SHA256()), salt_length=32))
)
csr_b64 = base64.b64encode(csr.public_bytes(serialization.Encoding.DER)).decode()
```

### IMDS Calls

Same as KeyGuard path but **no attestation token**:

```python
# Step 1: getplatformmetadata (identical)
meta = http_client.get(imds_base + "/metadata/identity/getplatformmetadata",
                       params={"cred-api-version": "2.0"}, headers={"Metadata": "true"})

# Step 2: issuecredential — empty attestation_token
cred = http_client.post(imds_base + "/metadata/identity/issuecredential",
                        params={"cred-api-version": "2.0"},
                        headers={"Metadata": "true", "Content-Type": "application/json"},
                        json={"csr": csr_b64, "attestation_token": ""})  # ← empty
```

### Token Acquisition (mTLS)

Since the key is exportable, use `requests` with cert + key PEM:

```python
import requests
import tempfile, os

# Write cert + key to temp files (requests needs file paths)
with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as cf:
    cf.write(cert_pem)
    cert_path = cf.name
with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as kf:
    kf.write(key_pem)
    key_path = kf.name

try:
    token_resp = requests.post(
        token_endpoint,
        cert=(cert_path, key_path),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "scope": scope,
            "token_type": "mtls_pop",
        },
    )
finally:
    os.unlink(cert_path)
    os.unlink(key_path)
```

### Auth Result

```python
{
    "access_token": "eyJ...",
    "token_type": "mtls_pop",
    "expires_in": 86399,
    "cert_pem": "-----BEGIN CERTIFICATE-----\n...",
    "key_pem": "-----BEGIN RSA PRIVATE KEY-----\n...",  # ← AVAILABLE
    "cert_thumbprint_sha256": "abc123...",
}
```

### Resource Call — No Helper Needed!

The caller uses standard `requests`:

```python
result = client.acquire_token_for_client(
    resource="https://vault.azure.net",
    mtls_proof_of_possession=True,
)

# Write cert+key to temp files (or use in-memory with urllib3)
# ... (same temp file pattern as above)

resp = requests.get(
    "https://tokenbinding.vault.azure.net/secrets/boundsecret/?api-version=2015-06-01",
    cert=(cert_path, key_path),
    headers={
        "Authorization": f"{result['token_type']} {result['access_token']}",
        "x-ms-tokenboundauth": "true",
    },
)
```

---

## Comparison: KeyGuard vs In-Memory

| Aspect | KeyGuard (current PR) | In-Memory (this design) |
|--------|----------------------|------------------------|
| Key type | Non-exportable CNG/VBS | Exportable software RSA |
| Attestation | MAA (proves hardware) | None |
| `key_pem` in result? | ❌ Impossible | ✅ Yes |
| Token acquisition | WinHTTP/SChannel (ctypes) | `requests` + cert/key PEM |
| Resource call | `mtls_http_request()` helper | Standard `requests` |
| Helper needed? | **Yes** | **No** |
| Platform | Windows + Credential Guard | **Any** (cross-platform) |
| Dependencies | `msal-key-attestation`, ctypes | `cryptography` (already used) |
| Security | ★★★★★ | ★★☆☆☆ |

---

## API Design

```python
# KeyGuard + attestation (high security, helper required)
result = client.acquire_token_for_client(
    resource=...,
    mtls_proof_of_possession=True,
    with_attestation_support=True,     # ← KeyGuard path
)
# result has cert_pem, cert_der_b64 but NO key_pem
# Must use: mtls_http_request() for resource calls

# In-memory (lower security, no helper needed)
result = client.acquire_token_for_client(
    resource=...,
    mtls_proof_of_possession=True,
    # with_attestation_support=False (default) ← In-memory path
)
# result has cert_pem AND key_pem
# Standard: requests.get(url, cert=(cert, key)) just works
```

---

## Implementation Effort

| Component | KeyGuard (done) | In-Memory (new) |
|-----------|----------------|-----------------|
| Key generation | NCrypt via ctypes | `cryptography.rsa.generate_private_key()` |
| CSR building | Manual DER builder (500+ LOC) | `cryptography.x509.CertificateSigningRequestBuilder` (~20 LOC) |
| IMDS calls | Shared | Shared |
| Token acquisition | WinHTTP/SChannel via ctypes | `requests.post(cert=...)` |
| Platform | Windows only | Cross-platform |
| Complexity | High (ctypes, Win32 APIs) | Low (pure Python) |

The in-memory path is **significantly simpler** — most of the complexity in `msi_v2.py`
(NCrypt, Crypt32, WinHTTP, manual DER) is specifically for KeyGuard. The in-memory path
can be implemented with `cryptography` + `requests` in ~200 lines.
