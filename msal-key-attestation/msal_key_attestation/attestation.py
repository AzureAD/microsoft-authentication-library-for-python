# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
Windows attestation for MSI v2 KeyGuard keys using AttestationClientLib.dll.

This module calls into AttestationClientLib.dll to mint an attestation JWT for
a KeyGuard key handle.  It also provides a small in-memory cache to reuse the
attestation JWT until ~90% of its lifetime.

Caching notes:
  - Cache is process-local (in-memory).  Does not persist across process
    restarts.
  - Cache is keyed by (attestation_endpoint, client_id, cache_key).
  - Provide a stable cache_key (e.g., the named per-boot key name) to
    maximize hits.
  - If the token cannot be parsed or has no ``exp`` claim, it is not cached.

Env vars:
  - ATTESTATION_CLIENTLIB_PATH: absolute path to AttestationClientLib.dll
  - MSAL_MSI_V2_ATTESTATION_CACHE: "0" disables caching (default enabled)
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import threading
import time
from ctypes import POINTER, Structure, c_char_p, c_int, c_void_p
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Native callback type — prevent GC of the delegate
# ---------------------------------------------------------------------------

_NATIVE_LOG_CB = None

# void LogFunc(void* ctx, const char* tag, int lvl, const char* func,
#              int line, const char* msg);
_LogFunc = ctypes.CFUNCTYPE(
    None, c_void_p, c_char_p, c_int, c_char_p, c_int, c_char_p)


class AttestationLogInfo(Structure):
    _fields_ = [("Log", c_void_p), ("Ctx", c_void_p)]


def _default_logger(ctx, tag, lvl, func, line, msg):
    try:
        tag_s = tag.decode("utf-8", errors="replace") if tag else ""
        func_s = func.decode("utf-8", errors="replace") if func else ""
        msg_s = msg.decode("utf-8", errors="replace") if msg else ""
        logger.debug("[Native:%s:%s] %s:%s - %s",
                     tag_s, lvl, func_s, line, msg_s)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _truthy_env(name: str, default: str = "1") -> bool:
    val = os.getenv(name, default)
    return (val or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _maybe_add_dll_dirs():
    """Make DLL resolution more reliable (especially for packaged apps).

    Only adds the Python executable directory and the package directory.
    Does NOT add os.getcwd() to avoid DLL preloading/hijacking risk.
    Use ATTESTATION_CLIENTLIB_PATH env var for custom locations.
    """
    if sys.platform != "win32":
        return
    add_dir = getattr(os, "add_dll_directory", None)
    if not add_dir:
        return
    for p in (os.path.dirname(sys.executable),
              os.path.dirname(__file__)):
        try:
            if p and os.path.isdir(p):
                add_dir(p)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DLL loading
# ---------------------------------------------------------------------------

def _load_lib():
    if sys.platform != "win32":
        raise RuntimeError(
            "[msal_key_attestation] AttestationClientLib is Windows-only.")

    _maybe_add_dll_dirs()

    explicit = os.getenv("ATTESTATION_CLIENTLIB_PATH")
    try:
        if explicit:
            return ctypes.CDLL(explicit)
        # Try bundled DLL next to this module first
        bundled = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "AttestationClientLib.dll")
        if os.path.isfile(bundled):
            return ctypes.CDLL(bundled)
        return ctypes.CDLL("AttestationClientLib.dll")
    except OSError as exc:
        raise RuntimeError(
            "[msal_key_attestation] Unable to load AttestationClientLib.dll. "
            "Install msal-key-attestation package or set ATTESTATION_CLIENTLIB_PATH."
        ) from exc


# ---------------------------------------------------------------------------
# JWT parsing (for cache lifetime)
# ---------------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    s = (s or "").strip()
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _try_extract_exp_iat(jwt: str) -> Tuple[Optional[int], Optional[int]]:
    """Extract exp and iat (Unix seconds) from a JWT without validation."""
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None, None
        payload = json.loads(
            _b64url_decode(parts[1]).decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return None, None

        def _to_int(v):
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return v
            if isinstance(v, float):
                return int(v)
            if isinstance(v, str) and v.strip().isdigit():
                return int(v.strip())
            return None

        return _to_int(payload.get("exp")), _to_int(payload.get("iat"))
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# MAA token cache (in-memory, process-local)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _CacheKey:
    attestation_endpoint: str
    client_id: str
    cache_key: str
    auth_token: str
    client_payload: str


@dataclass
class _CacheEntry:
    jwt: str
    exp: int
    refresh_after: float  # epoch seconds


_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}


def _cache_lookup(key: _CacheKey) -> Optional[str]:
    if not _truthy_env("MSAL_MSI_V2_ATTESTATION_CACHE", "1"):
        return None
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if now >= entry.refresh_after or now >= entry.exp - 5:
            return None
        logger.debug("[msal_key_attestation] MAA cache HIT")
        return entry.jwt


def _cache_store(key: _CacheKey, jwt: str) -> None:
    if not _truthy_env("MSAL_MSI_V2_ATTESTATION_CACHE", "1"):
        return
    exp, iat = _try_extract_exp_iat(jwt)
    if exp is None:
        return
    now = int(time.time())
    issued_at = iat if iat is not None else now
    lifetime = exp - issued_at
    if lifetime <= 0:
        return
    # Refresh at 90% of lifetime with small absolute guard
    refresh_after = issued_at + (0.90 * lifetime)
    refresh_after = min(refresh_after, exp - 10)
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(
            jwt=jwt, exp=exp, refresh_after=float(refresh_after))
        logger.debug("[msal_key_attestation] MAA cache SET")


def _cache_clear() -> None:
    """Clear cache (for testing)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Core attestation call
# ---------------------------------------------------------------------------

def get_attestation_jwt(
    *,
    attestation_endpoint: str,
    client_id: str,
    key_handle: int,
    auth_token: str = "",
    client_payload: str = "{}",
    cache_key: Optional[str] = None,
) -> str:
    """
    Get attestation JWT from AttestationClientLib.dll for a KeyGuard key.

    Args:
        attestation_endpoint: MAA endpoint URL.
        client_id: Client ID for attestation.
        key_handle: NCrypt key handle (integer).
        auth_token: Optional auth token for attestation service.
        client_payload: Optional JSON payload.
        cache_key: Stable identifier for caching (recommended: key name).

    Returns:
        Attestation JWT string.

    Raises:
        RuntimeError: on DLL load or attestation failure.
    """
    if not attestation_endpoint:
        raise ValueError(
            "[msal_key_attestation] attestation_endpoint must be non-empty")
    if not client_id:
        raise ValueError(
            "[msal_key_attestation] client_id must be non-empty")
    if not key_handle:
        raise ValueError(
            "[msal_key_attestation] key_handle must be non-zero")

    stable = cache_key if cache_key is not None else f"handle:{key_handle}"
    ck = _CacheKey(
        attestation_endpoint=str(attestation_endpoint),
        client_id=str(client_id),
        cache_key=str(stable),
        auth_token=str(auth_token or ""),
        client_payload=str(client_payload or "{}"),
    )

    cached = _cache_lookup(ck)
    if cached:
        return cached

    lib = _load_lib()

    lib.InitAttestationLib.argtypes = [POINTER(AttestationLogInfo)]
    lib.InitAttestationLib.restype = c_int

    lib.AttestKeyGuardImportKey.argtypes = [
        c_char_p,           # endpoint
        c_char_p,           # authToken
        c_char_p,           # clientPayload
        c_void_p,           # keyHandle (NCRYPT_KEY_HANDLE)
        POINTER(c_void_p),  # out token (char*)
        c_char_p,           # clientId
    ]
    lib.AttestKeyGuardImportKey.restype = c_int

    lib.FreeAttestationToken.argtypes = [c_void_p]
    lib.FreeAttestationToken.restype = None

    lib.UninitAttestationLib.argtypes = []
    lib.UninitAttestationLib.restype = None

    global _NATIVE_LOG_CB  # pylint: disable=global-statement
    _NATIVE_LOG_CB = _LogFunc(_default_logger)

    info = AttestationLogInfo()
    info.Log = ctypes.cast(_NATIVE_LOG_CB, c_void_p).value
    info.Ctx = c_void_p(0)

    rc = lib.InitAttestationLib(ctypes.byref(info))
    if rc != 0:
        raise RuntimeError(
            f"[msal_key_attestation] InitAttestationLib failed: {rc}")

    token_ptr = c_void_p()
    try:
        rc = lib.AttestKeyGuardImportKey(
            attestation_endpoint.encode("utf-8"),
            (auth_token or "").encode("utf-8"),
            (client_payload or "{}").encode("utf-8"),
            c_void_p(int(key_handle)),
            ctypes.byref(token_ptr),
            client_id.encode("utf-8"),
        )
        if rc != 0:
            raise RuntimeError(
                f"[msal_key_attestation] AttestKeyGuardImportKey failed: {rc}")
        if not token_ptr.value:
            raise RuntimeError(
                "[msal_key_attestation] Attestation token pointer is NULL")

        token = ctypes.string_at(token_ptr.value).decode(
            "utf-8", errors="replace")
        if not token or "." not in token:
            raise RuntimeError(
                "[msal_key_attestation] Attestation token looks malformed")

        _cache_store(ck, token)
        return token
    finally:
        try:
            if token_ptr.value:
                lib.FreeAttestationToken(token_ptr)
        finally:
            try:
                lib.UninitAttestationLib()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public factory — matches the callback signature MSAL expects:
#   (endpoint: str, key_handle: int, client_id: str, cache_key: str) -> str
# ---------------------------------------------------------------------------

def create_attestation_provider() -> Callable[[str, int, str, str], str]:
    """
    Create an attestation token provider callable for MSAL MSI v2.

    The returned callable has signature::

        provider(attestation_endpoint: str, key_handle: int,
                 client_id: str, cache_key: str) -> str

    ``cache_key`` should be the stable per-boot key name.  Using the key
    name (rather than the numeric handle) maximizes MAA-token cache hits
    across key re-opens.

    It wraps :func:`get_attestation_jwt` with caching support.

    Usage::

        from msal_key_attestation import create_attestation_provider
        provider = create_attestation_provider()

        # MSAL auto-discovers this when with_attestation_support=True.
        # Or pass explicitly:
        from msal.msi_v2 import obtain_token
        result = obtain_token(
            http_client, managed_identity, resource,
            attestation_token_provider=provider,
        )

    Returns:
        Callable suitable for ``attestation_token_provider`` parameter.
    """
    def _provider(
        attestation_endpoint: str,
        key_handle: int,
        client_id: str,
        cache_key: str = "",
    ) -> str:
        return get_attestation_jwt(
            attestation_endpoint=attestation_endpoint,
            client_id=client_id,
            key_handle=key_handle,
            cache_key=cache_key or None,
        )
    return _provider
