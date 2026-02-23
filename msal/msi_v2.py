# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
MSI v2 (IMDSv2) Managed Identity flow — Windows KeyGuard + Attestation + SChannel mTLS PoP.

This matches your working PowerShell flow:
  - KeyGuard RSACng key (VBS isolated)
  - GET /getplatformmetadata?cred-api-version=2.0
  - CSR (RSA-PSS/SHA256) + OID attribute 1.3.6.1.4.1.311.90.2.10 with DER UTF8String(JSON(cuId))
  - AttestationClientLib.dll → attestation JWT
  - POST /issuecredential?cred-api-version=2.0 with attestation_token
  - Token request to ESTS v2 over mTLS using .NET HttpClient (SChannel), token_type=mtls_pop

No MSI-v1 fallback happens here: any failure raises MsiV2Error.
"""

from __future__ import annotations

import base64
import json
import logging
import hashlib
import os
import sys
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IMDS_DEFAULT_BASE = "http://169.254.169.254"
_IMDS_BASE_ENVVAR = "AZURE_POD_IDENTITY_AUTHORITY_HOST"

_API_VERSION_QUERY_PARAM = "cred-api-version"
_IMDS_V2_API_VERSION = "2.0"

_CSR_METADATA_PATH = "/metadata/identity/getplatformmetadata"
_ISSUE_CREDENTIAL_PATH = "/metadata/identity/issuecredential"
_ACQUIRE_ENTRA_TOKEN_PATH = "/oauth2/v2.0/token"

_CU_ID_OID_STR = "1.3.6.1.4.1.311.90.2.10"

# flags from your PS script
_NCRYPT_USE_VIRTUAL_ISOLATION_FLAG = 0x00020000
_NCRYPT_USE_PER_BOOT_KEY_FLAG      = 0x00040000

_RSA_KEY_SIZE = 2048

# ----------------------------
# Compatibility helpers (tests + cross-language parity)
# ----------------------------

def get_cert_thumbprint_sha256(cert_pem: str) -> str:
    """
    Return base64url(SHA256(der(cert))) without padding, for cnf.x5t#S256 comparisons.

    Accepts PEM certificate string.
    """
    try:
        # lightweight: use cryptography if present
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"), default_backend())
        der = cert.public_bytes(serialization.Encoding.DER)
        digest = hashlib.sha256(der).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    except Exception:
        # If cryptography isn't available, fail closed (binding cannot be verified)
        return ""

def verify_cnf_binding(token: str, cert_pem: str) -> bool:
    """
    Verify that JWT payload contains cnf.x5t#S256 matching the cert thumbprint.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False

        payload_b64 = parts[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))

        cnf = claims.get("cnf", {}) if isinstance(claims, dict) else {}
        token_x5t = cnf.get("x5t#S256")
        if not token_x5t:
            return False

        cert_x5t = get_cert_thumbprint_sha256(cert_pem)
        if not cert_x5t:
            return False

        return token_x5t == cert_x5t
    except Exception:
        return False

def _imds_base() -> str:
    return os.getenv(_IMDS_BASE_ENVVAR, _IMDS_DEFAULT_BASE).strip().rstrip("/")


def _new_correlation_id() -> str:
    return str(uuid.uuid4())


def _imds_headers(correlation_id: Optional[str] = None) -> Dict[str, str]:
    return {"Metadata": "true", "x-ms-client-request-id": correlation_id or _new_correlation_id()}


def _resource_to_scope(resource_or_scope: str) -> str:
    s = (resource_or_scope or "").strip()
    if not s:
        raise ValueError("resource must be non-empty")
    if s.endswith("/.default"):
        return s
    return s.rstrip("/") + "/.default"


def _der_utf8string(value: str) -> bytes:
    """
    DER UTF8String encoder (tag 0x0C). (Used only if you want to match PS fallback.)
    """
    raw = value.encode("utf-8")
    n = len(raw)
    if n < 0x80:
        len_bytes = bytes([n])
    else:
        tmp = bytearray()
        m = n
        while m > 0:
            tmp.insert(0, m & 0xFF)
            m >>= 8
        len_bytes = bytes([0x80 | len(tmp)]) + bytes(tmp)
    return bytes([0x0C]) + len_bytes + raw


def _json_loads(text: str, what: str) -> Dict[str, Any]:
    from .managed_identity import MsiV2Error
    try:
        return json.loads(text)
    except Exception as exc:  # pylint: disable=broad-except
        raise MsiV2Error(f"[msi_v2] Invalid JSON from {what}: {text!r}") from exc


def _get_first(obj: Dict[str, Any], *names: str) -> Optional[str]:
    # direct keys
    for n in names:
        if n in obj and obj[n] is not None and str(obj[n]).strip() != "":
            return str(obj[n])
    # case-insensitive
    lower = {str(k).lower(): k for k in obj.keys()}
    for n in names:
        k = lower.get(n.lower())
        if k and obj[k] is not None and str(obj[k]).strip() != "":
            return str(obj[k])
    return None


def _mi_query_params(managed_identity: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Adds cred-api-version=2.0 plus optional UAMI selector params.
    managed_identity shape (MSAL python): {"ManagedIdentityIdType": "...", "Id": "..."}
    """
    params: Dict[str, str] = {_API_VERSION_QUERY_PARAM: _IMDS_V2_API_VERSION}
    if not isinstance(managed_identity, dict):
        return params

    id_type = managed_identity.get("ManagedIdentityIdType")
    identifier = managed_identity.get("Id")

    mapping = {"ClientId": "client_id", "ObjectId": "object_id", "ResourceId": "msi_res_id"}
    wire = mapping.get(id_type)
    if wire and identifier:
        params[wire] = str(identifier)
    return params

def _dotnet_imports():
    """
    Loads needed .NET types via pythonnet.
    """
    from .managed_identity import MsiV2Error

    if sys.platform != "win32":
        raise MsiV2Error("[msi_v2] KeyGuard + attested mTLS PoP is Windows-only.")

    try:
        import clr  # type: ignore
    except Exception as exc:
        raise MsiV2Error(
            "[msi_v2] pythonnet (clr) is required for Windows KeyGuard + SChannel mTLS. "
            "Install pythonnet and ensure .NET runtime is available."
        ) from exc

    # best-effort references
    for asm in ("System", "System.Net.Http", "System.Security", "System.Security.Cryptography"):
        try:
            clr.AddReference(asm)
        except Exception:
            pass

    # IMPORTANT: AsnEncodedData is in System.Security.Cryptography, not X509Certificates
    from System import Array, Byte, BitConverter, Convert, Enum  # type: ignore
    from System.Security.Cryptography import (  # type: ignore
        AsnEncodedData,
        CngAlgorithm,
        CngExportPolicies,
        CngKey,
        CngKeyCreationOptions,
        CngKeyCreationParameters,
        CngKeyUsages,
        CngProperty,
        CngPropertyOptions,
        CngProvider,
        HashAlgorithmName,
        RSACng,
        RSASignaturePadding,
        X509Certificates,
    )
    from System.Security.Cryptography.X509Certificates import (  # type: ignore
        CertificateRequest,
        RSACertificateExtensions,
        X500DistinguishedName,
        X509Certificate2,
        X509KeyStorageFlags,
    )
    from System.Net.Http import HttpClient, HttpClientHandler  # type: ignore

    return {
        "Array": Array,
        "Byte": Byte,
        "BitConverter": BitConverter,
        "Convert": Convert,
        "Enum": Enum,
        "AsnEncodedData": AsnEncodedData,
        "CngAlgorithm": CngAlgorithm,
        "CngExportPolicies": CngExportPolicies,
        "CngKey": CngKey,
        "CngKeyCreationOptions": CngKeyCreationOptions,
        "CngKeyCreationParameters": CngKeyCreationParameters,
        "CngKeyUsages": CngKeyUsages,
        "CngProperty": CngProperty,
        "CngPropertyOptions": CngPropertyOptions,
        "CngProvider": CngProvider,
        "HashAlgorithmName": HashAlgorithmName,
        "RSACng": RSACng,
        "RSASignaturePadding": RSASignaturePadding,
        "CertificateRequest": CertificateRequest,
        "RSACertificateExtensions": RSACertificateExtensions,
        "X500DistinguishedName": X500DistinguishedName,
        "X509Certificate2": X509Certificate2,
        "X509KeyStorageFlags": X509KeyStorageFlags,
        "HttpClient": HttpClient,
        "HttpClientHandler": HttpClientHandler,
    }


def _create_keyguard_rsa(dotnet) -> Any:
    """
    Creates RSACng with KeyGuard isolation. Fixes common pythonnet pitfalls:
      - "Length" must be DWORD (4 bytes), not Int64 (8 bytes)
      - enum member named "None" must be accessed via getattr()
    """
    from .managed_identity import MsiV2Error

    Array = dotnet["Array"]
    Byte = dotnet["Byte"]
    CngKeyCreationParameters = dotnet["CngKeyCreationParameters"]
    CngProvider = dotnet["CngProvider"]
    CngKeyUsages = dotnet["CngKeyUsages"]
    CngExportPolicies = dotnet["CngExportPolicies"]
    CngKeyCreationOptions = dotnet["CngKeyCreationOptions"]
    CngProperty = dotnet["CngProperty"]
    CngPropertyOptions = dotnet["CngPropertyOptions"]
    CngKey = dotnet["CngKey"]
    CngAlgorithm = dotnet["CngAlgorithm"]
    RSACng = dotnet["RSACng"]
    Enum = dotnet["Enum"]

    p = CngKeyCreationParameters()
    p.Provider = CngProvider("Microsoft Software Key Storage Provider")
    p.KeyUsage = CngKeyUsages.AllUsages
    p.ExportPolicy = getattr(CngExportPolicies, "None")

    # Add KeyGuard flags
    virt = Enum.ToObject(CngKeyCreationOptions, _NCRYPT_USE_VIRTUAL_ISOLATION_FLAG)
    perboot = Enum.ToObject(CngKeyCreationOptions, _NCRYPT_USE_PER_BOOT_KEY_FLAG)
    p.KeyCreationOptions = CngKeyCreationOptions.OverwriteExistingKey | virt | perboot

    # Length must be DWORD (4 bytes LE)
    length_dword = int(_RSA_KEY_SIZE).to_bytes(4, byteorder="little", signed=False)
    length_arr = Array[Byte](length_dword)
    p.Parameters.Add(CngProperty("Length", length_arr, getattr(CngPropertyOptions, "None")))

    # unique key name avoids collisions
    key_name = "MsalMsiV2Key_" + _new_correlation_id()

    try:
        cng_key = CngKey.Create(CngAlgorithm.Rsa, key_name, p)
    except Exception as exc:
        raise MsiV2Error("[msi_v2] Failed to create KeyGuard CNG key (CngKey.Create).") from exc

    # Validate Virtual Iso property is present
    try:
        vi = cng_key.GetProperty("Virtual Iso", getattr(CngPropertyOptions, "None")).GetValue()
        if vi is None or len(vi) < 4:
            raise MsiV2Error("[msi_v2] Virtual Iso property missing/invalid; Credential Guard likely not active.")
    except Exception as exc:
        raise MsiV2Error("[msi_v2] Virtual Iso property not available; Credential Guard likely not active.") from exc

    return RSACng(cng_key)


def _safehandle_to_intptr(rsa_cng: Any) -> int:
    """
    Extract NCRYPT_KEY_HANDLE as int from RSACng.Key.Handle (SafeHandle).
    """
    h = rsa_cng.Key.Handle
    ip = h.DangerousGetHandle()
    return int(ip.ToInt64())


def _build_csr_b64(dotnet, rsa_cng: Any, client_id: str, tenant_id: str, cu_id: Any) -> str:
    """
    CSR = CertificateRequest.CreateSigningRequest() signed by RSACng with RSA-PSS SHA256.
    Adds CSR request attribute OID 1.3.6.1.4.1.311.90.2.10 with DER UTF8String(JSON(cuId)).
    """
    Array = dotnet["Array"]
    Byte = dotnet["Byte"]
    CertificateRequest = dotnet["CertificateRequest"]
    X500DistinguishedName = dotnet["X500DistinguishedName"]
    HashAlgorithmName = dotnet["HashAlgorithmName"]
    RSASignaturePadding = dotnet["RSASignaturePadding"]
    AsnEncodedData = dotnet["AsnEncodedData"]
    Convert = dotnet["Convert"]

    subject = X500DistinguishedName(f"CN={client_id}, DC={tenant_id}")
    req = CertificateRequest(subject, rsa_cng, HashAlgorithmName.SHA256, RSASignaturePadding.Pss)

    cuid_json = json.dumps(cu_id, separators=(",", ":"), ensure_ascii=False)
    # prefer raw DER UTF8String (matches PS)
    der = _der_utf8string(cuid_json)
    der_arr = Array[Byte](der)

    asn = AsnEncodedData(_CU_ID_OID_STR, der_arr)
    req.OtherRequestAttributes.Add(asn)

    csr_der = req.CreateSigningRequest()
    return Convert.ToBase64String(csr_der)


def _attach_private_key(dotnet, cert_der: bytes, rsa_cng: Any) -> Any:
    Array = dotnet["Array"]
    Byte = dotnet["Byte"]
    X509Certificate2 = dotnet["X509Certificate2"]
    X509KeyStorageFlags = dotnet["X509KeyStorageFlags"]
    RSACertificateExtensions = dotnet["RSACertificateExtensions"]

    cert_bytes = Array[Byte](cert_der)
    cert_public = X509Certificate2(cert_bytes, None, X509KeyStorageFlags.DefaultKeySet)
    return RSACertificateExtensions.CopyWithPrivateKey(cert_public, rsa_cng)


def _imds_get_json(http_client, url: str, params: Dict[str, str], headers: Dict[str, str]) -> Dict[str, Any]:
    from .managed_identity import MsiV2Error
    resp = http_client.get(url, params=params, headers=headers)
    server = (resp.headers or {}).get("server", "")
    if "imds" not in str(server).lower():
        raise MsiV2Error(f"[msi_v2] IMDS server header check failed. server={server!r} url={url}")
    if resp.status_code != 200:
        raise MsiV2Error(f"[msi_v2] IMDSv2 GET {url} failed: HTTP {resp.status_code}: {resp.text}")
    return _json_loads(resp.text, f"GET {url}")


def _imds_post_json(http_client, url: str, params: Dict[str, str], headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
    from .managed_identity import MsiV2Error
    resp = http_client.post(url, params=params, headers=headers, data=json.dumps(body, separators=(",", ":")))
    server = (resp.headers or {}).get("server", "")
    if "imds" not in str(server).lower():
        raise MsiV2Error(f"[msi_v2] IMDS server header check failed. server={server!r} url={url}")
    if resp.status_code != 200:
        raise MsiV2Error(f"[msi_v2] IMDSv2 POST {url} failed: HTTP {resp.status_code}: {resp.text}")
    return _json_loads(resp.text, f"POST {url}")


def _token_endpoint_from_credential(cred: Dict[str, Any]) -> str:
    token_endpoint = _get_first(cred, "token_endpoint", "tokenEndpoint")
    if token_endpoint:
        return token_endpoint

    mtls_auth = _get_first(cred, "mtls_authentication_endpoint", "mtlsAuthenticationEndpoint", "mtls_authenticationEndpoint")
    tenant_id = _get_first(cred, "tenant_id", "tenantId")
    if not mtls_auth or not tenant_id:
        from .managed_identity import MsiV2Error
        raise MsiV2Error(f"[msi_v2] issuecredential missing mtls_authentication_endpoint/tenant_id: {cred}")

    base = mtls_auth.rstrip("/") + "/" + tenant_id.strip("/")
    return base + _ACQUIRE_ENTRA_TOKEN_PATH


def _acquire_token_mtls_dotnet(dotnet, token_endpoint: str, cert_with_key: Any, client_id: str, scope: str) -> Dict[str, Any]:
    from .managed_identity import MsiV2Error

    HttpClientHandler = dotnet["HttpClientHandler"]
    HttpClient = dotnet["HttpClient"]

    handler = HttpClientHandler()
    handler.ClientCertificates.Add(cert_with_key)
    client = HttpClient(handler)
    try:
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "scope": scope,
            "token_type": "mtls_pop",
        })
        # Create StringContent via pythonnet
        import clr  # type: ignore
        clr.AddReference("System.Net.Http")
        from System.Net.Http import StringContent  # type: ignore
        from System.Text import Encoding  # type: ignore

        content = StringContent(form, Encoding.UTF8, "application/x-www-form-urlencoded")
        resp = client.PostAsync(token_endpoint, content).GetAwaiter().GetResult()
        text = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if not resp.IsSuccessStatusCode:
            raise MsiV2Error(f"[msi_v2] ESTS token request failed: HTTP {int(resp.StatusCode)} {resp.ReasonPhrase} Body={text!r}")
        return _json_loads(text, "ESTS token")
    finally:
        client.Dispose()
        handler.Dispose()


def obtain_token(
    http_client,
    managed_identity: Dict[str, Any],
    resource: str,
    *,
    attestation_enabled: bool = True,
) -> Dict[str, Any]:
    """
    Acquire mtls_pop token using Windows KeyGuard + attestation.
    """
    from .managed_identity import MsiV2Error

    dotnet = _dotnet_imports()

    base = _imds_base()
    params = _mi_query_params(managed_identity)
    corr = _new_correlation_id()

    # 1) metadata
    meta_url = base + _CSR_METADATA_PATH
    meta = _imds_get_json(http_client, meta_url, params, _imds_headers(corr))

    client_id = _get_first(meta, "clientId", "client_id")
    tenant_id = _get_first(meta, "tenantId", "tenant_id")
    cu_id = meta.get("cuId") if "cuId" in meta else meta.get("cu_id")
    attestation_endpoint = _get_first(meta, "attestationEndpoint", "attestation_endpoint")

    if not client_id or not tenant_id or cu_id is None:
        raise MsiV2Error(f"[msi_v2] getplatformmetadata missing required fields: {meta}")

    # 2) KeyGuard RSA
    rsa_cng = _create_keyguard_rsa(dotnet)

    # 3) CSR
    csr_b64 = _build_csr_b64(dotnet, rsa_cng, client_id, tenant_id, cu_id)

    # 4) Attestation (required in your environment)
    if not attestation_enabled:
        raise MsiV2Error("[msi_v2] attestation_enabled must be True for this KeyGuard flow.")
    if not attestation_endpoint:
        raise MsiV2Error("[msi_v2] attestationEndpoint missing from metadata.")

    key_handle = _safehandle_to_intptr(rsa_cng)
    from .msi_v2_attestation import get_attestation_jwt
    att_jwt = get_attestation_jwt(
        attestation_endpoint=str(attestation_endpoint),
        client_id=str(client_id),
        key_handle=key_handle,
    )
    if not att_jwt or not str(att_jwt).strip():
        raise MsiV2Error("[msi_v2] Attestation token is missing/empty; refusing to call issuecredential.")

    # 5) issuecredential
    issue_url = base + _ISSUE_CREDENTIAL_PATH
    issue_headers = _imds_headers(corr)
    issue_headers["Content-Type"] = "application/json"

    body = {"csr": csr_b64, "attestation_token": att_jwt}
    cred = _imds_post_json(http_client, issue_url, params, issue_headers, body)

    cert_b64 = _get_first(cred, "certificate", "Certificate")
    if not cert_b64:
        raise MsiV2Error(f"[msi_v2] issuecredential missing certificate: {cred}")

    try:
        cert_der = base64.b64decode(cert_b64)
    except Exception as exc:
        raise MsiV2Error("[msi_v2] issuecredential returned invalid base64 certificate") from exc

    canonical_client_id = _get_first(cred, "client_id", "clientId") or str(client_id)
    token_endpoint = _token_endpoint_from_credential(cred)

    # 6) Attach KeyGuard key to cert and call ESTS over mTLS using SChannel
    cert_with_key = _attach_private_key(dotnet, cert_der, rsa_cng)
    scope = _resource_to_scope(resource)

    token_json = _acquire_token_mtls_dotnet(dotnet, token_endpoint, cert_with_key, canonical_client_id, scope)

    if token_json.get("access_token") and token_json.get("expires_in"):
        return {
            "access_token": token_json["access_token"],
            "expires_in": int(token_json["expires_in"]),
            "token_type": token_json.get("token_type") or "mtls_pop",
            "resource": token_json.get("resource"),
        }

    return token_json