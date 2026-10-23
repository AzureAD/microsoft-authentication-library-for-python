import base64
import io
import json
import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock

from msal import mtls, MtlsBindingContext
from msal.application import (
    _is_certificate_issuer, _load_mtls_cert_material,
    _private_key_to_unencrypted_pem, extract_certs)


class TestMtlsEndpointTransform(unittest.TestCase):
    """The login.* -> [region.]mtlsauth.* transform and the sovereign guardrail.

    These values mirror MSAL.NET's RegionAndMtlsDiscoveryProvider so MSAL Python
    stays wire-compatible across SDKs.
    """

    def test_public_hosts_normalize_to_global_mtls_host(self):
        for host in [
            "login.microsoftonline.com", "login.microsoft.com",
            "login.windows.net",
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
        self.assertIn("not supported", str(cm.exception))

    def test_unsupported_login_hosts_fail_fast(self):
        for host in ("login.example.com", "login.partner.microsoftonline.cn",
                     "login.microsoftonline.de"):
            with self.subTest(host=host), self.assertRaisesRegex(
                    ValueError, "not supported"):
                mtls.transform_token_endpoint(
                    "https://{}/tenant/token".format(host), host)

    def test_non_login_host_fails_fast(self):
        for host in ("contoso.example.com", "sts.windows.net", "STS.Windows.Net"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                mtls.mtls_pop_host(host)


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

    def test_pem_certificate_loader_supports_required_backend(self):
        from cryptography import x509

        material = self._material()
        load = x509.load_pem_x509_certificate

        def legacy_load(data, backend):
            return load(data, backend=backend)

        with patch("cryptography.x509.load_pem_x509_certificate",
                   side_effect=legacy_load):
            loaded = _load_mtls_cert_material({
                "private_key": material["private_key_pem"].decode("ascii"),
                "public_certificate": material["cert_pem"].decode("ascii"),
            })
        self.assertEqual(material["key_id"], loaded["key_id"])

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

    def test_selected_pool_preserves_certificate_context_on_requests_2322(self):
        import requests
        from requests.adapters import HTTPAdapter

        material = self._material()
        context = mtls._create_ssl_context(
            material["cert_pem"], material["private_key_pem"])
        adapter = mtls._make_mtls_adapter(context)
        self.addCleanup(adapter.close)
        request = requests.Request("GET", "https://resource.example/path").prepare()
        preloaded = ssl.create_default_context()

        def legacy_connection(adapter, request, verify, proxies=None, cert=None):
            return adapter.poolmanager.connection_from_url(
                request.url, pool_kwargs={"ssl_context": preloaded})

        with patch.object(HTTPAdapter, "get_connection_with_tls_context",
                          legacy_connection, create=True):
            for proxies in (None, {"https": "http://proxy.example:8080"}):
                with self.subTest(proxies=proxies):
                    pool = adapter.get_connection_with_tls_context(
                        request, verify=True, proxies=proxies)
                    self.assertIs(context, pool.conn_kw["ssl_context"])

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

    def test_unsupported_mtls_dependencies_fail_with_upgrade_instructions(self):
        binding = MtlsBindingContext(b"cert", b"key", [], "thumbprint")
        unsupported = (
            patch("requests.__version__", "2.0.0"),
            patch("requests.Session.merge_environment_settings", None),
            patch("urllib3.connection.HTTPSConnection.__init__",
                  lambda self, host: None),
        )
        for dependency in unsupported:
            with dependency:
                for factory in (
                        lambda: mtls._MtlsHttpClient(b"cert", b"key"),
                        binding.create_requests_session):
                    with self.subTest(factory=factory), self.assertRaisesRegex(
                            ValueError, r"mTLS requires Requests >= 2\.25\.0.*"
                                        r"python -m pip install --upgrade"):
                        factory()

    def test_binding_context_exposes_metadata_without_key_material(self):
        material = self._material()
        binding = MtlsBindingContext(
            material["cert_pem"],
            material["private_key_pem"],
            material["x5c"],
            material["key_id"])
        self.assertEqual(material["x5c"], binding.x5c)
        self.assertEqual(material["x5c"], binding["x5c"])
        self.assertEqual(material["key_id"], binding.thumbprint_sha256)
        self.assertEqual(material["key_id"], binding["thumbprint_sha256"])
        self.assertEqual(
            {"x5c", "thumbprint_sha256"}, set(binding))
        self.assertNotIn("PRIVATE KEY", repr(binding))

    def test_binding_context_creates_ssl_context(self):
        material = self._material()
        binding = MtlsBindingContext(
            material["cert_pem"],
            material["private_key_pem"],
            material["x5c"],
            material["key_id"])
        self.assertIsInstance(binding.create_ssl_context(), ssl.SSLContext)

    def test_binding_context_creates_requests_session(self):
        material = self._material()
        binding = MtlsBindingContext(
            material["cert_pem"],
            material["private_key_pem"],
            material["x5c"],
            material["key_id"])
        adapter = Mock()
        with patch("msal.mtls._make_mtls_adapter", return_value=adapter):
            session = binding.create_requests_session(
                verify=False, proxies={"https": "https://proxy.example"})
        try:
            self.assertFalse(session.verify)
            self.assertEqual(
                "https://proxy.example", session.proxies["https"])
            self.assertIs(adapter, session.adapters["https://"])
        finally:
            session.close()


class TestMtlsCertificateChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        def make_cert(name, key, issuer, issuer_key, ca):
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            now = datetime.now(timezone.utc)
            return (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(issuer or subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    issuer_key.public_key()), False)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                    encipher_only=False, decipher_only=False), True)
                .sign(issuer_key, hashes.SHA256()))

        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        root = make_cert("root", root_key, None, root_key, True)
        cls.root = root
        upper_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.upper_intermediate = make_cert(
            "upper", upper_key, root.subject, root_key, True)
        intermediate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.intermediate = make_cert(
            "intermediate", intermediate_key, cls.upper_intermediate.subject, upper_key, True)
        unrelated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.unrelated = make_cert("unrelated", unrelated_key, None, unrelated_key, True)
        cls.impostor = make_cert("intermediate", unrelated_key, None, unrelated_key, True)
        cls.leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.leaf = make_cert(
            "client", cls.leaf_key, cls.intermediate.subject, intermediate_key, False)
        cls.chain_pem = b"".join(cert.public_bytes(serialization.Encoding.PEM)
            for cert in (cls.leaf, cls.intermediate, cls.upper_intermediate))
        cls.key_pem = _private_key_to_unencrypted_pem(cls.leaf_key)
        cls.root_pem = root.public_bytes(serialization.Encoding.PEM)
        cls.server_pem = cls.root_pem + _private_key_to_unencrypted_pem(root_key)
        cls.key_id = base64.urlsafe_b64encode(
            cls.leaf.fingerprint(hashes.SHA256())).rstrip(b"=").decode("ascii")

    def _assert_chain_and_handshake(self, material):
        self.assertEqual(self.chain_pem, material["cert_pem"])
        self.assertEqual(extract_certs(self.chain_pem.decode("ascii")), material["x5c"])
        self.assertEqual(self.key_id, material["key_id"])
        client = mtls._create_ssl_context(
            material["cert_pem"], material["private_key_pem"], verify=False)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.verify_mode = ssl.CERT_REQUIRED
        server.load_verify_locations(cadata=self.root_pem.decode("ascii"))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "server.pem")
            with open(path, "wb") as stream:
                stream.write(self.server_pem)
            server.load_cert_chain(path)
        client_in, client_out, server_in, server_out = [
            ssl.MemoryBIO() for _ in range(4)]
        client_tls = client.wrap_bio(client_in, client_out, server_side=False)
        server_tls = server.wrap_bio(server_in, server_out, server_side=True)
        client_done = server_done = False
        for _ in range(10):
            if not client_done:
                try:
                    client_tls.do_handshake()
                    client_done = True
                except ssl.SSLWantReadError:
                    pass
            if client_out.pending:
                server_in.write(client_out.read())
            if not server_done:
                try:
                    server_tls.do_handshake()
                    server_done = True
                except ssl.SSLWantReadError:
                    pass
            if server_out.pending:
                client_in.write(server_out.read())
            if client_done and server_done:
                break
        self.assertTrue(client_done and server_done, "mTLS handshake did not complete")

    def test_pem_intermediates_reach_tls_handshake(self):
        self._assert_chain_and_handshake(_load_mtls_cert_material({
            "private_key": self.key_pem.decode("ascii"),
            "public_certificate": self.chain_pem.decode("ascii"),
            }))

    def test_non_mtls_assertion_preserves_pem_chain(self):
        from cryptography.hazmat.primitives import hashes
        from msal.application import ConfidentialClientApplication
        from tests.http_client import MinimalResponse

        endpoint = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
        expected_chain = extract_certs(self.chain_pem.decode("ascii"))
        with patch("msal.authority.tenant_discovery", return_value={
                "authorization_endpoint": endpoint.replace("/token", "/authorize"),
                "token_endpoint": endpoint,
                "issuer": "https://login.microsoftonline.com/tenant/v2.0"}):
            app = ConfidentialClientApplication(
                "cid", authority="https://login.microsoftonline.com/tenant",
                azure_region=False, client_credential={
                    "private_key": self.key_pem.decode("ascii"),
                    "thumbprint": self.leaf.fingerprint(hashes.SHA1()).hex(),
                    "public_certificate": self.chain_pem.decode("ascii"),
                })

            def post(url, data=None, **kwargs):
                header = data["client_assertion"].split(b".")[0]
                decoded = json.loads(base64.urlsafe_b64decode(
                    header + b"=" * (-len(header) % 4)))
                self.assertEqual(expected_chain, decoded["x5c"])
                return MinimalResponse(
                    status_code=200, text='{"access_token": "at", "token_type": "Bearer"}')

            app.acquire_token_for_client(["s1"], post=post)

    def test_pfx_intermediates_reach_tls_handshake(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import pkcs12
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "chain.pfx")
            with open(path, "wb") as stream:
                stream.write(pkcs12.serialize_key_and_certificates(
                    b"client", self.leaf_key, self.leaf,
                    [self.intermediate, self.upper_intermediate],
                    serialization.NoEncryption()))
            material = _load_mtls_cert_material({"private_key_pfx_path": path})
        self._assert_chain_and_handshake(material)

    def test_unordered_pfx_chain_preserves_non_mtls_leaf_only_assertion(self):
        from cryptography.hazmat.primitives import serialization
        from msal.application import ConfidentialClientApplication
        from tests.http_client import MinimalResponse

        endpoint = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
        path = os.path.join(os.path.dirname(__file__), "certificate-with-password.pfx")
        expected_pem = (self.chain_pem + self.root_pem).decode("ascii")
        additional = [
            self.root, self.impostor, self.upper_intermediate,
            self.unrelated, self.intermediate, self.leaf, self.intermediate]
        with patch("cryptography.hazmat.primitives.serialization.pkcs12."
                   "load_key_and_certificates",
                   return_value=(self.leaf_key, self.leaf, additional)):
            material = _load_mtls_cert_material({"private_key_pfx_path": path})
            self.assertEqual(expected_pem.encode("ascii"), material["cert_pem"])
            self.assertEqual(extract_certs(expected_pem), material["x5c"])
            self.assertEqual(self.key_id, material["key_id"])
            with patch("msal.authority.tenant_discovery", return_value={
                    "authorization_endpoint": endpoint.replace("/token", "/authorize"),
                    "token_endpoint": endpoint,
                    "issuer": "https://login.microsoftonline.com/tenant/v2.0"}):
                app = ConfidentialClientApplication(
                    "cid", authority="https://login.microsoftonline.com/tenant",
                    azure_region=False, client_credential={
                        "private_key_pfx_path": path, "public_certificate": True})

                def post(url, data=None, **kwargs):
                    self.assertEqual(endpoint, url)
                    header = data["client_assertion"].split(b".")[0]
                    decoded = json.loads(base64.urlsafe_b64decode(
                        header + b"=" * (-len(header) % 4)))
                    leaf_pem = self.leaf.public_bytes(
                        serialization.Encoding.PEM).decode("ascii")
                    self.assertEqual(extract_certs(leaf_pem), decoded["x5c"])
                    return MinimalResponse(
                        status_code=200,
                        text='{"access_token": "at", "token_type": "Bearer"}')

                app.acquire_token_for_client(["s1"], post=post)

    def test_issuer_signature_verification_on_older_cryptography(self):
        class LegacyCertificate:
            issuer = self.leaf.issuer
            signature = self.leaf.signature
            tbs_certificate_bytes = self.leaf.tbs_certificate_bytes
            signature_algorithm_oid = self.leaf.signature_algorithm_oid
            signature_hash_algorithm = self.leaf.signature_hash_algorithm

        self.assertFalse(_is_certificate_issuer(LegacyCertificate(), self.impostor))
        self.assertTrue(_is_certificate_issuer(LegacyCertificate(), self.intermediate))

    def test_environment_ca_bundles_replace_default_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "private-ca.pem")
            with open(path, "wb") as stream:
                stream.write(self.root_pem)
            for variable in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                with self.subTest(variable=variable), patch.dict(
                        os.environ, {"REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": "",
                                     variable: path}):
                    client = mtls._MtlsHttpClient(self.chain_pem, self.key_pem)
                    self.addCleanup(client.close)
                    session = client._ensure_session("https://mtlsauth.microsoft.com")
                    context = session.get_adapter("https://")._ssl_context
                    self.assertEqual(1, len(context.get_ca_certs()))
                    self.assertEqual(
                        session, client._ensure_session("https://mtlsauth.microsoft.com"))
                    unverified = client._ensure_session(
                        "https://mtlsauth.microsoft.com", verify=False)
                    self.assertIsNot(session, unverified)
                    self.assertEqual(ssl.CERT_NONE,
                        unverified.get_adapter("https://")._ssl_context.verify_mode)

    def test_pathlike_ca_bundle_replaces_roots_and_reuses_transport_session(self):
        from cryptography.hazmat.primitives import serialization

        binding = MtlsBindingContext(
            self.chain_pem, self.key_pem,
            extract_certs(self.chain_pem.decode("ascii")), self.key_id)
        expected = [self.root.public_bytes(serialization.Encoding.DER)]
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": ""}):
            path = Path(directory) / "private-ca.pem"
            path.write_bytes(self.root_pem)
            for verify in (path, os.fsencode(path)):
                with self.subTest(verify=verify):
                    context = binding.create_ssl_context(verify=verify)
                    self.assertEqual(expected, context.get_ca_certs(binary_form=True))
                    client = mtls._MtlsHttpClient(
                        self.chain_pem, self.key_pem, verify=verify)
                    self.addCleanup(client.close)
                    session = client._ensure_session("https://mtlsauth.microsoft.com")
                    self.assertEqual(
                        expected, session.get_adapter("https://")._ssl_context
                        .get_ca_certs(binary_form=True))
                    self.assertIs(session, client._ensure_session(
                        "https://mtlsauth.microsoft.com", verify=str(path)))

    def test_binding_session_uses_only_effective_ca_bundle(self):
        from cryptography.hazmat.primitives import serialization
        from urllib3.response import HTTPResponse

        binding = MtlsBindingContext(
            self.chain_pem, self.key_pem,
            extract_certs(self.chain_pem.decode("ascii")), self.key_id)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "private-ca.pem")
            with open(path, "wb") as stream:
                stream.write(self.root_pem)
            for setting in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                            "session", "request", "session_path", "request_path"):
                environment = {"REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": ""}
                if setting in environment:
                    environment[setting] = path
                with self.subTest(setting=setting), patch.dict(
                        os.environ, environment):
                    contexts = []

                    def urlopen(pool, *args, **kwargs):
                        contexts.append(pool.conn_kw["ssl_context"])
                        return HTTPResponse(
                            body=io.BytesIO(b"ok"), status=200, preload_content=False)

                    with binding.create_requests_session(
                            verify=(Path(path) if setting == "session_path"
                                    else path if setting == "session"
                                    else True)) as session:
                        with patch("urllib3.connectionpool.HTTPSConnectionPool.urlopen",
                                   autospec=True, side_effect=urlopen):
                            options = (
                                {"verify": Path(path)} if setting == "request_path"
                                else {"verify": path} if setting == "request" else {})
                            session.get("https://resource.example", **options)
                            session.get("https://resource.example", verify=False)
                            if setting in ("session_path", "request_path"):
                                options = {"verify": path}
                            session.get("https://resource.example", **options)
                    self.assertEqual(
                        [self.root.public_bytes(serialization.Encoding.DER)],
                        contexts[0].get_ca_certs(binary_form=True))
                    self.assertEqual(ssl.CERT_REQUIRED, contexts[0].verify_mode)
                    self.assertEqual(ssl.CERT_NONE, contexts[1].verify_mode)
                    self.assertIs(contexts[0], contexts[2])


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
