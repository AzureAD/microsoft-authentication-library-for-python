# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
SchannelSession — WinHTTP/SChannel-backed HTTP client for mTLS.

This module provides a requests-like HTTP session that uses Windows native
WinHTTP for TLS, enabling mTLS with non-exportable private keys.

Design:
  - App developer creates SchannelSession with a WindowsCertificate
  - session.get() / session.post() use WinHTTP under the hood
  - The private key never leaves Windows CNG — SChannel performs the
    TLS CertificateVerify signature via the NCRYPT_KEY_HANDLE
  - This is NOT part of MSAL — it's a separate transport for downstream calls
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlencode


class SchannelResponse:
    """Response from a WinHTTP request (requests.Response-like interface)."""

    def __init__(self, status_code: int, body: bytes, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    @property
    def content(self) -> bytes:
        """Raw response body bytes."""
        return self._body

    @property
    def text(self) -> str:
        """Response body as UTF-8 text."""
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse response body as JSON."""
        return json.loads(self._body)

    def raise_for_status(self) -> None:
        """Raise an exception for 4xx/5xx status codes."""
        if 400 <= self.status_code < 600:
            raise SchannelHttpError(
                f"HTTP {self.status_code}: {self.text[:200]}",
                status_code=self.status_code,
                body=self._body,
            )

    def __repr__(self) -> str:
        return f"<SchannelResponse [{self.status_code}]>"


class SchannelHttpError(Exception):
    """HTTP error from a SChannel transport call."""

    def __init__(self, message: str, status_code: int = 0, body: bytes = b""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SchannelSession:
    """
    WinHTTP/SChannel-backed HTTP session for mTLS downstream calls.

    This is the Python equivalent of .NET HttpClient configured with an
    X509Certificate2 for client certificate authentication.

    Args:
        client_certificate: A WindowsCertificate object (from MSAL auth result
            or WindowsCertificate.from_store()). The certificate's private key
            handle is used by SChannel for the TLS handshake.

    Example:
        from msal_schannel_transport import SchannelSession

        with SchannelSession(client_certificate=cert) as session:
            resp = session.get(
                "https://api.example.com/resource",
                headers={"Authorization": "MTLS_POP eyJ..."}
            )
            print(resp.json())
    """

    def __init__(self, client_certificate: Any):
        """
        Create a session for mTLS downstream calls.

        Args:
            client_certificate: A WindowsCertificate object. Must remain open
                (not closed) for the lifetime of this session.

        Raises:
            OSError: if not on Windows.
            TypeError: if client_certificate lacks required interface.
            ValueError: if certificate has no private key.
        """
        if sys.platform != "win32":
            raise OSError("SchannelSession is only supported on Windows")

        self._certificate = client_certificate
        self._cert_ctx: Any = None
        self._win32: Optional[dict] = None
        self._closed = False

        if not hasattr(client_certificate, "create_cert_context"):
            raise TypeError(
                "client_certificate must be a WindowsCertificate instance "
                "(or compatible object with create_cert_context method)")

        if not client_certificate.has_private_key:
            raise ValueError(
                "client_certificate has no private key — cannot perform mTLS")

        self._setup()

    def _setup(self) -> None:
        """Initialize WinHTTP bindings and create CERT_CONTEXT."""
        self._win32 = self._load_winhttp()
        self._cert_ctx = self._certificate.create_cert_context()

    @staticmethod
    def _load_winhttp() -> dict:
        """Load minimal WinHTTP bindings (independent of MSAL internals)."""
        import ctypes
        from ctypes import wintypes

        winhttp = ctypes.WinDLL("winhttp.dll", use_last_error=True)
        crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)

        class CERT_CONTEXT(ctypes.Structure):
            _fields_ = [
                ("dwCertEncodingType", wintypes.DWORD),
                ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbCertEncoded", wintypes.DWORD),
                ("pCertInfo", ctypes.c_void_p),
                ("hCertStore", ctypes.c_void_p),
            ]

        PCCERT_CONTEXT = ctypes.POINTER(CERT_CONTEXT)

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

        crypt32.CertFreeCertificateContext.argtypes = [PCCERT_CONTEXT]
        crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

        return {
            "ctypes": ctypes, "wintypes": wintypes,
            "winhttp": winhttp, "crypt32": crypt32,
            "CERT_CONTEXT": CERT_CONTEXT,
            "WINHTTP_ACCESS_TYPE_DEFAULT_PROXY": 0,
            "WINHTTP_FLAG_SECURE": 0x00800000,
            "WINHTTP_OPTION_CLIENT_CERT_CONTEXT": 47,
            "WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT": 161,
            "WINHTTP_QUERY_STATUS_CODE": 19,
            "WINHTTP_QUERY_FLAG_NUMBER": 0x20000000,
        }

    def get(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> SchannelResponse:
        """HTTP GET with mTLS client certificate."""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode(params)
        return self._request("GET", url, headers=headers or {})

    def post(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        json_body: Optional[Any] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> SchannelResponse:
        """HTTP POST with mTLS client certificate."""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode(params)

        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")

        return self._request("POST", url, headers=headers or {}, body=data)

    def _request(
        self, method: str, url: str, headers: Dict[str, str],
        body: Optional[bytes] = None,
    ) -> SchannelResponse:
        """Execute an HTTP request over WinHTTP with mTLS."""
        if self._closed:
            raise RuntimeError("SchannelSession has been closed")

        # Validate headers against CRLF injection
        for k, v in headers.items():
            if "\r" in k or "\n" in k or ":" in k:
                raise ValueError(
                    f"Invalid header name (contains CR/LF/colon): {k!r}")
            if "\r" in v or "\n" in v:
                raise ValueError(
                    f"Invalid header value for '{k}' (contains CR/LF)")

        ctypes_mod = self._win32["ctypes"]
        wintypes = self._win32["wintypes"]
        winhttp = self._win32["winhttp"]

        u = urlparse(url)
        if u.scheme.lower() != "https":
            raise ValueError(f"SchannelSession requires https, got: {url!r}")
        if not u.hostname:
            raise ValueError(f"Invalid URL: {url!r}")

        host = u.hostname
        port = u.port or 443
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        h_session = winhttp.WinHttpOpen(
            "msal-schannel-transport/0.1",
            self._win32["WINHTTP_ACCESS_TYPE_DEFAULT_PROXY"],
            None, None, 0)
        if not h_session:
            self._raise_last_error("WinHttpOpen failed")

        h_connect = None
        h_request = None
        try:
            # Best-effort HTTP/2 + client cert
            enable = wintypes.DWORD(1)
            try:
                winhttp.WinHttpSetOption(
                    h_session,
                    self._win32["WINHTTP_OPTION_ENABLE_HTTP2_PLUS_CLIENT_CERT"],
                    ctypes_mod.byref(enable), ctypes_mod.sizeof(enable))
            except Exception:
                pass

            h_connect = winhttp.WinHttpConnect(h_session, host, int(port), 0)
            if not h_connect:
                self._raise_last_error("WinHttpConnect failed")

            h_request = winhttp.WinHttpOpenRequest(
                h_connect, method, path, None, None, None,
                self._win32["WINHTTP_FLAG_SECURE"])
            if not h_request:
                self._raise_last_error("WinHttpOpenRequest failed")

            # Attach client certificate for mTLS
            ok = winhttp.WinHttpSetOption(
                h_request,
                self._win32["WINHTTP_OPTION_CLIENT_CERT_CONTEXT"],
                self._cert_ctx,
                ctypes_mod.sizeof(self._win32["CERT_CONTEXT"]))
            if not ok:
                self._raise_last_error("WinHttpSetOption(CLIENT_CERT) failed")

            # Format headers
            header_lines = "".join(
                f"{k}: {v}\r\n" for k, v in headers.items())

            # Send request
            if body:
                body_buf = ctypes_mod.create_string_buffer(body)
                ok = winhttp.WinHttpSendRequest(
                    h_request, header_lines, 0xFFFFFFFF,
                    body_buf, len(body), len(body), 0)
            else:
                ok = winhttp.WinHttpSendRequest(
                    h_request, header_lines, 0xFFFFFFFF,
                    None, 0, 0, 0)
            if not ok:
                self._raise_last_error("WinHttpSendRequest failed")

            ok = winhttp.WinHttpReceiveResponse(h_request, None)
            if not ok:
                self._raise_last_error("WinHttpReceiveResponse failed")

            # Read status code
            status = wintypes.DWORD(0)
            status_size = wintypes.DWORD(ctypes_mod.sizeof(status))
            index = wintypes.DWORD(0)

            ok = winhttp.WinHttpQueryHeaders(
                h_request,
                self._win32["WINHTTP_QUERY_STATUS_CODE"]
                | self._win32["WINHTTP_QUERY_FLAG_NUMBER"],
                None, ctypes_mod.byref(status),
                ctypes_mod.byref(status_size), ctypes_mod.byref(index))
            if not ok:
                self._raise_last_error("WinHttpQueryHeaders(STATUS_CODE) failed")

            # Read response body
            chunks = []
            while True:
                avail = wintypes.DWORD(0)
                ok = winhttp.WinHttpQueryDataAvailable(
                    h_request, ctypes_mod.byref(avail))
                if not ok:
                    self._raise_last_error("WinHttpQueryDataAvailable failed")
                if avail.value == 0:
                    break
                buf = (ctypes_mod.c_ubyte * avail.value)()
                read = wintypes.DWORD(0)
                ok = winhttp.WinHttpReadData(
                    h_request, buf, avail.value, ctypes_mod.byref(read))
                if not ok:
                    self._raise_last_error("WinHttpReadData failed")
                if read.value:
                    chunks.append(bytes(buf[:read.value]))
                if read.value == 0:
                    break

            return SchannelResponse(
                status_code=int(status.value),
                body=b"".join(chunks),
            )
        finally:
            self._close_handle(h_request)
            self._close_handle(h_connect)
            self._close_handle(h_session)

    def _close_handle(self, h: Any) -> None:
        """Close a WinHTTP handle safely."""
        try:
            if h:
                self._win32["winhttp"].WinHttpCloseHandle(h)
        except Exception:
            pass

    def _raise_last_error(self, context: str) -> None:
        """Raise with Win32 last error."""
        ctypes_mod = self._win32["ctypes"]
        err = ctypes_mod.get_last_error()
        detail = ""
        try:
            detail = ctypes_mod.FormatError(err).strip()
        except Exception:
            pass
        raise OSError(
            f"[SchannelSession] {context} (winerror={err} {detail})"
            if detail else
            f"[SchannelSession] {context} (winerror={err})")

    def close(self) -> None:
        """Release the CERT_CONTEXT. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True

        if self._cert_ctx and self._win32:
            try:
                self._win32["crypt32"].CertFreeCertificateContext(
                    self._cert_ctx)
            except Exception:
                pass
        self._cert_ctx = None

    def __enter__(self) -> "SchannelSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<SchannelSession [{state}]>"
