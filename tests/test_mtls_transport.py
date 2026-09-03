import os
import ssl
import unittest

from msal import mtls
from msal.application import (
    _load_mtls_cert_material, _private_key_to_unencrypted_pem)


class TestMtlsEndpointTransform(unittest.TestCase):
    """The login.* -> [region.]mtlsauth.* transform and the sovereign guardrail.

    These values mirror MSAL.NET's RegionAndMtlsDiscoveryProvider so MSAL Python
    stays wire-compatible across SDKs.
    """

    def test_public_hosts_normalize_to_global_mtls_host(self):
        for host in [
            "login.microsoftonline.com", "login.microsoft.com",
            "login.windows.net", "sts.windows.net",
        ]:
            self.assertEqual(mtls.mtls_pop_host(host), "mtlsauth.microsoft.com")

    def test_case_insensitive_host(self):
        self.assertEqual(
            mtls.mtls_pop_host("Login.MicrosoftOnline.Com"),
            "mtlsauth.microsoft.com")

    def test_region_prefixes_global_host(self):
        self.assertEqual(
            mtls.mtls_pop_host("login.microsoftonline.com", region="westus3"),
            "westus3.mtlsauth.microsoft.com")

    def test_non_public_login_host_keeps_its_suffix(self):
        # e.g. a national cloud that IS supported keeps its own suffix
        self.assertEqual(
            mtls.mtls_pop_host("login.microsoftonline.us"),
            "mtlsauth.microsoftonline.us")

    def test_transform_token_endpoint_swaps_only_the_host(self):
        self.assertEqual(
            mtls.transform_token_endpoint(
                "https://login.microsoftonline.com/my_tenant/oauth2/v2.0/token",
                "login.microsoftonline.com"),
            "https://mtlsauth.microsoft.com/my_tenant/oauth2/v2.0/token")

    def test_transform_token_endpoint_regional(self):
        self.assertEqual(
            mtls.transform_token_endpoint(
                "https://login.microsoftonline.com/my_tenant/oauth2/v2.0/token",
                "login.microsoftonline.com", region="westus3"),
            "https://westus3.mtlsauth.microsoft.com/my_tenant/oauth2/v2.0/token")

    def test_transform_preserves_non_default_port(self):
        self.assertEqual(
            mtls.transform_token_endpoint(
                "https://login.microsoftonline.com:8443/t/token",
                "login.microsoftonline.com"),
            "https://mtlsauth.microsoft.com:8443/t/token")

    def test_sovereign_us_gov_fails_fast(self):
        with self.assertRaises(ValueError) as cm:
            mtls.mtls_pop_host("login.usgovcloudapi.net")
        self.assertIn("login.microsoftonline.us", str(cm.exception))

    def test_sovereign_china_fails_fast(self):
        with self.assertRaises(ValueError) as cm:
            mtls.mtls_pop_host("login.chinacloudapi.cn")
        self.assertIn("login.partner.microsoftonline.cn", str(cm.exception))

    def test_non_login_host_fails_fast(self):
        with self.assertRaises(ValueError):
            mtls.mtls_pop_host("contoso.example.com")


class TestMtlsSslContext(unittest.TestCase):
    """The mTLS SSLContext is built from in-memory PEM and leaves no key on disk."""

    _PFX = os.path.join(os.path.dirname(__file__), "certificate-with-password.pfx")
    _PASSPHRASE = "password"

    def _material(self):
        return _load_mtls_cert_material({
            "private_key_pfx_path": self._PFX,
            "passphrase": self._PASSPHRASE,
            "public_certificate": True,
        })

    def test_cert_material_shape(self):
        material = self._material()
        self.assertTrue(material["private_key_pem"].startswith(b"-----BEGIN "))
        self.assertIn(b"PRIVATE KEY", material["private_key_pem"])
        self.assertIn(b"BEGIN CERTIFICATE", material["cert_pem"])
        self.assertTrue(material["x5c"])
        self.assertEqual(len(material["sha256_thumbprint"]), 64)  # hex sha256
        # key_id is the base64url (unpadded) x5t#S256 used for cache binding
        self.assertNotIn("=", material["key_id"])

    def test_create_ssl_context_loads_cert_and_unlinks_tempfile(self):
        material = self._material()
        created = {}
        # Capture the temp path that _create_ssl_context uses, then assert it is
        # gone after the call (the key must not linger on disk).
        import tempfile as _tempfile
        orig_mkstemp = _tempfile.mkstemp

        def spy_mkstemp(*a, **k):
            fd, path = orig_mkstemp(*a, **k)
            created["path"] = path
            return fd, path

        _tempfile.mkstemp = spy_mkstemp
        try:
            context = mtls._create_ssl_context(
                material["cert_pem"], material["private_key_pem"])
        finally:
            _tempfile.mkstemp = orig_mkstemp
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIn("path", created)
        self.assertFalse(
            os.path.exists(created["path"]),
            "temp key file must be unlinked immediately after load_cert_chain")

    def test_adapter_injects_ssl_context(self):
        material = self._material()
        context = mtls._create_ssl_context(
            material["cert_pem"], material["private_key_pem"])
        adapter = mtls._make_mtls_adapter(context)
        # The adapter builds its default poolmanager on construction; our
        # ssl_context must have been injected into the pool's connection kwargs.
        self.assertIs(
            adapter.poolmanager.connection_pool_kw.get("ssl_context"), context)

    def test_http_client_builds_session_lazily(self):
        material = self._material()
        client = mtls._MtlsHttpClient(
            material["cert_pem"], material["private_key_pem"])
        self.assertIsNone(client._session, "session must not be built until first use")
        session = client._ensure_session()
        self.assertIsNotNone(session)
        # The https adapter is our mTLS adapter (cert-bearing), not the default
        self.assertIn("https://", session.adapters)
        client.close()


class TestUnencryptedPemLoading(unittest.TestCase):
    """_private_key_to_unencrypted_pem pins the cryptography backend and turns
    low-level load failures into a clear, actionable ValueError."""

    @staticmethod
    def _encrypted_pem(passphrase=b"secret"):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend
        key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend())
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase))

    def test_encrypted_key_without_passphrase_raises_clear_error(self):
        with self.assertRaises(ValueError) as cm:
            _private_key_to_unencrypted_pem(self._encrypted_pem(), None)
        self.assertIn("passphrase", str(cm.exception))

    def test_garbage_key_raises_clear_error(self):
        with self.assertRaises(ValueError) as cm:
            _private_key_to_unencrypted_pem(b"not a real pem", None)
        self.assertIn("private key for mTLS", str(cm.exception))

    def test_encrypted_key_with_passphrase_round_trips_to_unencrypted_pem(self):
        pem = _private_key_to_unencrypted_pem(
            self._encrypted_pem(b"secret"), b"secret")
        self.assertIn(b"PRIVATE KEY", pem)
        self.assertNotIn(b"ENCRYPTED", pem)


if __name__ == "__main__":
    unittest.main()
