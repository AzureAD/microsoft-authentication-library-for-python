# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
MSI v2 (IMDSv2) Managed Identity flow — Windows KeyGuard + Attestation + SChannel mTLS PoP.

This module implements the end-to-end "MSI v2" flow used by Azure Managed Identity on Windows
when *certificate-bound* access tokens are requested (token_type=mtls_pop).

It is intentionally "Python-only": no pythonnet/.NET interop is required. Instead, it uses
ctypes to call a small set of Windows APIs:

  * CNG/NCrypt (ncrypt.dll)    - Create a KeyGuard/VBS isolated RSA key + sign CSR (RSA-PSS/SHA256)
  * Crypt32 (crypt32.dll)      - Bind the issued certificate to the CNG private key
  * WinHTTP (winhttp.dll)      - Perform the token request over mTLS using SChannel

Flow summary (mirrors your working PowerShell implementation):

  1) GET  /metadata/identity/getplatformmetadata?cred-api-version=2.0
  2) Create KeyGuard RSA key (non-exportable, VBS-isolated)
  3) Build CSR signed with RSA-PSS/SHA256 and include a special CSR attribute:
        OID 1.3.6.1.4.1.311.90.2.10  ->  DER UTF8String(JSON(cuId))
  4) Get an attestation JWT for the key (via .msi_v2_attestation.get_attestation_jwt)
  5) POST /metadata/identity/issuecredential?cred-api-version=2.0 with csr + attestation_token
  6) POST {tenant}/oauth2/v2.0/token (mTLS) with the issued certificate and token_type=mtls_pop

Important design choices:

  * Windows-only: importing on non-Windows platforms is supported, but calling obtain_token()
    will raise MsiV2Error.
  * No MSI v1 fallback: any failure raises MsiV2Error.
  * Defensive certificate-to-key binding: we set multiple certificate context properties so
    WinHTTP/SChannel can consistently locate the private key.

Security notes:

  * Access tokens are secrets. Avoid logging or printing them in production.
  * The KeyGuard RSA key is created as persisted, but is deleted in cleanup.

Public entrypoint: obtain_token(http_client, managed_identity, resource, attestation_enabled=True)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "get_cert_thumbprint_sha256",
    "verify_cnf_binding",
    "obtain_token",
]

# --------------------------------------------------------------------------------------
# IMDS / MSI v2 constants
# --------------------------------------------------------------------------------------

_IMDS_DEFAULT_BASE = "http://169.254.169.254"
_IMDS_BASE_ENVVAR = "AZURE_POD_IDENTITY_AUTHORITY_HOST"

_API_VERSION_QUERY_PARAM = "cred-api-version"
_IMDS_V2_API_VERSION = "2.0"

_CSR_METADATA_PATH = "/metadata/identity/getplatformmetadata"
_ISSUE_CREDENTIAL_PATH = "/metadata/identity/issuecredential"
_ACQUIRE_ENTRA_TOKEN_PATH = "/oauth2/v2.0/token"

# OID for the special CSR request attribute carrying cuId JSON.
_CU_ID_OID_STR = "1.3.6.1.4.1.311.90.2.10"

# Flags from ncrypt.h used by the PowerShell reference implementation.
_NCRYPT_USE_VIRTUAL_ISOLATION_FLAG = 0x00020000
_NCRYPT_USE_PER_BOOT_KEY_FLAG = 0x00040000

_RSA_KEY_SIZE = 2048

# Legacy KeySpec values (CAPI compatibility / CNG interop).
# Used by NCryptCreatePersistedKey.dwLegacyKeySpec and by CRYPT_KEY_PROV_INFO.dwKeySpec.
_AT_KEYEXCHANGE = 1
_AT_SIGNATURE = 2

# CRYPT_KEY_PROV_INFO.dwFlags for CNG keys (best-effort suppression of UI prompts).
_NCRYPT_SILENT_FLAG = 0x40

_DEFAULT_KSP_NAME = "Microsoft Software Key Storage Provider"

# --------------------------------------------------------------------------------------
# Compatibility helpers (optional; useful for tests or debugging)
# --------------------------------------------------------------------------------------


def get_cert_thumbprint_sha256(cert_pem: str) -> str:
    """
    Compute base64url(SHA256(der(cert))) without padding, for cnf.x5t#S256 comparisons.

    Accepts a PEM-encoded certificate string.

    Returns:
        Base64url-encoded SHA-256 thumbprint without '=' padding, or "" if cryptography is
        unavailable or parsing fails.
    """
    try:
        # cryptography is optional; keep this helper lightweight.
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"), default_backend())
        der = cert.public_bytes(serialization.Encoding.DER)
        digest = hashlib.sha256(der).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    except Exception:
        # Fail closed: if we cannot compute the thumbprint, binding verification cannot succeed.
        return ""


def verify_cnf_binding(token: str, cert_pem: str) -> bool:
    """
    Verify that a JWT payload contains cnf.x5t#S256 matching the cert thumbprint.

    This is a *best-effort* helper for validating certificate binding in tests. It does not
    validate JWT signature or claims (aud/iss/exp/etc).

    Args:
        token: A JWT access token (3-part base64url string).
        cert_pem: PEM certificate string.

    Returns:
        True if cnf.x5t#S256 exists and equals the SHA-256 certificate thumbprint.
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


# --------------------------------------------------------------------------------------
# IMDS helpers
# --------------------------------------------------------------------------------------


def _imds_base() -> str:
    """Resolve IMDS base URI (supports pod identity override via env var)."""
    return os.getenv(_IMDS_BASE_ENVVAR, _IMDS_DEFAULT_BASE).strip().rstrip("/")


def _new_correlation_id() -> str:
    """Generate an RFC 4122 correlation id used in x-ms-client-request-id."""
    return str(uuid.uuid4())


def _imds_headers(correlation_id: Optional[str] = None) -> Dict[str, str]:
    """
    Headers required by IMDS. The Metadata=true header is mandatory.

    We also include x-ms-client-request-id to correlate IMDS and ESTS requests.
    """
    return {
        "Metadata": "true",
        "x-ms-client-request-id": correlation_id or _new_correlation_id(),
    }


def _resource_to_scope(resource_or_scope: str) -> str:
    """
    Convert an ADAL-style 'resource' string into an MSAL v2 scope string.

    IMDS v2 uses MSAL v2 token endpoint semantics (scope=.../.default).
    """
    s = (resource_or_scope or "").strip()
    if not s:
        raise ValueError("resource must be non-empty")
    if s.endswith("/.default"):
        return s
    return s.rstrip("/") + "/.default"


def _der_utf8string(value: str) -> bytes:
    """
    Minimal DER UTF8String encoder (tag 0x0C).

    Used for the CSR request attribute value (cuId JSON) and for X.500 CN when applicable.
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
    """Parse JSON or raise MsiV2Error with context."""
    from .managed_identity import MsiV2Error

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise MsiV2Error(f"[msi_v2] Expected JSON object from {what}, got {type(data).__name__}")
    except Exception as exc:  # pylint: disable=broad-except
        raise MsiV2Error(f"[msi_v2] Invalid JSON from {what}: {text!r}") from exc


def _get_first(obj: Dict[str, Any], *names: str) -> Optional[str]:
    """
    Fetch the first non-empty string value among several possible keys.

    IMDS field casing can vary (camelCase vs snake_case), so this helper checks:
      1) Exact keys
      2) Case-insensitive matches
    """
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
    Build IMDS query parameters:
      * cred-api-version=2.0 (required)
      * optional user-assigned identity selectors

    managed_identity shape (MSAL Python):
        {"ManagedIdentityIdType": "ClientId"|"ObjectId"|"ResourceId", "Id": "<value>"}
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


def _imds_get_json(http_client, url: str, params: Dict[str, str], headers: Dict[str, str]) -> Dict[str, Any]:
    """
    GET JSON from IMDS with a basic 'Server' header sanity check.

    Note: The "server: IMDS/..." header check is a defense-in-depth measure to reduce the
    chance of SSRF misuse. Keep it strict unless you have a concrete reason to loosen it.
    """
    from .managed_identity import MsiV2Error

    resp = http_client.get(url, params=params, headers=headers)

    server = (resp.headers or {}).get("server", "")
    if "imds" not in str(server).lower():
        raise MsiV2Error(f"[msi_v2] IMDS server header check failed. server={server!r} url={url}")

    if resp.status_code != 200:
        raise MsiV2Error(f"[msi_v2] IMDSv2 GET {url} failed: HTTP {resp.status_code}: {resp.text}")

    return _json_loads(resp.text, f"GET {url}")


def _imds_post_json(
    http_client, url: str, params: Dict[str, str], headers: Dict[str, str], body: Dict[str, Any]
) -> Dict[str, Any]:
    """POST JSON to IMDS and return JSON response (with same header sanity check)."""
    from .managed_identity import MsiV2Error

    resp = http_client.post(url, params=params, headers=headers, data=json.dumps(body, separators=(",", ":")))

    server = (resp.headers or {}).get("server", "")
    if "imds" not in str(server).lower():
        raise MsiV2Error(f"[msi_v2] IMDS server header check failed. server={server!r} url={url}")

    if resp.status_code != 200:
        raise MsiV2Error(f"[msi_v2] IMDSv2 POST {url} failed: HTTP {resp.status_code}: {resp.text}")

    return _json_loads(resp.text, f"POST {url}")


def _token_endpoint_from_credential(cred: Dict[str, Any]) -> str:
    """
    Determine the token endpoint returned by /issuecredential.

    IMDS can return either:
      * token_endpoint
      * mtls_authentication_endpoint + tenant_id (compose into {mtls_auth}/{tenant}/oauth2/v2.0/token)
    """
    token_endpoint = _get_first(cred, "token_endpoint", "tokenEndpoint")
    if token_endpoint:
        return token_endpoint

    mtls_auth = _get_first(
        cred,
        "mtls_authentication_endpoint",
        "mtlsAuthenticationEndpoint",
        "mtls_authenticationEndpoint",
    )
    tenant_id = _get_first(cred, "tenant_id", "tenantId")
    if not mtls_auth or not tenant_id:
        from .managed_identity import MsiV2Error

        raise MsiV2Error(f"[msi_v2] issuecredential missing mtls_authentication_endpoint/tenant_id: {cred}")

    base = mtls_auth.rstrip("/") + "/" + tenant_id.strip("/")
    return base + _ACQUIRE_ENTRA_TOKEN_PATH


# --------------------------------------------------------------------------------------
# Win32 primitives (ctypes) - lazy loaded
# --------------------------------------------------------------------------------------

_WIN32: Optional[Dict[str, Any]] = None


def _load_win32() -> Dict[str, Any]:
    """
    Lazy-load Win32 APIs via ctypes.

    Keeping the import behind a function allows importing this module on non-Windows platforms
    without failing at import time. The public obtain_token() function enforces Windows-only.
    """
    global _WIN32

    from .managed_identity import MsiV2Error

    if _WIN32 is not None:
        return _WIN32

    if sys.platform != "win32":
        raise MsiV2Error("[msi_v2] KeyGuard + attested mTLS PoP is Windows-only.")

    import ctypes
    from ctypes import wintypes

    # DLLs (use_last_error makes ctypes.get_last_error() reliable for BOOL-returning APIs)
    ncrypt = ctypes.WinDLL("ncrypt.dll")
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    winhttp = ctypes.WinDLL("winhttp.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    # --- Types ---
    NCRYPT_PROV_HANDLE = ctypes.c_void_p
    NCRYPT_KEY_HANDLE = ctypes.c_void_p
    SECURITY_STATUS = ctypes.c_long  # LONG / NTSTATUS style

    # Crypt32 certificate context
    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", ctypes.c_void_p),
            ("hCertStore", ctypes.c_void_p),
        ]

    PCCERT_CONTEXT = ctypes.POINTER(CERT_CONTEXT)

    # Padding info for NCryptSignHash (RSA-PSS)
    class BCRYPT_PSS_PADDING_INFO(ctypes.Structure):
        _fields_ = [
            ("pszAlgId", ctypes.c_wchar_p),
            ("cbSalt", wintypes.ULONG),
        ]

    # --- Constants (subset) ---
    ERROR_SUCCESS = 0

    # ncrypt.h flags
    NCRYPT_OVERWRITE_KEY_FLAG = 0x00000080

    # key properties (ncrypt.h)
    NCRYPT_LENGTH_PROPERTY = "Length"
    NCRYPT_EXPORT_POLICY_PROPERTY = "Export Policy"
    NCRYPT_KEY_USAGE_PROPERTY = "Key Usage"

    # key usage flags (ncrypt.h)
    NCRYPT_ALLOW_SIGNING_FLAG = 0x00000002
    NCRYPT_ALLOW_DECRYPT_FLAG = 0x00000001

    # bcrypt.h / padding
    BCRYPT_PAD_PSS = 0x00000008
    BCRYPT_SHA256_ALGORITHM = "SHA256"
    BCRYPT_RSA_ALGORITHM = "RSA"
    BCRYPT_RSAPUBLIC_BLOB = "RSAPUBLICBLOB"
    BCRYPT_RSAPUBLIC_MAGIC = 0x31415352  # 'RSA1'

    # wincrypt.h
    X509_ASN_ENCODING = 0x00000001
    PKCS_7_ASN_ENCODING = 0x00010000
    CERT_NCRYPT_KEY_HANDLE_PROP_ID = 78
    CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG = 0x40000000

    # WinHTTP constants
    WINHTTP_ACCESS_TYPE_DEFAULT_PROXY = 0
    WINHTTP_FLAG_SECURE = 0x00800000
    WINHTTP_OPTION_CLIENT_CERT_CONTEXT = 47
    WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT = 161
    WINHTTP_QUERY_STATUS_CODE = 19
    WINHTTP_QUERY_FLAG_NUMBER = 0x20000000

    # --- Function prototypes (argtypes/restype) ---
    # NCrypt
    ncrypt.NCryptOpenStorageProvider.argtypes = [ctypes.POINTER(NCRYPT_PROV_HANDLE), ctypes.c_wchar_p, wintypes.DWORD]
    ncrypt.NCryptOpenStorageProvider.restype = SECURITY_STATUS

    ncrypt.NCryptCreatePersistedKey.argtypes = [
        NCRYPT_PROV_HANDLE,
        ctypes.POINTER(NCRYPT_KEY_HANDLE),
        ctypes.c_wchar_p,  # alg id
        ctypes.c_wchar_p,  # key name
        wintypes.DWORD,  # legacy keyspec
        wintypes.DWORD,  # flags
    ]
    ncrypt.NCryptCreatePersistedKey.restype = SECURITY_STATUS

    ncrypt.NCryptSetProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    ncrypt.NCryptSetProperty.restype = SECURITY_STATUS

    ncrypt.NCryptFinalizeKey.argtypes = [NCRYPT_KEY_HANDLE, wintypes.DWORD]
    ncrypt.NCryptFinalizeKey.restype = SECURITY_STATUS

    ncrypt.NCryptGetProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    ncrypt.NCryptGetProperty.restype = SECURITY_STATUS

    ncrypt.NCryptExportKey.argtypes = [
        NCRYPT_KEY_HANDLE,
        NCRYPT_KEY_HANDLE,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    ncrypt.NCryptExportKey.restype = SECURITY_STATUS

    ncrypt.NCryptSignHash.argtypes = [
        NCRYPT_KEY_HANDLE,
        ctypes.c_void_p,  # padding info
        ctypes.c_void_p,  # hash bytes
        wintypes.DWORD,  # hash len
        ctypes.c_void_p,  # sig out
        wintypes.DWORD,  # sig out len
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,  # flags
    ]
    ncrypt.NCryptSignHash.restype = SECURITY_STATUS

    ncrypt.NCryptDeleteKey.argtypes = [NCRYPT_KEY_HANDLE, wintypes.DWORD]
    ncrypt.NCryptDeleteKey.restype = SECURITY_STATUS

    ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]
    ncrypt.NCryptFreeObject.restype = SECURITY_STATUS

    # Crypt32
    crypt32.CertCreateCertificateContext.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    crypt32.CertCreateCertificateContext.restype = PCCERT_CONTEXT

    crypt32.CertSetCertificateContextProperty.argtypes = [PCCERT_CONTEXT, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    crypt32.CertSetCertificateContextProperty.restype = wintypes.BOOL

    crypt32.CertFreeCertificateContext.argtypes = [PCCERT_CONTEXT]
    crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

    # WinHTTP
    winhttp.WinHttpOpen.argtypes = [
        ctypes.c_wchar_p,
        wintypes.DWORD,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        wintypes.DWORD,
    ]
    winhttp.WinHttpOpen.restype = ctypes.c_void_p

    winhttp.WinHttpConnect.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, wintypes.WORD, wintypes.DWORD]
    winhttp.WinHttpConnect.restype = ctypes.c_void_p

    winhttp.WinHttpOpenRequest.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    winhttp.WinHttpOpenRequest.restype = ctypes.c_void_p

    winhttp.WinHttpSetOption.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    winhttp.WinHttpSetOption.restype = wintypes.BOOL

    winhttp.WinHttpSendRequest.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_ulonglong,  # context
    ]
    winhttp.WinHttpSendRequest.restype = wintypes.BOOL

    winhttp.WinHttpReceiveResponse.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    winhttp.WinHttpReceiveResponse.restype = wintypes.BOOL

    winhttp.WinHttpQueryHeaders.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    winhttp.WinHttpQueryHeaders.restype = wintypes.BOOL

    winhttp.WinHttpQueryDataAvailable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    winhttp.WinHttpQueryDataAvailable.restype = wintypes.BOOL

    winhttp.WinHttpReadData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    winhttp.WinHttpReadData.restype = wintypes.BOOL

    winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
    winhttp.WinHttpCloseHandle.restype = wintypes.BOOL

    # Kernel32
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD

    _WIN32 = {
        "ctypes": ctypes,
        "wintypes": wintypes,
        "ncrypt": ncrypt,
        "crypt32": crypt32,
        "winhttp": winhttp,
        "kernel32": kernel32,
        # types
        "NCRYPT_PROV_HANDLE": NCRYPT_PROV_HANDLE,
        "NCRYPT_KEY_HANDLE": NCRYPT_KEY_HANDLE,
        "SECURITY_STATUS": SECURITY_STATUS,
        "CERT_CONTEXT": CERT_CONTEXT,
        "PCCERT_CONTEXT": PCCERT_CONTEXT,
        "BCRYPT_PSS_PADDING_INFO": BCRYPT_PSS_PADDING_INFO,
        # constants
        "ERROR_SUCCESS": ERROR_SUCCESS,
        "NCRYPT_OVERWRITE_KEY_FLAG": NCRYPT_OVERWRITE_KEY_FLAG,
        "NCRYPT_LENGTH_PROPERTY": NCRYPT_LENGTH_PROPERTY,
        "NCRYPT_EXPORT_POLICY_PROPERTY": NCRYPT_EXPORT_POLICY_PROPERTY,
        "NCRYPT_KEY_USAGE_PROPERTY": NCRYPT_KEY_USAGE_PROPERTY,
        "NCRYPT_ALLOW_SIGNING_FLAG": NCRYPT_ALLOW_SIGNING_FLAG,
        "NCRYPT_ALLOW_DECRYPT_FLAG": NCRYPT_ALLOW_DECRYPT_FLAG,
        "BCRYPT_PAD_PSS": BCRYPT_PAD_PSS,
        "BCRYPT_SHA256_ALGORITHM": BCRYPT_SHA256_ALGORITHM,
        "BCRYPT_RSA_ALGORITHM": BCRYPT_RSA_ALGORITHM,
        "BCRYPT_RSAPUBLIC_BLOB": BCRYPT_RSAPUBLIC_BLOB,
        "BCRYPT_RSAPUBLIC_MAGIC": BCRYPT_RSAPUBLIC_MAGIC,
        "X509_ASN_ENCODING": X509_ASN_ENCODING,
        "PKCS_7_ASN_ENCODING": PKCS_7_ASN_ENCODING,
        "CERT_NCRYPT_KEY_HANDLE_PROP_ID": CERT_NCRYPT_KEY_HANDLE_PROP_ID,
        "CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG": CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG,
        "WINHTTP_ACCESS_TYPE_DEFAULT_PROXY": WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        "WINHTTP_FLAG_SECURE": WINHTTP_FLAG_SECURE,
        "WINHTTP_OPTION_CLIENT_CERT_CONTEXT": WINHTTP_OPTION_CLIENT_CERT_CONTEXT,
        "WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT": WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT,
        "WINHTTP_QUERY_STATUS_CODE": WINHTTP_QUERY_STATUS_CODE,
        "WINHTTP_QUERY_FLAG_NUMBER": WINHTTP_QUERY_FLAG_NUMBER,
    }
    return _WIN32


def _format_win32_error(ctypes_mod, code: int) -> str:
    """Format a Win32 error code into a human-readable string (best-effort)."""
    try:
        return ctypes_mod.FormatError(code).strip()
    except Exception:
        return ""


def _raise_win32_last_error(msg: str) -> None:
    """
    Raise MsiV2Error with the current Win32 last-error code.

    Use for WinHTTP/Crypt32 APIs where failure is indicated via BOOL/NULL and details are in GetLastError().
    """
    from .managed_identity import MsiV2Error

    win32 = _load_win32()
    ctypes_mod = win32["ctypes"]
    err = ctypes_mod.get_last_error()
    detail = _format_win32_error(ctypes_mod, err)
    if detail:
        raise MsiV2Error(f"{msg} (winerror={err} {detail})")
    raise MsiV2Error(f"{msg} (winerror={err})")


def _check_security_status(status: int, what: str) -> None:
    """
    Check SECURITY_STATUS/NTSTATUS-style return codes from NCrypt.

    Most NCrypt APIs return 0 for success; otherwise they return a status code (often an NTSTATUS).
    """
    from .managed_identity import MsiV2Error

    if int(status) != 0:
        code_u32 = int(status) & 0xFFFFFFFF
        raise MsiV2Error(f"[msi_v2] {what} failed: status=0x{code_u32:08X}")


# --------------------------------------------------------------------------------------
# DER helpers (minimal PKCS#10 CSR builder)
# --------------------------------------------------------------------------------------

# This is a minimal DER encoder sufficient for:
#   * PKCS#10 CertificationRequestInfo (subject, spki, attributes)
#   * RSASSA-PSS AlgorithmIdentifier params
# It intentionally avoids general ASN.1 frameworks to keep dependencies low.


def _der_len(n: int) -> bytes:
    if n < 0:
        raise ValueError("DER length cannot be negative")
    if n < 0x80:
        return bytes([n])
    out = bytearray()
    m = n
    while m > 0:
        out.insert(0, m & 0xFF)
        m >>= 8
    return bytes([0x80 | len(out)]) + bytes(out)


def _der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _der_null() -> bytes:
    return b"\x05\x00"


def _der_integer(value: int) -> bytes:
    if value < 0:
        raise ValueError("Only non-negative INTEGER supported")
    if value == 0:
        raw = b"\x00"
    else:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if raw[0] & 0x80:
            raw = b"\x00" + raw
    return _der(0x02, raw)


def _der_oid(oid: str) -> bytes:
    parts = [int(x) for x in oid.split(".")]
    if len(parts) < 2:
        raise ValueError(f"Invalid OID: {oid}")
    if parts[0] > 2 or parts[1] >= 40:
        raise ValueError(f"Invalid OID: {oid}")
    first = 40 * parts[0] + parts[1]
    out = bytearray([first])
    for p in parts[2:]:
        if p < 0:
            raise ValueError(f"Invalid OID component: {oid}")
        # base-128 encoding
        stack = bytearray()
        if p == 0:
            stack.append(0)
        else:
            m = p
            while m > 0:
                stack.insert(0, m & 0x7F)
                m >>= 7
            for i in range(len(stack) - 1):
                stack[i] |= 0x80
        out.extend(stack)
    return _der(0x06, bytes(out))


def _der_sequence(*items: bytes) -> bytes:
    return _der(0x30, b"".join(items))


def _der_set(*items: bytes) -> bytes:
    # DER SET requires elements to be sorted by their full DER encoding.
    enc = sorted(items)
    return _der(0x31, b"".join(enc))


def _der_bitstring(data: bytes) -> bytes:
    # 0 unused bits
    return _der(0x03, b"\x00" + data)


def _der_ia5string(value: str) -> bytes:
    raw = value.encode("ascii")
    return _der(0x16, raw)


def _der_context_explicit(tagnum: int, inner: bytes) -> bytes:
    if not 0 <= tagnum <= 30:
        raise ValueError("Unsupported tag number")
    return _der(0xA0 + tagnum, inner)


def _der_context_implicit_constructed(tagnum: int, inner_content: bytes) -> bytes:
    """
    Context-specific IMPLICIT, constructed.

    Used for PKCS#10 attributes:
        attributes [0] IMPLICIT SET OF Attribute
    Where we encode the SET OF's contents without the SET tag (0x31).
    """
    if not 0 <= tagnum <= 30:
        raise ValueError("Unsupported tag number")
    return _der(0xA0 + tagnum, inner_content)


def _der_name_cn_dc(cn: str, dc: str) -> bytes:
    """
    Encode X.500 Name with CN and DC RDNs.

    CN  (2.5.4.3) is encoded as UTF8String.
    DC  (0.9.2342.19200300.100.1.25) is usually IA5String (ASCII), else UTF8String.
    """
    cn_atv = _der_sequence(_der_oid("2.5.4.3"), _der_utf8string(cn))
    cn_rdn = _der_set(cn_atv)

    try:
        dc_value = _der_ia5string(dc)
    except Exception:
        dc_value = _der_utf8string(dc)
    dc_atv = _der_sequence(_der_oid("0.9.2342.19200300.100.1.25"), dc_value)
    dc_rdn = _der_set(dc_atv)

    # RDNSequence is a SEQUENCE of SETs
    return _der_sequence(cn_rdn, dc_rdn)


def _der_subject_public_key_info_rsa(modulus: int, exponent: int) -> bytes:
    rsa_pub = _der_sequence(_der_integer(modulus), _der_integer(exponent))
    alg = _der_sequence(_der_oid("1.2.840.113549.1.1.1"), _der_null())  # rsaEncryption + NULL
    return _der_sequence(alg, _der_bitstring(rsa_pub))


def _der_algid_rsapss_sha256() -> bytes:
    """
    AlgorithmIdentifier for RSASSA-PSS with SHA-256, MGF1(SHA-256), saltLength=32, trailerField=1.

    This matches what .NET / PowerShell emits for the working flow.
    """
    sha256 = _der_sequence(_der_oid("2.16.840.1.101.3.4.2.1"), _der_null())
    mgf1 = _der_sequence(_der_oid("1.2.840.113549.1.1.8"), sha256)
    salt_len = _der_integer(32)
    trailer = _der_integer(1)

    params = _der_sequence(
        _der_context_explicit(0, sha256),
        _der_context_explicit(1, mgf1),
        _der_context_explicit(2, salt_len),
        _der_context_explicit(3, trailer),
    )
    return _der_sequence(_der_oid("1.2.840.113549.1.1.10"), params)


# --------------------------------------------------------------------------------------
# CNG/NCrypt wrappers
# --------------------------------------------------------------------------------------


def _ncrypt_get_property(win32: Dict[str, Any], handle: Any, name: str) -> bytes:
    """Get an NCrypt property value as raw bytes."""
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    cb = wintypes.DWORD(0)

    status = ncrypt.NCryptGetProperty(handle, name, None, 0, ctypes_mod.byref(cb), 0)
    if int(status) != 0 and cb.value == 0:
        _check_security_status(status, f"NCryptGetProperty({name})")

    if cb.value == 0:
        return b""

    buf = (ctypes_mod.c_ubyte * cb.value)()
    status = ncrypt.NCryptGetProperty(handle, name, buf, cb.value, ctypes_mod.byref(cb), 0)
    _check_security_status(status, f"NCryptGetProperty({name})")

    return bytes(buf[: cb.value])


def _create_keyguard_rsa_key(win32: Dict[str, Any]) -> Tuple[Any, Any, str]:
    """
    Create a non-exportable RSA key protected with VBS/KeyGuard.

    Returns:
        (prov_handle, key_handle, key_name)

    key_name is the persisted CNG key name (container name). WinHTTP/SChannel can require it to
    re-open the key when doing client-certificate authentication.
    """
    from .managed_identity import MsiV2Error

    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    prov = win32["NCRYPT_PROV_HANDLE"]()
    status = ncrypt.NCryptOpenStorageProvider(ctypes_mod.byref(prov), _DEFAULT_KSP_NAME, 0)
    _check_security_status(status, "NCryptOpenStorageProvider")

    key = win32["NCRYPT_KEY_HANDLE"]()
    key_name = "MsalMsiV2Key_" + _new_correlation_id()

    flags = win32["NCRYPT_OVERWRITE_KEY_FLAG"] | _NCRYPT_USE_VIRTUAL_ISOLATION_FLAG | _NCRYPT_USE_PER_BOOT_KEY_FLAG

    # IMPORTANT:
    #   When a certificate is bound to a CNG key via CERT_KEY_PROV_INFO_PROP_ID, Schannel/WinHTTP
    #   re-opens the key using NCryptOpenKey and passes CRYPT_KEY_PROV_INFO.dwKeySpec as the legacy
    #   keyspec parameter. The CRYPT_KEY_PROV_INFO docs specify that for CNG (dwProvType==0),
    #   dwKeySpec must be AT_SIGNATURE or AT_KEYEXCHANGE (not CERT_NCRYPT_KEY_SPEC).
    #
    # We therefore create the key with dwLegacyKeySpec=AT_SIGNATURE so re-open works reliably.
    status = ncrypt.NCryptCreatePersistedKey(
        prov,
        ctypes_mod.byref(key),
        win32["BCRYPT_RSA_ALGORITHM"],
        key_name,
        _AT_SIGNATURE,
        flags,
    )

    try:
        _check_security_status(status, "NCryptCreatePersistedKey")

        # Length must be DWORD.
        length = wintypes.DWORD(int(_RSA_KEY_SIZE))
        status = ncrypt.NCryptSetProperty(
            key,
            win32["NCRYPT_LENGTH_PROPERTY"],
            ctypes_mod.byref(length),
            ctypes_mod.sizeof(length),
            0,
        )
        _check_security_status(status, "NCryptSetProperty(Length)")

        # Key usage: signing is required; decrypt doesn't hurt for TLS use-cases.
        usage = wintypes.DWORD(win32["NCRYPT_ALLOW_SIGNING_FLAG"] | win32["NCRYPT_ALLOW_DECRYPT_FLAG"])
        status = ncrypt.NCryptSetProperty(
            key,
            win32["NCRYPT_KEY_USAGE_PROPERTY"],
            ctypes_mod.byref(usage),
            ctypes_mod.sizeof(usage),
            0,
        )
        _check_security_status(status, "NCryptSetProperty(Key Usage)")

        # Export policy: 0 (disallow export).
        export_policy = wintypes.DWORD(0)
        status = ncrypt.NCryptSetProperty(
            key,
            win32["NCRYPT_EXPORT_POLICY_PROPERTY"],
            ctypes_mod.byref(export_policy),
            ctypes_mod.sizeof(export_policy),
            0,
        )
        _check_security_status(status, "NCryptSetProperty(Export Policy)")

        status = ncrypt.NCryptFinalizeKey(key, 0)
        _check_security_status(status, "NCryptFinalizeKey")

        # Validate Virtual Iso property (Credential Guard / VBS). Helps fail fast if KeyGuard isn't active.
        try:
            vi = _ncrypt_get_property(win32, key, "Virtual Iso")
            if vi is None or len(vi) < 4:
                raise MsiV2Error("[msi_v2] Virtual Iso property missing/invalid; Credential Guard likely not active.")
        except Exception as exc:
            raise MsiV2Error("[msi_v2] Virtual Iso property not available; Credential Guard likely not active.") from exc

        return prov, key, key_name

    except Exception:
        # Best-effort cleanup. The caller also cleans up in obtain_token().
        try:
            if key:
                ncrypt.NCryptDeleteKey(key, 0)
        except Exception:
            pass
        try:
            if key:
                ncrypt.NCryptFreeObject(key)
        except Exception:
            pass
        try:
            if prov:
                ncrypt.NCryptFreeObject(prov)
        except Exception:
            pass
        raise


def _ncrypt_export_rsa_public(win32: Dict[str, Any], key: Any) -> Tuple[int, int]:
    """
    Export RSA public key (modulus, exponent) from an NCrypt key handle.

    We export as BCRYPT_RSAPUBLIC_BLOB and parse it.
    """
    from .managed_identity import MsiV2Error

    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    cb = wintypes.DWORD(0)
    status = ncrypt.NCryptExportKey(key, None, win32["BCRYPT_RSAPUBLIC_BLOB"], None, None, 0, ctypes_mod.byref(cb), 0)
    if int(status) != 0 and cb.value == 0:
        _check_security_status(status, "NCryptExportKey(size)")
    if cb.value == 0:
        raise MsiV2Error("[msi_v2] NCryptExportKey returned empty public blob size")

    buf = (ctypes_mod.c_ubyte * cb.value)()
    status = ncrypt.NCryptExportKey(key, None, win32["BCRYPT_RSAPUBLIC_BLOB"], None, buf, cb.value, ctypes_mod.byref(cb), 0)
    _check_security_status(status, "NCryptExportKey(RSAPUBLICBLOB)")
    blob = bytes(buf[: cb.value])

    # BCRYPT_RSAKEY_BLOB header is 6 DWORDs, little-endian.
    if len(blob) < 24:
        raise MsiV2Error("[msi_v2] RSAPUBLICBLOB too small")

    import struct

    magic, bitlen, cb_exp, cb_mod, cb_p1, cb_p2 = struct.unpack("<6I", blob[:24])
    if magic != win32["BCRYPT_RSAPUBLIC_MAGIC"]:
        raise MsiV2Error(f"[msi_v2] RSAPUBLICBLOB magic mismatch: 0x{magic:08X}")

    if cb_p1 != 0 or cb_p2 != 0:
        # Public blob should have primes=0; ignore if present (defensive).
        logger.debug("[msi_v2] RSAPUBLICBLOB contains primes unexpectedly (ignored).")

    offset = 24
    if len(blob) < offset + cb_exp + cb_mod:
        raise MsiV2Error("[msi_v2] RSAPUBLICBLOB truncated")

    exp_bytes = blob[offset : offset + cb_exp]
    offset += cb_exp
    mod_bytes = blob[offset : offset + cb_mod]

    exponent = int.from_bytes(exp_bytes, "big")
    modulus = int.from_bytes(mod_bytes, "big")

    # sanity: header bitlen should match modulus bit length (often does).
    if bitlen != modulus.bit_length():
        logger.debug("[msi_v2] RSA bit length mismatch: header=%d computed=%d", bitlen, modulus.bit_length())

    return modulus, exponent


def _ncrypt_sign_pss_sha256(win32: Dict[str, Any], key: Any, digest: bytes) -> bytes:
    """
    Sign a SHA-256 digest using RSA-PSS via NCryptSignHash.

    NCryptSignHash expects the *hash digest*, not the original message.
    """
    from .managed_identity import MsiV2Error

    if len(digest) != 32:
        raise MsiV2Error("[msi_v2] Expected SHA-256 digest (32 bytes)")

    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    PaddingInfo = win32["BCRYPT_PSS_PADDING_INFO"]
    pad = PaddingInfo(win32["BCRYPT_SHA256_ALGORITHM"], 32)

    hash_buf = (ctypes_mod.c_ubyte * len(digest)).from_buffer_copy(digest)

    cb_sig = wintypes.DWORD(0)
    status = ncrypt.NCryptSignHash(
        key,
        ctypes_mod.byref(pad),
        hash_buf,
        len(digest),
        None,
        0,
        ctypes_mod.byref(cb_sig),
        win32["BCRYPT_PAD_PSS"],
    )
    if int(status) != 0 and cb_sig.value == 0:
        _check_security_status(status, "NCryptSignHash(size)")
    if cb_sig.value == 0:
        raise MsiV2Error("[msi_v2] NCryptSignHash returned empty signature size")

    sig_buf = (ctypes_mod.c_ubyte * cb_sig.value)()
    status = ncrypt.NCryptSignHash(
        key,
        ctypes_mod.byref(pad),
        hash_buf,
        len(digest),
        sig_buf,
        cb_sig.value,
        ctypes_mod.byref(cb_sig),
        win32["BCRYPT_PAD_PSS"],
    )
    _check_security_status(status, "NCryptSignHash")

    return bytes(sig_buf[: cb_sig.value])


# --------------------------------------------------------------------------------------
# CSR builder (KeyGuard key handle)
# --------------------------------------------------------------------------------------


def _build_csr_b64(win32: Dict[str, Any], key: Any, client_id: str, tenant_id: str, cu_id: Any) -> str:
    """
    Build a PKCS#10 CSR signed by the KeyGuard key (RSA-PSS/SHA256) and return base64.

    The CSR includes a request attribute (OID _CU_ID_OID_STR) whose value is:
        DER UTF8String(JSON(cuId))
    """
    modulus, exponent = _ncrypt_export_rsa_public(win32, key)

    subject = _der_name_cn_dc(client_id, tenant_id)
    spki = _der_subject_public_key_info_rsa(modulus, exponent)

    cuid_json = json.dumps(cu_id, separators=(",", ":"), ensure_ascii=False)
    cuid_val = _der_utf8string(cuid_json)

    # Attribute: SEQUENCE { OID, SET { <DER UTF8String> } }
    attr = _der_sequence(_der_oid(_CU_ID_OID_STR), _der_set(cuid_val))

    # PKCS#10 attributes: [0] IMPLICIT SET OF Attribute
    attrs_content = b"".join(sorted([attr]))
    attrs = _der_context_implicit_constructed(0, attrs_content)

    cri = _der_sequence(_der_integer(0), subject, spki, attrs)

    digest = hashlib.sha256(cri).digest()
    signature = _ncrypt_sign_pss_sha256(win32, key, digest)

    csr = _der_sequence(cri, _der_algid_rsapss_sha256(), _der_bitstring(signature))
    return base64.b64encode(csr).decode("ascii")


# --------------------------------------------------------------------------------------
# Certificate binding + WinHTTP mTLS
# --------------------------------------------------------------------------------------


def _create_cert_context_with_key(
    win32: Dict[str, Any],
    cert_der: bytes,
    key: Any,
    key_name: str,
    *,
    ksp_name: str = _DEFAULT_KSP_NAME,
) -> Tuple[Any, Tuple[Any, ...]]:
    """
    Create a CERT_CONTEXT from DER bytes and associate it with the given CNG private key.

    Why set multiple properties?

    WinHTTP/SChannel sometimes fails to locate the private key unless the cert context contains
    enough information. We set (best-effort):

      * CERT_NCRYPT_KEY_HANDLE_PROP_ID (78)  - direct handle binding
      * CERT_KEY_CONTEXT_PROP_ID (5)         - CERT_KEY_CONTEXT with hNCryptKey + CERT_NCRYPT_KEY_SPEC
      * CERT_KEY_PROV_INFO_PROP_ID (2)       - CRYPT_KEY_PROV_INFO referencing the persisted key name

    The returned keepalive tuple MUST remain referenced for as long as the CERT_CONTEXT is used,
    because it contains buffers referenced by the cert properties.

    Returns:
        (PCCERT_CONTEXT, keepalive)
    """
    from .managed_identity import MsiV2Error

    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    crypt32 = win32["crypt32"]

    enc = win32["X509_ASN_ENCODING"] | win32["PKCS_7_ASN_ENCODING"]

    # Keep DER bytes alive to be safe.
    cert_buf = ctypes_mod.create_string_buffer(cert_der)
    ctx = crypt32.CertCreateCertificateContext(enc, cert_buf, len(cert_der))
    if not ctx:
        _raise_win32_last_error("[msi_v2] CertCreateCertificateContext failed")

    keepalive: List[Any] = [cert_buf]

    try:
        key_value = int(getattr(key, "value", key) or 0)
        if not key_value:
            raise MsiV2Error("[msi_v2] Invalid CNG key handle (0)")

        # --- (A) CERT_NCRYPT_KEY_HANDLE_PROP_ID (78)
        key_handle = ctypes_mod.c_void_p(key_value)
        keepalive.append(key_handle)

        ok = crypt32.CertSetCertificateContextProperty(
            ctx,
            win32["CERT_NCRYPT_KEY_HANDLE_PROP_ID"],
            win32["CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG"],
            ctypes_mod.byref(key_handle),
        )
        if not ok:
            _raise_win32_last_error("[msi_v2] CertSetCertificateContextProperty(CERT_NCRYPT_KEY_HANDLE_PROP_ID) failed")

        # --- (B) CERT_KEY_CONTEXT_PROP_ID (5) - optional but helpful
        CERT_KEY_CONTEXT_PROP_ID = 5
        CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF  # wincrypt.h: CERT_NCRYPT_KEY_SPEC

        class CERT_KEY_CONTEXT(ctypes_mod.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hCryptProvOrNCryptKey", ctypes_mod.c_void_p),  # union: HCRYPTPROV / NCRYPT_KEY_HANDLE
                ("dwKeySpec", wintypes.DWORD),
            ]

        key_ctx = CERT_KEY_CONTEXT(ctypes_mod.sizeof(CERT_KEY_CONTEXT), key_handle, wintypes.DWORD(CERT_NCRYPT_KEY_SPEC))
        keepalive.append(key_ctx)

        ok = crypt32.CertSetCertificateContextProperty(
            ctx,
            CERT_KEY_CONTEXT_PROP_ID,
            win32["CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG"],
            ctypes_mod.byref(key_ctx),
        )
        if not ok:
            # Not fatal in all environments; keep going.
            logger.debug("[msi_v2] Failed to set CERT_KEY_CONTEXT_PROP_ID (last_error=%s)", ctypes_mod.get_last_error())

        # --- (C) CERT_KEY_PROV_INFO_PROP_ID (2) - allows Schannel to re-open key by name
        CERT_KEY_PROV_INFO_PROP_ID = 2

        class CRYPT_KEY_PROV_INFO(ctypes_mod.Structure):
            _fields_ = [
                ("pwszContainerName", wintypes.LPWSTR),
                ("pwszProvName", wintypes.LPWSTR),
                ("dwProvType", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("cProvParam", wintypes.DWORD),
                ("rgProvParam", ctypes_mod.c_void_p),
                ("dwKeySpec", wintypes.DWORD),
            ]

        container_buf = ctypes_mod.create_unicode_buffer(str(key_name))
        provider_buf = ctypes_mod.create_unicode_buffer(str(ksp_name))
        keepalive.extend([container_buf, provider_buf])

        # For CNG keys (dwProvType==0), dwKeySpec is passed as dwLegacyKeySpec to NCryptOpenKey.
        # It must be AT_SIGNATURE or AT_KEYEXCHANGE per CRYPT_KEY_PROV_INFO docs.
        prov_info = CRYPT_KEY_PROV_INFO(
            ctypes_mod.cast(container_buf, wintypes.LPWSTR),
            ctypes_mod.cast(provider_buf, wintypes.LPWSTR),
            wintypes.DWORD(0),  # CNG
            wintypes.DWORD(_NCRYPT_SILENT_FLAG),
            wintypes.DWORD(0),
            None,
            wintypes.DWORD(_AT_SIGNATURE),
        )
        keepalive.append(prov_info)

        ok = crypt32.CertSetCertificateContextProperty(
            ctx,
            CERT_KEY_PROV_INFO_PROP_ID,
            win32["CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG"],
            ctypes_mod.byref(prov_info),
        )
        if not ok:
            # If this fails, WinHTTP may still work via CERT_NCRYPT_KEY_HANDLE_PROP_ID.
            logger.debug("[msi_v2] Failed to set CERT_KEY_PROV_INFO_PROP_ID (last_error=%s)", ctypes_mod.get_last_error())

        return ctx, tuple(keepalive)

    except Exception:
        try:
            crypt32.CertFreeCertificateContext(ctx)
        except Exception:
            pass
        raise


def _winhttp_close(win32: Dict[str, Any], handle: Any) -> None:
    """Close a WinHTTP HINTERNET handle (best-effort)."""
    try:
        if handle:
            win32["winhttp"].WinHttpCloseHandle(handle)
    except Exception:
        pass


def _winhttp_set_option_dword(win32: Dict[str, Any], handle: Any, option: int, value: int, *, fatal: bool = False) -> None:
    """Set a WinHTTP option that takes a DWORD value."""
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    winhttp = win32["winhttp"]

    v = wintypes.DWORD(int(value))
    ok = winhttp.WinHttpSetOption(handle, option, ctypes_mod.byref(v), ctypes_mod.sizeof(v))
    if not ok and fatal:
        _raise_win32_last_error(f"[msi_v2] WinHttpSetOption({option}) failed")


def _winhttp_post(win32: Dict[str, Any], url: str, cert_ctx: Any, body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
    """
    POST bytes to an https:// URL using WinHTTP + SChannel, presenting the provided cert context.

    Returns:
        (status_code, response_body_bytes)
    """
    from .managed_identity import MsiV2Error

    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    winhttp = win32["winhttp"]

    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme.lower() != "https":
        raise MsiV2Error(f"[msi_v2] Token endpoint must be https, got: {url!r}")
    if not u.hostname:
        raise MsiV2Error(f"[msi_v2] Invalid token endpoint: {url!r}")

    host = u.hostname
    port = u.port or 443
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    # WinHTTP uses UTF-16 wide strings.
    user_agent = "msal-python-msi-v2"

    h_session = winhttp.WinHttpOpen(
        user_agent,
        win32["WINHTTP_ACCESS_TYPE_DEFAULT_PROXY"],
        None,
        None,
        0,
    )
    if not h_session:
        _raise_win32_last_error("[msi_v2] WinHttpOpen failed")

    try:
        # Best-effort: ensure client cert context is honored even when HTTP/2 is negotiated.
        # Not all Windows builds support this option; ignore failures.
        _winhttp_set_option_dword(win32, h_session, win32["WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT"], 1, fatal=False)

        h_connect = winhttp.WinHttpConnect(h_session, host, int(port), 0)
        if not h_connect:
            _raise_win32_last_error("[msi_v2] WinHttpConnect failed")
        try:
            h_request = winhttp.WinHttpOpenRequest(
                h_connect,
                "POST",
                path,
                None,
                None,
                None,
                win32["WINHTTP_FLAG_SECURE"],
            )
            if not h_request:
                _raise_win32_last_error("[msi_v2] WinHttpOpenRequest failed")
            try:
                # Set client certificate context on request.
                CertContext = win32["CERT_CONTEXT"]
                ok = winhttp.WinHttpSetOption(
                    h_request,
                    win32["WINHTTP_OPTION_CLIENT_CERT_CONTEXT"],
                    cert_ctx,
                    ctypes_mod.sizeof(CertContext),
                )
                if not ok:
                    _raise_win32_last_error("[msi_v2] WinHttpSetOption(WINHTTP_OPTION_CLIENT_CERT_CONTEXT) failed")

                header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                header_str = header_lines  # unicode / wide

                body_buf = ctypes_mod.create_string_buffer(body)

                ok = winhttp.WinHttpSendRequest(
                    h_request,
                    header_str,
                    0xFFFFFFFF,  # -1L (auto compute)
                    body_buf,
                    len(body),
                    len(body),
                    0,
                )
                if not ok:
                    _raise_win32_last_error("[msi_v2] WinHttpSendRequest failed")

                ok = winhttp.WinHttpReceiveResponse(h_request, None)
                if not ok:
                    _raise_win32_last_error("[msi_v2] WinHttpReceiveResponse failed")

                # Query status code as DWORD.
                status = wintypes.DWORD(0)
                status_size = wintypes.DWORD(ctypes_mod.sizeof(status))
                index = wintypes.DWORD(0)
                ok = winhttp.WinHttpQueryHeaders(
                    h_request,
                    win32["WINHTTP_QUERY_STATUS_CODE"] | win32["WINHTTP_QUERY_FLAG_NUMBER"],
                    None,
                    ctypes_mod.byref(status),
                    ctypes_mod.byref(status_size),
                    ctypes_mod.byref(index),
                )
                if not ok:
                    _raise_win32_last_error("[msi_v2] WinHttpQueryHeaders(WINHTTP_QUERY_STATUS_CODE) failed")

                chunks: List[bytes] = []
                while True:
                    avail = wintypes.DWORD(0)
                    ok = winhttp.WinHttpQueryDataAvailable(h_request, ctypes_mod.byref(avail))
                    if not ok:
                        _raise_win32_last_error("[msi_v2] WinHttpQueryDataAvailable failed")
                    if avail.value == 0:
                        break
                    buf = (ctypes_mod.c_ubyte * avail.value)()
                    read = wintypes.DWORD(0)
                    ok = winhttp.WinHttpReadData(h_request, buf, avail.value, ctypes_mod.byref(read))
                    if not ok:
                        _raise_win32_last_error("[msi_v2] WinHttpReadData failed")
                    if read.value:
                        chunks.append(bytes(buf[: read.value]))
                    if read.value == 0:
                        break

                return int(status.value), b"".join(chunks)
            finally:
                _winhttp_close(win32, h_request)
        finally:
            _winhttp_close(win32, h_connect)
    finally:
        _winhttp_close(win32, h_session)


def _acquire_token_mtls_schannel(
    win32: Dict[str, Any],
    token_endpoint: str,
    cert_ctx: Any,
    client_id: str,
    scope: str,
) -> Dict[str, Any]:
    """
    Acquire an mtls_pop token from ESTS using WinHTTP/SChannel with the provided cert context.
    """
    from .managed_identity import MsiV2Error
    from urllib.parse import urlencode

    form = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "scope": scope,
            "token_type": "mtls_pop",
        }
    ).encode("utf-8")

    status, resp_body = _winhttp_post(
        win32,
        token_endpoint,
        cert_ctx,
        form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    text = resp_body.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise MsiV2Error(f"[msi_v2] ESTS token request failed: HTTP {status} Body={text!r}")

    return _json_loads(text, "ESTS token")


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def obtain_token(
    http_client,
    managed_identity: Dict[str, Any],
    resource: str,
    *,
    attestation_enabled: bool = True,
) -> Dict[str, Any]:
    """
    Acquire an mtls_pop access token using Windows KeyGuard + attestation.

    Args:
        http_client:
            Requests-like object that provides .get() and .post() returning responses with
            .status_code, .text, .headers. (MSAL passes its own session by default.)
        managed_identity:
            MSAL-managed identity selector dict (system-assigned or user-assigned). Used only
            to set optional IMDS query params for UAMI.
        resource:
            Resource or scope. If it doesn't end with "/.default", we append "/.default".
        attestation_enabled:
            Must be True for this KeyGuard flow. If False, we fail closed.

    Returns:
        Dict with access_token, expires_in, token_type, and optional resource.

    Raises:
        MsiV2Error on any failure (no MSI v1 fallback).
    """
    from .managed_identity import MsiV2Error

    win32 = _load_win32()
    ncrypt = win32["ncrypt"]
    crypt32 = win32["crypt32"]

    base = _imds_base()
    params = _mi_query_params(managed_identity)
    corr = _new_correlation_id()

    prov = None
    key = None
    key_name = None
    cert_ctx = None
    cert_keepalive: Optional[Tuple[Any, ...]] = None

    try:
        # 1) Read platform metadata (client_id, tenant_id, cuId, attestation endpoint).
        meta_url = base + _CSR_METADATA_PATH
        meta = _imds_get_json(http_client, meta_url, params, _imds_headers(corr))

        client_id = _get_first(meta, "clientId", "client_id")
        tenant_id = _get_first(meta, "tenantId", "tenant_id")
        cu_id = meta.get("cuId") if "cuId" in meta else meta.get("cu_id")
        attestation_endpoint = _get_first(meta, "attestationEndpoint", "attestation_endpoint")

        if not client_id or not tenant_id or cu_id is None:
            raise MsiV2Error(f"[msi_v2] getplatformmetadata missing required fields: {meta}")

        # 2) Create KeyGuard RSA key (NCrypt).
        prov, key, key_name = _create_keyguard_rsa_key(win32)

        # 3) CSR signed with RSA-PSS/SHA256, includes cuId request attribute.
        csr_b64 = _build_csr_b64(win32, key, str(client_id), str(tenant_id), cu_id)

        # 4) Attestation JWT (required in this flow).
        if not attestation_enabled:
            raise MsiV2Error("[msi_v2] attestation_enabled must be True for this KeyGuard flow.")
        if not attestation_endpoint:
            raise MsiV2Error("[msi_v2] attestationEndpoint missing from metadata.")

        key_handle_int = int(getattr(key, "value", 0) or 0)
        if not key_handle_int:
            raise MsiV2Error("[msi_v2] Invalid key handle for attestation")

        from .msi_v2_attestation import get_attestation_jwt

        att_jwt = get_attestation_jwt(
            attestation_endpoint=str(attestation_endpoint),
            client_id=str(client_id),
            key_handle=key_handle_int,
        )
        if not att_jwt or not str(att_jwt).strip():
            raise MsiV2Error("[msi_v2] Attestation token is missing/empty; refusing to call issuecredential.")

        # 5) Exchange CSR + attestation for an issued certificate (IMDS /issuecredential).
        issue_url = base + _ISSUE_CREDENTIAL_PATH
        issue_headers = _imds_headers(corr)
        issue_headers["Content-Type"] = "application/json"

        cred = _imds_post_json(
            http_client,
            issue_url,
            params,
            issue_headers,
            {"csr": csr_b64, "attestation_token": att_jwt},
        )

        cert_b64 = _get_first(cred, "certificate", "Certificate")
        if not cert_b64:
            raise MsiV2Error(f"[msi_v2] issuecredential missing certificate: {cred}")

        try:
            cert_der = base64.b64decode(cert_b64)
        except Exception as exc:
            raise MsiV2Error("[msi_v2] issuecredential returned invalid base64 certificate") from exc

        canonical_client_id = _get_first(cred, "client_id", "clientId") or str(client_id)
        token_endpoint = _token_endpoint_from_credential(cred)

        # 6) Bind KeyGuard key to the issued cert and request token over mTLS (SChannel).
        cert_ctx, cert_keepalive = _create_cert_context_with_key(win32, cert_der, key, str(key_name))

        scope = _resource_to_scope(resource)
        token_json = _acquire_token_mtls_schannel(win32, token_endpoint, cert_ctx, canonical_client_id, scope)

        if token_json.get("access_token") and token_json.get("expires_in"):
            return {
                "access_token": token_json["access_token"],
                "expires_in": int(token_json["expires_in"]),
                "token_type": token_json.get("token_type") or "mtls_pop",
                "resource": token_json.get("resource"),
            }

        # Some error shapes could still be JSON; return raw for caller to interpret.
        return token_json

    finally:
        # Cleanup: cert context (WinHTTP duplicates it internally during request).
        try:
            if cert_ctx:
                crypt32.CertFreeCertificateContext(cert_ctx)
        except Exception:
            pass

        # Cleanup: key and provider handles.
        # The key is persisted, so we delete it explicitly and then free handles.
        try:
            if key:
                ncrypt.NCryptDeleteKey(key, 0)
        except Exception:
            pass
        try:
            if key:
                ncrypt.NCryptFreeObject(key)
        except Exception:
            pass
        try:
            if prov:
                ncrypt.NCryptFreeObject(prov)
        except Exception:
            pass

        # keepalive is intentionally unused; it just keeps buffers alive while cert_ctx existed.
        _ = cert_keepalive

