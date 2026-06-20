# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
WindowsCertificate — Python equivalent of .NET X509Certificate2 for
platform-backed certificates with non-exportable private keys.

This class holds a reference to a certificate in the Windows certificate
store (or an in-memory CERT_CONTEXT) and the associated CNG key handle.
The private key is NEVER exported — all signing/TLS operations are
delegated to Windows CNG/SChannel via the native handle.

Usage:
    # From store (for app developers)
    cert = WindowsCertificate.from_store(
        store_path="CurrentUser/My",
        thumbprint="7C0F1A2B3C4D5E6F7890ABCDEF1234567890ABCDE",
    )

    # From internal MSAL flow (returned in auth result)
    cert = WindowsCertificate._from_handles(win32, cert_der, key_handle, ...)

    # App developer uses cert for downstream mTLS via compatible transport
    session = SomeSchannelTransport(client_certificate=cert)
    session.get(url, headers={"Authorization": f"{result['token_type']} {result['access_token']}"})
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sys
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WindowsCertificate:
    """
    Platform-backed certificate reference (Python equivalent of X509Certificate2).

    This object:
      - Holds the public certificate metadata (thumbprint, subject, issuer, DER bytes)
      - Maintains a live reference to the CNG private key handle
      - Can create a CERT_CONTEXT bound to the key for SChannel/WinHTTP usage
      - NEVER exposes private key bytes

    The object is safe to pass to an mTLS transport for downstream API calls.
    It implements context manager protocol for deterministic handle cleanup.
    """

    def __init__(self):
        """Use factory methods (from_store, _from_handles) instead."""
        self._cert_der: bytes = b""
        self._key_handle: Any = None  # NCRYPT_KEY_HANDLE (c_void_p)
        self._prov_handle: Any = None  # NCRYPT_PROV_HANDLE (c_void_p)
        self._key_name: str = ""
        self._store_path: str = ""
        self._win32: Optional[dict] = None
        self._closed = False
        self._owns_key_handle = True  # Whether we should free the key handle
        self._lock = threading.Lock()

    @classmethod
    def from_store(
        cls,
        store_path: str = "CurrentUser/My",
        *,
        thumbprint: Optional[str] = None,
        subject_name: Optional[str] = None,
    ) -> "WindowsCertificate":
        """
        Open a certificate from the Windows certificate store.

        Args:
            store_path: Certificate store location (e.g., "CurrentUser/My",
                "LocalMachine/My").
            thumbprint: SHA-1 thumbprint (hex, case-insensitive) to select
                the certificate. Preferred selector.
            subject_name: Subject CN to match. Less precise than thumbprint.

        Returns:
            WindowsCertificate with the key handle opened.

        Raises:
            OSError: if certificate not found or key not accessible.
            ValueError: if arguments are invalid.
        """
        if sys.platform != "win32":
            raise OSError(
                "WindowsCertificate.from_store() is only supported on Windows")

        if not thumbprint and not subject_name:
            raise ValueError(
                "Either 'thumbprint' or 'subject_name' must be provided")

        from .msi_v2 import _load_win32, _raise_win32_last_error

        win32 = _load_win32()
        ctypes_mod = win32["ctypes"]
        crypt32 = win32["crypt32"]

        # Parse store_path: "CurrentUser/My" -> (location_flag, "My")
        parts = store_path.replace("\\", "/").split("/", 1)
        if len(parts) != 2:
            raise ValueError(
                f"store_path must be 'Location/StoreName', got: {store_path!r}")

        location_str, store_name = parts
        location_map = {
            "currentuser": 0x00010000,   # CERT_SYSTEM_STORE_CURRENT_USER
            "localmachine": 0x00020000,  # CERT_SYSTEM_STORE_LOCAL_MACHINE
        }
        location_flag = location_map.get(location_str.lower())
        if location_flag is None:
            raise ValueError(
                f"Unsupported store location: {location_str!r}. "
                f"Use 'CurrentUser' or 'LocalMachine'.")

        # Use CERT_STORE_PROV_SYSTEM_W (numeric 10) with CERT_STORE_READONLY_FLAG
        CERT_STORE_PROV_SYSTEM_W = ctypes_mod.c_void_p(10)
        CERT_STORE_READONLY_FLAG = 0x00008000

        h_store = crypt32.CertOpenStore(
            CERT_STORE_PROV_SYSTEM_W,
            0,
            None,
            location_flag | CERT_STORE_READONLY_FLAG,
            ctypes_mod.c_wchar_p(store_name),
        )
        if not h_store:
            _raise_win32_last_error(
                f"[WindowsCertificate] CertOpenStore failed for {store_path}")

        cert_ctx = None
        try:
            cert_ctx = cls._find_cert_in_store(
                win32, h_store, thumbprint=thumbprint,
                subject_name=subject_name)
            if not cert_ctx:
                selector = thumbprint or subject_name
                raise OSError(
                    f"[WindowsCertificate] Certificate not found in "
                    f"{store_path} with selector: {selector}")

            # Extract DER from context
            cert_info = ctypes_mod.cast(
                cert_ctx,
                ctypes_mod.POINTER(win32["CERT_CONTEXT"])
            ).contents
            cert_der = ctypes_mod.string_at(
                cert_info.pbCertEncoded, cert_info.cbCertEncoded)

            # Acquire private key handle
            key_handle = ctypes_mod.c_void_p()
            key_spec = ctypes_mod.c_ulong()
            caller_must_free = ctypes_mod.c_int()

            CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG = 0x00040000
            ok = crypt32.CryptAcquireCertificatePrivateKey(
                cert_ctx,
                CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG,
                None,
                ctypes_mod.byref(key_handle),
                ctypes_mod.byref(key_spec),
                ctypes_mod.byref(caller_must_free),
            )
            if not ok or not key_handle.value:
                _raise_win32_last_error(
                    "[WindowsCertificate] CryptAcquireCertificatePrivateKey "
                    "failed — private key may not be accessible")

            # Verify we got an NCrypt key (CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF)
            CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF
            if key_spec.value != CERT_NCRYPT_KEY_SPEC:
                # Got a legacy CryptoAPI handle, not CNG — not supported
                raise OSError(
                    f"[WindowsCertificate] Certificate has a legacy CryptoAPI "
                    f"key (spec={key_spec.value}), CNG key required")

            # Build the object
            obj = cls()
            obj._cert_der = bytes(cert_der)
            obj._key_handle = key_handle
            obj._prov_handle = None
            obj._key_name = ""
            obj._store_path = store_path
            obj._win32 = win32
            # Track whether we own the key handle
            obj._owns_key_handle = bool(caller_must_free.value)
            return obj

        finally:
            # Free the CERT_CONTEXT from the store search (we extracted DER)
            if cert_ctx:
                try:
                    crypt32.CertFreeCertificateContext(cert_ctx)
                except Exception:
                    pass
            crypt32.CertCloseStore(h_store, 0)

    @classmethod
    def _from_handles(
        cls,
        win32: dict,
        cert_der: bytes,
        key_handle: Any,
        prov_handle: Any,
        key_name: str,
    ) -> "WindowsCertificate":
        """
        Internal: create from existing NCrypt handles (used by obtain_token).

        The caller transfers ownership of key_handle and prov_handle to this
        object. They will be freed when the WindowsCertificate is closed.
        """
        obj = cls()
        obj._cert_der = bytes(cert_der)
        obj._key_handle = key_handle
        obj._prov_handle = prov_handle
        obj._key_name = key_name
        obj._store_path = ""
        obj._win32 = win32
        return obj

    # ------------------------------------------------------------------
    # Public properties (safe to access, no private key exposure)
    # ------------------------------------------------------------------

    @property
    def thumbprint_sha1(self) -> str:
        """SHA-1 thumbprint of the certificate (hex uppercase)."""
        return hashlib.sha1(self._cert_der).hexdigest().upper()

    @property
    def thumbprint_sha256(self) -> str:
        """SHA-256 thumbprint of the certificate (hex uppercase)."""
        return hashlib.sha256(self._cert_der).hexdigest().upper()

    @property
    def x5t_s256(self) -> str:
        """Base64url-encoded SHA-256 thumbprint (for cnf claim matching)."""
        digest = hashlib.sha256(self._cert_der).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @property
    def public_certificate_der(self) -> bytes:
        """DER-encoded public certificate bytes."""
        return self._cert_der

    @property
    def public_certificate_pem(self) -> str:
        """PEM-encoded public certificate."""
        b64 = base64.b64encode(self._cert_der).decode("ascii")
        lines = [b64[i:i+64] for i in range(0, len(b64), 64)]
        return (
            "-----BEGIN CERTIFICATE-----\n"
            + "\n".join(lines)
            + "\n-----END CERTIFICATE-----\n"
        )

    @property
    def has_private_key(self) -> bool:
        """Whether a private key handle is available."""
        return self._key_handle is not None and not self._closed

    @property
    def store_path(self) -> str:
        """The store path this certificate was loaded from (if any)."""
        return self._store_path

    @property
    def key_name(self) -> str:
        """The CNG key name (if known)."""
        return self._key_name

    # ------------------------------------------------------------------
    # Native handle access (for transports that need CERT_CONTEXT)
    # ------------------------------------------------------------------

    def create_cert_context(self) -> Any:
        """
        Create a new CERT_CONTEXT bound to this certificate's private key.

        The caller is responsible for freeing the returned CERT_CONTEXT
        via CertFreeCertificateContext when done.

        The CERT_CONTEXT references (but does NOT own) the private key handle.
        The WindowsCertificate must remain open while the CERT_CONTEXT is in use.

        Returns:
            PCCERT_CONTEXT (ctypes pointer) with private key bound.

        Raises:
            RuntimeError: if the certificate has been closed.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    "WindowsCertificate has been closed — cannot create "
                    "CERT_CONTEXT")
            if not self._key_handle:
                raise RuntimeError(
                    "WindowsCertificate has no private key handle")

            from .msi_v2 import _create_cert_context_with_key

            cert_ctx, _, _ = _create_cert_context_with_key(
                self._win32, self._cert_der, self._key_handle, self._key_name)
            return cert_ctx

    @property
    def _native_key_handle(self) -> Any:
        """Internal: raw NCRYPT_KEY_HANDLE for signing operations."""
        if self._closed:
            raise RuntimeError("WindowsCertificate has been closed")
        return self._key_handle

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release native handles. Safe to call multiple times."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

            if self._win32:
                ncrypt = self._win32.get("ncrypt")
                if ncrypt:
                    if self._key_handle and self._owns_key_handle:
                        try:
                            ncrypt.NCryptFreeObject(self._key_handle)
                        except Exception:
                            pass
                    if self._prov_handle:
                        try:
                            ncrypt.NCryptFreeObject(self._prov_handle)
                        except Exception:
                            pass

            self._key_handle = None
            self._prov_handle = None

    def __enter__(self) -> "WindowsCertificate":
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
        tp = self.thumbprint_sha1[:16] + "..." if self._cert_der else "empty"
        return f"<WindowsCertificate {tp} [{state}]>"

    # ------------------------------------------------------------------
    # Serialization helpers (for auth result metadata — no private key)
    # ------------------------------------------------------------------

    def to_metadata_dict(self) -> dict:
        """
        Return JSON-safe metadata about this certificate.
        Safe for logging and cross-process diagnostics.
        """
        return {
            "store_path": self._store_path,
            "thumbprint_sha1": self.thumbprint_sha1,
            "thumbprint_sha256": self.thumbprint_sha256,
            "x5t#S256": self.x5t_s256,
            "has_private_key": self.has_private_key,
            "key_name": self._key_name or None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_cert_in_store(
        win32: dict, h_store: Any, *,
        thumbprint: Optional[str] = None,
        subject_name: Optional[str] = None,
    ) -> Any:
        """Find a certificate in an open store by thumbprint or subject."""
        ctypes_mod = win32["ctypes"]
        crypt32 = win32["crypt32"]

        if thumbprint:
            # Normalize: remove spaces, colons, dashes
            normalized = thumbprint.replace(" ", "").replace(":", "").replace("-", "")
            if len(normalized) != 40:
                raise ValueError(
                    f"thumbprint must be a 40-character SHA-1 hex string, "
                    f"got {len(normalized)} characters after normalization")
            thumb_bytes = bytes.fromhex(normalized)
            blob = win32["CRYPT_HASH_BLOB"]()
            buf = ctypes_mod.create_string_buffer(thumb_bytes)
            blob.cbData = len(thumb_bytes)
            blob.pbData = ctypes_mod.cast(buf, ctypes_mod.c_void_p)

            CERT_FIND_HASH = 0x10000  # CERT_FIND_SHA1_HASH
            ctx = crypt32.CertFindCertificateInStore(
                h_store,
                win32["X509_ASN_ENCODING"] | win32["PKCS_7_ASN_ENCODING"],
                0,
                CERT_FIND_HASH,
                ctypes_mod.byref(blob),
                None,
            )
            return ctx

        if subject_name:
            CERT_FIND_SUBJECT_STR = 0x00080007
            ctx = crypt32.CertFindCertificateInStore(
                h_store,
                win32["X509_ASN_ENCODING"] | win32["PKCS_7_ASN_ENCODING"],
                0,
                CERT_FIND_SUBJECT_STR,
                ctypes_mod.c_wchar_p(subject_name),
                None,
            )
            return ctx

        return None
