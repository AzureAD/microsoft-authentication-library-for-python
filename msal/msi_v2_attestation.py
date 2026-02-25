# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
Windows attestation for MSI v2 KeyGuard keys using AttestationClientLib.dll.

This module calls into AttestationClientLib.dll to mint an attestation JWT for a KeyGuard key handle.
It also provides a small in-memory cache to reuse the attestation JWT until ~90% of its lifetime.

Caching notes:
  - Cache is process-local (in-memory). It does not persist across process restarts.
  - Cache is keyed by (attestation_endpoint, client_id, cache_key/auth_token/payload).
    Provide a stable cache_key (e.g., the named per-boot key name) to maximize hits.
  - If the token cannot be parsed or has no exp claim, it is not cached.

Env vars:
  - ATTESTATION_CLIENTLIB_PATH: absolute path to AttestationClientLib.dll (optional)
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
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_NATIVE_LOG_CB = None

# void LogFunc(void* ctx, const char* tag, int lvl, const char* func, int line, const char* msg);
_LogFunc = ctypes.CFUNCTYPE(None, c_void_p, c_char_p, c_int, c_char_p, c_int, c_char_p)


class AttestationLogInfo(Structure):
    _fields_ = [("Log", c_void_p), ("Ctx", c_void_p)]


def _default_logger(ctx, tag, lvl, func, line, msg):
    try:
        tag_s = tag.decode("utf-8", errors="replace") if tag else ""
        func_s = func.decode("utf-8", errors="replace") if func else ""
        msg_s = msg.decode("utf-8", errors="replace") if msg else ""
        logger.debug("[Native:%s:%s] %s:%s - %s", tag_s, lvl, func_s, line, msg_s)
    except Exception:
        pass


def _truthy_env(name: str, default: str = "1") -> bool:
    val = os.getenv(name, default)
    return (val or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _maybe_add_dll_dirs():
    """Make DLL resolution more reliable (especially for packaged apps)."""
    if sys.platform != "win32":
        return

    add_dir = getattr(os, "add_dll_directory", None)
    if not add_dir:
        return

    for p in (os.path.dirname(sys.executable), os.getcwd(), os.path.dirname(__file__)):
        try:
            if p and os.path.isdir(p):
                add_dir(p)
        except Exception:
            pass


def _load_lib():
    from .managed_identity import MsiV2Error

    if sys.platform != "win32":
        raise MsiV2Error("[msi_v2_attestation] AttestationClientLib is Windows-only.")

    _maybe_add_dll_dirs()

    explicit = os.getenv("ATTESTATION_CLIENTLIB_PATH")
    try:
        if explicit:
            return ctypes.CDLL(explicit)
        return ctypes.CDLL("AttestationClientLib.dll")
    except OSError as exc:
        raise MsiV2Error(
            "[msi_v2_attestation] Unable to load AttestationClientLib.dll. "
            "Place it next to the app/exe or set ATTESTATION_CLIENTLIB_PATH."
        ) from exc


def _b64url_decode(s: str) -> bytes:
    s = (s or "").strip()
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _try_extract_exp_iat(jwt: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Extract exp and iat (Unix seconds) from a JWT without validating signature.
    Returns (exp, iat). Either can be None.
    """
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None, None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8", errors="replace"))
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

        exp = _to_int(payload.get("exp"))
        iat = _to_int(payload.get("iat"))
        return exp, iat
    except Exception:
        return None, None


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
    refresh_after: float  # epoch seconds when we should refresh


_CACHE_LOCK = threading.Lock()
_CACHE: dict[_CacheKey, _CacheEntry] = {}


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

    # Refresh at 90% of lifetime, with a small absolute guard.
    refresh_after = issued_at + (0.90 * lifetime)
    refresh_after = min(refresh_after, exp - 10)

    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(jwt=jwt, exp=exp, refresh_after=float(refresh_after))


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
    Returns attestation JWT string. Raises MsiV2Error on failure.

    cache_key:
      - Optional stable identifier used for caching (recommended: named per-boot key name).
      - If not provided, key_handle is used as part of the cache key (less cache-friendly
        if the key is opened/closed between calls).
    """
    from .managed_identity import MsiV2Error

    if not attestation_endpoint:
        raise MsiV2Error("[msi_v2_attestation] attestation_endpoint must be non-empty")
    if not client_id:
        raise MsiV2Error("[msi_v2_attestation] client_id must be non-empty")
    if not key_handle:
        raise MsiV2Error("[msi_v2_attestation] key_handle must be non-zero")

    stable_cache_key = cache_key if cache_key is not None else f"handle:{int(key_handle)}"
    ck = _CacheKey(
        attestation_endpoint=str(attestation_endpoint),
        client_id=str(client_id),
        cache_key=str(stable_cache_key),
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
        c_void_p,           # keyHandle
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
        raise MsiV2Error(f"[msi_v2_attestation] InitAttestationLib failed: {rc}")

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
            raise MsiV2Error(f"[msi_v2_attestation] AttestKeyGuardImportKey failed: {rc}")
        if not token_ptr.value:
            raise MsiV2Error("[msi_v2_attestation] Attestation token pointer is NULL")

        token = ctypes.string_at(token_ptr.value).decode("utf-8", errors="replace")
        if not token or "." not in token:
            raise MsiV2Error("[msi_v2_attestation] Attestation token looks malformed")

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
