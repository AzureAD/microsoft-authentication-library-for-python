# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
MSI v2 (IMDSv2) Managed Identity flow — Windows KeyGuard + SChannel mTLS PoP.

This module implements the MSI v2 token acquisition path using Windows native APIs
via ctypes:
  - CNG/NCrypt: create/open a KeyGuard-protected per-boot RSA key (non-exportable)
  - Minimal DER/PKCS#10: build a CSR signed with RSA-PSS/SHA256
  - IMDS: call getplatformmetadata + issuecredential
  - Crypt32: bind the issued certificate to the CNG private key
  - WinHTTP/SChannel: acquire access token over mTLS (token_type=mtls_pop)

Key behavior:
  - Uses a *named per-boot key*: opens the key if it already exists for this boot;
    otherwise creates it.
  - No MSI v1 fallback: any MSI v2 failure raises MsiV2Error.
  - Production-ready handle management: all WinHTTP / Crypt32 / NCrypt handles are
    released in finally blocks.
  - Certificate cache: in-memory with lifetime-based eviction (like .NET
    InMemoryCertificateCache).
  - Returns certificate with token for mTLS with resource.

Environment variables (optional):
  - AZURE_POD_IDENTITY_AUTHORITY_HOST: override IMDS base URL
    (default http://169.254.169.254)
  - MSAL_MSI_V2_KEY_NAME: override the per-boot key name (otherwise derived from
    metadata clientId)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlencode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IMDS constants
# ---------------------------------------------------------------------------

_IMDS_DEFAULT_BASE = "http://169.254.169.254"
_IMDS_BASE_ENVVAR = "AZURE_POD_IDENTITY_AUTHORITY_HOST"

_API_VERSION_QUERY_PARAM = "cred-api-version"
_IMDS_V2_API_VERSION = "2.0"

_CSR_METADATA_PATH = "/metadata/identity/getplatformmetadata"
_ISSUE_CREDENTIAL_PATH = "/metadata/identity/issuecredential"
_ACQUIRE_ENTRA_TOKEN_PATH = "/oauth2/v2.0/token"

_CU_ID_OID_STR = "1.3.6.1.4.1.311.90.2.10"

# ---------------------------------------------------------------------------
# NCrypt/CNG flags
# ---------------------------------------------------------------------------

_NCRYPT_USE_VIRTUAL_ISOLATION_FLAG = 0x00020000
_NCRYPT_USE_PER_BOOT_KEY_FLAG = 0x00040000

_RSA_KEY_SIZE = 2048

_AT_SIGNATURE = 2

_NCRYPT_SILENT_FLAG = 0x40

_KEY_NAME_ENVVAR = "MSAL_MSI_V2_KEY_NAME"

# NCrypt "not found" status codes
_NTE_BAD_KEYSET = 0x80090016
_NTE_NO_KEY = 0x8009000D
_NTE_NOT_FOUND = 0x80090011
_NTE_KEY_DOES_NOT_EXIST = 0x8009003A  # KeyGuard/VBS provider uses this
_NTE_EXISTS = 0x8009000F

# Lazy-loaded Win32 API cache
_WIN32: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Certificate cache (in-memory, process-local)
# ---------------------------------------------------------------------------

class _CertCacheEntry:
    """Cached mTLS certificate + metadata."""
    __slots__ = ("cert_der", "cert_pem", "token_endpoint", "client_id",
                 "not_after", "created_at")

    # Minimum remaining cert lifetime to cache (24 hours)
    MIN_REMAINING_LIFETIME_SEC = 24 * 3600

    def __init__(self, cert_der: bytes, cert_pem: str,
                 token_endpoint: str, client_id: str,
                 not_after: float):
        self.cert_der = cert_der
        self.cert_pem = cert_pem
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.not_after = not_after
        self.created_at = time.time()

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return now >= self.not_after - self.MIN_REMAINING_LIFETIME_SEC


_CERT_CACHE_LOCK = threading.Lock()
_CERT_CACHE: Dict[str, _CertCacheEntry] = {}


def _cert_cache_key(managed_identity: Optional[Any],
                    attested: bool) -> str:
    """Build a cache key from managed identity + identifier type + attestation flag."""
    mi_id_type = "SYSTEM_ASSIGNED"
    mi_id = "SYSTEM_ASSIGNED"
    getter = getattr(managed_identity, "get", None)
    if callable(getter):
        mi_id_type = str(getter("ManagedIdentityIdType") or "SYSTEM_ASSIGNED")
        mi_id = str(getter("Id") or "SYSTEM_ASSIGNED")
    tag = "#att=1" if attested else "#att=0"
    return mi_id_type + ":" + mi_id + tag


def _cert_cache_get(key: str) -> Optional[_CertCacheEntry]:
    """Return cached entry or None if missing/expired."""
    now = time.time()
    with _CERT_CACHE_LOCK:
        entry = _CERT_CACHE.get(key)
        if entry is None:
            return None
        if entry.is_expired(now):
            del _CERT_CACHE[key]
            logger.debug("[msi_v2] Cert cache EVICT (expired) key=%s", key[:20])
            return None
        logger.debug("[msi_v2] Cert cache HIT key=%s", key[:20])
        return entry


def _cert_cache_set(key: str, entry: _CertCacheEntry) -> None:
    """Store entry if it has sufficient remaining lifetime."""
    now = time.time()
    if entry.not_after <= now + _CertCacheEntry.MIN_REMAINING_LIFETIME_SEC:
        logger.debug("[msi_v2] Cert cache SKIP (insufficient lifetime) key=%s",
                     key[:20])
        return
    with _CERT_CACHE_LOCK:
        _CERT_CACHE[key] = entry
        logger.debug("[msi_v2] Cert cache SET key=%s", key[:20])


def _cert_cache_remove(key: str) -> None:
    """Remove entry (e.g., on SChannel failure)."""
    with _CERT_CACHE_LOCK:
        _CERT_CACHE.pop(key, None)


def _cert_cache_clear() -> None:
    """Clear all entries (for testing)."""
    with _CERT_CACHE_LOCK:
        _CERT_CACHE.clear()


# ---------------------------------------------------------------------------
# Compatibility helpers (tests + cross-language parity)
# ---------------------------------------------------------------------------

def get_cert_thumbprint_sha256(cert_pem: str) -> str:
    """
    Return base64url(SHA256(der(cert))) without padding, for cnf.x5t#S256
    comparisons. Accepts a PEM certificate string.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(
            cert_pem.encode("utf-8"), default_backend())
        der = cert.public_bytes(serialization.Encoding.DER)
        digest = hashlib.sha256(der).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    except Exception:
        return ""


def verify_cnf_binding(token: str, cert_pem: str) -> bool:
    """
    Verify that JWT payload contains cnf.x5t#S256 matching the cert
    thumbprint.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False

        payload_b64 = parts[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        claims = json.loads(
            base64.urlsafe_b64decode(payload_b64.encode("ascii")))

        cnf = claims.get("cnf", {}) if isinstance(claims, dict) else {}
        if not isinstance(cnf, dict):
            return False
        token_x5t = cnf.get("x5t#S256")
        if not token_x5t:
            return False

        cert_x5t = get_cert_thumbprint_sha256(cert_pem)
        if not cert_x5t:
            return False

        return token_x5t == cert_x5t
    except Exception:
        return False


def _der_to_pem(der_bytes: bytes) -> str:
    """Convert DER certificate bytes to PEM string format."""
    b64 = base64.b64encode(der_bytes).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN CERTIFICATE-----\n"
            + "\n".join(lines)
            + "\n-----END CERTIFICATE-----")


def _try_parse_cert_not_after(der_bytes: bytes) -> float:
    """
    Best-effort extraction of notAfter from a DER X.509 certificate.
    Returns epoch seconds. Falls back to now + 8 hours on any failure.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_der_x509_certificate(der_bytes, default_backend())
        na = cert.not_valid_after_utc if hasattr(
            cert, "not_valid_after_utc") else cert.not_valid_after
        if na.tzinfo is None:
            import calendar
            return float(calendar.timegm(na.timetuple()))
        return na.timestamp()
    except Exception:
        # Default: assume 8-hour cert lifetime (IMDS typical)
        return time.time() + 8 * 3600


# ---------------------------------------------------------------------------
# IMDS helpers
# ---------------------------------------------------------------------------

def _imds_base() -> str:
    base = os.getenv(_IMDS_BASE_ENVVAR)
    if base is None:
        return _IMDS_DEFAULT_BASE.rstrip("/")
    base = base.strip().rstrip("/")
    return base or _IMDS_DEFAULT_BASE.rstrip("/")


def _new_correlation_id() -> str:
    return str(uuid.uuid4())


def _imds_headers(correlation_id: Optional[str] = None) -> Dict[str, str]:
    return {
        "Metadata": "true",
        "x-ms-client-request-id": correlation_id or _new_correlation_id(),
    }


def _resource_to_scope(resource_or_scope: str) -> str:
    """Normalize resource to scope format (append /.default if needed)."""
    s = (resource_or_scope or "").strip()
    if not s:
        raise ValueError("resource must be non-empty")
    if s.endswith("/.default"):
        return s
    return s.rstrip("/") + "/.default"


def _der_utf8string(value: str) -> bytes:
    """DER UTF8String encoder (tag 0x0C)."""
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
    """Parse JSON with error context."""
    from .managed_identity import MsiV2Error
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise TypeError("expected JSON object")
        return obj
    except Exception as exc:
        raise MsiV2Error(
            f"[msi_v2] Invalid JSON from {what}: {text!r}") from exc


def _get_first(obj: Dict[str, Any], *names: str) -> Optional[str]:
    """Get first non-empty value from object by multiple name variants."""
    for n in names:
        if n in obj and obj[n] is not None and str(obj[n]).strip() != "":
            return str(obj[n])
    lower = {str(k).lower(): k for k in obj.keys()}
    for n in names:
        k = lower.get(n.lower())
        if k and obj[k] is not None and str(obj[k]).strip() != "":
            return str(obj[k])
    return None


def _mi_query_params(
    managed_identity: Optional[Any],
) -> Dict[str, str]:
    """Build IMDS query params: cred-api-version=2.0 + optional UAMI selector."""
    params: Dict[str, str] = {_API_VERSION_QUERY_PARAM: _IMDS_V2_API_VERSION}
    getter = getattr(managed_identity, "get", None)
    if not callable(getter):
        return params
    id_type = getter("ManagedIdentityIdType")
    identifier = getter("Id")
    mapping = {"ClientId": "client_id", "ObjectId": "object_id",
               "ResourceId": "msi_res_id"}
    wire = mapping.get(id_type)
    if wire and identifier:
        params[wire] = str(identifier)
    return params


def _imds_get_json(
    http_client, url: str, params: Dict[str, str],
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """GET request to IMDS with server header verification."""
    from .managed_identity import MsiV2Error
    resp = http_client.get(url, params=params, headers=headers)
    server = (resp.headers or {}).get("server", "")
    if "imds" not in str(server).lower():
        raise MsiV2Error(
            f"[msi_v2] IMDS server header check failed. "
            f"server={server!r} url={url}")
    if resp.status_code != 200:
        raise MsiV2Error(
            f"[msi_v2] IMDSv2 GET {url} failed: "
            f"HTTP {resp.status_code}: {resp.text}")
    return _json_loads(resp.text, f"GET {url}")


def _imds_post_json(
    http_client, url: str, params: Dict[str, str],
    headers: Dict[str, str], body: Dict[str, Any],
) -> Dict[str, Any]:
    """POST request to IMDS with server header verification."""
    from .managed_identity import MsiV2Error
    resp = http_client.post(
        url, params=params, headers=headers,
        data=json.dumps(body, separators=(",", ":")))
    server = (resp.headers or {}).get("server", "")
    if "imds" not in str(server).lower():
        raise MsiV2Error(
            f"[msi_v2] IMDS server header check failed. "
            f"server={server!r} url={url}")
    if resp.status_code != 200:
        raise MsiV2Error(
            f"[msi_v2] IMDSv2 POST {url} failed: "
            f"HTTP {resp.status_code}: {resp.text}")
    return _json_loads(resp.text, f"POST {url}")


def _token_endpoint_from_credential(cred: Dict[str, Any]) -> str:
    """
    Extract token endpoint from issuecredential response.
    Prefers explicit token_endpoint, falls back to
    mtls_authentication_endpoint + tenant_id.
    """
    token_endpoint = _get_first(cred, "token_endpoint", "tokenEndpoint")
    if token_endpoint:
        return token_endpoint

    mtls_auth = _get_first(
        cred, "mtls_authentication_endpoint",
        "mtlsAuthenticationEndpoint", "mtls_authenticationEndpoint")
    tenant_id = _get_first(cred, "tenant_id", "tenantId")
    if not mtls_auth or not tenant_id:
        from .managed_identity import MsiV2Error
        raise MsiV2Error(
            f"[msi_v2] issuecredential missing "
            f"mtls_authentication_endpoint/tenant_id: {cred}")

    base = mtls_auth.rstrip("/") + "/" + tenant_id.strip("/")
    return base + _ACQUIRE_ENTRA_TOKEN_PATH


# ---------------------------------------------------------------------------
# Win32 primitives (ctypes)  —  lazy loaded
# ---------------------------------------------------------------------------

def _load_win32() -> Dict[str, Any]:
    """Lazy-load Win32 APIs via ctypes (safe to import on non-Windows)."""
    global _WIN32
    from .managed_identity import MsiV2Error

    if _WIN32 is not None:
        return _WIN32
    if sys.platform != "win32":
        raise MsiV2Error("[msi_v2] KeyGuard + mTLS PoP is Windows-only.")

    import ctypes
    from ctypes import wintypes

    ncrypt = ctypes.WinDLL("ncrypt.dll")
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    winhttp = ctypes.WinDLL("winhttp.dll", use_last_error=True)

    NCRYPT_PROV_HANDLE = ctypes.c_void_p
    NCRYPT_KEY_HANDLE = ctypes.c_void_p
    SECURITY_STATUS = ctypes.c_long

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", ctypes.c_void_p),
            ("hCertStore", ctypes.c_void_p),
        ]

    PCCERT_CONTEXT = ctypes.POINTER(CERT_CONTEXT)

    class BCRYPT_PSS_PADDING_INFO(ctypes.Structure):
        _fields_ = [
            ("pszAlgId", ctypes.c_wchar_p),
            ("cbSalt", wintypes.ULONG),
        ]

    # NCrypt prototypes
    ncrypt.NCryptOpenStorageProvider.argtypes = [
        ctypes.POINTER(NCRYPT_PROV_HANDLE), ctypes.c_wchar_p, wintypes.DWORD]
    ncrypt.NCryptOpenStorageProvider.restype = SECURITY_STATUS

    ncrypt.NCryptOpenKey.argtypes = [
        NCRYPT_PROV_HANDLE, ctypes.POINTER(NCRYPT_KEY_HANDLE),
        ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD]
    ncrypt.NCryptOpenKey.restype = SECURITY_STATUS

    ncrypt.NCryptCreatePersistedKey.argtypes = [
        NCRYPT_PROV_HANDLE, ctypes.POINTER(NCRYPT_KEY_HANDLE),
        ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD]
    ncrypt.NCryptCreatePersistedKey.restype = SECURITY_STATUS

    ncrypt.NCryptSetProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD]
    ncrypt.NCryptSetProperty.restype = SECURITY_STATUS

    ncrypt.NCryptFinalizeKey.argtypes = [NCRYPT_KEY_HANDLE, wintypes.DWORD]
    ncrypt.NCryptFinalizeKey.restype = SECURITY_STATUS

    ncrypt.NCryptGetProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    ncrypt.NCryptGetProperty.restype = SECURITY_STATUS

    ncrypt.NCryptExportKey.argtypes = [
        NCRYPT_KEY_HANDLE, NCRYPT_KEY_HANDLE, ctypes.c_wchar_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    ncrypt.NCryptExportKey.restype = SECURITY_STATUS

    ncrypt.NCryptSignHash.argtypes = [
        NCRYPT_KEY_HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD]
    ncrypt.NCryptSignHash.restype = SECURITY_STATUS

    ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]
    ncrypt.NCryptFreeObject.restype = SECURITY_STATUS

    # Crypt32 prototypes
    crypt32.CertCreateCertificateContext.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    crypt32.CertCreateCertificateContext.restype = PCCERT_CONTEXT

    crypt32.CertSetCertificateContextProperty.argtypes = [
        PCCERT_CONTEXT, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    crypt32.CertSetCertificateContextProperty.restype = wintypes.BOOL

    crypt32.CertFreeCertificateContext.argtypes = [PCCERT_CONTEXT]
    crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

    # Crypt32 — certificate store APIs (for WindowsCertificate.from_store)
    crypt32.CertOpenStore.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p]
    crypt32.CertOpenStore.restype = ctypes.c_void_p

    crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    crypt32.CertCloseStore.restype = wintypes.BOOL

    crypt32.CertFindCertificateInStore.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    crypt32.CertFindCertificateInStore.restype = PCCERT_CONTEXT

    crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
        PCCERT_CONTEXT, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int)]
    crypt32.CryptAcquireCertificatePrivateKey.restype = wintypes.BOOL

    class CRYPT_HASH_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.c_void_p),
        ]

    # WinHTTP prototypes
    winhttp.WinHttpOpen.argtypes = [
        ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_wchar_p,
        ctypes.c_wchar_p, wintypes.DWORD]
    winhttp.WinHttpOpen.restype = ctypes.c_void_p

    winhttp.WinHttpConnect.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, wintypes.WORD, wintypes.DWORD]
    winhttp.WinHttpConnect.restype = ctypes.c_void_p

    winhttp.WinHttpOpenRequest.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, wintypes.DWORD]
    winhttp.WinHttpOpenRequest.restype = ctypes.c_void_p

    winhttp.WinHttpSetOption.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    winhttp.WinHttpSetOption.restype = wintypes.BOOL

    winhttp.WinHttpSendRequest.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    winhttp.WinHttpSendRequest.restype = wintypes.BOOL

    winhttp.WinHttpReceiveResponse.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p]
    winhttp.WinHttpReceiveResponse.restype = wintypes.BOOL

    winhttp.WinHttpQueryHeaders.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    winhttp.WinHttpQueryHeaders.restype = wintypes.BOOL

    winhttp.WinHttpQueryDataAvailable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    winhttp.WinHttpQueryDataAvailable.restype = wintypes.BOOL

    winhttp.WinHttpReadData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    winhttp.WinHttpReadData.restype = wintypes.BOOL

    winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
    winhttp.WinHttpCloseHandle.restype = wintypes.BOOL

    _WIN32 = {
        "ctypes": ctypes, "wintypes": wintypes,
        "ncrypt": ncrypt, "crypt32": crypt32, "winhttp": winhttp,
        "NCRYPT_PROV_HANDLE": NCRYPT_PROV_HANDLE,
        "NCRYPT_KEY_HANDLE": NCRYPT_KEY_HANDLE,
        "SECURITY_STATUS": SECURITY_STATUS,
        "CERT_CONTEXT": CERT_CONTEXT,
        "PCCERT_CONTEXT": PCCERT_CONTEXT,
        "BCRYPT_PSS_PADDING_INFO": BCRYPT_PSS_PADDING_INFO,
        "CRYPT_HASH_BLOB": CRYPT_HASH_BLOB,
        "ERROR_SUCCESS": 0,
        "NCRYPT_OVERWRITE_KEY_FLAG": 0x00000080,
        "NCRYPT_LENGTH_PROPERTY": "Length",
        "NCRYPT_EXPORT_POLICY_PROPERTY": "Export Policy",
        "NCRYPT_KEY_USAGE_PROPERTY": "Key Usage",
        "NCRYPT_ALLOW_SIGNING_FLAG": 0x00000002,
        "NCRYPT_ALLOW_DECRYPT_FLAG": 0x00000001,
        "BCRYPT_PAD_PSS": 0x00000008,
        "BCRYPT_SHA256_ALGORITHM": "SHA256",
        "BCRYPT_RSA_ALGORITHM": "RSA",
        "BCRYPT_RSAPUBLIC_BLOB": "RSAPUBLICBLOB",
        "BCRYPT_RSAPUBLIC_MAGIC": 0x31415352,
        "X509_ASN_ENCODING": 0x00000001,
        "PKCS_7_ASN_ENCODING": 0x00010000,
        "CERT_NCRYPT_KEY_HANDLE_PROP_ID": 78,
        "CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG": 0x40000000,
        "WINHTTP_ACCESS_TYPE_DEFAULT_PROXY": 0,
        "WINHTTP_FLAG_SECURE": 0x00800000,
        "WINHTTP_OPTION_CLIENT_CERT_CONTEXT": 47,
        "WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT": 161,
        "WINHTTP_QUERY_STATUS_CODE": 19,
        "WINHTTP_QUERY_FLAG_NUMBER": 0x20000000,
    }
    return _WIN32


# ---------------------------------------------------------------------------
# Win32 error helpers
# ---------------------------------------------------------------------------

def _raise_win32_last_error(msg: str) -> None:
    from .managed_identity import MsiV2Error
    win32 = _load_win32()
    ctypes_mod = win32["ctypes"]
    err = ctypes_mod.get_last_error()
    detail = ""
    try:
        detail = ctypes_mod.FormatError(err).strip()
    except Exception:
        pass
    raise MsiV2Error(f"{msg} (winerror={err} {detail})" if detail
                     else f"{msg} (winerror={err})")


def _check_security_status(status: int, what: str) -> None:
    from .managed_identity import MsiV2Error
    if int(status) != 0:
        code_u32 = int(status) & 0xFFFFFFFF
        raise MsiV2Error(f"[msi_v2] {what} failed: status=0x{code_u32:08X}")


def _status_u32(status: int) -> int:
    return int(status) & 0xFFFFFFFF


def _is_key_not_found(status: int) -> bool:
    return _status_u32(status) in (
        _NTE_BAD_KEYSET, _NTE_NO_KEY, _NTE_NOT_FOUND, _NTE_KEY_DOES_NOT_EXIST)


# ---------------------------------------------------------------------------
# DER helpers (minimal PKCS#10 CSR builder)
# ---------------------------------------------------------------------------

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
    if len(parts) < 2 or parts[0] > 2 or (parts[0] < 2 and parts[1] >= 40):
        raise ValueError(f"Invalid OID: {oid}")
    first = 40 * parts[0] + parts[1]
    out = bytearray([first])
    for p in parts[2:]:
        if p < 0:
            raise ValueError(f"Invalid OID component: {oid}")
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
    enc = sorted(items)
    return _der(0x31, b"".join(enc))


def _der_bitstring(data: bytes) -> bytes:
    return _der(0x03, b"\x00" + data)


def _der_ia5string(value: str) -> bytes:
    return _der(0x16, value.encode("ascii"))


def _der_context_explicit(tagnum: int, inner: bytes) -> bytes:
    return _der(0xA0 + tagnum, inner)


def _der_context_implicit_constructed(tagnum: int, inner_content: bytes) -> bytes:
    return _der(0xA0 + tagnum, inner_content)


def _der_name_cn_dc(cn: str, dc: str) -> bytes:
    cn_atv = _der_sequence(_der_oid("2.5.4.3"), _der_utf8string(cn))
    cn_rdn = _der_set(cn_atv)
    try:
        dc_value = _der_ia5string(dc)
    except Exception:
        dc_value = _der_utf8string(dc)
    dc_atv = _der_sequence(
        _der_oid("0.9.2342.19200300.100.1.25"), dc_value)
    dc_rdn = _der_set(dc_atv)
    return _der_sequence(cn_rdn, dc_rdn)


def _der_subject_public_key_info_rsa(modulus: int, exponent: int) -> bytes:
    rsa_pub = _der_sequence(_der_integer(modulus), _der_integer(exponent))
    alg = _der_sequence(
        _der_oid("1.2.840.113549.1.1.1"), _der_null())  # rsaEncryption
    return _der_sequence(alg, _der_bitstring(rsa_pub))


def _der_algid_rsapss_sha256() -> bytes:
    """AlgorithmIdentifier for RSASSA-PSS with SHA-256, MGF1(SHA-256),
    saltLength=32. trailerField omitted (DEFAULT=1, per .NET)."""
    sha256 = _der_sequence(
        _der_oid("2.16.840.1.101.3.4.2.1"), _der_null())
    mgf1 = _der_sequence(_der_oid("1.2.840.113549.1.1.8"), sha256)
    salt_len = _der_integer(32)
    params = _der_sequence(
        _der_context_explicit(0, sha256),
        _der_context_explicit(1, mgf1),
        _der_context_explicit(2, salt_len),
        # trailerField [3] omitted — DEFAULT trailerFieldBC(1)
    )
    return _der_sequence(_der_oid("1.2.840.113549.1.1.10"), params)


# ---------------------------------------------------------------------------
# CNG/NCrypt wrappers
# ---------------------------------------------------------------------------

def _ncrypt_get_property(win32: Dict[str, Any], h: Any, name: str) -> bytes:
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]
    cb = wintypes.DWORD(0)
    status = ncrypt.NCryptGetProperty(h, name, None, 0,
                                      ctypes_mod.byref(cb), 0)
    if int(status) != 0 and cb.value == 0:
        _check_security_status(status, f"NCryptGetProperty({name})")
    if cb.value == 0:
        return b""
    buf = (ctypes_mod.c_ubyte * cb.value)()
    status = ncrypt.NCryptGetProperty(h, name, buf, cb.value,
                                      ctypes_mod.byref(cb), 0)
    _check_security_status(status, f"NCryptGetProperty({name})")
    return bytes(buf[:cb.value])


def _stable_key_name(client_id: str) -> str:
    base = (client_id or "").strip()
    safe = []
    for ch in base:
        if ch.isalnum() or ch in ("-", "_"):
            safe.append(ch)
        else:
            safe.append("_")
    return "MsalMsiV2Key_" + "".join(safe)[:90]


def _open_or_create_keyguard_rsa_key(
    win32: Dict[str, Any], *, key_name: str,
) -> Tuple[Any, Any, str, bool]:
    """
    Open a named per-boot KeyGuard RSA key if it exists; otherwise create it.
    Returns: (prov_handle, key_handle, key_name, opened_existing)
    """
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    prov = win32["NCRYPT_PROV_HANDLE"]()
    status = ncrypt.NCryptOpenStorageProvider(
        ctypes_mod.byref(prov),
        "Microsoft Software Key Storage Provider", 0)
    _check_security_status(status, "NCryptOpenStorageProvider")

    key = win32["NCRYPT_KEY_HANDLE"]()

    # 1) Try open first
    status = ncrypt.NCryptOpenKey(prov, ctypes_mod.byref(key),
                                  str(key_name), _AT_SIGNATURE, 0)
    if int(status) == 0:
        vi = _ncrypt_get_property(win32, key, "Virtual Iso")
        if not vi or len(vi) < 4:
            from .managed_identity import MsiV2Error
            raise MsiV2Error(
                "[msi_v2] Virtual Iso property missing/invalid; "
                "Credential Guard likely not active.")
        return prov, key, str(key_name), True

    if not _is_key_not_found(status):
        _check_security_status(status, f"NCryptOpenKey({key_name})")

    # 2) Create if missing
    flags = (win32["NCRYPT_OVERWRITE_KEY_FLAG"]
             | _NCRYPT_USE_VIRTUAL_ISOLATION_FLAG
             | _NCRYPT_USE_PER_BOOT_KEY_FLAG)

    status = ncrypt.NCryptCreatePersistedKey(
        prov, ctypes_mod.byref(key), win32["BCRYPT_RSA_ALGORITHM"],
        str(key_name), _AT_SIGNATURE, flags)

    if _status_u32(status) == _NTE_EXISTS:
        # Race: another thread/process created it
        status2 = ncrypt.NCryptOpenKey(prov, ctypes_mod.byref(key),
                                       str(key_name), _AT_SIGNATURE, 0)
        _check_security_status(status2,
                               f"NCryptOpenKey({key_name}) after exists")
        return prov, key, str(key_name), True

    _check_security_status(status, "NCryptCreatePersistedKey")

    # Set key properties
    length = wintypes.DWORD(int(_RSA_KEY_SIZE))
    status = ncrypt.NCryptSetProperty(
        key, win32["NCRYPT_LENGTH_PROPERTY"],
        ctypes_mod.byref(length), ctypes_mod.sizeof(length), 0)
    _check_security_status(status, "NCryptSetProperty(Length)")

    usage = wintypes.DWORD(
        win32["NCRYPT_ALLOW_SIGNING_FLAG"]
        | win32["NCRYPT_ALLOW_DECRYPT_FLAG"])
    status = ncrypt.NCryptSetProperty(
        key, win32["NCRYPT_KEY_USAGE_PROPERTY"],
        ctypes_mod.byref(usage), ctypes_mod.sizeof(usage), 0)
    _check_security_status(status, "NCryptSetProperty(Key Usage)")

    export_policy = wintypes.DWORD(0)  # non-exportable
    status = ncrypt.NCryptSetProperty(
        key, win32["NCRYPT_EXPORT_POLICY_PROPERTY"],
        ctypes_mod.byref(export_policy), ctypes_mod.sizeof(export_policy), 0)
    _check_security_status(status, "NCryptSetProperty(Export Policy)")

    status = ncrypt.NCryptFinalizeKey(key, 0)
    _check_security_status(status, "NCryptFinalizeKey")

    vi = _ncrypt_get_property(win32, key, "Virtual Iso")
    if not vi or len(vi) < 4:
        from .managed_identity import MsiV2Error
        raise MsiV2Error(
            "[msi_v2] Virtual Iso property not available; "
            "Credential Guard likely not active.")

    return prov, key, str(key_name), False


def _ncrypt_export_rsa_public(
    win32: Dict[str, Any], key: Any,
) -> Tuple[int, int]:
    """Export RSA public key (modulus, exponent) from an NCrypt key handle."""
    from .managed_identity import MsiV2Error
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    cb = wintypes.DWORD(0)
    status = ncrypt.NCryptExportKey(
        key, None, win32["BCRYPT_RSAPUBLIC_BLOB"], None, None, 0,
        ctypes_mod.byref(cb), 0)
    if int(status) != 0 and cb.value == 0:
        _check_security_status(status, "NCryptExportKey(size)")
    if cb.value == 0:
        raise MsiV2Error("[msi_v2] NCryptExportKey returned empty blob size")

    buf = (ctypes_mod.c_ubyte * cb.value)()
    status = ncrypt.NCryptExportKey(
        key, None, win32["BCRYPT_RSAPUBLIC_BLOB"], None,
        buf, cb.value, ctypes_mod.byref(cb), 0)
    _check_security_status(status, "NCryptExportKey(RSAPUBLICBLOB)")
    blob = bytes(buf[:cb.value])

    if len(blob) < 24:
        raise MsiV2Error("[msi_v2] RSAPUBLICBLOB too small")

    magic, bitlen, cb_exp, cb_mod, cb_p1, cb_p2 = struct.unpack(
        "<6I", blob[:24])
    if magic != win32["BCRYPT_RSAPUBLIC_MAGIC"]:
        raise MsiV2Error(
            f"[msi_v2] RSAPUBLICBLOB magic mismatch: 0x{magic:08X}")

    offset = 24
    if len(blob) < offset + cb_exp + cb_mod:
        raise MsiV2Error("[msi_v2] RSAPUBLICBLOB truncated")

    exp_bytes = blob[offset:offset + cb_exp]
    offset += cb_exp
    mod_bytes = blob[offset:offset + cb_mod]

    exponent = int.from_bytes(exp_bytes, "big")
    modulus = int.from_bytes(mod_bytes, "big")
    return modulus, exponent


def _ncrypt_sign_pss_sha256(
    win32: Dict[str, Any], key: Any, digest: bytes,
) -> bytes:
    """Sign a SHA-256 digest using RSA-PSS via NCryptSignHash."""
    from .managed_identity import MsiV2Error
    if len(digest) != 32:
        raise MsiV2Error("[msi_v2] Expected SHA-256 digest (32 bytes)")

    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    ncrypt = win32["ncrypt"]

    pad = win32["BCRYPT_PSS_PADDING_INFO"](
        win32["BCRYPT_SHA256_ALGORITHM"], 32)
    hash_buf = (ctypes_mod.c_ubyte * len(digest)).from_buffer_copy(digest)

    cb_sig = wintypes.DWORD(0)
    status = ncrypt.NCryptSignHash(
        key, ctypes_mod.byref(pad), hash_buf, len(digest),
        None, 0, ctypes_mod.byref(cb_sig), win32["BCRYPT_PAD_PSS"])
    if int(status) != 0 and cb_sig.value == 0:
        _check_security_status(status, "NCryptSignHash(size)")
    if cb_sig.value == 0:
        raise MsiV2Error("[msi_v2] NCryptSignHash returned empty sig size")

    sig_buf = (ctypes_mod.c_ubyte * cb_sig.value)()
    status = ncrypt.NCryptSignHash(
        key, ctypes_mod.byref(pad), hash_buf, len(digest),
        sig_buf, cb_sig.value, ctypes_mod.byref(cb_sig),
        win32["BCRYPT_PAD_PSS"])
    _check_security_status(status, "NCryptSignHash")
    return bytes(sig_buf[:cb_sig.value])


# ---------------------------------------------------------------------------
# CSR builder
# ---------------------------------------------------------------------------

def _build_csr_b64(
    win32: Dict[str, Any], key: Any,
    client_id: str, tenant_id: str, cu_id: Any,
) -> str:
    """Build CSR signed by KeyGuard key (RSA-PSS SHA256), with cuId OID
    attribute."""
    modulus, exponent = _ncrypt_export_rsa_public(win32, key)
    subject = _der_name_cn_dc(client_id, tenant_id)
    spki = _der_subject_public_key_info_rsa(modulus, exponent)

    cuid_json = json.dumps(cu_id, separators=(",", ":"), ensure_ascii=False)
    cuid_val = _der_utf8string(cuid_json)

    attr = _der_sequence(_der_oid(_CU_ID_OID_STR), _der_set(cuid_val))
    attrs_content = b"".join(sorted([attr]))
    attrs = _der_context_implicit_constructed(0, attrs_content)

    cri = _der_sequence(_der_integer(0), subject, spki, attrs)
    digest = hashlib.sha256(cri).digest()
    signature = _ncrypt_sign_pss_sha256(win32, key, digest)

    csr = _der_sequence(cri, _der_algid_rsapss_sha256(),
                        _der_bitstring(signature))
    return base64.b64encode(csr).decode("ascii")


# ---------------------------------------------------------------------------
# Certificate binding + WinHTTP mTLS
# ---------------------------------------------------------------------------

def _create_cert_context_with_key(
    win32: Dict[str, Any], cert_der: bytes, key: Any, key_name: str,
    *, ksp_name: str = "Microsoft Software Key Storage Provider",
) -> Tuple[Any, Any, Tuple[Any, ...]]:
    """Create a CERT_CONTEXT from DER bytes and associate it with a CNG
    private key via multiple properties for SChannel compatibility."""
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    crypt32 = win32["crypt32"]

    enc = win32["X509_ASN_ENCODING"] | win32["PKCS_7_ASN_ENCODING"]
    buf = ctypes_mod.create_string_buffer(cert_der)
    ctx = crypt32.CertCreateCertificateContext(enc, buf, len(cert_der))
    if not ctx:
        _raise_win32_last_error(
            "[msi_v2] CertCreateCertificateContext failed")

    keepalive: List[Any] = [buf]

    try:
        # (A) Direct NCrypt key handle
        key_handle = ctypes_mod.c_void_p(int(key.value))
        keepalive.append(key_handle)

        ok = crypt32.CertSetCertificateContextProperty(
            ctx, win32["CERT_NCRYPT_KEY_HANDLE_PROP_ID"],
            win32["CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG"],
            ctypes_mod.byref(key_handle))
        if not ok:
            _raise_win32_last_error(
                "[msi_v2] CertSetCertificateContextProperty"
                "(CERT_NCRYPT_KEY_HANDLE_PROP_ID) failed")

        # (B) CERT_KEY_CONTEXT_PROP_ID (best-effort)
        CERT_KEY_CONTEXT_PROP_ID = 5
        CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF

        class CERT_KEY_CONTEXT(ctypes_mod.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hCryptProvOrNCryptKey", ctypes_mod.c_void_p),
                ("dwKeySpec", wintypes.DWORD),
            ]

        key_ctx = CERT_KEY_CONTEXT(
            ctypes_mod.sizeof(CERT_KEY_CONTEXT), key_handle,
            wintypes.DWORD(CERT_NCRYPT_KEY_SPEC))
        keepalive.append(key_ctx)

        ok = crypt32.CertSetCertificateContextProperty(
            ctx, CERT_KEY_CONTEXT_PROP_ID,
            win32["CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG"],
            ctypes_mod.byref(key_ctx))
        if not ok:
            logger.debug("[msi_v2] Failed to set CERT_KEY_CONTEXT_PROP_ID "
                         "(last_error=%s)", ctypes_mod.get_last_error())

        # (C) CERT_KEY_PROV_INFO_PROP_ID (for SChannel reopen by name)
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

        prov_info = CRYPT_KEY_PROV_INFO(
            ctypes_mod.cast(container_buf, wintypes.LPWSTR),
            ctypes_mod.cast(provider_buf, wintypes.LPWSTR),
            wintypes.DWORD(0),  # CNG/KSP
            wintypes.DWORD(_NCRYPT_SILENT_FLAG),
            wintypes.DWORD(0), None,
            wintypes.DWORD(_AT_SIGNATURE))
        keepalive.append(prov_info)

        ok = crypt32.CertSetCertificateContextProperty(
            ctx, CERT_KEY_PROV_INFO_PROP_ID,
            win32["CERT_SET_PROPERTY_INHIBIT_PERSIST_FLAG"],
            ctypes_mod.byref(prov_info))
        if not ok:
            logger.debug("[msi_v2] Failed to set CERT_KEY_PROV_INFO_PROP_ID "
                         "(last_error=%s)", ctypes_mod.get_last_error())

        return ctx, buf, tuple(keepalive)

    except Exception:
        try:
            crypt32.CertFreeCertificateContext(ctx)
        except Exception:
            pass
        raise


def _winhttp_close(win32: Dict[str, Any], h: Any) -> None:
    try:
        if h:
            win32["winhttp"].WinHttpCloseHandle(h)
    except Exception:
        pass


def _winhttp_post(
    win32: Dict[str, Any], url: str, cert_ctx: Any,
    body: bytes, headers: Dict[str, str],
) -> Tuple[int, bytes]:
    """POST to https URL using WinHTTP + SChannel with client cert."""
    from .managed_identity import MsiV2Error
    ctypes_mod = win32["ctypes"]
    wintypes = win32["wintypes"]
    winhttp = win32["winhttp"]

    u = urlparse(url)
    if u.scheme.lower() != "https":
        raise MsiV2Error(
            f"[msi_v2] Token endpoint must be https, got: {url!r}")
    if not u.hostname:
        raise MsiV2Error(f"[msi_v2] Invalid token endpoint: {url!r}")

    host = u.hostname
    port = u.port or 443
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    h_session = winhttp.WinHttpOpen(
        "msal-python-msi-v2", win32["WINHTTP_ACCESS_TYPE_DEFAULT_PROXY"],
        None, None, 0)
    if not h_session:
        _raise_win32_last_error("[msi_v2] WinHttpOpen failed")

    h_connect = None
    h_request = None
    try:
        # Best-effort: HTTP/2 + client cert
        enable = wintypes.DWORD(1)
        try:
            winhttp.WinHttpSetOption(
                h_session,
                win32["WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT"],
                ctypes_mod.byref(enable), ctypes_mod.sizeof(enable))
        except Exception:
            pass

        h_connect = winhttp.WinHttpConnect(h_session, host, int(port), 0)
        if not h_connect:
            _raise_win32_last_error("[msi_v2] WinHttpConnect failed")

        h_request = winhttp.WinHttpOpenRequest(
            h_connect, "POST", path, None, None, None,
            win32["WINHTTP_FLAG_SECURE"])
        if not h_request:
            _raise_win32_last_error("[msi_v2] WinHttpOpenRequest failed")

        # Attach cert for mTLS
        ok = winhttp.WinHttpSetOption(
            h_request, win32["WINHTTP_OPTION_CLIENT_CERT_CONTEXT"],
            cert_ctx, ctypes_mod.sizeof(win32["CERT_CONTEXT"]))
        if not ok:
            _raise_win32_last_error(
                "[msi_v2] WinHttpSetOption(CLIENT_CERT) failed")

        header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        body_buf = ctypes_mod.create_string_buffer(body)

        ok = winhttp.WinHttpSendRequest(
            h_request, header_lines, 0xFFFFFFFF,
            body_buf, len(body), len(body), 0)
        if not ok:
            _raise_win32_last_error("[msi_v2] WinHttpSendRequest failed")

        ok = winhttp.WinHttpReceiveResponse(h_request, None)
        if not ok:
            _raise_win32_last_error("[msi_v2] WinHttpReceiveResponse failed")

        # Read status code
        status = wintypes.DWORD(0)
        status_size = wintypes.DWORD(ctypes_mod.sizeof(status))
        index = wintypes.DWORD(0)

        ok = winhttp.WinHttpQueryHeaders(
            h_request,
            win32["WINHTTP_QUERY_STATUS_CODE"]
            | win32["WINHTTP_QUERY_FLAG_NUMBER"],
            None, ctypes_mod.byref(status),
            ctypes_mod.byref(status_size), ctypes_mod.byref(index))
        if not ok:
            _raise_win32_last_error(
                "[msi_v2] WinHttpQueryHeaders(STATUS_CODE) failed")

        # Read body
        chunks: List[bytes] = []
        while True:
            avail = wintypes.DWORD(0)
            ok = winhttp.WinHttpQueryDataAvailable(
                h_request, ctypes_mod.byref(avail))
            if not ok:
                _raise_win32_last_error(
                    "[msi_v2] WinHttpQueryDataAvailable failed")
            if avail.value == 0:
                break
            buf = (ctypes_mod.c_ubyte * avail.value)()
            read = wintypes.DWORD(0)
            ok = winhttp.WinHttpReadData(
                h_request, buf, avail.value, ctypes_mod.byref(read))
            if not ok:
                _raise_win32_last_error("[msi_v2] WinHttpReadData failed")
            if read.value:
                chunks.append(bytes(buf[:read.value]))
            if read.value == 0:
                break

        return int(status.value), b"".join(chunks)
    finally:
        _winhttp_close(win32, h_request)
        _winhttp_close(win32, h_connect)
        _winhttp_close(win32, h_session)


def _acquire_token_mtls_schannel(
    win32: Dict[str, Any], token_endpoint: str, cert_ctx: Any,
    client_id: str, scope: str,
) -> Dict[str, Any]:
    """Acquire an mtls_pop token from ESTS using WinHTTP/SChannel."""
    from .managed_identity import MsiV2Error

    form = urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": scope,
        "token_type": "mtls_pop",
    }).encode("utf-8")

    status, resp_body = _winhttp_post(
        win32, token_endpoint, cert_ctx, form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })

    text = resp_body.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise MsiV2Error(
            f"[msi_v2] ESTS token request failed: "
            f"HTTP {status} Body={text!r}")
    return _json_loads(text, "ESTS token")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Type alias for attestation provider callback.
# Signature: (endpoint, key_handle, client_id, cache_key) -> JWT string.
# cache_key is the stable per-boot key name for optimal caching.
AttestationTokenProvider = Callable[[str, int, str, str], str]


def obtain_token(
    http_client,
    managed_identity: Dict[str, Any],
    resource: str,
    *,
    attestation_enabled: bool = True,
    attestation_token_provider: Optional[AttestationTokenProvider] = None,
) -> Dict[str, Any]:
    """
    Acquire mtls_pop token using Windows KeyGuard + optional MAA attestation.

    Flow:
      1. getplatformmetadata → client_id, tenant_id, cu_id, attestationEndpoint
      2. Open/create named per-boot KeyGuard RSA key (non-exportable)
      3. Build PKCS#10 CSR with cuId attribute, sign with RSA-PSS/SHA256
      4. Get attestation JWT from MAA (if attestation_token_provider given)
      5. issuecredential → X.509 cert
      6. Create CERT_CONTEXT, bind to KeyGuard private key
      7. POST /oauth2/v2.0/token via WinHTTP/SChannel with mTLS

    Args:
        http_client: HTTP client (e.g., requests.Session())
        managed_identity: MSAL managed identity dict
        resource: Resource URI for token acquisition
        attestation_enabled: Whether attestation is enabled
        attestation_token_provider: Callback (endpoint, key_handle,
            client_id, cache_key) -> JWT string.  Provided by
            msal-key-attestation package.  cache_key is the stable
            per-boot key name for optimal caching.  None means
            non-attested flow.

    Returns:
        Token response dict with access_token, expires_in, token_type,
        cert_pem, cert_der_b64, cert_thumbprint_sha256.

    Raises:
        MsiV2Error: on any failure (no fallback to MSI v1)
    """
    from .managed_identity import MsiV2Error

    win32 = _load_win32()
    ncrypt = win32["ncrypt"]
    crypt32 = win32["crypt32"]

    base = _imds_base()
    params = _mi_query_params(managed_identity)
    corr = _new_correlation_id()

    # Check certificate cache first. The cache key must reflect the
    # effective attestation mode so a non-attested certificate is never
    # reused as if it were attested (or vice versa).
    attested = attestation_enabled and attestation_token_provider is not None
    cache_key = _cert_cache_key(managed_identity, attested)
    cached = _cert_cache_get(cache_key)

    prov = None
    key = None
    cert_ctx = None
    cert_der = None

    try:
        # 1) getplatformmetadata
        meta_url = base + _CSR_METADATA_PATH
        meta = _imds_get_json(http_client, meta_url, params,
                              _imds_headers(corr))

        client_id = _get_first(meta, "clientId", "client_id")
        tenant_id = _get_first(meta, "tenantId", "tenant_id")
        cu_id = meta.get("cuId") if "cuId" in meta else meta.get("cu_id")
        attestation_endpoint = _get_first(
            meta, "attestationEndpoint", "attestation_endpoint")

        if not client_id or not tenant_id or cu_id is None:
            raise MsiV2Error(
                f"[msi_v2] getplatformmetadata missing required fields: "
                f"{meta}")

        # 2) Open-or-create KeyGuard RSA key
        key_name = (os.getenv(_KEY_NAME_ENVVAR)
                    or _stable_key_name(str(client_id)))
        prov, key, key_name, opened = _open_or_create_keyguard_rsa_key(
            win32, key_name=key_name)
        logger.debug("[msi_v2] KeyGuard key=%s opened_existing=%s",
                     key_name, opened)

        # Use cached cert if available
        if cached is not None:
            cert_der = cached.cert_der
            token_endpoint = cached.token_endpoint
            canonical_client_id = cached.client_id
            logger.debug("[msi_v2] Using cached certificate")
        else:
            # 3) Build CSR
            csr_b64 = _build_csr_b64(
                win32, key, str(client_id), str(tenant_id), cu_id)

            # 4) Attestation (if provider given)
            att_jwt = ""
            if attestation_enabled and attestation_token_provider is not None:
                if not attestation_endpoint:
                    raise MsiV2Error(
                        "[msi_v2] attestationEndpoint missing from metadata.")
                try:
                    att_jwt = attestation_token_provider(
                        str(attestation_endpoint),
                        int(key.value),
                        str(client_id),
                        str(key_name))
                except MsiV2Error:
                    raise
                except Exception as exc:
                    raise MsiV2Error(
                        f"[msi_v2] Attestation provider failed: {exc}"
                    ) from exc
                if not att_jwt or not str(att_jwt).strip():
                    raise MsiV2Error(
                        "[msi_v2] Attestation provider returned empty JWT.")

            # 5) issuecredential
            issue_url = base + _ISSUE_CREDENTIAL_PATH
            issue_headers = _imds_headers(corr)
            issue_headers["Content-Type"] = "application/json"

            cred = _imds_post_json(
                http_client, issue_url, params, issue_headers,
                {"csr": csr_b64, "attestation_token": att_jwt})

            cert_b64 = _get_first(cred, "certificate", "Certificate")
            if not cert_b64:
                raise MsiV2Error(
                    f"[msi_v2] issuecredential missing certificate: {cred}")

            try:
                cert_der = base64.b64decode(cert_b64)
            except Exception as exc:
                raise MsiV2Error(
                    "[msi_v2] issuecredential returned invalid base64 "
                    "certificate") from exc

            canonical_client_id = (_get_first(cred, "client_id", "clientId")
                                   or str(client_id))
            token_endpoint = _token_endpoint_from_credential(cred)

            # Cache the cert
            not_after = _try_parse_cert_not_after(cert_der)
            _cert_cache_set(cache_key, _CertCacheEntry(
                cert_der=cert_der,
                cert_pem=_der_to_pem(cert_der),
                token_endpoint=token_endpoint,
                client_id=canonical_client_id,
                not_after=not_after or (time.time() + 8 * 3600),
            ))

        # 6) Create CERT_CONTEXT, bind to KeyGuard private key
        cert_ctx, _, _ = _create_cert_context_with_key(
            win32, cert_der, key, key_name)
        scope = _resource_to_scope(resource)

        # 7) POST token via WinHTTP/SChannel mTLS
        token_json = _acquire_token_mtls_schannel(
            win32, token_endpoint, cert_ctx, canonical_client_id, scope)

        if token_json.get("access_token") and token_json.get("expires_in"):
            cert_pem = _der_to_pem(cert_der)
            cert_thumbprint = get_cert_thumbprint_sha256(cert_pem)

            token_type = token_json.get("token_type") or "mtls_pop"
            access_token = token_json["access_token"]

            result = {
                "access_token": access_token,
                "expires_in": int(token_json["expires_in"]),
                "token_type": token_type,
                "resource": token_json.get("resource"),
                # Legacy fields (kept for backward compat)
                "cert_pem": cert_pem,
                "cert_der_b64": base64.b64encode(
                    cert_der).decode("ascii"),
                "cert_thumbprint_sha256": cert_thumbprint,
            }

            # binding_certificate is only present for mTLS PoP tokens
            # on Windows. For non-mTLS or non-Windows flows it is None.
            if (sys.platform == "win32"
                    and token_type.lower() in ("mtls_pop", "pop")):
                from .windows_certificate import WindowsCertificate

                # Create WindowsCertificate — transfers key/prov ownership
                binding_cert = WindowsCertificate._from_handles(
                    win32, cert_der, key, prov, key_name)
                # Ownership transferred — don't free in finally
                key = None
                prov = None

                result["binding_certificate"] = binding_cert
                result["binding_certificate_metadata"] = (
                    binding_cert.to_metadata_dict())
            else:
                result["binding_certificate"] = None
                result["binding_certificate_metadata"] = None

            return result
        return token_json

    except Exception:
        # On failure, evict cached cert (may be stale/bad)
        _cert_cache_remove(cache_key)
        raise

    finally:
        try:
            if cert_ctx:
                crypt32.CertFreeCertificateContext(cert_ctx)
        except Exception:
            pass
        # Only free if ownership was NOT transferred to WindowsCertificate
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
