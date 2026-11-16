# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Bounded Python ownership for optional Windows Rust transport and protected keys."""

import base64
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import sysconfig
import threading
from urllib.parse import quote, urlsplit
import weakref

import requests
from requests.structures import CaseInsensitiveDict

from .managed_identity import ManagedIdentityError


_ABI_VERSION = 1
_MAX_HEADERS = 16
_MAX_HEADER_BYTES = 16384
_MAX_BODY_BYTES = 1048576
_ARTIFACTS = (
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "microsoft_identity_native_provider.dll",
    "microsoft_identity_native_primitives.dll",
)


class _Header(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("value", ctypes.c_char_p)]


class _Request(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("provider_module_path", ctypes.c_char_p),
        ("store_uri", ctypes.c_char_p),
        ("client_certificate_path", ctypes.c_char_p),
        ("ca_bundle_path", ctypes.c_char_p),
        ("host", ctypes.c_char_p),
        ("port", ctypes.c_uint16),
        ("method", ctypes.c_char_p),
        ("target", ctypes.c_char_p),
        ("headers", ctypes.POINTER(_Header)),
        ("header_count", ctypes.c_size_t),
        ("body", ctypes.c_void_p),
        ("body_size", ctypes.c_size_t),
        ("connect_timeout_ms", ctypes.c_uint32),
        ("io_timeout_ms", ctypes.c_uint32),
    ]


class _Response(ctypes.Structure):
    _fields_ = [
        ("http_status", ctypes.c_int),
        ("headers", ctypes.c_void_p),
        ("headers_length", ctypes.c_size_t),
        ("body", ctypes.c_void_p),
        ("body_length", ctypes.c_size_t),
    ]


class _Error(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_int),
        ("system_error", ctypes.c_int),
        ("openssl_error", ctypes.c_uint32),
        ("message", ctypes.c_char * 512),
    ]


class _PublicKey(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("bits", ctypes.c_uint32),
        ("exponent", ctypes.c_uint32),
        ("modulus_length", ctypes.c_uint32),
        ("modulus", ctypes.c_ubyte * 2048),
    ]


class _NativeMtlsError(ManagedIdentityError):
    def __init__(self, message, *, native_code=None, system_error=None, openssl_error=None):
        super().__init__(message)
        self.stage = "native_transport"
        self.native_code = native_code
        self.system_error = system_error
        self.openssl_error = openssl_error


class _NativeKeyError(_NativeMtlsError):
    def __init__(self, message, **kwargs):
        super().__init__(message, **kwargs)
        self.stage = "keyguard"


def _free_owned_key(runtime, handle):
    if handle.value:
        runtime._library.microsoft_identity_native_key_free(handle)


def _encoded(value):
    if not isinstance(value, str) or "\0" in value:
        raise _NativeMtlsError("Native transport strings must be NUL-free text.")
    return value.encode("utf-8")


def _timeouts(timeout):
    values = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    if len(values) != 2 or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 120
            for value in values):
        raise _NativeMtlsError(
            "Native connect/read timeouts must be finite numbers in (0, 120].")
    return tuple(max(1, math.ceil(value * 1000)) for value in values)


def _https_origin(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.netloc.endswith(":")
                or any(ord(character) <= 32 or ord(character) >= 127
                    for character in url)):
            raise ValueError()
        port = 443 if parsed.port is None else parsed.port
        if not 0 < port <= 65535:
            raise ValueError()
        return parsed, (parsed.hostname.lower(), port)
    except (ValueError, TypeError):
        raise _NativeMtlsError(
            "A credential-free absolute HTTPS URL without a fragment is required.") from None


def _verified_manifest(directory, *, allow_development, artifacts=_ARTIFACTS,
                       manifest_name="native-manifest.json"):
    root = Path(directory).resolve(strict=True)
    with (root / manifest_name).open(encoding="utf-8-sig") as stream:
        manifest = json.load(stream)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("abi_version") != _ABI_VERSION
            or not isinstance(manifest.get("build_id"), str)
            or not manifest["build_id"]
            or not isinstance(manifest.get("development"), bool)
            or manifest["development"] and not allow_development
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != set(artifacts)):
        raise _NativeMtlsError(
            "The native package manifest is incompatible or a development build was not enabled.")
    for name in artifacts:
        path = (root / name).resolve(strict=True)
        if path.parent != root or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]:
            raise _NativeMtlsError("Native package integrity verification failed.")
    return root, manifest["build_id"]


class _NativeRuntime:
    def __init__(self, directory, *, allow_development=False):
        if sys.platform != "win32" or sysconfig.get_platform() != "win-amd64":
            raise _NativeMtlsError("The Rust mTLS transport requires Windows x64 Python.")
        if tuple(map(ctypes.sizeof, (_Header, _Request, _Response, _Error))) != (16, 112, 40, 524):
            raise _NativeMtlsError("Python native ABI layouts do not match version 1.")
        try:
            self.directory, self.build_id = _verified_manifest(
                directory, allow_development=allow_development)
            self._libraries = self._load_libraries()
        except _NativeMtlsError:
            raise
        except (OSError, ValueError) as error:
            raise _NativeMtlsError(
                "The Windows native package or an OpenSSL dependency could not be verified or loaded."
            ) from error
        self._library = self._libraries[-1]
        try:
            self._library.microsoft_identity_native_abi_version.argtypes = []
            self._library.microsoft_identity_native_abi_version.restype = ctypes.c_uint32
            if self._library.microsoft_identity_native_abi_version() != _ABI_VERSION:
                raise _NativeMtlsError("The Windows native ABI version is incompatible.")
            self._library.microsoft_identity_native_request.argtypes = [
                ctypes.POINTER(_Request), ctypes.POINTER(_Response), ctypes.POINTER(_Error)]
            self._library.microsoft_identity_native_request.restype = ctypes.c_int
            self._library.microsoft_identity_native_response_free.argtypes = [ctypes.POINTER(_Response)]
            self._library.microsoft_identity_native_response_free.restype = None
        except AttributeError as error:
            raise _NativeMtlsError("The Windows native package is missing a required ABI export.") from error

    def _load_libraries(self):
        return self._load_package(self.directory, _ARTIFACTS)

    @staticmethod
    def _load_package(directory, artifacts):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        kernel.GetModuleFileNameW.restype = ctypes.c_uint32
        libraries = []
        for name in artifacts:
            expected = directory / name
            existing = kernel.GetModuleHandleW(name)
            if existing:
                _NativeRuntime._verify_loaded_path(kernel, existing, expected)
            # Verified package dependencies are loaded in order; other imports
            # may come from System32, never an unmanifested neighboring DLL.
            library = ctypes.CDLL(str(expected), winmode=0x00000800)
            _NativeRuntime._verify_loaded_path(kernel, library._handle, expected)
            libraries.append(library)
        return libraries

    @staticmethod
    def _verify_loaded_path(kernel, handle, expected):
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
        if not length or length >= len(buffer) or Path(buffer.value).resolve() != expected:
            raise _NativeMtlsError("A conflicting native dependency is already loaded from another location.")

    def request(self, *, store_uri, certificate_path, ca_bundle_path,
                method, url, headers, body=b"", timeout=10):
        parsed, origin = _https_origin(url)
        connect_ms, io_ms = _timeouts(timeout)
        if not isinstance(body, bytes) or len(body) > _MAX_BODY_BYTES:
            raise _NativeMtlsError("Native request bodies must be bounded bytes, not streams.")
        if len(headers) > _MAX_HEADERS:
            raise _NativeMtlsError("The native request has too many headers.")
        header_storage = (_Header * len(headers))(*(
            _Header(_encoded(name), _encoded(value)) for name, value in headers.items()))
        body_storage = ctypes.create_string_buffer(body)
        request = _Request(
            ctypes.sizeof(_Request), _ABI_VERSION,
            _encoded(str(self.directory / "microsoft_identity_native_provider.dll")),
            _encoded(store_uri), _encoded(os.fspath(certificate_path)),
            _encoded(os.fspath(ca_bundle_path)), _encoded(origin[0]), origin[1],
            _encoded(method), _encoded((parsed.path or "/") + (
                "?" + parsed.query if parsed.query else "")),
            header_storage, len(headers), ctypes.cast(body_storage, ctypes.c_void_p),
            len(body), connect_ms, io_ms)
        response, error = _Response(), _Error()
        success = self._library.microsoft_identity_native_request(
            ctypes.byref(request), ctypes.byref(response), ctypes.byref(error))
        if not success:
            raise _NativeMtlsError(
                "Native mTLS request failed (category {}, system {}).".format(
                    error.code, error.system_error),
                native_code=error.code, system_error=error.system_error,
                openssl_error=error.openssl_error)
        try:
            if (not 100 <= response.http_status <= 599
                    or response.headers_length > _MAX_HEADER_BYTES + 2
                    or response.body_length > _MAX_BODY_BYTES
                    or response.headers_length and not response.headers
                    or response.body_length and not response.body):
                raise _NativeMtlsError("The native response violates the bounded ABI contract.")
            header_bytes = ctypes.string_at(response.headers, response.headers_length)
            result = requests.Response()
            result.status_code = response.http_status
            result.headers = CaseInsensitiveDict()
            for line in header_bytes.decode("ascii").split("\r\n"):
                if line:
                    name, value = line.split(":", 1)
                    result.headers[name] = value.strip()
            result._content = ctypes.string_at(response.body, response.body_length)
            result._content_consumed = True
            result.encoding = requests.utils.get_encoding_from_headers(result.headers)
            result.url = url
            return result
        finally:
            self._library.microsoft_identity_native_response_free(ctypes.byref(response))


class _NativeKey:
    """Owns the Rust key object, never private-key bytes or the borrowed OS handle."""

    def __init__(self, runtime, name):
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self._runtime = runtime
        self._lock = threading.RLock()
        self._closed = False
        self._borrow_depth = 0
        self._library = library = runtime._library
        if ctypes.sizeof(_PublicKey) != 2068:
            raise _NativeKeyError("Python protected-key ABI layout does not match version 1.")
        contracts = {
            "key_open_protected": [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(_Error)],
            "key_public": [ctypes.c_void_p, ctypes.POINTER(_PublicKey), ctypes.POINTER(_Error)],
            "key_sign_sha256_pss": [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_Error)],
            "key_borrow_handle": [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_Error)],
            "key_free": [ctypes.c_void_p],
        }
        try:
            for name_suffix, arguments in contracts.items():
                function = getattr(library, "microsoft_identity_native_" + name_suffix)
                function.argtypes = arguments
                function.restype = None if name_suffix == "key_free" else ctypes.c_int
        except AttributeError as error:
            raise _NativeKeyError(
                "The installed Rust native package lacks protected-key primitives; install version 0.3 or later."
            ) from error
        self._handle = ctypes.c_void_p()
        self._finalizer = weakref.finalize(self, _free_owned_key, runtime, self._handle)
        self._call(library.microsoft_identity_native_key_open_protected,
                   _encoded(name), ctypes.byref(self._handle))
        if not self._handle.value:
            raise _NativeKeyError("The native key open returned no owned object.")
        complete = False
        try:
            public = _PublicKey(ctypes.sizeof(_PublicKey), _ABI_VERSION)
            self._call(library.microsoft_identity_native_key_public, self._handle, ctypes.byref(public))
            if (public.struct_size != ctypes.sizeof(_PublicKey) or public.abi_version != _ABI_VERSION
                    or public.bits != 2048 or public.modulus_length != 256
                    or not 3 <= public.exponent <= 0xFFFFFFFF or not public.exponent & 1):
                raise _NativeKeyError("The native protected public key violates the expected RSA contract.")
            modulus = int.from_bytes(bytes(public.modulus[:public.modulus_length]), "big")
            if modulus.bit_length() != public.bits:
                raise _NativeKeyError("The native public modulus does not match its bit length.")
            self.public_key = rsa.RSAPublicNumbers(public.exponent, modulus).public_key(
                backend=default_backend())
            spki = self.public_key.public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            self.generation_id = base64.urlsafe_b64encode(
                hashlib.sha256(spki).digest()).rstrip(b"=").decode("ascii")
            self.store_uri = "cng://?key={}&provider={}".format(
                quote(name, safe=""), quote("Microsoft Software Key Storage Provider", safe=""))
            complete = True
        finally:
            if not complete:
                self.close()

    @staticmethod
    def _call(function, *arguments):
        error = _Error()
        if not function(*arguments, ctypes.byref(error)):
            raise _NativeKeyError(
                "Protected-key operation failed (category {}, system {}); no fallback was used.".format(
                    error.code, error.system_error),
                native_code=error.code, system_error=error.system_error)

    def sign_sha256_pss(self, digest):
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise _NativeKeyError("Protected signing requires an exactly 32-byte SHA-256 digest.")
        with self._lock:
            self._require_open()
            digest_buffer = ctypes.create_string_buffer(digest)
            signature = (ctypes.c_ubyte * 256)()
            written = ctypes.c_size_t()
            self._call(
                self._library.microsoft_identity_native_key_sign_sha256_pss,
                self._handle, ctypes.cast(digest_buffer, ctypes.c_void_p), len(digest),
                ctypes.cast(signature, ctypes.c_void_p), len(signature), ctypes.byref(written))
            if written.value != 256:
                raise _NativeKeyError("The native protected signature length is invalid.")
            return bytes(signature)

    def with_borrowed_handle(self, operation):
        with self._lock:
            self._require_open()
            handle = ctypes.c_size_t()
            self._call(self._library.microsoft_identity_native_key_borrow_handle,
                       self._handle, ctypes.byref(handle))
            if not handle.value:
                raise _NativeKeyError("The protected-key primitive returned an invalid borrowed handle.")
            self._borrow_depth += 1
            try:
                return operation(handle.value)
            finally:
                self._borrow_depth -= 1

    def _require_open(self):
        if self._closed:
            raise _NativeKeyError("The protected-key object is closed.")

    def close(self):
        with self._lock:
            if not self._closed:
                if self._borrow_depth:
                    raise _NativeKeyError("Cannot close a key while its native handle is borrowed.")
                self._closed = True
                self._finalizer()
