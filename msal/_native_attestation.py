# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Optional, separately loaded KeyGuard attestation ABI; no JWT diagnostics."""

import ctypes
import threading

from ._managed_identity_v2 import _V2Error
from ._native_mtls import _NativeRuntime, _verified_manifest


_LOG_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_LIBRARY_LOCK = threading.Lock()


class _LogInfo(ctypes.Structure):
    _fields_ = [("log", _LOG_CALLBACK), ("context", ctypes.c_void_p)]


class _NativeAttestation:
    def __init__(self, directory, *, allow_development=False):
        try:
            root, self.build_id = _verified_manifest(
                directory, allow_development=allow_development,
                artifacts=("AttestationClientLib.dll",),
                manifest_name="attestation-manifest.json")
            self._libraries = _NativeRuntime._load_package(root, ("AttestationClientLib.dll",))
            self._library = library = self._libraries[-1]
            for name, arguments, result in (
                    ("InitAttestationLib", [ctypes.POINTER(_LogInfo)], ctypes.c_int),
                    ("AttestKeyGuardImportKey", [
                        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p], ctypes.c_int),
                    ("FreeAttestationToken", [ctypes.c_void_p], None),
                    ("UninitAttestationLib", [], None)):
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result
        except (OSError, ValueError, AttributeError) as error:
            raise _V2Error(
                "The independently installed KeyGuard attestation package could not be verified or loaded.",
                stage="attestation", error_code="native_dependency_unavailable") from error

    def attest(self, endpoint, key, tenant_id):
        # Native logging strings remain opaque; ctypes must not scan or decode them.
        callback = _LOG_CALLBACK(lambda *_arguments: None)
        info = _LogInfo(callback, None)

        def operation(handle):
            token = ctypes.c_void_p()
            initialized = False
            with _LIBRARY_LOCK:
                try:
                    status = self._library.InitAttestationLib(ctypes.byref(info))
                    if status:
                        raise _V2Error("Native key attestation initialization failed (code {}).".format(status),
                                       stage="attestation", error_code="native_initialization_failed")
                    initialized = True
                    status = self._library.AttestKeyGuardImportKey(
                        endpoint.encode("ascii"), None, None, handle,
                        ctypes.byref(token), tenant_id.encode("ascii"))
                    if status or not token.value:
                        raise _V2Error("Native key attestation failed (code {}).".format(status),
                                       stage="attestation", error_code="attestation_failed")
                    result = bytearray()
                    for offset in range(1048577):
                        value = ctypes.c_ubyte.from_address(token.value + offset).value
                        if value == 0:
                            break
                        result.append(value)
                    else:
                        raise _V2Error("Native key attestation exceeded its token size bound.",
                                       stage="attestation", error_code="attestation_failed")
                    try:
                        value = result.decode("ascii")
                    except UnicodeError:
                        raise _V2Error("Native key attestation returned an invalid JWT.",
                                       stage="attestation", error_code="attestation_failed") from None
                    if not value.strip():
                        raise _V2Error("Native key attestation returned an empty JWT.",
                                       stage="attestation", error_code="attestation_failed")
                    return value
                finally:
                    if token.value:
                        self._library.FreeAttestationToken(token)
                    if initialized:
                        self._library.UninitAttestationLib()

        return key.with_borrowed_handle(operation)
