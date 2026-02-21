# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""Tests for MSI v2 (mTLS PoP) implementation."""
import base64
import datetime
import hashlib
import json
import os
import unittest
try:
    from unittest.mock import patch, MagicMock, call
except ImportError:
    from mock import patch, MagicMock, call

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import msal
from msal import MsiV2Error
from msal.msi_v2 import (
    _CU_ID_OID,
    _IMDS_API_VERSION,
    _build_csr,
    _encode_der_octet_string,
    _generate_rsa_key,
    _get_platform_metadata,
    _issue_credential,
    get_cert_thumbprint_sha256,
    verify_cnf_binding,
)
from tests.test_throttled_http_client import MinimalResponse


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _make_self_signed_cert(private_key, common_name="test"):
    """Create a minimal self-signed certificate for testing."""
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
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


# ---------------------------------------------------------------------------
# RSA key generation
# ---------------------------------------------------------------------------

class TestGenerateRsaKey(unittest.TestCase):
    def test_generates_rsa_2048_key(self):
        key = _generate_rsa_key()
        self.assertIsInstance(key, rsa.RSAPrivateKey)
        self.assertEqual(key.key_size, 2048)

    def test_each_call_generates_unique_key(self):
        key1 = _generate_rsa_key()
        key2 = _generate_rsa_key()
        pub1 = key1.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        pub2 = key2.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertNotEqual(pub1, pub2)


# ---------------------------------------------------------------------------
# DER OCTET STRING encoding
# ---------------------------------------------------------------------------

class TestEncodeDerOctetString(unittest.TestCase):
    def test_short_value(self):
        data = b"hello"
        result = _encode_der_octet_string(data)
        self.assertEqual(result[0], 0x04)   # OCTET STRING tag
        self.assertEqual(result[1], 5)      # length
        self.assertEqual(result[2:], data)

    def test_127_byte_value(self):
        data = b"x" * 127
        result = _encode_der_octet_string(data)
        self.assertEqual(result[0], 0x04)
        self.assertEqual(result[1], 127)
        self.assertEqual(result[2:], data)

    def test_128_byte_value_uses_long_form(self):
        data = b"x" * 128
        result = _encode_der_octet_string(data)
        self.assertEqual(result[0], 0x04)
        # Long-form: 0x80 | 1 byte follows, then the length
        self.assertEqual(result[1], 0x81)
        self.assertEqual(result[2], 128)
        self.assertEqual(result[3:], data)

    def test_empty_value(self):
        result = _encode_der_octet_string(b"")
        self.assertEqual(result, bytes([0x04, 0x00]))


# ---------------------------------------------------------------------------
# CSR generation
# ---------------------------------------------------------------------------

class TestBuildCsr(unittest.TestCase):
    def setUp(self):
        self.private_key = _generate_rsa_key()
        self.client_id = "test-client-id"
        self.cu_id = "test-cu-id-12345"

    def test_returns_der_bytes(self):
        csr_der = _build_csr(self.private_key, self.client_id, self.cu_id)
        self.assertIsInstance(csr_der, bytes)
        self.assertGreater(len(csr_der), 0)

    def test_csr_is_valid_der(self):
        csr_der = _build_csr(self.private_key, self.client_id, self.cu_id)
        csr = x509.load_der_x509_csr(csr_der, default_backend())
        self.assertIsNotNone(csr)

    def test_csr_subject_common_name(self):
        csr_der = _build_csr(self.private_key, self.client_id, self.cu_id)
        csr = x509.load_der_x509_csr(csr_der, default_backend())
        cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        self.assertEqual(len(cn), 1)
        self.assertEqual(cn[0].value, self.client_id)

    def test_csr_contains_cu_id_extension(self):
        csr_der = _build_csr(self.private_key, self.client_id, self.cu_id)
        csr = x509.load_der_x509_csr(csr_der, default_backend())
        ext = csr.extensions.get_extension_for_oid(_CU_ID_OID)
        self.assertIsNotNone(ext)

    def test_cu_id_extension_contains_json(self):
        csr_der = _build_csr(self.private_key, self.client_id, self.cu_id)
        csr = x509.load_der_x509_csr(csr_der, default_backend())
        ext = csr.extensions.get_extension_for_oid(_CU_ID_OID)
        # Extension value is DER OCTET STRING wrapping JSON
        raw = ext.value.value  # bytes of the extension value
        # Strip the DER OCTET STRING header (first 2 bytes for short values)
        json_bytes = raw[2:]
        parsed = json.loads(json_bytes)
        self.assertEqual(parsed["cuId"], self.cu_id)

    def test_csr_signature_is_valid(self):
        csr_der = _build_csr(self.private_key, self.client_id, self.cu_id)
        csr = x509.load_der_x509_csr(csr_der, default_backend())
        self.assertTrue(csr.is_signature_valid)


# ---------------------------------------------------------------------------
# Certificate thumbprint (x5t#S256)
# ---------------------------------------------------------------------------

class TestGetCertThumbprintSha256(unittest.TestCase):
    def setUp(self):
        self.key = _generate_rsa_key()
        self.cert_pem = _make_self_signed_cert(self.key, "thumbprint-test")

    def test_returns_base64url_string(self):
        thumbprint = get_cert_thumbprint_sha256(self.cert_pem)
        self.assertIsInstance(thumbprint, str)
        # Must be valid base64url (no padding)
        self.assertNotIn("=", thumbprint)
        # Must be decodable
        decoded = base64.urlsafe_b64decode(thumbprint + "==")
        self.assertEqual(len(decoded), 32)  # SHA-256 = 32 bytes

    def test_same_cert_produces_same_thumbprint(self):
        t1 = get_cert_thumbprint_sha256(self.cert_pem)
        t2 = get_cert_thumbprint_sha256(self.cert_pem)
        self.assertEqual(t1, t2)

    def test_different_certs_produce_different_thumbprints(self):
        key2 = _generate_rsa_key()
        cert2_pem = _make_self_signed_cert(key2, "other-cert")
        t1 = get_cert_thumbprint_sha256(self.cert_pem)
        t2 = get_cert_thumbprint_sha256(cert2_pem)
        self.assertNotEqual(t1, t2)

    def test_matches_manual_sha256_of_der(self):
        cert = x509.load_pem_x509_certificate(
            self.cert_pem.encode("utf-8"), default_backend())
        cert_der = cert.public_bytes(serialization.Encoding.DER)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(cert_der).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(get_cert_thumbprint_sha256(self.cert_pem), expected)


# ---------------------------------------------------------------------------
# verify_cnf_binding
# ---------------------------------------------------------------------------

class TestVerifyCnfBinding(unittest.TestCase):
    def _make_token_with_cnf(self, thumbprint):
        """Build a minimal JWT with cnf.x5t#S256 in the payload."""
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"cnf": {"x5t#S256": thumbprint}}).encode()
        ).rstrip(b"=").decode()
        signature = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
        return "{}.{}.{}".format(header, payload, signature)

    def setUp(self):
        self.key = _generate_rsa_key()
        self.cert_pem = _make_self_signed_cert(self.key, "cnf-test")
        self.thumbprint = get_cert_thumbprint_sha256(self.cert_pem)

    def test_valid_cnf_returns_true(self):
        token = self._make_token_with_cnf(self.thumbprint)
        self.assertTrue(verify_cnf_binding(token, self.cert_pem))

    def test_wrong_thumbprint_returns_false(self):
        token = self._make_token_with_cnf("wrongthumbprint")
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_missing_cnf_returns_false(self):
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "nobody"}).encode()).rstrip(b"=").decode()
        sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
        token = "{}.{}.{}".format(header, payload, sig)
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))

    def test_not_a_jwt_returns_false(self):
        self.assertFalse(verify_cnf_binding("notajwt", self.cert_pem))

    def test_malformed_payload_returns_false(self):
        token = "header.!!!.sig"
        self.assertFalse(verify_cnf_binding(token, self.cert_pem))


# ---------------------------------------------------------------------------
# _get_platform_metadata
# ---------------------------------------------------------------------------

class TestGetPlatformMetadata(unittest.TestCase):
    def _make_http_client(self, status_code, text):
        http_client = MagicMock()
        http_client.get.return_value = MinimalResponse(
            status_code=status_code, text=text)
        return http_client

    def test_returns_metadata_dict_on_success(self):
        metadata = {
            "clientId": "client-id",
            "tenantId": "tenant-id",
            "cuId": "cu-id",
            "attestationEndpoint": "https://attestation.example.com",
        }
        http_client = self._make_http_client(200, json.dumps(metadata))
        result = _get_platform_metadata(http_client)
        self.assertEqual(result, metadata)
        http_client.get.assert_called_once()
        call_args = http_client.get.call_args
        self.assertIn("getplatformmetadata", call_args[0][0])
        self.assertEqual(
            call_args[1]["params"]["api-version"], _IMDS_API_VERSION)
        self.assertEqual(call_args[1]["headers"]["Metadata"], "true")

    def test_raises_on_non_200(self):
        http_client = self._make_http_client(404, "Not Found")
        with self.assertRaises(MsiV2Error):
            _get_platform_metadata(http_client)

    def test_raises_on_invalid_json(self):
        http_client = self._make_http_client(200, "not json")
        with self.assertRaises(MsiV2Error):
            _get_platform_metadata(http_client)


# ---------------------------------------------------------------------------
# _issue_credential
# ---------------------------------------------------------------------------

class TestIssueCredential(unittest.TestCase):
    def _make_http_client(self, status_code, text):
        http_client = MagicMock()
        http_client.post.return_value = MinimalResponse(
            status_code=status_code, text=text)
        return http_client

    def test_returns_credential_dict_on_success(self):
        credential = {
            "certificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
            "tokenEndpoint": "https://login.microsoftonline.com/tenant/oauth2/token",
        }
        http_client = self._make_http_client(200, json.dumps(credential))
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        result = _issue_credential(http_client, csr_der, None)
        self.assertEqual(result, credential)
        http_client.post.assert_called_once()
        call_args = http_client.post.call_args
        self.assertIn("issuecredential", call_args[0][0])

    def test_sends_attestation_jwt_when_provided(self):
        credential = {
            "certificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
            "tokenEndpoint": "https://login.microsoftonline.com/tenant/oauth2/token",
        }
        http_client = self._make_http_client(200, json.dumps(credential))
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        _issue_credential(http_client, csr_der, "fake.attestation.jwt")
        call_args = http_client.post.call_args
        body = json.loads(call_args[1]["data"])
        self.assertEqual(body["attestation"], "fake.attestation.jwt")

    def test_omits_attestation_when_none(self):
        credential = {
            "certificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
            "tokenEndpoint": "https://example.com/token",
        }
        http_client = self._make_http_client(200, json.dumps(credential))
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        _issue_credential(http_client, csr_der, None)
        call_args = http_client.post.call_args
        body = json.loads(call_args[1]["data"])
        self.assertNotIn("attestation", body)

    def test_csr_is_base64_encoded(self):
        credential = {
            "certificate": "cert",
            "tokenEndpoint": "https://example.com/token",
        }
        http_client = self._make_http_client(200, json.dumps(credential))
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        _issue_credential(http_client, csr_der, None)
        call_args = http_client.post.call_args
        body = json.loads(call_args[1]["data"])
        decoded = base64.b64decode(body["csr"])
        self.assertEqual(decoded, csr_der)

    def test_raises_on_non_200(self):
        http_client = self._make_http_client(400, '{"error": "bad_request"}')
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        with self.assertRaises(MsiV2Error):
            _issue_credential(http_client, csr_der, None)


# ---------------------------------------------------------------------------
# ManagedIdentityClient MSI v2 integration
# ---------------------------------------------------------------------------

class TestManagedIdentityClientMsiV2(unittest.TestCase):
    """Tests for MsiV2Error export and msi_v2_enabled parameter."""

    def test_msi_v2_error_is_subclass_of_managed_identity_error(self):
        self.assertTrue(issubclass(MsiV2Error, msal.ManagedIdentityError))

    def test_msi_v2_error_is_exported_from_msal(self):
        self.assertIs(msal.MsiV2Error, MsiV2Error)

    def test_client_accepts_msi_v2_enabled_true(self):
        import requests
        client = msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
            msi_v2_enabled=True,
        )
        self.assertTrue(client._msi_v2_enabled)

    def test_client_accepts_msi_v2_enabled_false(self):
        import requests
        client = msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
            msi_v2_enabled=False,
        )
        self.assertFalse(client._msi_v2_enabled)

    def test_client_msi_v2_disabled_by_default(self):
        import requests
        # No MSAL_ENABLE_MSI_V2 env var, no param => disabled
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MSAL_ENABLE_MSI_V2", None)
            client = msal.ManagedIdentityClient(
                msal.SystemAssignedManagedIdentity(),
                http_client=requests.Session(),
            )
        self.assertFalse(client._msi_v2_enabled)

    @patch.dict(os.environ, {"MSAL_ENABLE_MSI_V2": "true"})
    def test_client_msi_v2_enabled_via_env_var_true(self):
        import requests
        client = msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
        )
        self.assertTrue(client._msi_v2_enabled)

    @patch.dict(os.environ, {"MSAL_ENABLE_MSI_V2": "1"})
    def test_client_msi_v2_enabled_via_env_var_1(self):
        import requests
        client = msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
        )
        self.assertTrue(client._msi_v2_enabled)

    @patch.dict(os.environ, {"MSAL_ENABLE_MSI_V2": "false"})
    def test_client_msi_v2_disabled_via_env_var(self):
        import requests
        client = msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
        )
        self.assertFalse(client._msi_v2_enabled)


class TestMsiV2TokenAcquisitionIntegration(unittest.TestCase):
    """Integration tests for MSI v2 token acquisition flow with mocked IMDS."""

    def _make_client(self, msi_v2_enabled=True):
        import requests
        return msal.ManagedIdentityClient(
            msal.SystemAssignedManagedIdentity(),
            http_client=requests.Session(),
            msi_v2_enabled=msi_v2_enabled,
        )

    def _make_mock_responses(self, client_id, cu_id, cert_pem, token_endpoint,
                              access_token, expires_in):
        """Build a list of mock HTTP responses for the MSI v2 flow."""
        platform_metadata = {
            "clientId": client_id,
            "tenantId": "tenant-id",
            "cuId": cu_id,
            "attestationEndpoint": "https://attest.example.com",
        }
        credential = {
            "certificate": cert_pem,
            "tokenEndpoint": token_endpoint,
        }
        token_response = {
            "access_token": access_token,
            "expires_in": str(expires_in),
            "token_type": "mtls_pop",
            "resource": "https://management.azure.com/",
        }
        return platform_metadata, credential, token_response

    @patch("msal.msi_v2._acquire_token_via_mtls")
    def test_msi_v2_happy_path(self, mock_mtls):
        """MSI v2 succeeds end-to-end (mTLS call is mocked)."""
        import requests

        key = _generate_rsa_key()
        cert_pem = _make_self_signed_cert(key, "test-client-id")
        access_token = "MSI_V2_ACCESS_TOKEN"
        expires_in = 3600
        token_endpoint = "https://login.microsoftonline.com/tenant/oauth2/token"

        platform_metadata, credential, token_response = self._make_mock_responses(
            "test-client-id", "test-cu-id", cert_pem, token_endpoint,
            access_token, expires_in)

        mock_mtls.return_value = token_response

        client = self._make_client(msi_v2_enabled=True)

        def _mock_get(url, **kwargs):
            if "getplatformmetadata" in url:
                return MinimalResponse(
                    status_code=200, text=json.dumps(platform_metadata))
            raise ValueError("Unexpected GET: {}".format(url))

        def _mock_post(url, **kwargs):
            if "issuecredential" in url:
                return MinimalResponse(
                    status_code=200, text=json.dumps(credential))
            raise ValueError("Unexpected POST: {}".format(url))

        with patch.object(client._http_client, "get", side_effect=_mock_get), \
             patch.object(client._http_client, "post", side_effect=_mock_post):
            result = client.acquire_token_for_client(
                resource="https://management.azure.com/")

        self.assertEqual(result["access_token"], access_token)
        self.assertEqual(result["token_type"], "mtls_pop")
        self.assertEqual(result["token_source"], "identity_provider")

    @patch("msal.msi_v2._acquire_token_via_mtls")
    def test_msi_v2_fallback_to_v1_on_metadata_failure(self, mock_mtls):
        """MSI v2 falls back to MSI v1 if IMDS metadata call fails."""
        import requests
        client = self._make_client(msi_v2_enabled=True)

        def _mock_get(url, **kwargs):
            if "getplatformmetadata" in url:
                return MinimalResponse(status_code=404, text="Not Found")
            # MSI v1 fallback (VM endpoint)
            if "oauth2/token" in url:
                return MinimalResponse(status_code=200, text=json.dumps({
                    "access_token": "V1_TOKEN",
                    "expires_in": "3600",
                    "resource": "R",
                }))
            raise ValueError("Unexpected GET: {}".format(url))

        with patch.object(client._http_client, "get", side_effect=_mock_get):
            result = client.acquire_token_for_client(resource="R")

        # Should have fallen back to MSI v1
        self.assertEqual(result["access_token"], "V1_TOKEN")
        mock_mtls.assert_not_called()

    @patch("msal.msi_v2._acquire_token_via_mtls")
    def test_msi_v2_not_attempted_when_disabled(self, mock_mtls):
        """MSI v2 is not attempted when msi_v2_enabled=False."""
        import requests

        client = self._make_client(msi_v2_enabled=False)

        def _mock_get(url, **kwargs):
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "V1_TOKEN",
                "expires_in": "3600",
                "resource": "R",
            }))

        with patch.object(client._http_client, "get", side_effect=_mock_get):
            result = client.acquire_token_for_client(resource="R")

        mock_mtls.assert_not_called()
        self.assertEqual(result["access_token"], "V1_TOKEN")

    @patch("msal.msi_v2._acquire_token_via_mtls")
    def test_msi_v2_fallback_on_unexpected_error(self, mock_mtls):
        """MSI v2 falls back to MSI v1 on unexpected errors."""
        import requests
        client = self._make_client(msi_v2_enabled=True)

        platform_metadata = {
            "clientId": "client-id",
            "tenantId": "tenant-id",
            "cuId": "cu-id",
            "attestationEndpoint": None,
        }

        call_count = [0]

        def _mock_get(url, **kwargs):
            call_count[0] += 1
            if "getplatformmetadata" in url:
                return MinimalResponse(status_code=200, text=json.dumps(platform_metadata))
            # MSI v1 fallback
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "V1_FALLBACK",
                "expires_in": "3600",
                "resource": "R",
            }))

        def _mock_post(url, **kwargs):
            if "issuecredential" in url:
                # Return missing fields to trigger MsiV2Error
                return MinimalResponse(status_code=200, text=json.dumps({}))
            raise ValueError("Unexpected POST: {}".format(url))

        with patch.object(client._http_client, "get", side_effect=_mock_get), \
             patch.object(client._http_client, "post", side_effect=_mock_post):
            result = client.acquire_token_for_client(resource="R")

        # Should fall back to MSI v1
        self.assertEqual(result["access_token"], "V1_FALLBACK")
        mock_mtls.assert_not_called()


# ---------------------------------------------------------------------------
# Attestation module
# ---------------------------------------------------------------------------

class TestAttestationModule(unittest.TestCase):
    def test_get_attestation_jwt_returns_none_on_non_windows(self):
        from msal.msi_v2_attestation import get_attestation_jwt
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        with patch("msal.msi_v2_attestation.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = get_attestation_jwt(
                MagicMock(), csr_der, "https://attest.example.com", key)
        self.assertIsNone(result)

    def test_get_attestation_jwt_returns_none_when_dll_missing(self):
        from msal.msi_v2_attestation import get_attestation_jwt
        key = _generate_rsa_key()
        csr_der = _build_csr(key, "client-id", "cu-id")
        with patch("msal.msi_v2_attestation.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("ctypes.CDLL", side_effect=OSError("DLL not found")):
                result = get_attestation_jwt(
                    MagicMock(), csr_der, "https://attest.example.com", key)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
