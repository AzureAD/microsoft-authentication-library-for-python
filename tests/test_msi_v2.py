# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""Tests for MSI v2 (mTLS PoP) implementation.

Goals:
- Provide strong unit coverage without depending on KeyGuard / real IMDS.
- Validate:
  * x5t#S256 helper correctness (local)
  * verify_cnf_binding behavior (msal.msi_v2)
  * Certificate cache behavior
  * ManagedIdentityClient strict gating behavior
  * IMDS wire-contract helpers
"""

import base64
import datetime
import hashlib
import json
import os
import time
import unittest

try:
    from unittest.mock import patch, MagicMock
except ImportError:
    from mock import patch, MagicMock

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import msal
from msal import MsiV2Error
from msal.msi_v2 import (
    verify_cnf_binding,
    _cert_cache_clear,
    _cert_cache_get,
    _cert_cache_set,
    _cert_cache_key,
    _cert_cache_remove,
    _CertCacheEntry,
    _mi_query_params,
    _resource_to_scope,
    _token_endpoint_from_credential,
    _der_to_pem,
)

from tests.http_client import MinimalResponse


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _make_self_signed_cert(private_key, common_name="test"):
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(private_key, hashes.SHA256(), default_backend())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def get_cert_thumbprint_sha256(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(
        cert_pem.encode("utf-8"), default_backend())
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return (base64.urlsafe_b64encode(hashlib.sha256(cert_der).digest())
            .rstrip(b"=").decode("ascii"))


def _b64url(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).rstrip(b"=").decode("ascii")


def _make_jwt(payload_obj, header_obj=None) -> str:
    header_obj = header_obj or {"alg": "RS256", "typ": "JWT"}
    header = _b64url(
        json.dumps(header_obj, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(
        json.dumps(payload_obj, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(b"sig")
    return f"{header}.{payload}.{sig}"


# ---------------------------------------------------------------------------
# Thumbprint helper
# ---------------------------------------------------------------------------

class TestThumbprintHelper(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048)
        self.cert_pem = _make_self_signed_cert(self.key, "thumbprint-test")

    def test_returns_base64url_no_padding(self):
        thumb = get_cert_thumbprint_sha256(self.cert_pem)
        self.assertIsInstance(thumb, str)
        self.assertNotIn("=", thumb)
        decoded = base64.urlsafe_b64decode(thumb + "==")
        self.assertEqual(len(decoded), 32)

    def test_same_cert_same_thumbprint(self):
        t1 = get_cert_thumbprint_sha256(self.cert_pem)
        t2 = get_cert_thumbprint_sha256(self.cert_pem)
        self.assertEqual(t1, t2)

    def test_different_certs_different_thumbprints(self):
        key2 = rsa.generate_private_key(
            public_exponent=65537, key_size=2048)
        cert2_pem = _make_self_signed_cert(key2, "thumbprint-test-2")
        self.assertNotEqual(
            get_cert_thumbprint_sha256(self.cert_pem),
            get_cert_thumbprint_sha256(cert2_pem))

    def test_matches_manual_sha256_der(self):
        cert = x509.load_pem_x509_certificate(
            self.cert_pem.encode("utf-8"), default_backend())
        cert_der = cert.public_bytes(serialization.Encoding.DER)
        expected = (base64.urlsafe_b64encode(
            hashlib.sha256(cert_der).digest())
            .rstrip(b"=").decode("ascii"))
        self.assertEqual(get_cert_thumbprint_sha256(self.cert_pem), expected)


# ---------------------------------------------------------------------------
# verify_cnf_binding
# ---------------------------------------------------------------------------

class TestVerifyCnfBinding(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048)
        self.cert_pem = _make_self_signed_cert(self.key, "cnf-test")
        self.thumbprint = get_cert_thumbprint_sha256(self.cert_pem)

    def test_valid_binding_true(self):
        token = _make_jwt({"cnf": {"x5t#S256": self.thumbprint}})
        self.assertTrue(verify_cnf_binding(token, self.cert_pem))

    def test_wrong_thumbprint_false(self):
        token = _make_jwt({"cnf": {"x5t#S256": "wrong"}})
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_missing_cnf_false(self):
        token = _make_jwt({"sub": "nobody"})
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_missing_x5t_false(self):
        token = _make_jwt({"cnf": {}})
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_cnf_not_object_false(self):
        token = _make_jwt({"cnf": "not-an-object"})
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_not_a_jwt_false(self):
        self.assertFalse(verify_cnf_binding("notajwt", self.cert_pem))

    def test_two_part_jwt_false(self):
        self.assertFalse(verify_cnf_binding("a.b", self.cert_pem))

    def test_four_part_jwt_false(self):
        self.assertFalse(verify_cnf_binding("a.b.c.d", self.cert_pem))

    def test_malformed_payload_base64_false(self):
        self.assertFalse(verify_cnf_binding("header.!!!.sig", self.cert_pem))

    def test_payload_not_json_false(self):
        header = _b64url(b'{"alg":"none"}')
        payload = _b64url(b"not-json")
        self.assertFalse(
            verify_cnf_binding(f"{header}.{payload}.sig", self.cert_pem))

    def test_payload_with_padding_works(self):
        header = base64.urlsafe_b64encode(
            b'{"alg":"RS256"}').decode("ascii")
        payload = base64.urlsafe_b64encode(json.dumps(
            {"cnf": {"x5t#S256": self.thumbprint}}).encode("utf-8")
        ).decode("ascii")
        self.assertTrue(
            verify_cnf_binding(f"{header}.{payload}.sig", self.cert_pem))

    def test_unicode_in_payload(self):
        token = _make_jwt({
            "cnf": {"x5t#S256": self.thumbprint}, "msg": "こんにちは"})
        self.assertTrue(verify_cnf_binding(token, self.cert_pem))


# ---------------------------------------------------------------------------
# Certificate cache
# ---------------------------------------------------------------------------

class TestCertificateCache(unittest.TestCase):
    def setUp(self):
        _cert_cache_clear()

    def tearDown(self):
        _cert_cache_clear()

    def _make_entry(self, *, not_after=None):
        return _CertCacheEntry(
            cert_der=b"fake-der",
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n"
                     "-----END CERTIFICATE-----",
            token_endpoint="https://login.microsoftonline.com/t/oauth2/v2.0/token",
            client_id="test-client-id",
            not_after=not_after or (time.time() + 48 * 3600),
        )

    def test_set_and_get(self):
        entry = self._make_entry()
        _cert_cache_set("k1", entry)
        got = _cert_cache_get("k1")
        self.assertIsNotNone(got)
        self.assertEqual(got.cert_der, b"fake-der")
        self.assertEqual(got.client_id, "test-client-id")

    def test_miss_returns_none(self):
        self.assertIsNone(_cert_cache_get("no-such-key"))

    def test_expired_entry_evicted(self):
        entry = self._make_entry(not_after=time.time() + 100)
        _cert_cache_set("k2", entry)
        # Force it to look expired
        entry.not_after = time.time() - 1
        self.assertIsNone(_cert_cache_get("k2"))

    def test_insufficient_lifetime_not_cached(self):
        # Not enough remaining lifetime (< 24h)
        entry = self._make_entry(not_after=time.time() + 3600)
        _cert_cache_set("k3", entry)
        self.assertIsNone(_cert_cache_get("k3"))

    def test_remove(self):
        entry = self._make_entry()
        _cert_cache_set("k4", entry)
        _cert_cache_remove("k4")
        self.assertIsNone(_cert_cache_get("k4"))

    def test_clear(self):
        _cert_cache_set("k5", self._make_entry())
        _cert_cache_set("k6", self._make_entry())
        _cert_cache_clear()
        self.assertIsNone(_cert_cache_get("k5"))
        self.assertIsNone(_cert_cache_get("k6"))

    def test_cache_key_generation(self):
        mi_sys = {"ManagedIdentityIdType": "SystemAssigned", "Id": None}
        mi_user = {"ManagedIdentityIdType": "ClientId", "Id": "abc"}
        mi_obj = {"ManagedIdentityIdType": "ObjectId", "Id": "abc"}
        k1 = _cert_cache_key(mi_sys, True)
        k2 = _cert_cache_key(mi_sys, False)
        k3 = _cert_cache_key(mi_user, True)
        k4 = _cert_cache_key(mi_obj, True)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        # Same Id but different IdType must produce different keys
        self.assertNotEqual(k3, k4)
        self.assertIn("#att=1", k1)
        self.assertIn("#att=0", k2)
        self.assertIn("ClientId:", k3)
        self.assertIn("ObjectId:", k4)


# ---------------------------------------------------------------------------
# IMDS wire-contract helpers
# ---------------------------------------------------------------------------

class TestImdsHelpers(unittest.TestCase):
    def test_mi_query_params_system_assigned(self):
        p = _mi_query_params(
            {"ManagedIdentityIdType": "SystemAssigned", "Id": None})
        self.assertEqual(p["cred-api-version"], "2.0")
        self.assertNotIn("client_id", p)

    def test_mi_query_params_client_id(self):
        p = _mi_query_params(
            {"ManagedIdentityIdType": "ClientId", "Id": "abc"})
        self.assertEqual(p["client_id"], "abc")

    def test_mi_query_params_object_id(self):
        p = _mi_query_params(
            {"ManagedIdentityIdType": "ObjectId", "Id": "oid"})
        self.assertEqual(p["object_id"], "oid")

    def test_mi_query_params_resource_id(self):
        p = _mi_query_params(
            {"ManagedIdentityIdType": "ResourceId", "Id": "/sub/..."})
        self.assertEqual(p["msi_res_id"], "/sub/...")

    def test_resource_to_scope_appends_default(self):
        self.assertEqual(
            _resource_to_scope("https://graph.microsoft.com"),
            "https://graph.microsoft.com/.default")

    def test_resource_to_scope_preserves_existing(self):
        self.assertEqual(
            _resource_to_scope("https://graph.microsoft.com/.default"),
            "https://graph.microsoft.com/.default")

    def test_resource_to_scope_strips_trailing_slash(self):
        self.assertEqual(
            _resource_to_scope("https://graph.microsoft.com/"),
            "https://graph.microsoft.com/.default")

    def test_resource_to_scope_raises_on_empty(self):
        with self.assertRaises(ValueError):
            _resource_to_scope("")

    def test_token_endpoint_prefers_explicit(self):
        cred = {"token_endpoint": "https://explicit.com/token",
                "mtls_authentication_endpoint": "https://other"}
        self.assertEqual(
            _token_endpoint_from_credential(cred),
            "https://explicit.com/token")

    def test_token_endpoint_falls_back_to_mtls_auth(self):
        cred = {
            "mtls_authentication_endpoint": "https://login.example.com",
            "tenant_id": "tid",
        }
        self.assertEqual(
            _token_endpoint_from_credential(cred),
            "https://login.example.com/tid/oauth2/v2.0/token")

    def test_token_endpoint_raises_on_missing(self):
        with self.assertRaises(MsiV2Error):
            _token_endpoint_from_credential({})


# ---------------------------------------------------------------------------
# ManagedIdentityClient gating
# ---------------------------------------------------------------------------

class TestManagedIdentityClientStrictGating(unittest.TestCase):
    def _make_client(self):
        import requests
        return msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
        )

    def test_error_is_exported(self):
        self.assertIs(msal.MsiV2Error, MsiV2Error)

    def test_error_is_subclass(self):
        self.assertTrue(issubclass(MsiV2Error, msal.ManagedIdentityError))

    @patch("msal.managed_identity._obtain_token")
    def test_default_path_calls_v1(self, mock_v1):
        mock_v1.return_value = {
            "access_token": "V1", "expires_in": 3600, "token_type": "Bearer"}
        client = self._make_client()
        res = client.acquire_token_for_client(resource="R")
        self.assertEqual(res["access_token"], "V1")
        mock_v1.assert_called_once()

    def test_attestation_requires_pop(self):
        client = self._make_client()
        with self.assertRaises(msal.ManagedIdentityError):
            client.acquire_token_for_client(
                resource="R",
                mtls_proof_of_possession=False,
                with_attestation_support=True)

    @patch("msal.msi_v2.obtain_token")
    @patch("msal.managed_identity._obtain_token")
    def test_pop_without_attestation_does_not_call_v2(
            self, mock_v1, mock_v2):
        mock_v1.return_value = {
            "access_token": "V1", "expires_in": 3600, "token_type": "Bearer"}
        client = self._make_client()
        res = client.acquire_token_for_client(
            resource="R",
            mtls_proof_of_possession=True,
            with_attestation_support=False)
        self.assertEqual(res["token_type"], "Bearer")
        mock_v2.assert_not_called()
        mock_v1.assert_called_once()

    @patch("msal.managed_identity.create_attestation_provider",
           create=True)
    @patch("msal.msi_v2.obtain_token")
    def test_v2_called_when_both_flags_true(self, mock_v2, _):
        mock_v2.return_value = {
            "access_token": "V2", "expires_in": 3600,
            "token_type": "mtls_pop"}
        client = self._make_client()

        with patch.dict("sys.modules", {
            "msal_key_attestation": MagicMock(
                create_attestation_provider=MagicMock(
                    return_value=lambda ep, kh, ci, ck="": "fake.jwt"))
        }):
            res = client.acquire_token_for_client(
                resource="https://mtlstb.graph.microsoft.com",
                mtls_proof_of_possession=True,
                with_attestation_support=True)

        self.assertEqual(res["token_type"], "mtls_pop")
        mock_v2.assert_called_once()
        args, kwargs = mock_v2.call_args
        self.assertTrue(len(args) >= 3)
        self.assertEqual(args[2], "https://mtlstb.graph.microsoft.com")
        self.assertTrue(kwargs["attestation_enabled"])

    @patch("msal.msi_v2.obtain_token", side_effect=MsiV2Error("boom"))
    @patch("msal.managed_identity._obtain_token")
    def test_strict_v2_failure_raises_no_v1_fallback(
            self, mock_v1, mock_v2):
        client = self._make_client()
        with patch.dict("sys.modules", {
            "msal_key_attestation": MagicMock(
                create_attestation_provider=MagicMock(
                    return_value=lambda ep, kh, ci, ck="": "fake.jwt"))
        }):
            with self.assertRaises(MsiV2Error):
                client.acquire_token_for_client(
                    resource="https://mtlstb.graph.microsoft.com",
                    mtls_proof_of_possession=True,
                    with_attestation_support=True)
        mock_v1.assert_not_called()

    @patch("msal.msi_v2.obtain_token",
           side_effect=RuntimeError("DLL load failed"))
    @patch("msal.managed_identity._obtain_token")
    def test_runtime_error_wrapped_as_msi_v2_error(
            self, mock_v1, mock_v2):
        """RuntimeError from provider/DLL must surface as MsiV2Error."""
        client = self._make_client()
        with patch.dict("sys.modules", {
            "msal_key_attestation": MagicMock(
                create_attestation_provider=MagicMock(
                    return_value=lambda ep, kh, ci, ck="": "fake.jwt"))
        }):
            with self.assertRaises(MsiV2Error) as ctx:
                client.acquire_token_for_client(
                    resource="R",
                    mtls_proof_of_possession=True,
                    with_attestation_support=True)
            self.assertIn("DLL load failed", str(ctx.exception))
        mock_v1.assert_not_called()

    def test_missing_attestation_package_raises_clear_error(self):
        client = self._make_client()
        with patch.dict("sys.modules", {"msal_key_attestation": None}):
            with self.assertRaises(MsiV2Error) as ctx:
                client.acquire_token_for_client(
                    resource="R",
                    mtls_proof_of_possession=True,
                    with_attestation_support=True)
            self.assertIn("pip install msal-key-attestation",
                          str(ctx.exception))


# ---------------------------------------------------------------------------
# DER helpers
# ---------------------------------------------------------------------------

class TestDerHelpers(unittest.TestCase):
    def test_der_to_pem_roundtrip(self):
        key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048)
        cert_pem = _make_self_signed_cert(key, "der-test")
        cert = x509.load_pem_x509_certificate(
            cert_pem.encode("utf-8"), default_backend())
        cert_der = cert.public_bytes(serialization.Encoding.DER)

        pem_out = _der_to_pem(cert_der)
        self.assertIn("-----BEGIN CERTIFICATE-----", pem_out)
        self.assertIn("-----END CERTIFICATE-----", pem_out)

        # Verify the PEM round-trips back to same DER
        cert2 = x509.load_pem_x509_certificate(
            pem_out.encode("utf-8"), default_backend())
        self.assertEqual(
            cert2.public_bytes(serialization.Encoding.DER), cert_der)


if __name__ == "__main__":
    unittest.main()
