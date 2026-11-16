# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Generation-specific requests transport for the blocking Rust transaction ABI."""

import base64
import asyncio
import ctypes
import hashlib
import logging
import os
from pathlib import Path
import tempfile
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import urlencode

import requests
from requests.adapters import BaseAdapter

from ._managed_identity_v2 import _V2Error
from ._native_mtls import _NativeMtlsError, _https_origin


logger = logging.getLogger(__name__)


@contextmanager
def _locked_chain(path, digest):
    """Keep the exact verified file readable but not writable/deletable during TLS."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.ReadFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    kernel.ReadFile.restype = ctypes.c_int
    kernel.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise _V2Error("The binding chain could not be locked for a native request.",
                       stage="binding", error_code="chain_integrity_failed")
    try:
        attributes = (ctypes.c_uint32 * 2)()
        if (not kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes))
                or attributes[0] & 0x400):
            raise _V2Error("The binding chain is not a verified regular file.",
                           stage="binding", error_code="chain_integrity_failed")
        actual = hashlib.sha256()
        buffer, written, total = (ctypes.c_ubyte * 65536)(), ctypes.c_uint32(), 0
        while True:
            if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(written), None):
                raise _V2Error("The locked binding chain could not be read.",
                               stage="binding", error_code="chain_integrity_failed")
            if not written.value:
                break
            total += written.value
            if written.value > len(buffer) or total > 1048576:
                raise _V2Error("The binding chain exceeds its size bound.",
                               stage="binding", error_code="chain_integrity_failed")
            actual.update(bytes(buffer[:written.value]))
        if actual.digest() != digest:
            raise _V2Error("The binding chain changed after issuance.",
                           stage="binding", error_code="chain_integrity_failed")
        yield
    finally:
        if not kernel.CloseHandle(handle):
            logger.error("Closing a binding-chain read lock failed.")


class _BindingCertificate:
    """Process-local certificate generation; private material stays in CNG.

    Sessions buffer responses and never redirect, proxy, stream or accept a
    replacement client certificate. Only the acquisition resource origin can
    receive a bound resource request.
    """
    def __init__(self, runtime, key, credential, resource, ca_bundle_path, *, attested):
        self.leaf_der = credential["leaf_der"]
        self.key_id = base64.urlsafe_b64encode(
            hashlib.sha256(self.leaf_der).digest()).rstrip(b"=").decode("ascii")
        self.binding_strength, self.attested = 3, attested
        self.expires_on = credential["expires_on"]
        self.client_id, self.token_endpoint = credential["client_id"], credential["token_endpoint"]
        self._runtime, self._key = runtime, key
        self._resource_origin = _https_origin(resource)[1] if resource is not None else None
        self._token_origin = _https_origin(self.token_endpoint)[1]
        self._ca_bundle_path = ca_bundle_path
        self._lock, self._active, self._closed = threading.RLock(), 0, False
        self._temporary = tempfile.TemporaryDirectory(prefix="msal-mi-public-chain-")
        self._chain_path = Path(self._temporary.name) / "chain.pem"
        with self._chain_path.open("xb") as stream:
            stream.write(credential["chain_pem"])
        self._chain_hash = hashlib.sha256(credential["chain_pem"]).digest()

    @contextmanager
    def _lease(self):
        with self._lock:
            if self._closed:
                raise _V2Error("The binding certificate is closed.",
                               stage="binding", error_code="binding_closed")
            self._active += 1
        try:
            with _locked_chain(self._chain_path, self._chain_hash):
                yield
        finally:
            with self._lock:
                self._active -= 1
                if self._closed and not self._active:
                    self._cleanup()

    def _cleanup(self):
        self._temporary.cleanup()
        self._key = None

    def close(self):
        with self._lock:
            self._closed = True
            if not self._active:
                self._cleanup()

    def _request(self, method, url, *, headers, body=b"", timeout=10,
                 token_request=False, resource_origin=None):
        origin = _https_origin(url)[1]
        if origin != (self._token_origin if token_request else resource_origin or self._resource_origin):
            raise _V2Error("The binding cannot forward credentials to a different origin.",
                           stage="binding", error_code="origin_mismatch")
        with self._lease():
            try:
                return self._runtime.request(
                    store_uri=self._key.store_uri, certificate_path=self._chain_path,
                    ca_bundle_path=self._ca_bundle_path, method=method, url=url,
                    headers=headers, body=body, timeout=timeout)
            except _NativeMtlsError as error:
                error.stage = "ests" if token_request else "resource"
                raise

    def post(self, url, *, headers, data, params=None, **kwargs):
        if params or kwargs.keys() - {"timeout"}:
            raise _V2Error("The mTLS token request cannot override its endpoint query.",
                           stage="ests", error_code="invalid_request")
        result = self._request(
            "POST", url, headers=dict(headers, **{"Content-Type": "application/x-www-form-urlencoded"}),
            body=urlencode(data).encode("ascii"), token_request=True,
            timeout=kwargs.pop("timeout", 10))
        return result

    def open_session(self, *, resource_origin=None):
        """Bind this generation to one HTTPS origin, defaulting to the audience.

        For audiences such as ``https://vault.azure.net``, explicitly select
        the actual vault origin rather than treating the audience as a wildcard.
        """
        return _BoundSession(self, resource_origin=resource_origin)

    def open_async_session(self, *, max_concurrency=4, resource_origin=None):
        """Return a buffered worker-backed adapter, not an aiohttp SSL context."""
        return _AsyncBoundSession(self, max_concurrency, resource_origin=resource_origin)


class _BoundAdapter(BaseAdapter):
    def __init__(self, binding, *, resource_origin=None):
        self.binding = binding
        self._origin = _https_origin(resource_origin)[1] if resource_origin is not None else None

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if stream or cert is not None or proxies or verify is False:
            raise _V2Error("Bound requests do not support streaming, proxies or replacement TLS credentials.",
                           stage="binding", error_code="unsupported_transport_option")
        if isinstance(verify, (str, os.PathLike)) and os.fspath(verify) != os.fspath(self.binding._ca_bundle_path):
            raise _V2Error("The bound request must retain its configured trust bundle.",
                           stage="binding", error_code="unsupported_transport_option")
        body = request.body or b""
        body = body.encode("utf-8") if isinstance(body, str) else body
        if not isinstance(body, bytes):
            raise _V2Error("Bound request bodies must be bounded bytes, not streams.",
                           stage="binding", error_code="unsupported_transport_option")
        headers = dict(request.headers)
        length = headers.pop("Content-Length", None)
        if length is not None and (not length.isdecimal() or int(length) != len(body)):
            raise _V2Error("The bound request has inconsistent Content-Length framing.",
                           stage="binding", error_code="invalid_request")
        result = self.binding._request(
            request.method, request.url, headers=headers, body=body,
            timeout=10 if timeout is None else timeout, resource_origin=self._origin)
        result.request = request
        return result

    def close(self):
        pass  # Each native transaction already closes its own TLS connection.


class _BoundSession(requests.Session):
    def __init__(self, binding, *, resource_origin=None):
        super().__init__()
        for adapter in self.adapters.values():
            adapter.close()
        self.adapters.clear()
        self.headers.clear()
        self.headers["Accept"] = "*/*"
        self.trust_env = False
        self.verify = binding._ca_bundle_path
        self.mount("https://", _BoundAdapter(binding, resource_origin=resource_origin))

    def send(self, request, **kwargs):
        kwargs["allow_redirects"] = False
        return super().send(request, **kwargs)


class _AsyncBoundSession:
    def __init__(self, binding, concurrency, *, resource_origin=None):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
            raise _V2Error("Async binding concurrency must be an integer in [1, 32].",
                           stage="binding", error_code="unsupported_transport_option")
        self._binding = binding
        self._adapter = _BoundAdapter(binding, resource_origin=resource_origin)
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="msal-mi-mtls")
        self._slots = asyncio.Semaphore(concurrency)
        self._loop, self._closed, self._inflight = None, False, set()

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise _V2Error("An async binding session must stay on its original event loop.",
                           stage="binding", error_code="unsupported_transport_option")
        self._loop = loop
        return loop

    async def request(self, method, url, *, headers=None, data=None, json=None,
                      params=None, timeout=10, allow_redirects=False):
        loop = self._check_loop()
        if allow_redirects:
            raise _V2Error("Bound async requests cannot follow redirects.",
                           stage="binding", error_code="unsupported_transport_option")
        await self._slots.acquire()
        if self._closed:
            self._slots.release()
            raise _V2Error("The async binding session is closed.",
                           stage="binding", error_code="binding_closed")
        try:
            prepared = requests.Request(method, url, headers=headers, data=data,
                                        json=json, params=params).prepare()
            operation = partial(self._adapter.send, prepared, timeout=timeout,
                                verify=self._binding._ca_bundle_path)
            future = loop.run_in_executor(self._executor, operation)
        except BaseException:
            self._slots.release()
            raise
        self._inflight.add(future)
        future.add_done_callback(self._completed)
        # Cancellation must not release the native call's slot, buffers or binding.
        return await asyncio.shield(future)

    def _completed(self, future):
        self._inflight.discard(future)
        self._slots.release()
        if not future.cancelled():
            future.exception()

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def aclose(self):
        self._check_loop()
        self._closed = True
        self._executor.shutdown(wait=False)
        if self._inflight:
            await asyncio.shield(asyncio.gather(*self._inflight, return_exceptions=True))

    async def __aenter__(self):
        self._check_loop()
        return self

    async def __aexit__(self, *_exception):
        await self.aclose()
