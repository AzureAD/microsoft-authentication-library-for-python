import ipaddress
import json
import os
import select
import socket
import ssl
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import requests

import msal
from msal import mtls
from sample.confidential_client_mtls_http_client_sample import PolicyHttpClient
from tests.http_client import MinimalResponse


_AUTHORITY = "https://login.microsoftonline.com/tenant"
_METADATA = {
    "authorization_endpoint": _AUTHORITY + "/oauth2/v2.0/authorize",
    "token_endpoint": _AUTHORITY + "/oauth2/v2.0/token",
    "issuer": _AUTHORITY + "/v2.0",
}
_CREDENTIAL = {
    "private_key_pfx_path": os.path.join(
        os.path.dirname(__file__), "certificate-with-password.pfx"),
    "passphrase": "password",
    "public_certificate": True,
}
_TOKEN = '{"access_token": "at", "token_type": "mtls_pop", "expires_in": 3600}'
_TOKEN_PATH = "/tenant/oauth2/v2.0/token"
_ORDINARY_PATH = "/tenant/oauth2/v2.0/ordinary"


@patch("msal.authority.tenant_discovery", return_value=_METADATA)
class TestMtlsHttpClientFactory(unittest.TestCase):
    def setUp(self):
        self.pipeline = Mock(spec=["post"])
        self.pipeline.post.return_value = MinimalResponse(
            status_code=200, text=_TOKEN)
        self.ordinary = Mock(spec=["get", "post", "close", "create_mtls_client"])
        self.ordinary.create_mtls_client.return_value = self.pipeline
        self.ordinary.post.return_value = MinimalResponse(
            status_code=200,
            text='{"access_token": "ordinary", "token_type": "Bearer"}')

    def _app(self, http_client=None, **kwargs):
        return msal.ConfidentialClientApplication(
            "cid", authority=_AUTHORITY, client_credential=_CREDENTIAL,
            http_client=self.ordinary if http_client is None else http_client,
            azure_region=False, instance_discovery=False, **kwargs)

    def test_factory_receives_selected_binding_and_request_options(self, discovery):
        app = self._app(verify=False, timeout=999,
                        proxies={"https": "http://unused.example"})
        self.ordinary.create_mtls_client.assert_not_called()
        result = app.acquire_token_for_client(
            ["s1"], mtls_proof_of_possession=True, timeout=7,
            verify="request-ca.pem", proxies={"https": "http://request.example"})
        binding = self.ordinary.create_mtls_client.call_args.args[0]
        self.assertIsInstance(binding, msal.MtlsBindingContext)
        self.assertEqual(binding.thumbprint_sha256,
                         result["binding_certificate"].thumbprint_sha256)
        self.assertNotIn("PRIVATE KEY", repr(binding))
        request = self.pipeline.post.call_args
        self.assertEqual(
            "https://mtlsauth.microsoft.com/tenant/oauth2/v2.0/token",
            request.args[0])
        self.assertEqual(7, request.kwargs["timeout"])
        self.assertEqual("request-ca.pem", request.kwargs["verify"])
        self.assertEqual({"https": "http://request.example"},
                         request.kwargs["proxies"])
        self.assertIs(False, request.kwargs["allow_redirects"])
        self.assertNotIn("client_assertion", request.kwargs["data"])
        self.assertNotIn("key_id", request.kwargs["data"])
        self.ordinary.post.assert_not_called()

    def test_ordinary_http_and_post_override_do_not_use_factory(self, discovery):
        app = self._app()
        result = app.acquire_token_for_client(["ordinary"])
        self.assertEqual("Bearer", result["token_type"])
        self.assertNotIn("binding_certificate", result)
        self.assertNotIn("allow_redirects", self.ordinary.post.call_args.kwargs)
        post = Mock(return_value=self.ordinary.post.return_value)
        result = app.acquire_token_for_client(["override"], post=post)
        post.assert_called_once()
        self.assertNotIn("binding_certificate", result)
        self.ordinary.create_mtls_client.assert_not_called()
        self.pipeline.post.assert_not_called()

    def test_factory_is_reused_for_network_calls_and_not_called_on_cache_hit(
            self, discovery):
        app = self._app()
        for scope in ("s1", "s1", "s2"):
            result = app.acquire_token_for_client(
                [scope], mtls_proof_of_possession=True)
            self.assertIsInstance(
                result["binding_certificate"], msal.MtlsBindingContext)
        self.ordinary.create_mtls_client.assert_called_once()
        self.assertEqual(2, self.pipeline.post.call_count)
        other = self._app(token_cache=app.token_cache)
        result = other.acquire_token_for_client(
            ["s1"], mtls_proof_of_possession=True)
        self.assertEqual("cache", result["token_source"])
        self.assertIsNone(other._mtls_http_client)
        self.ordinary.create_mtls_client.assert_called_once()

    def test_global_and_regional_clients_share_certificate_pipeline(self, discovery):
        app = self._app()
        with patch.object(app, "_compute_region_to_use",
                          side_effect=[None, "westus3", "westus3"]):
            central = app._get_mtls_client(app.authority)
            regional = app._get_mtls_client(app.authority)
            self.assertIs(regional, app._get_mtls_client(app.authority))
        self.assertIsNot(central, regional)
        self.assertIn("westus3.mtlsauth.microsoft.com",
                      regional.configuration["token_endpoint"])
        self.ordinary.create_mtls_client.assert_called_once()

    def test_concurrent_creation_does_not_orphan_pipelines(self, discovery):
        app = self._app()

        def factory(binding):
            time.sleep(0.02)
            return self.pipeline

        self.ordinary.create_mtls_client.side_effect = factory
        with ThreadPoolExecutor(max_workers=12) as executor:
            clients = list(executor.map(
                lambda _: app._get_mtls_client(app.authority), range(24)))
        self.assertTrue(all(client is clients[0] for client in clients))
        self.ordinary.create_mtls_client.assert_called_once()

    def test_legacy_or_noncallable_capability_fails_before_cache_lookup(
            self, discovery):
        legacy = Mock(spec=["get", "post"])
        malformed = Mock(spec=["get", "post", "create_mtls_client"])
        malformed.create_mtls_client = 42
        for client in (legacy, malformed):
            with self.subTest(client=client):
                app = self._app(http_client=client)
                with patch.object(app, "_acquire_token_silent_with_error") as cache:
                    with self.assertRaisesRegex(ValueError, "create_mtls_client"):
                        app.acquire_token_for_client(
                            ["s1"], mtls_proof_of_possession=True)
                    cache.assert_not_called()
                client.post.assert_not_called()

    def test_malformed_factory_results_fail_without_fallback(self, discovery):
        for result in (None, object(), SimpleNamespace(post=None),
                       SimpleNamespace(post=42)):
            with self.subTest(result=result):
                self.ordinary.create_mtls_client.return_value = result
                app = self._app()
                with self.assertRaisesRegex(TypeError, "callable post"):
                    app.acquire_token_for_client(
                        ["s1"], mtls_proof_of_possession=True)
                self.assertIsNone(app._mtls_http_client)
        self.ordinary.post.assert_not_called()

    def test_factory_cannot_return_ordinary_client_or_msal_wrapper(self, discovery):
        app = self._app()
        for client in (self.ordinary, app.http_client):
            with self.subTest(client=client):
                self.ordinary.create_mtls_client.return_value = client
                with self.assertRaisesRegex(ValueError, "new, dedicated"):
                    app.acquire_token_for_client(
                        ["s1"], mtls_proof_of_possession=True)
                self.assertIsNone(app._mtls_http_client)
        self.ordinary.post.assert_not_called()

    def test_factory_cannot_reuse_client_across_live_applications(self, discovery):
        first, second = self._app(), self._app()
        first._get_mtls_client(first.authority)
        with self.assertRaisesRegex(ValueError, "another application"):
            second._get_mtls_client(second.authority)
        self.assertIsNone(second._mtls_http_client)
        self.pipeline.post.assert_not_called()

    def test_client_identity_tracking_does_not_retain_application(self, discovery):
        import gc
        import weakref

        app = self._app()
        # A post-only, non-weak-referenceable client is a valid pipeline.
        self.ordinary.create_mtls_client.return_value = SimpleNamespace(
            post=self.pipeline.post)
        app._get_mtls_client(app.authority)
        reference = weakref.ref(app)
        del app
        gc.collect()
        self.assertIsNone(reference())

    def test_factory_errors_propagate_and_creation_can_be_retried(self, discovery):
        app = self._app()
        for error in (TypeError("factory type error"), OSError("factory failed"),
                      ValueError("factory configuration error")):
            with self.subTest(error=error):
                self.ordinary.create_mtls_client.side_effect = error
                with self.assertRaises(type(error)) as raised:
                    app.acquire_token_for_client(
                        ["s1"], mtls_proof_of_possession=True)
                self.assertIs(error, raised.exception)
                self.assertIsNone(app._mtls_http_client)
        self.ordinary.create_mtls_client.side_effect = None
        self.assertIn("access_token", app.acquire_token_for_client(
            ["s1"], mtls_proof_of_possession=True))
        self.ordinary.post.assert_not_called()

    def test_request_errors_propagate_without_replacing_pipeline(self, discovery):
        app = self._app()
        error = requests.exceptions.ConnectionError("network policy failure")
        self.pipeline.post.side_effect = [
            error, MinimalResponse(status_code=200, text=_TOKEN)]
        with self.assertRaises(requests.exceptions.ConnectionError) as raised:
            app.acquire_token_for_client(["s1"], mtls_proof_of_possession=True)
        self.assertIs(error, raised.exception)
        self.assertIn("access_token", app.acquire_token_for_client(
            ["s1"], mtls_proof_of_possession=True))
        self.ordinary.create_mtls_client.assert_called_once()
        self.ordinary.post.assert_not_called()

    def test_request_overrides_are_rejected_before_cache_lookup(self, discovery):
        for custom in (False, True):
            app = self._app() if custom else msal.ConfidentialClientApplication(
                "cid", authority=_AUTHORITY, client_credential=_CREDENTIAL,
                azure_region=False, instance_discovery=False)
            self.addCleanup(app.http_client.close)
            for kwargs in ({"post": None}, {"post": Mock()},
                           {"cert": ("other.pem", "other.key")},
                           {"allow_redirects": True}, {"allow_redirects": None}):
                with self.subTest(custom=custom, kwargs=kwargs):
                    with patch.object(
                            app, "_acquire_token_silent_with_error") as cache:
                        with self.assertRaises(ValueError):
                            app.acquire_token_for_client(
                                ["s1"], mtls_proof_of_possession=True, **kwargs)
                        cache.assert_not_called()
        self.ordinary.create_mtls_client.assert_not_called()

    def test_observable_redirects_are_not_cached(self, discovery):
        endpoint = "https://mtlsauth.microsoft.com/tenant/oauth2/v2.0/token"
        for changes in ({"status_code": 307}, {"history": [object()]},
                        {"url": "https://other.example/token"}):
            with self.subTest(changes=changes):
                response = SimpleNamespace(
                    status_code=200, text=_TOKEN, url=endpoint, headers={})
                for name, value in changes.items():
                    setattr(response, name, value)
                pipeline = Mock(spec=["post"])
                pipeline.post.return_value = response
                self.ordinary.create_mtls_client.return_value = pipeline
                app = self._app()
                with self.assertRaisesRegex(ValueError, "must not redirect"):
                    app.acquire_token_for_client(
                        ["s1"], mtls_proof_of_possession=True)
                self.assertEqual({}, app.token_cache._cache.get("AccessToken", {}))
        self.ordinary.post.assert_not_called()

    def test_downgraded_custom_response_has_no_binding_or_cache_entry(self, discovery):
        app = self._app()
        self.pipeline.post.return_value = self.ordinary.post.return_value
        result = app.acquire_token_for_client(
            ["s1"], mtls_proof_of_possession=True)
        self.assertEqual("token_type_mismatch", result["error"])
        self.assertNotIn("binding_certificate", result)
        self.assertEqual({}, app.token_cache._cache.get("AccessToken", {}))

    def test_request_query_parameters_are_not_treated_as_redirects(self, discovery):
        self.pipeline.post.return_value = SimpleNamespace(
            status_code=200, text=_TOKEN, headers={},
            url="https://mtlsauth.microsoft.com/tenant/oauth2/v2.0/token?extra=value")
        result = self._app().acquire_token_for_client(
            ["s1"], mtls_proof_of_possession=True, params={"extra": "value"})
        self.assertEqual("mtls_pop", result["token_type"])
        self.assertEqual({"extra": "value"}, self.pipeline.post.call_args.kwargs["params"])


class TestMtlsPipelineHelpers(unittest.TestCase):
    def test_token_transport_is_restricted_to_https_token_endpoint(self):
        pipeline = Mock(spec=["post"])
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            mtls._MtlsTokenHttpClient(pipeline, "http://token.example")
        client = mtls._MtlsTokenHttpClient(pipeline, "https://token.example/token")
        with self.assertRaisesRegex(ValueError, "only call its token endpoint"):
            client.post("https://token.example/discovery")
        pipeline.post.assert_not_called()

    def test_public_adapter_retains_pool_and_retry_options(self):
        from msal.application import _load_mtls_cert_material

        material = _load_mtls_cert_material(_CREDENTIAL)
        binding = msal.MtlsBindingContext(
            material["cert_pem"], material["private_key_pem"],
            material["x5c"], material["key_id"])
        adapter = binding.create_requests_adapter(
            pool_connections=3, pool_maxsize=7, pool_block=True, max_retries=0)
        self.addCleanup(adapter.close)
        request = requests.Request("POST", "https://token.example/token").prepare()
        with patch("requests.adapters.HTTPAdapter.send"):
            adapter.send(request, verify=False)
            adapter.send(request, verify=False)
        child = adapter._adapters[False]
        self.assertEqual(3, child._pool_connections)
        self.assertEqual(7, child._pool_maxsize)
        self.assertTrue(child._pool_block)
        self.assertEqual(0, child.max_retries.total)
        self.assertEqual(ssl.CERT_NONE, child._ssl_context.verify_mode)
        self.assertEqual(1, len(adapter._adapters))
        for cert in ("other.pem", ("other.pem", "other.key")):
            with self.subTest(cert=cert), self.assertRaisesRegex(
                    ValueError, "cert override"):
                adapter.send(request, cert=cert)


class TestCustomPipelineLocalTls(unittest.TestCase):
    """Real loopback TLS, with no lab credentials or external network access."""

    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        def key():
            return rsa.generate_private_key(public_exponent=65537, key_size=2048)

        def certificate(name, private_key, issuer, issuer_key, ca=False, server=False):
            now = datetime.now(timezone.utc)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            builder = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(issuer.subject if issuer else subject)
                .public_key(private_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                    private_key.public_key()), False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    issuer_key.public_key()), False)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=not ca, data_encipherment=False,
                    key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                    encipher_only=False, decipher_only=False), True))
            if not ca:
                builder = builder.add_extension(x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.SERVER_AUTH if server
                    else ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            if server:
                builder = builder.add_extension(x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            return builder.sign(issuer_key, hashes.SHA256())

        def pem(private_key):
            return private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption())

        root_key = key()
        root = certificate("test root", root_key, None, root_key, ca=True)
        cls.root_pem = root.public_bytes(serialization.Encoding.PEM)
        server_key = key()
        server = certificate(
            "localhost", server_key, root, root_key, server=True)
        cls.server_pem = server.public_bytes(serialization.Encoding.PEM) + pem(server_key)
        cls.credentials = []
        cls.client_der = []
        for name in ("first client", "rotated client"):
            private_key = key()
            cert = certificate(name, private_key, root, root_key)
            cls.credentials.append({
                "private_key": pem(private_key).decode("ascii"),
                "public_certificate": cert.public_bytes(
                    serialization.Encoding.PEM).decode("ascii"),
                "thumbprint": cert.fingerprint(hashes.SHA1()).hex(),
            })
            cls.client_der.append(cert.public_bytes(serialization.Encoding.DER))

    def _start_server(self, handler, context=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        if context is not None:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ca_path = os.path.join(directory.name, "ca.pem")
        server_path = os.path.join(directory.name, "server.pem")
        for path, content in ((self.ca_path, self.root_pem),
                              (server_path, self.server_pem)):
            with open(path, "wb") as stream:
                stream.write(content)
        environment = patch.dict(os.environ, {
            "REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": "",
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": ""})
        environment.start()
        self.addCleanup(environment.stop)
        owner = self
        self.records = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.do_POST()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = parse_qs(body.decode("ascii"))
                certificate = self.connection.getpeercert(binary_form=True)
                owner.records.append({
                    "path": self.path, "certificate": certificate, "data": data,
                    "port": self.client_address[1],
                    "middleware": self.headers.get("X-Msal-Pipeline"),
                })
                status = 200
                response = {
                    "access_token": "local-at", "token_type": "Bearer",
                    "expires_in": 3600}
                if self.path == "/metadata":
                    response = dict(_METADATA, token_endpoint=owner.url + _ORDINARY_PATH)
                elif data.get("scope") == ["redirect"]:
                    status = 307
                    response = {}
                elif self.path == _TOKEN_PATH:
                    if not certificate or (data.get("scope") == ["reject-first"]
                            and certificate == owner.client_der[0]):
                        status, response = 400, {"error": "invalid_client"}
                    else:
                        response["token_type"] = "mtls_pop"
                payload = json.dumps(response).encode("utf-8")
                self.send_response(status)
                if status == 307:
                    self.send_header("Location", owner.url + "/forbidden")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_path)
        context.load_verify_locations(cafile=self.ca_path)
        context.verify_mode = ssl.CERT_OPTIONAL
        self.server = self._start_server(Handler, context)
        self.url = "https://127.0.0.1:{}".format(self.server.server_port)
        self.http_client = PolicyHttpClient(
            verify=self.ca_path, timeout=(2, 3), trust_env=False)
        self.addCleanup(self.http_client.close)

    def _app(self, http_client=None, certificate=0, discover=True, **kwargs):
        def discovery(endpoint, client):
            response = client.get(self.url + "/metadata")
            response.raise_for_status()
            return json.loads(response.text)

        with patch("msal.authority.tenant_discovery",
                   side_effect=discovery if discover else None,
                   return_value=dict(_METADATA, token_endpoint=self.url + _ORDINARY_PATH)):
            return msal.ConfidentialClientApplication(
                "cid", authority=_AUTHORITY,
                client_credential=self.credentials[certificate],
                http_client=self.http_client if http_client is None else http_client,
                azure_region=False, instance_discovery=False, **kwargs)

    def _acquire(self, app, scope="s1", **kwargs):
        with patch("msal.mtls.transform_token_endpoint",
                   return_value=self.url + _TOKEN_PATH):
            return app.acquire_token_for_client(
                [scope], mtls_proof_of_possession=True, **kwargs)

    def test_custom_pipeline_preserves_certificate_middleware_policy_and_pooling(self):
        adapter = self.http_client._session.adapters["https://"]
        app = self._app(verify=False, timeout=0.001,
                        proxies={"https": "http://unused.example"})
        ordinary = app.acquire_token_for_client(["ordinary"])
        first = self._acquire(app)
        child = self.http_client._children[0]
        with patch.object(child._session, "send", wraps=child._session.send) as send:
            self._acquire(app, "s2")
        self.assertEqual((2, 3), send.call_args.kwargs["timeout"])
        self.assertEqual(self.ca_path, send.call_args.kwargs["verify"])
        self.assertEqual(2, child.response_count)
        self.assertIs(adapter, self.http_client._session.adapters["https://"])
        self.assertEqual(1, len(self.http_client._children))
        self.assertNotIn("binding_certificate", ordinary)
        self.assertIsInstance(first["binding_certificate"], msal.MtlsBindingContext)
        self.assertEqual(
            ["/metadata", _ORDINARY_PATH, _TOKEN_PATH, _TOKEN_PATH],
            [record["path"] for record in self.records])
        self.assertTrue(all(record["middleware"] == "custom-policy"
                            for record in self.records))
        self.assertTrue(all(record["certificate"] is None for record in self.records[:2]))
        self.assertTrue(all(record["certificate"] == self.client_der[0]
                            for record in self.records[2:]))
        self.assertEqual(self.records[2]["port"], self.records[3]["port"])
        self.assertNotEqual(self.records[1]["port"], self.records[2]["port"])
        self._acquire(app)
        self.assertEqual(4, len(self.records), "A cache hit must not use HTTP")

    def test_certificate_rotation_isolates_connections_tokens_and_http_errors(self):
        token_cache, http_cache = msal.TokenCache(), {}
        first = self._app(token_cache=token_cache, http_cache=http_cache)
        rotated = self._app(certificate=1, token_cache=token_cache, http_cache=http_cache)
        results = [self._acquire(app) for app in (first, rotated)]
        self.assertNotEqual(results[0]["binding_certificate"].thumbprint_sha256,
                            results[1]["binding_certificate"].thumbprint_sha256)
        self._acquire(first, "s2")
        tokens = [record for record in self.records if record["path"] == _TOKEN_PATH]
        self.assertEqual(
            [self.client_der[0], self.client_der[1], self.client_der[0]],
            [record["certificate"] for record in tokens])
        self.assertEqual(tokens[0]["port"], tokens[2]["port"])
        self.assertNotEqual(tokens[0]["port"], tokens[1]["port"])
        self.assertEqual(2, len(self.http_client._children))
        for _ in range(2):
            self.assertEqual("invalid_client", self._acquire(first, "reject-first")["error"])
        self.assertEqual("mtls_pop", self._acquire(rotated, "reject-first")["token_type"])
        failures = [record for record in self.records
                    if record["data"].get("scope") == ["reject-first"]]
        self.assertEqual([self.client_der[0], self.client_der[1]],
                         [record["certificate"] for record in failures])
        cached = self._acquire(first)
        self.assertEqual("cache", cached["token_source"])
        self.assertEqual(results[0]["binding_certificate"].thumbprint_sha256,
                         cached["binding_certificate"].thumbprint_sha256)

    def test_effective_ca_settings_and_verification_pools(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        expected_root = x509.load_pem_x509_certificate(self.root_pem).public_bytes(
            serialization.Encoding.DER)
        for setting in ("session", "request", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            environment = {"REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": ""}
            if setting in environment:
                environment[setting] = self.ca_path
            with self.subTest(setting=setting), patch.dict(os.environ, environment):
                with PolicyHttpClient(
                        verify=self.ca_path if setting == "session" else True,
                        timeout=(2, 3)) as client:
                    app = self._app(http_client=client, discover=False)
                    options = {"verify": self.ca_path} if setting == "request" else {}
                    self._acquire(app, **options)
                    adapter = client._children[0]._session.adapters["https://"]
                    verified = adapter._adapters[self.ca_path]
                    self.assertEqual([expected_root],
                        verified._ssl_context.get_ca_certs(binary_form=True))
                    with self.assertWarns(requests.packages.urllib3.exceptions.InsecureRequestWarning):
                        self._acquire(app, "unverified", verify=False)
                    self._acquire(app, "verified-again", **options)
                    self.assertIs(verified, adapter._adapters[self.ca_path])
                    self.assertEqual(ssl.CERT_NONE,
                                     adapter._adapters[False]._ssl_context.verify_mode)
                    records = self.records[-3:]
                    self.assertEqual(records[0]["port"], records[2]["port"])
                    self.assertNotEqual(records[0]["port"], records[1]["port"])
                    self.assertTrue(all(record["certificate"] == self.client_der[0]
                                        for record in records))

    def test_untrusted_server_failure_does_not_fall_back(self):
        app = self._app()
        with self.assertRaises(requests.exceptions.SSLError):
            self._acquire(app, verify=True)
        self.assertEqual(["/metadata"], [record["path"] for record in self.records])
        self.assertEqual(1, len(self.http_client._children))
        self.assertEqual("mtls_pop", self._acquire(app)["token_type"])
        self.assertEqual(self.client_der[0], self.records[-1]["certificate"])

    def test_default_and_custom_pipelines_do_not_follow_token_redirects(self):
        custom = self._app()
        with patch("msal.authority.tenant_discovery",
                   return_value=dict(_METADATA, token_endpoint=self.url + _ORDINARY_PATH)):
            default = msal.ConfidentialClientApplication(
                "cid", authority=_AUTHORITY, client_credential=self.credentials[0],
                verify=self.ca_path, timeout=(2, 3), azure_region=False,
                instance_discovery=False)
        self.addCleanup(default.http_client.close)
        for app in (default, custom):
            with self.subTest(custom=app is custom):
                self.assertEqual("mtls_pop", self._acquire(app)["token_type"])
                error_type = (ValueError if app is custom
                              else requests.exceptions.HTTPError)
                error_message = ("must not redirect" if app is custom
                                 else "does not support HTTP redirects")
                with self.assertRaisesRegex(error_type, error_message):
                    self._acquire(app, "redirect")
        default._mtls_http_client.close()
        self.assertNotIn("/forbidden", [record["path"] for record in self.records])
        tokens = [record for record in self.records if record["path"] == _TOKEN_PATH]
        self.assertEqual(4, len(tokens))
        self.assertTrue(all(record["certificate"] == self.client_der[0] for record in tokens))

    def test_factory_owns_lifecycle_and_closing_child_does_not_close_ordinary(self):
        app = self._app()
        self._acquire(app)
        child = self.http_client._children[0]
        child.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self._acquire(app, "after-close")
        self.assertEqual(1, len(self.http_client._children))
        response = self.http_client.get(self.url + _ORDINARY_PATH)
        self.assertEqual(200, response.status_code)
        self.assertIsNone(self.records[-1]["certificate"])
        self.http_client.close()
        self.http_client.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.http_client.create_mtls_client(None)
        self.assertTrue(child._closed)

    def test_https_proxy_preserves_custom_pipeline_and_certificate(self):
        target = self.server.server_address
        tunnels = []

        class ProxyHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                if self.path != "{}:{}".format(*target):
                    self.send_error(403)
                    return
                tunnels.append(self.path)
                with socket.create_connection(target, timeout=3) as upstream:
                    self.send_response(200)
                    self.end_headers()
                    self.close_connection = True
                    sockets = (self.connection, upstream)
                    while True:
                        readable, _, _ = select.select(sockets, [], [], 3)
                        if not readable:
                            return
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            destination = upstream if source is self.connection else self.connection
                            destination.sendall(data)

        proxy = self._start_server(ProxyHandler)
        proxies = {"https": "http://127.0.0.1:{}".format(proxy.server_port)}
        with PolicyHttpClient(
                verify=self.ca_path, proxies=proxies, timeout=(2, 3),
                trust_env=False) as client:
            app = self._app(http_client=client)
            result = self._acquire(app)
            self.assertEqual("mtls_pop", result["token_type"])
            self.assertEqual(proxies, client._children[0]._session.proxies)
            self.assertEqual(2, len(tunnels))
            self.assertEqual([None, self.client_der[0]],
                             [record["certificate"] for record in self.records])
            self.assertTrue(all(record["middleware"] == "custom-policy"
                                for record in self.records))


if __name__ == "__main__":
    unittest.main()
