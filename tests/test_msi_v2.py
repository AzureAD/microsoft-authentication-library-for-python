# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""Tests for MSI v2 (mTLS PoP) implementation.

Goals:
- Provide strong unit coverage without depending on pythonnet / KeyGuard / real IMDS.
- Avoid importing optional helpers that may not exist in the KeyGuard implementation.
- Validate:
  * x5t#S256 helper correctness (local)
  * verify_cnf_binding behavior (msal.msi_v2)
  * ManagedIdentityClient strict gating behavior (msi v2 invoked only when explicitly requested)
  * Optional IMDSv2 wire-contract helpers when present (skipped if not exposed)
"""

import base64
import datetime
import hashlib
import json
import os
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


# Import only stable surface from msal.msi_v2
from msal.msi_v2 import verify_cnf_binding

# MinimalResponse is used in other test modules; safe to reuse here
from tests.test_throttled_http_client import MinimalResponse


# ---------------------------------------------------------------------------
# Local helpers (do not rely on msal.msi_v2 exporting these)
# ---------------------------------------------------------------------------

def _make_self_signed_cert(private_key, common_name="test"):
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
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
    """x5t#S256 = base64url(SHA256(der(cert))) without padding."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"), default_backend())
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return base64.urlsafe_b64encode(hashlib.sha256(cert_der).digest()).rstrip(b"=").decode("ascii")


def _b64url(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).rstrip(b"=").decode("ascii")


def _make_jwt(payload_obj, header_obj=None) -> str:
    header_obj = header_obj or {"alg": "RS256", "typ": "JWT"}
    header = _b64url(json.dumps(header_obj, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(json.dumps(payload_obj, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(b"sig")
    return f"{header}.{payload}.{sig}"


# ---------------------------------------------------------------------------
# Thumbprint helper
# ---------------------------------------------------------------------------

class TestThumbprintHelper(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
        key2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert2_pem = _make_self_signed_cert(key2, "thumbprint-test-2")
        self.assertNotEqual(get_cert_thumbprint_sha256(self.cert_pem),
                            get_cert_thumbprint_sha256(cert2_pem))

    def test_matches_manual_sha256_der(self):
        cert = x509.load_pem_x509_certificate(self.cert_pem.encode("utf-8"), default_backend())
        cert_der = cert.public_bytes(serialization.Encoding.DER)
        expected = base64.urlsafe_b64encode(hashlib.sha256(cert_der).digest()).rstrip(b"=").decode("ascii")
        self.assertEqual(get_cert_thumbprint_sha256(self.cert_pem), expected)


# ---------------------------------------------------------------------------
# verify_cnf_binding (more coverage)
# ---------------------------------------------------------------------------

class TestVerifyCnfBinding(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
        token = "a.b"
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_four_part_jwt_false(self):
        token = "a.b.c.d"
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_malformed_payload_base64_false(self):
        token = "header.!!!.sig"
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_payload_not_json_false(self):
        header = _b64url(b'{"alg":"none"}')
        payload = _b64url(b"not-json")
        token = f"{header}.{payload}.sig"
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_payload_with_padding_still_works(self):
        # Create payload base64 with explicit padding (library should tolerate)
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii")  # includes padding sometimes
        payload = base64.urlsafe_b64encode(json.dumps({"cnf": {"x5t#S256": self.thumbprint}}).encode("utf-8")).decode("ascii")
        token = f"{header}.{payload}.sig"
        self.assertTrue(verify_cnf_binding(token, self.cert_pem))

    def test_unicode_in_payload_does_not_break(self):
        token = _make_jwt({"cnf": {"x5t#S256": self.thumbprint}, "msg": "こんにちは"})
        self.assertTrue(verify_cnf_binding(token, self.cert_pem))


# ---------------------------------------------------------------------------
# ManagedIdentityClient gating + strict behavior (better coverage)
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
        mock_v1.return_value = {"access_token": "V1", "expires_in": 3600, "token_type": "Bearer"}
        client = self._make_client()
        res = client.acquire_token_for_client(resource="R")
        self.assertEqual(res["access_token"], "V1")
        mock_v1.assert_called_once()

    def test_attestation_requires_pop(self):
        client = self._make_client()
        with self.assertRaises(msal.ManagedIdentityError):
            client.acquire_token_for_client(resource="R",
                                            mtls_proof_of_possession=False,
                                            with_attestation_support=True)

    @patch("msal.msi_v2.obtain_token")
    @patch("msal.managed_identity._obtain_token")
    def test_pop_without_attestation_does_not_call_v2(self, mock_v1, mock_v2):
        # If your implementation requires BOTH flags, v2 must not run here.
        mock_v1.return_value = {"access_token": "V1", "expires_in": 3600, "token_type": "Bearer"}
        client = self._make_client()
        res = client.acquire_token_for_client(resource="R",
                                              mtls_proof_of_possession=True,
                                              with_attestation_support=False)
        # depending on your design this could either raise or fall back to v1.
        # If you changed to "v2 only when both flags", it should use v1.
        self.assertEqual(res["token_type"], "Bearer")
        mock_v2.assert_not_called()
        mock_v1.assert_called_once()

    @patch("msal.msi_v2.obtain_token")
    def test_v2_called_when_both_flags_true(self, mock_v2):
        mock_v2.return_value = {"access_token": "V2", "expires_in": 3600, "token_type": "mtls_pop"}
        client = self._make_client()
        res = client.acquire_token_for_client(resource="https://mtlstb.graph.microsoft.com",
                                              mtls_proof_of_possession=True,
                                              with_attestation_support=True)
        self.assertEqual(res["token_type"], "mtls_pop")
        mock_v2.assert_called_once()
        # Ensure v2 called with expected signature (resource argument passed through)
        args, kwargs = mock_v2.call_args
        # obtain_token(http_client, managed_identity, resource, attestation_enabled=...)
        self.assertTrue(len(args) >= 3)
        self.assertEqual(args[2], "https://mtlstb.graph.microsoft.com")
        self.assertIn("attestation_enabled", kwargs)
        self.assertTrue(kwargs["attestation_enabled"])

    @patch("msal.msi_v2.obtain_token", side_effect=MsiV2Error("boom"))
    @patch("msal.managed_identity._obtain_token")
    def test_strict_v2_failure_raises_no_v1_fallback(self, mock_v1, mock_v2):
        client = self._make_client()
        with self.assertRaises(MsiV2Error):
            client.acquire_token_for_client(resource="https://mtlstb.graph.microsoft.com",
                                            mtls_proof_of_possession=True,
                                            with_attestation_support=True)
        mock_v1.assert_not_called()


# ---------------------------------------------------------------------------
# Optional: wire contract helper tests (skip if helpers not present)
# ---------------------------------------------------------------------------

class TestImdsV2OptionalHelpers(unittest.TestCase):
    def test_mi_query_params_adds_version_and_uami_selector(self):
        if not hasattr(msal.msi_v2, "_mi_query_params"):
            self.skipTest("msal.msi_v2._mi_query_params not exposed")

        p = msal.msi_v2._mi_query_params({"ManagedIdentityIdType": "ClientId", "Id": "abc"})
        self.assertIn("cred-api-version", p)
        self.assertEqual(p["cred-api-version"], "2.0")
        self.assertEqual(p.get("client_id"), "abc")

        p2 = msal.msi_v2._mi_query_params({"ManagedIdentityIdType": "ObjectId", "Id": "oid"})
        self.assertEqual(p2.get("object_id"), "oid")

        p3 = msal.msi_v2._mi_query_params({"ManagedIdentityIdType": "ResourceId", "Id": "/sub/..."})
        self.assertEqual(p3.get("msi_res_id"), "/sub/...")

    def test_issuecredential_body_uses_attestation_token(self):
        if not hasattr(msal.msi_v2, "_imds_post_json"):
            self.skipTest("msal.msi_v2._imds_post_json not exposed")
        if not hasattr(msal.msi_v2, "_issue_credential"):
            self.skipTest("msal.msi_v2._issue_credential not exposed")

        http_client = MagicMock()
        http_client.post.return_value = MinimalResponse(
            status_code=200,
            text=json.dumps({
                "certificate": "Zg==",
                "mtls_authentication_endpoint": "https://login",
                "tenant_id": "t",
                "client_id": "c",
            }),
        )

        msal.msi_v2._issue_credential(
            http_client,
            managed_identity={"ManagedIdentityIdType": "SystemAssigned", "Id": None},
            csr_b64="QUJD",
            attestation_jwt="fake.jwt",
        )

        _, kwargs = http_client.post.call_args
        body = json.loads(kwargs["data"])
        self.assertEqual(body["csr"], "QUJD")
        self.assertEqual(body["attestation_token"], "fake.jwt")

    def test_token_endpoint_derived_from_mtls_auth_endpoint(self):
        if not hasattr(msal.msi_v2, "_token_endpoint_from_credential"):
            self.skipTest("msal.msi_v2._token_endpoint_from_credential not exposed")

        cred = {
            "mtls_authentication_endpoint": "https://login.example.com",
            "tenant_id": "tenant123",
        }
        ep = msal.msi_v2._token_endpoint_from_credential(cred)
        self.assertTrue(ep.endswith("/tenant123/oauth2/v2.0/token"))


if __name__ == "__main__":
    unittest.main()
