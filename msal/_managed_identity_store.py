# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Best-effort public binding persistence in the Windows CurrentUser/My store."""

import ctypes
from contextlib import contextmanager
from datetime import timezone
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from ._managed_identity_v2 import _endpoint, _V2Error


logger = logging.getLogger(__name__)
_PROPERTY = 0x8000


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]


class _Context(ctypes.Structure):
    _fields_ = [
        ("encoding", ctypes.c_uint32), ("encoded", ctypes.c_void_p),
        ("length", ctypes.c_uint32), ("info", ctypes.c_void_p), ("store", ctypes.c_void_p)]


class _ProviderInfo(ctypes.Structure):
    _fields_ = [
        ("container", ctypes.c_wchar_p), ("provider", ctypes.c_wchar_p),
        ("provider_type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("parameter_count", ctypes.c_uint32), ("parameters", ctypes.c_void_p),
        ("key_spec", ctypes.c_uint32)]


class _CertificateStore:
    def __init__(self):
        self._api = None

    @staticmethod
    def enabled():
        return (sys.platform == "win32" and os.getenv(
            "MSAL_MI_DISABLE_PERSISTENT_CERT_CACHE", "").lower() not in ("1", "true"))

    def _library(self):
        if self._api is None:
            library = ctypes.WinDLL("crypt32", use_last_error=True)
            contracts = {
                "CertOpenStore": ([ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                   ctypes.c_uint32, ctypes.c_void_p], ctypes.c_void_p),
                "CertCloseStore": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
                "CertEnumCertificatesInStore": ([ctypes.c_void_p, ctypes.POINTER(_Context)],
                                                 ctypes.POINTER(_Context)),
                "CertFreeCertificateContext": ([ctypes.POINTER(_Context)], ctypes.c_int),
                "CertGetCertificateContextProperty": ([ctypes.POINTER(_Context), ctypes.c_uint32,
                                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
                                                      ctypes.c_int),
                "CertSetCertificateContextProperty": ([ctypes.POINTER(_Context), ctypes.c_uint32,
                                                       ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int),
                "CertCreateCertificateContext": ([ctypes.c_uint32, ctypes.c_void_p,
                                                   ctypes.c_uint32], ctypes.POINTER(_Context)),
                "CertAddCertificateContextToStore": ([ctypes.c_void_p, ctypes.POINTER(_Context),
                                                      ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int),
                "CertDeleteCertificateFromStore": ([ctypes.POINTER(_Context)], ctypes.c_int),
            }
            for name, (arguments, result) in contracts.items():
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result
            self._api = library
        return self._api

    @contextmanager
    def _open(self, *, write=False):
        library = self._library()
        name = ctypes.c_wchar_p("My")
        flags = 0x10000 | 0x4000 | (0 if write else 0x8000)
        store = library.CertOpenStore(10, 0, None, flags, ctypes.cast(name, ctypes.c_void_p))
        if not store:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield library, store
        finally:
            if not library.CertCloseStore(store, 0):
                logger.warning("Closing the optional managed identity certificate store failed.")

    def _entries(self, library, store):
        context = None
        try:
            while True:
                context = library.CertEnumCertificatesInStore(store, context)
                if not context:
                    error = ctypes.get_last_error() & 0xFFFFFFFF
                    if error != 0x80092004:  # CRYPT_E_NOT_FOUND: normal end of enumeration.
                        raise ctypes.WinError(error)
                    return
                size = ctypes.c_uint32()
                if not library.CertGetCertificateContextProperty(context, _PROPERTY, None, ctypes.byref(size)):
                    continue
                if not 0 < size.value <= 16384:
                    continue
                buffer = ctypes.create_string_buffer(size.value)
                if not library.CertGetCertificateContextProperty(
                        context, _PROPERTY, buffer, ctypes.byref(size)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    metadata = json.loads(buffer.raw[:size.value])
                except (ValueError, UnicodeError):
                    continue
                if isinstance(metadata, dict) and metadata.get("msal_python_v2") == 1:
                    yield context, metadata
        finally:
            if context and not library.CertFreeCertificateContext(context):
                logger.warning("Freeing an optional certificate-store context failed.")

    def read(self, alias, key):
        if not self.enabled():
            return None
        best = None
        try:
            with self._open() as (library, store):
                for context, metadata in self._entries(library, store):
                    if metadata.get("alias") != alias or metadata.get("generation") != key.generation_id:
                        continue
                    encoded = context.contents
                    if not encoded.encoded or not 0 < encoded.length <= 1048576:
                        continue
                    try:
                        certificate = x509.load_der_x509_certificate(
                            ctypes.string_at(encoded.encoded, encoded.length), default_backend())
                        public = certificate.public_key()
                        expires = getattr(certificate, "not_valid_after_utc", None)
                        expires = expires or certificate.not_valid_after.replace(tzinfo=timezone.utc)
                        starts = getattr(certificate, "not_valid_before_utc", None)
                        starts = starts or certificate.not_valid_before.replace(tzinfo=timezone.utc)
                        if (not hasattr(public, "public_numbers")
                                or public.public_numbers() != key.public_key.public_numbers()
                                or starts.timestamp() > time.time()
                                or expires.timestamp() <= time.time() + 86400):
                            continue
                        client = metadata["client_id"]
                        uuid.UUID(client)
                        endpoint = _endpoint(metadata["token_endpoint"], stage="binding")
                        parts = urlsplit(endpoint).path.strip("/").split("/")
                        if len(parts) != 4 or parts[1:] != ["oauth2", "v2.0", "token"]:
                            continue
                        uuid.UUID(parts[0])
                        value = dict(
                            client_id=client, token_endpoint=endpoint,
                            leaf_der=certificate.public_bytes(serialization.Encoding.DER),
                            chain_pem=certificate.public_bytes(serialization.Encoding.PEM),
                            expires_on=expires.timestamp())
                    except (ValueError, TypeError, KeyError, _V2Error):
                        logger.warning("An optional managed identity store entry was unusable.")
                        continue
                    if best is None or value["expires_on"] > best["expires_on"]:
                        best = value
        except OSError:
            logger.warning("Managed identity certificate-store lookup failed; minting instead.")
        return best

    def write(self, alias, key, credential):
        if not self.enabled():
            return False
        metadata = json.dumps(dict(
            msal_python_v2=1, alias=alias, generation=key.generation_id,
            client_id=credential["client_id"], token_endpoint=credential["token_endpoint"]),
            separators=(",", ":")).encode("utf-8")
        names = parse_qs(urlsplit(key.store_uri).query)
        container = names.get("key", [""])[0]
        if not container.startswith("microsoft-identity-native-vbs-"):
            logger.warning("Certificate persistence rejected a non-SDK key container.")
            return False
        context = ctypes.POINTER(_Context)()
        try:
            with self._open(write=True) as (library, store):
                encoded = ctypes.create_string_buffer(credential["leaf_der"])
                context = library.CertCreateCertificateContext(1, encoded, len(credential["leaf_der"]))
                if not context:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    provider = _ProviderInfo(
                        container, "Microsoft Software Key Storage Provider", 0, 0, 0, None, 0xFFFFFFFF)
                    if not library.CertSetCertificateContextProperty(context, 2, 0, ctypes.byref(provider)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    buffer = ctypes.create_string_buffer(metadata)
                    blob = _Blob(len(metadata), ctypes.cast(buffer, ctypes.c_void_p))
                    if not library.CertSetCertificateContextProperty(context, _PROPERTY, 0, ctypes.byref(blob)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if not library.CertAddCertificateContextToStore(store, context, 2, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    if not library.CertFreeCertificateContext(context):
                        logger.warning("Freeing a persisted binding context failed.")
        except OSError:
            logger.warning("Managed identity certificate persistence failed; keeping the live binding.")
            return False
        else:
            if credential.get("expires_on") is not None:
                self.remove(alias, older_than=credential["expires_on"], keep_leaf_der=credential["leaf_der"])
            return True

    def remove(self, alias, *, leaf_der=None, older_than=None, keep_leaf_der=None):
        if not self.enabled():
            return
        # Delete only SDK-tagged exact-alias entries, never unrelated store certificates.
        try:
            with self._open(write=True) as (library, store):
                entries = self._entries(library, store)
                try:
                    for context, metadata in entries:
                        if metadata.get("alias") != alias:
                            continue
                        encoded = context.contents
                        if not encoded.encoded or not 0 < encoded.length <= 1048576:
                            continue
                        actual = ctypes.string_at(encoded.encoded, encoded.length)
                        if actual == keep_leaf_der or leaf_der is not None and actual != leaf_der:
                            continue
                        if older_than is not None:
                            try:
                                certificate = x509.load_der_x509_certificate(actual, default_backend())
                                expiry = getattr(certificate, "not_valid_after_utc", None)
                                expiry = expiry or certificate.not_valid_after.replace(tzinfo=timezone.utc)
                                if expiry.timestamp() > older_than:
                                    continue
                            except ValueError:
                                logger.warning("An SDK-tagged pruning candidate was not a valid certificate.")
                                continue
                        # The delete API consumes a context; retain the enumerator's ownership.
                        duplicate = library.CertDuplicateCertificateContext
                        duplicate.argtypes = [ctypes.POINTER(_Context)]
                        duplicate.restype = ctypes.POINTER(_Context)
                        owned = duplicate(context)
                        if not owned:
                            raise ctypes.WinError(ctypes.get_last_error())
                        if not library.CertDeleteCertificateFromStore(owned):
                            raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    entries.close()
        except OSError:
            logger.warning("Removing an SDK binding from the certificate store failed.")


def _store_alias(policy):
    return hashlib.sha256(json.dumps(policy, separators=(",", ":")).encode("utf-8")).hexdigest()
