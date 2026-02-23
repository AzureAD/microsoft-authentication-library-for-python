# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
Windows attestation for MSI v2 KeyGuard keys using AttestationClientLib.dll.

Equivalent to your PowerShell / C# P/Invoke signatures:

  int InitAttestationLib(ref AttestationLogInfo info);
  int AttestKeyGuardImportKey(string endpoint, string authToken, string clientPayload,
                              IntPtr keyHandle, out IntPtr token, string clientId);
  void FreeAttestationToken(IntPtr token);
  void UninitAttestationLib();
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import POINTER, Structure, c_char_p, c_int, c_void_p

logger = logging.getLogger(__name__)

# keep callback alive
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


def _maybe_add_dll_dirs():
    """
    Make DLL resolution more reliable (especially for packaged apps).
    """
    if sys.platform != "win32":
        return

    add_dir = getattr(os, "add_dll_directory", None)
    if not add_dir:
        return

    # exe dir
    try:
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir and os.path.isdir(exe_dir):
            add_dir(exe_dir)
    except Exception:
        pass

    # cwd
    try:
        cwd = os.getcwd()
        if cwd and os.path.isdir(cwd):
            add_dir(cwd)
    except Exception:
        pass

    # module dir
    try:
        mod_dir = os.path.dirname(__file__)
        if mod_dir and os.path.isdir(mod_dir):
            add_dir(mod_dir)
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


def get_attestation_jwt(
    *,
    attestation_endpoint: str,
    client_id: str,
    key_handle: int,
    auth_token: str = "",
    client_payload: str = "{}",
) -> str:
    """
    Returns attestation JWT string. Raises MsiV2Error on failure.
    """
    from .managed_identity import MsiV2Error

    if not attestation_endpoint:
        raise MsiV2Error("[msi_v2_attestation] attestation_endpoint must be non-empty")
    if not client_id:
        raise MsiV2Error("[msi_v2_attestation] client_id must be non-empty")
    if not key_handle:
        raise MsiV2Error("[msi_v2_attestation] key_handle must be non-zero")

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
            auth_token.encode("utf-8"),
            client_payload.encode("utf-8"),
            c_void_p(int(key_handle)),
            ctypes.byref(token_ptr),
            client_id.encode("utf-8"),
        )
        if rc != 0:
            raise MsiV2Error(f"[msi_v2_attestation] AttestKeyGuardImportKey failed: {rc}")
        if not token_ptr.value:
            raise MsiV2Error("[msi_v2_attestation] Attestation token pointer is NULL")

        token = ctypes.string_at(token_ptr.value).decode("utf-8", errors="replace")
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