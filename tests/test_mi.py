import hashlib
import json
import os
import select
import socket
import ssl
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
import unittest
try:
    from unittest.mock import patch, ANY, mock_open, Mock
except:
    from mock import patch, ANY, mock_open, Mock
import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.exceptions import SSLError
from urllib3.util.retry import Retry
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tests.test_throttled_http_client import (
    MinimalResponse, ThrottledHttpClientBaseTestCase, DummyHttpClient)
from msal import (
    SystemAssignedManagedIdentity, UserAssignedManagedIdentity,
    ManagedIdentityClient,
    ManagedIdentityError,
    ServiceFabricHttpOptions,
    ArcPlatformNotSupportedError,
)
from msal.managed_identity import (
    _ThrottledHttpClient,
    _supported_arc_platforms_and_their_prefixes,
    get_managed_identity_source,
    APP_SERVICE,
    AZURE_ARC,
    CLOUD_SHELL,
    MACHINE_LEARNING,
    SERVICE_FABRIC,
    DEFAULT_TO_VM,
    _create_service_fabric_http_client,
    _obtain_token_on_service_fabric,
    _ServiceFabricHTTPAdapter,
    _ServiceFabricHTTPSConnection,
    _create_owned_service_fabric_http_client,
)
from msal.token_cache import is_subdict_of

EXPECTED_SKU = "MSAL.Python"  # Hardcoded constant, not imported from product


class ManagedIdentityTestCase(unittest.TestCase):
    def test_helper_class_should_be_interchangable_with_dict_which_could_be_loaded_from_file_or_env_var(self):
        self.assertEqual(
            UserAssignedManagedIdentity(client_id="foo"),
            {"ManagedIdentityIdType": "ClientId", "Id": "foo"})
        self.assertEqual(
            UserAssignedManagedIdentity(resource_id="foo"),
            {"ManagedIdentityIdType": "ResourceId", "Id": "foo"})
        self.assertEqual(
            UserAssignedManagedIdentity(object_id="foo"),
            {"ManagedIdentityIdType": "ObjectId", "Id": "foo"})
        with self.assertRaises(ManagedIdentityError):
            UserAssignedManagedIdentity()
        with self.assertRaises(ManagedIdentityError):
            UserAssignedManagedIdentity(client_id="foo", resource_id="bar")
        self.assertEqual(
            SystemAssignedManagedIdentity(),
            {"ManagedIdentityIdType": "SystemAssigned", "Id": None})


class ThrottledHttpClientTestCase(ThrottledHttpClientBaseTestCase):
    def test_throttled_http_client_should_not_alter_original_http_client(self):
        self.assertNotAlteringOriginalHttpClient(_ThrottledHttpClient)

    def test_throttled_http_client_should_not_cache_successful_http_response(self):
        http_cache = {}
        http_client=DummyHttpClient(
            status_code=200,
            response_text='{"access_token": "AT", "expires_in": "1234", "resource": "R"}',
            )
        app = ManagedIdentityClient(
            SystemAssignedManagedIdentity(), http_client=http_client, http_cache=http_cache)
        result = app.acquire_token_for_client(resource="R")
        self.assertEqual("AT", result["access_token"])
        self.assertEqual({}, http_cache, "Should not cache successful http response")

    def test_throttled_http_client_should_cache_unsuccessful_http_response(self):
        http_cache = {}
        http_client=DummyHttpClient(
            status_code=400,
            response_headers={"Retry-After": "1"},
            response_text='{"error": "invalid_request"}',
            )
        app = ManagedIdentityClient(
            SystemAssignedManagedIdentity(), http_client=http_client, http_cache=http_cache)
        result = app.acquire_token_for_client(resource="R")
        self.assertEqual("invalid_request", result["error"])
        self.assertNotEqual({}, http_cache, "Should cache unsuccessful http response")
        self.assertCleanPickle(http_cache)


class ClientTestCase(unittest.TestCase):
    maxDiff = None

    def _build_app(
        self,
        *,
        client_capabilities: Optional[List[str]] = None,
    ):
        return ManagedIdentityClient(
            {   # Here we test it with the raw dict form, to test that
                # the client has no hard dependency on ManagedIdentity object
                "ManagedIdentityIdType": "SystemAssigned", "Id": None,
            },
            http_client=requests.Session(),
            client_capabilities=client_capabilities,
            )

    def setUp(self):
        self.app = self._build_app()

    def test_error_out_on_invalid_input(self):
        with self.assertRaises(ManagedIdentityError):
            ManagedIdentityClient({"foo": "bar"}, http_client=requests.Session())
        with self.assertRaises(ManagedIdentityError):
            ManagedIdentityClient(
                {"ManagedIdentityIdType": "undefined", "Id": "foo"},
                http_client=requests.Session())

    def assertCacheStatus(self, app):
        cache = app._token_cache._cache
        self.assertEqual(1, len(cache.get("AccessToken", [])), "Should have 1 AT")
        at = list(cache["AccessToken"].values())[0]
        self.assertEqual(
            app._managed_identity.get("Id", "SYSTEM_ASSIGNED_MANAGED_IDENTITY"),
            at["client_id"],
            "Should have expected client_id")
        self.assertEqual("managed_identity", at["realm"], "Should have expected realm")

    def _test_happy_path(
        self, app, mocked_http, expires_in, *, resource="R", claims_challenge=None,
    ):
        """It tests a normal token request that is expected to hit IdP,
        a subsequent same token request that is expected to hit cache,
        and then a request with claims_challenge that shall hit IdP again.
        """
        result = app.acquire_token_for_client(resource=resource)
        mocked_http.assert_called()
        call_count = mocked_http.call_count
        expected_result = {
            "access_token": "AT",
            "token_type": "Bearer",
        }
        self.assertTrue(
            is_subdict_of(expected_result, result),  # We will test refresh_on later
            "Should obtain a token response")
        self.assertTrue(result["token_source"], "identity_provider")
        self.assertEqual(expires_in, result["expires_in"], "Should have expected expires_in")
        if expires_in >= 7200:
            expected_refresh_on = int(time.time() + expires_in / 2)
            self.assertTrue(
                expected_refresh_on - 1 <= result["refresh_on"] <= expected_refresh_on + 1,
                "Should have a refresh_on time around the middle of the token's life")

        result = app.acquire_token_for_client(resource=resource)
        self.assertCacheStatus(app)
        self.assertEqual("cache", result["token_source"], "Should hit cache")
        self.assertEqual(
            call_count, mocked_http.call_count,
            "No new call to the mocked http should be made for a cache hit")
        self.assertTrue(
            is_subdict_of(expected_result, result),  # We will test refresh_on later
            "Should obtain a token response")
        self.assertTrue(
            expires_in - 5 < result["expires_in"] <= expires_in,
            "Should have similar expires_in")
        if expires_in >= 7200:
            self.assertTrue(
                expected_refresh_on - 5 < result["refresh_on"] <= expected_refresh_on,
                "Should have a refresh_on time around the middle of the token's life")

        result = app.acquire_token_for_client(
            resource=resource, claims_challenge=claims_challenge or "placeholder")
        self.assertEqual("identity_provider", result["token_source"], "Should miss cache")


class VmTestCase(ClientTestCase):

    def _test_happy_path(self) -> callable:
        expires_in = 7890  # We test a bigger than 7200 value here
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_in": "%s", "resource": "R"}' % expires_in,
        )) as mocked_method:
            super(VmTestCase, self)._test_happy_path(self.app, mocked_method, expires_in)
            return mocked_method

    def test_happy_path_of_vm(self):
        mock_get = self._test_happy_path()
        mock_get.assert_called_with(
            # The last call contained claims_challenge
            # but since IMDS doesn't support token_sha256_to_refresh,
            # the request shall remain the same as before
            'http://169.254.169.254/metadata/identity/oauth2/token',
            params={'api-version': '2018-02-01', 'resource': 'R'},
            headers={
                'Metadata': 'true',
                'x-client-SKU': EXPECTED_SKU,
                'x-client-Ver': ANY,
                'x-ms-client-request-id': ANY,
            },
            )
        # Validate correlation ID is a valid UUID
        corr_id = mock_get.call_args.kwargs["headers"]["x-ms-client-request-id"]
        uuid.UUID(corr_id)

    @patch.object(ManagedIdentityClient, "_ManagedIdentityClient__instance", "MixedCaseHostName")
    def test_happy_path_of_theoretical_mixed_case_hostname(self):
        """Historically, we used to get the host name from socket.getfqdn(),
        which could return a mixed-case host name on Windows.
        Although we no longer use getfqdn(), we still keep this test case to ensure we tolerate it.
        """
        self.test_happy_path_of_vm()

    @patch.dict(os.environ, {"AZURE_POD_IDENTITY_AUTHORITY_HOST": "http://localhost:1234//"})
    def test_happy_path_of_pod_identity(self):
        mock_get = self._test_happy_path()
        mock_get.assert_called_with(
            'http://localhost:1234/metadata/identity/oauth2/token',
            params={'api-version': '2018-02-01', 'resource': 'R'},
            headers={
                'Metadata': 'true',
                'x-client-SKU': EXPECTED_SKU,
                'x-client-Ver': ANY,
                'x-ms-client-request-id': ANY,
            },
            )
        # Validate correlation ID is a valid UUID
        corr_id = mock_get.call_args.kwargs["headers"]["x-ms-client-request-id"]
        uuid.UUID(corr_id)

    def test_vm_error_should_be_returned_as_is(self):
        raw_error = '{"raw": "error format is undefined"}'
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=400,
            text=raw_error,
        )) as mocked_method:
            self.assertEqual(
                json.loads(raw_error), self.app.acquire_token_for_client(resource="R"))
            self.assertEqual({}, self.app._token_cache._cache)

    def test_vm_resource_id_parameter_should_be_msi_res_id(self):
        app = ManagedIdentityClient(
            {"ManagedIdentityIdType": "ResourceId", "Id": "1234"},
            http_client=requests.Session(),
            )
        with patch.object(app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_in": 3600, "resource": "R"}',
        )) as mocked_method:
            app.acquire_token_for_client(resource="R")
            mocked_method.assert_called_with(
                'http://169.254.169.254/metadata/identity/oauth2/token',
                params={'api-version': '2018-02-01', 'resource': 'R', 'msi_res_id': '1234'},
                headers={
                    'Metadata': 'true',
                    'x-client-SKU': EXPECTED_SKU,
                    'x-client-Ver': ANY,
                    'x-ms-client-request-id': ANY,
                },
                )
            # Validate correlation ID is a valid UUID
            corr_id = mocked_method.call_args.kwargs["headers"]["x-ms-client-request-id"]
            uuid.UUID(corr_id)


@patch.dict(os.environ, {"IDENTITY_ENDPOINT": "http://localhost", "IDENTITY_HEADER": "foo"})
class AppServiceTestCase(ClientTestCase):

    def test_happy_path(self):
        expires_in = 1234
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": "%s", "resource": "R"}' % (
                int(time.time()) + expires_in),
        )) as mocked_method:
            self._test_happy_path(self.app, mocked_method, expires_in)

    def test_app_service_error_should_be_normalized(self):
        raw_error = '{"statusCode": 500, "message": "error content is undefined"}'
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=500,
            text=raw_error,
        )) as mocked_method:
            self.assertEqual({
                "error": "invalid_scope",
                "error_description": "500, error content is undefined",
            }, self.app.acquire_token_for_client(resource="R"))
            self.assertEqual({}, self.app._token_cache._cache)

    def test_app_service_resource_id_parameter_should_be_mi_res_id(self):
        app = ManagedIdentityClient(
            {"ManagedIdentityIdType": "ResourceId", "Id": "1234"},
            http_client=requests.Session(),
            )
        with patch.object(app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": 12345, "resource": "R"}',
        )) as mocked_method:
            app.acquire_token_for_client(resource="R")
            mocked_method.assert_called_with(
                'http://localhost',
                params={'api-version': '2019-08-01', 'resource': 'R', 'mi_res_id': '1234'},
                headers={'X-IDENTITY-HEADER': 'foo', 'Metadata': 'true'},
                )

    def test_past_expires_on_should_not_be_cached_long(self):
        # Regression test: a Managed Identity endpoint returning a PAST "expires_on"
        # timestamp must NOT result in a long-lived cached token. MSAL Python computes
        # expires_in = expires_on - now; a past timestamp yields a non-positive lifetime,
        # so any cached entry is immediately treated as expired and not served from cache.
        past_expires_on = int(time.time()) - 3600  # 1 hour in the past
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": "%s", "resource": "R"}' % past_expires_on,
        )) as mocked_method:
            first = self.app.acquire_token_for_client(resource="R")
            # Must not be inflated into a huge positive lifetime (a past absolute epoch
            # value must never be treated as a relative "seconds from now").
            self.assertLessEqual(
                first["expires_in"], 0,
                "A past expires_on must yield a non-positive expires_in, not an inflated lifetime")
            # A subsequent acquisition must re-hit the endpoint, proving the past-dated
            # token is treated as already expired and is not served from the cache.
            second = self.app.acquire_token_for_client(resource="R")
            self.assertEqual(
                "identity_provider", second["token_source"],
                "A token minted from a past expires_on must not be served from cache")
            self.assertEqual(
                2, mocked_method.call_count,
                "Second acquisition should re-call the MI endpoint, not reuse a stale cache entry")


@patch.dict(os.environ, {"MSI_ENDPOINT": "http://localhost", "MSI_SECRET": "foo"})
class MachineLearningTestCase(ClientTestCase):

    def test_happy_path(self):
        expires_in = 1234
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": "%s", "resource": "R"}' % (
                int(time.time()) + expires_in),
        )) as mocked_method:
            self._test_happy_path(self.app, mocked_method, expires_in)

    def test_machine_learning_error_should_be_normalized(self):
        raw_error = '{"error": "placeholder", "message": "placeholder"}'
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=500,
            text=raw_error,
        )) as mocked_method:
            self.assertEqual({
                "error": "invalid_scope",
                "error_description": "{'error': 'placeholder', 'message': 'placeholder'}",
            }, self.app.acquire_token_for_client(resource="R"))
            self.assertEqual({}, self.app._token_cache._cache)


@patch.dict(os.environ, {
    "IDENTITY_ENDPOINT": "https://localhost",
    "IDENTITY_HEADER": "foo",
    "IDENTITY_SERVER_THUMBPRINT": "ab" * 20,
})
class ServiceFabricTestCase(ClientTestCase):
    access_token = "AT"
    access_token_sha256 = hashlib.sha256(access_token.encode()).hexdigest()

    def _test_happy_path(self, app, *, claims_challenge=None) -> callable:
        expires_in = 1234
        with patch(
                "msal.managed_identity._create_service_fabric_http_client",
                return_value=app._http_client):
            with patch.object(app._http_client, "get", return_value=MinimalResponse(
                status_code=200,
                text='{"access_token": "%s", "expires_on": %s, "resource": "R", "token_type": "Bearer"}' % (
                    self.access_token, int(time.time()) + expires_in),
            )) as mocked_method:
                super(ServiceFabricTestCase, self)._test_happy_path(
                    app, mocked_method, expires_in, claims_challenge=claims_challenge)
                return mocked_method

    def test_happy_path_with_client_capabilities_should_relay_capabilities(self):
        self._test_happy_path(self._build_app(client_capabilities=["foo", "bar"])).assert_called_with(
            'https://localhost',
            params={
                'api-version': '2019-07-01-preview',
                'resource': 'R',
                'token_sha256_to_refresh': self.access_token_sha256,
                "xms_cc": "foo,bar",
            },
            headers={'Secret': 'foo'},
        )

    def test_happy_path_with_claim_challenge_should_send_sha256_to_provider(self):
        self._test_happy_path(
            self._build_app(client_capabilities=[]),  # Test empty client_capabilities
            claims_challenge='{"access_token": {"nbf": {"essential": true, "value": "1563308371"}}}',
        ).assert_called_with(
            'https://localhost',
            params={
                'api-version': '2019-07-01-preview',
                'resource': 'R',
                'token_sha256_to_refresh': self.access_token_sha256,
                # There is no xms_cc in this case
            },
            headers={'Secret': 'foo'},
        )

    def test_unified_api_service_should_ignore_unnecessary_client_id(self):
        self._test_happy_path(ManagedIdentityClient(
            {"ManagedIdentityIdType": "ClientId", "Id": "foo"},
            http_client=requests.Session(),
            ))

    def test_sf_error_should_be_normalized(self):
        raw_error = '''
{"error": {
    "correlationId": "foo",
    "code": "SecretHeaderNotFound",
    "message": "Secret is not found in the request headers."
}}'''  # https://learn.microsoft.com/en-us/azure/service-fabric/how-to-managed-identity-service-fabric-app-code#error-handling
        with patch(
                "msal.managed_identity._create_service_fabric_http_client",
                return_value=self.app._http_client):
            with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
                status_code=404,
                text=raw_error,
            )) as mocked_method:
                self.assertEqual({
                    "error": "unauthorized_client",
                    "error_description": raw_error,
                }, self.app.acquire_token_for_client(resource="R"))
                self.assertEqual({}, self.app._token_cache._cache)


class _ServiceFabricTlsRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
        })
        body = getattr(self.server, "body", json.dumps({
            "access_token": "AT",
            "expires_on": str(int(time.time()) + 3600),
            "resource": "R",
            "token_type": "Bearer",
        })).encode("utf-8")
        self.send_response(getattr(self.server, "status", 200))
        for name, value in getattr(self.server, "response_headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if getattr(self.server, "stall", None):
            self.server.stall.wait(2)
        try:
            self.wfile.write(body)
        except (OSError, ssl.SSLError):
            pass

    def log_message(self, format, *args):
        pass


class _ServiceFabricTlsFixture(unittest.TestCase):
    _adapter_class = HTTPAdapter

    def setUp(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            ]))
            .issuer_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            ]))
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(
                datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )
        self.thumbprint = certificate.fingerprint(hashes.SHA1()).hex()
        file_prefix = ".service-fabric-test-" + uuid.uuid4().hex
        certificate_path = file_prefix + ".pem"
        private_key_path = file_prefix + ".key"
        self.addCleanup(lambda: os.path.exists(certificate_path) and os.remove(certificate_path))
        self.addCleanup(lambda: os.path.exists(private_key_path) and os.remove(private_key_path))
        with open(certificate_path, "wb") as certificate_file:
            certificate_file.write(certificate.public_bytes(serialization.Encoding.PEM))
        with open(private_key_path, "wb") as private_key_file:
            private_key_file.write(private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        self.server = ThreadingHTTPServer(
            ("localhost", 0), _ServiceFabricTlsRequestHandler)
        self.server.requests = []
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(certificate_path, private_key_path)
        os.remove(certificate_path)
        os.remove(private_key_path)
        self.tls_context = tls_context
        self.server.socket = tls_context.wrap_socket(self.server.socket, server_side=True)
        self.server_thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01))
        self.server_thread.start()
        self.endpoint = "https://localhost:{}/token".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()

    def _new_session(self, **adapter_kwargs):
        session = requests.Session()
        self.addCleanup(session.close)
        session.trust_env = False
        session.mount("https://", self._adapter_class(**adapter_kwargs))
        return session


class ServiceFabricTlsValidationTestCase(_ServiceFabricTlsFixture):
    def test_matching_thumbprint_sends_secret_after_validating_certificate(self):
        result = _obtain_token_on_service_fabric(
            _ThrottledHttpClient(self._new_session()),
            self.endpoint,
            "service-fabric-secret",
            ":".join(
                self.thumbprint[index:index + 2].upper()
                for index in range(0, len(self.thumbprint), 2)),
            "R",
        )

        self.assertEqual("AT", result["access_token"])
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual(
            "service-fabric-secret",
            self.server.requests[0]["headers"]["Secret"])

    def test_mismatching_thumbprint_prevents_the_secret_from_being_sent(self):
        source = self._new_session()
        source.verify = False
        with self.assertRaises(SSLError):
            _obtain_token_on_service_fabric(
                source,
                self.endpoint,
                "service-fabric-secret",
                "00" * 20,
                "R",
            )

        self.assertEqual([], self.server.requests)

    def test_non_https_endpoint_is_rejected_before_a_request_can_send_the_secret(self):
        with self.assertRaises(ManagedIdentityError):
            _obtain_token_on_service_fabric(
                self._new_session(),
                self.endpoint.replace("https://", "http://", 1),
                "service-fabric-secret",
                self.thumbprint,
                "R",
            )

        self.assertEqual([], self.server.requests)

    def test_malformed_thumbprint_is_rejected_before_a_request_can_send_the_secret(self):
        with self.assertRaises(ManagedIdentityError):
            _obtain_token_on_service_fabric(
                self._new_session(),
                self.endpoint,
                "service-fabric-secret",
                "not-a-thumbprint",
                "R",
            )

        self.assertEqual([], self.server.requests)

    def test_derived_client_preserves_standard_session_settings_without_mutation(self):
        source = self._new_session(
            max_retries=Retry(total=2),
            pool_connections=3,
            pool_maxsize=7,
            pool_block=True,
        )
        source.verify = False
        source.headers["X-Caller-Header"] = "caller-header"
        source.cookies.set("caller-cookie", "cookie-value")
        source.auth = ("caller", "password")
        source.params = {"caller-param": "caller-value"}
        source.proxies = {}
        source.max_redirects = 7
        source_adapter = source.get_adapter(self.endpoint)
        source_pool_classes = source_adapter.poolmanager.pool_classes_by_scheme.copy()

        derived = _create_service_fabric_http_client(
            source, self.endpoint, self.thumbprint)
        self.addCleanup(derived.close)
        derived_adapter = derived.get_adapter(self.endpoint)

        self.assertFalse(source.verify)
        self.assertTrue(derived.verify)
        self.assertEqual(source.headers, derived.headers)
        self.assertEqual(source.cookies, derived.cookies)
        self.assertEqual(source.auth, derived.auth)
        self.assertEqual(source.params, derived.params)
        self.assertEqual(source.proxies, derived.proxies)
        self.assertEqual(source.max_redirects, derived.max_redirects)
        self.assertEqual(2, derived_adapter.max_retries.total)
        self.assertIsNot(source_adapter.max_retries, derived_adapter.max_retries)
        self.assertEqual(3, derived_adapter._pool_connections)
        self.assertEqual(7, derived_adapter._pool_maxsize)
        self.assertTrue(derived_adapter._pool_block)
        self.assertIs(source_adapter, source.get_adapter(self.endpoint))
        self.assertIsNot(source_adapter, derived_adapter)
        self.assertIsNot(
            source_adapter.poolmanager.pool_classes_by_scheme,
            derived_adapter.poolmanager.pool_classes_by_scheme)
        self.assertEqual(source_pool_classes, source_adapter.poolmanager.pool_classes_by_scheme)
        response = derived.get(
            self.endpoint,
            params={"request-param": "request-value"},
            headers={"Secret": "service-fabric-secret"},
        )
        self.assertEqual(200, response.status_code)
        request = self.server.requests[0]
        self.assertIn("caller-param=caller-value", request["path"])
        self.assertIn("request-param=request-value", request["path"])
        self.assertEqual("caller-header", request["headers"]["X-Caller-Header"])
        self.assertIn("caller-cookie=cookie-value", request["headers"]["Cookie"])
        self.assertTrue(request["headers"]["Authorization"].startswith("Basic "))

    def test_non_http_adapter_is_rejected_before_a_request_can_send_the_secret(self):
        source = self._new_session()
        adapter = Mock(spec=BaseAdapter)
        source.mount("https://", adapter)
        with self.assertRaises(ManagedIdentityError):
            _obtain_token_on_service_fabric(
                source,
                self.endpoint,
                "service-fabric-secret",
                self.thumbprint,
                "R",
            )

        self.assertEqual([], self.server.requests)
        adapter.send.assert_not_called()


class _ServiceFabricSourceHTTPAdapter(HTTPAdapter):
    def send(self, *args, **kwargs):
        raise AssertionError("Service Fabric requests must use MSAL's pinned adapter")


class ServiceFabricHTTPAdapterSubclassTestCase(ServiceFabricTlsValidationTestCase):
    _adapter_class = _ServiceFabricSourceHTTPAdapter


class _UnopenedHttpClient:
    def __getattribute__(self, name):
        if name == "__class__":
            return type(self)
        raise AssertionError("Consumer transport must remain unopened and uninspected")

    def __setattr__(self, name, value):
        raise AssertionError("Consumer transport must not be mutated")


class _ConnectProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_CONNECT(self):
        self.server.connects.append((self.path, dict(self.headers)))
        with socket.create_connection(self.server.target, timeout=2) as upstream:
            self.send_response(200)
            self.end_headers()
            self.connection.settimeout(2)
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 2)
                if not ready:
                    return
                for source in ready:
                    try:
                        data = source.recv(65536)
                        if not data:
                            return
                        if source is self.connection:
                            self.server.tunnel_data.append(data)
                        (upstream if source is self.connection else self.connection).sendall(data)
                    except OSError:
                        return

    def log_message(self, format, *args):
        pass


class ServiceFabricHttpOptionsTestCase(_ServiceFabricTlsFixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {
            "IDENTITY_ENDPOINT": self.endpoint,
            "IDENTITY_HEADER": "service-fabric-secret",
            "IDENTITY_SERVER_THUMBPRINT": self.thumbprint,
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def _app(self, options=None, **kwargs):
        return ManagedIdentityClient(
            SystemAssignedManagedIdentity(), http_client=_UnopenedHttpClient(),
            service_fabric_http_options={} if options is None else options, **kwargs)

    def _seed_cache(self, app, *, refresh=False):
        app._token_cache.add({
            "client_id": None, "scope": ["R"],
            "token_endpoint": "https://localhost/managed_identity",
            "response": {"access_token": "cached", "expires_in": 3600,
                "token_type": "Bearer", **({"refresh_in": -1} if refresh else {})},
        })

    def _proxy(self, *, tls=False, target=None, hostname="localhost"):
        server = ThreadingHTTPServer(("localhost", 0), _ConnectProxyHandler)
        server.target = target or ("localhost", self.server.server_port)
        server.connects, server.tunnel_data, server.requests = [], [], []
        if tls:
            ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test proxy CA")])
            ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
                .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=False, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                    ca_key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            certificate = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
                .issuer_name(ca_name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                    key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
            prefix = ".service-fabric-proxy-test-" + uuid.uuid4().hex
            server.ca_path = os.path.abspath(prefix + "-ca.pem")
            server.certificate_der = certificate.public_bytes(serialization.Encoding.DER)
            server.thumbprint = certificate.fingerprint(hashes.SHA1()).hex()
            certificate_path, key_path = prefix + ".pem", prefix + ".key"
            for path, data in (
                    (server.ca_path, ca.public_bytes(serialization.Encoding.PEM)),
                    (certificate_path, certificate.public_bytes(serialization.Encoding.PEM)),
                    (key_path, key.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))):
                self.addCleanup(lambda path=path: os.path.exists(path) and os.remove(path))
                with open(path, "wb") as output:
                    output.write(data)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certificate_path, key_path)
            os.remove(certificate_path)
            os.remove(key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return server, "{}://localhost:{}".format("https" if tls else "http", server.server_port)

    def test_public_type_and_unopened_consumer_journey(self):
        self.assertEqual(
            {"headers", "proxies", "trust_env", "timeout", "max_retries"},
            set(ServiceFabricHttpOptions.__annotations__))
        self.assertFalse(ServiceFabricHttpOptions.__total__)
        self.assertEqual(frozenset(), ServiceFabricHttpOptions.__required_keys__)
        http_cache = {}
        app = self._app(ServiceFabricHttpOptions(), http_cache=http_cache,
            client_capabilities=["CP1", "CP2"])
        token = app.acquire_token_for_client(resource="R")
        self.assertEqual(("AT", "Bearer", "identity_provider"),
            (token["access_token"], token["token_type"], token["token_source"]))
        self.assertTrue(3595 <= token["expires_in"] <= 3600)
        with patch("msal.managed_identity.requests.Session") as allocation:
            self.assertEqual("cache", app.acquire_token_for_client(resource="R")["token_source"])
            allocation.assert_not_called()
        app.acquire_token_for_client(resource="R", claims_challenge="challenge")
        self.assertEqual(2, len(self.server.requests))
        params = parse_qs(urlparse(self.server.requests[-1]["path"]).query)
        self.assertEqual({
            "resource": ["R"], "api-version": ["2019-07-01-preview"],
            "xms_cc": ["CP1,CP2"],
            "token_sha256_to_refresh": [hashlib.sha256(b"AT").hexdigest()],
        }, params)
        self.assertEqual({}, http_cache)
        self.assertEqual("service-fabric-secret", self.server.requests[0]["headers"]["Secret"])

    def test_defaults_partial_fields_and_full_configuration(self):
        explicit = {"headers": {"User-Agent": "custom"}, "proxies": {},
            "trust_env": True, "timeout": (1.5, 2), "max_retries": 2}
        cases = [{}] + [{key: value} for key, value in explicit.items()]
        cases += [explicit] + [
            {key: value for key, value in explicit.items() if key != omitted}
            for omitted in explicit]
        from msal.managed_identity import _validate_service_fabric_options
        for options in cases:
            with self.subTest(options=options):
                normalized = _validate_service_fabric_options(options)
                session = _create_owned_service_fabric_http_client(normalized, self.thumbprint)
                try:
                    self.assertEqual(options.get("proxies", {}), session.proxies)
                    self.assertIs(options.get("trust_env", False), session.trust_env)
                    self.assertEqual(options.get("timeout", (5, 30)), normalized["timeout"])
                    self.assertIn("Accept", session.headers)
                    self.assertEqual(options.get("headers", {}).get(
                        "User-Agent", requests.utils.default_user_agent()), session.headers["User-Agent"])
                    retry = session.get_adapter(self.endpoint).max_retries
                    self.assertEqual(options.get("max_retries", 0), retry.total)
                    self.assertEqual(retry.total, retry.connect)
                    self.assertFalse(retry.read)
                    self.assertEqual((0, 0, 0), (
                        retry.status, retry.redirect, retry.backoff_factor))
                    self.assertFalse(retry.respect_retry_after_header)
                finally:
                    session.close()

    def test_snapshot_and_case_insensitive_header_overlay(self):
        proxy, address = self._proxy()
        headers = {"X-Label": "first", "x-label": "last", "User-Agent": "custom", "X-Empty": ""}
        proxies = {"https": address}
        options = {"headers": headers, "proxies": proxies, "timeout": 2}
        app = self._app(options)
        headers["x-label"], proxies["https"], options["timeout"] = "changed", "invalid", None
        options["unknown"] = "changed"
        app.acquire_token_for_client(resource="R")
        received = requests.structures.CaseInsensitiveDict(self.server.requests[0]["headers"])
        self.assertEqual(("last", "custom", "", "service-fabric-secret"), (
            received["X-Label"], received["User-Agent"], received["X-Empty"], received["Secret"]))
        self.assertIn("Accept", received)
        self.assertEqual(1, len(proxy.connects))
        self.assertEqual("changed", headers["x-label"])
        self.assertEqual("invalid", proxies["https"])

    def test_invalid_options_are_lazy_private_and_preallocation(self):
        private = "sensitive-option-value"
        cases = [False, 0, "", [], (), UserDict(), {private: private}]
        cases += [{key: None} for key in ServiceFabricHttpOptions.__annotations__]
        cases += [{"headers": value} for value in [
            [], UserDict(), {1: private}, {"X-Test": 1}, {"sEcReT": private},
            {"HOST": private}, {"": private}, {"Bad Name": private}, {"x:bad": private},
            {"é": private}, {"X-Test": "a\r\n" + private}, {"X-Test": "\n"},
            {"X-Test": "\x00"}, {"X-Test": "\x7f"}, {"X-Test": "☃"},
            {"X-Test": " " + private}, {"X-Test": "\t" + private}]]
        cases += [{"proxies": value} for value in [
            [], UserDict(), {1: private}, {"https": 123}]]
        cases += [{"trust_env": value} for value in [0, 1, "", "true", []]]
        cases += [{"max_retries": value} for value in [True, False, -1, 1.5, "1", []]]
        cases += [{"timeout": value} for value in [
            True, False, 0, -1, float("nan"), float("inf"), float("-inf"),
            "2", [1, 2], (), (1,), (1, 2, 3), (1, None), (True, 1),
            (1, float("nan")), 10 ** 1000]]
        for options in cases:
            with self.subTest(options=options), patch(
                    "msal.managed_identity.requests.Session") as allocation:
                app = self._app(options)
                with self.assertRaises(ManagedIdentityError) as error:
                    app.acquire_token_for_client(resource="R")
                self.assertNotIn(private, str(error.exception))
                allocation.assert_not_called()
        self.assertEqual([], self.server.requests)

    def test_proxy_validation_matrix(self):
        invalid_keys = ["", "ftp", "https://", "https://a:80", "https://user:password@a",
            "https://a/path", "https://a/", "https://a?", "https://a#",
            "https://a:", "https://a b", "https://a\\b", "https://[invalid]"]
        invalid_urls = ["", "localhost:8080", "socks5://localhost:1080", "ftp://a",
            "http://", "https://a:0", "https://a:65536", "https://a:invalid",
            "https://a:", "https://a/path", "https://a?q", "https://a#f",
            "https://a?", "https://a#", "https://a b", "https://[invalid]",
            "https://a/;", "https://a/;params"]
        for proxies in ([{key: "http://localhost"} for key in invalid_keys]
                + [{"https": value} for value in invalid_urls]):
            with self.subTest(proxies=proxies), patch(
                    "msal.managed_identity.requests.Session") as allocation:
                with self.assertRaises(ManagedIdentityError):
                    self._app({"proxies": proxies}).acquire_token_for_client(resource="R")
                allocation.assert_not_called()

    def test_transport_invalid_proxy_idna_is_private_and_preallocation(self):
        private = "synthetic-proxy-password"
        for scheme in ("http", "https"):
            for cached in (False, True):
                with self.subTest(scheme=scheme, cached=cached), patch(
                        "msal.managed_identity.requests.Session", wraps=requests.Session
                        ) as allocation, patch.object(
                        _ServiceFabricHTTPSConnection, "_new_conn") as connect, self.assertLogs(
                        level="DEBUG") as logs:
                    app = self._app({"proxies": {
                        "https": "{}://user:{}@\u2603.example".format(scheme, private)}})
                    if cached:
                        self._seed_cache(app)
                        self.assertEqual("cached", app.acquire_token_for_client(
                            resource="R")["access_token"])
                        self._seed_cache(app, refresh=True)
                    with self.assertRaisesRegex(
                            ManagedIdentityError, "^Invalid Service Fabric proxies configuration\\.$") as error:
                        app.acquire_token_for_client(resource="R")
                    self.assertIsNone(error.exception.__cause__)
                    self.assertIsNone(error.exception.__context__)
                    self.assertNotIn(private, "".join(traceback.format_exception(
                        type(error.exception), error.exception, error.exception.__traceback__)))
                    allocation.assert_not_called()
                    connect.assert_not_called()
                self.assertNotIn(private, "\n".join(logs.output))
        self.assertEqual([], self.server.requests)

    def test_validation_is_skipped_on_cache_hit_but_not_refresh_fallback(self):
        for refresh in (False, True):
            app = self._app({"timeout": None})
            self._seed_cache(app, refresh=refresh)
            with patch("msal.managed_identity.requests.Session") as allocation:
                if refresh:
                    with self.assertRaises(ManagedIdentityError):
                        app.acquire_token_for_client(resource="R")
                else:
                    self.assertEqual("cached", app.acquire_token_for_client(resource="R")["access_token"])
                allocation.assert_not_called()

    def test_matching_pin_normalization(self):
        for pin in (self.thumbprint, self.thumbprint.upper(),
                " \t" + ":".join(self.thumbprint[i:i+2].upper()
                    for i in range(0, 40, 2)) + "\r\n"):
            with self.subTest(pin=pin), patch.dict(os.environ, {"IDENTITY_SERVER_THUMBPRINT": pin}):
                self.assertEqual("AT", self._app().acquire_token_for_client(resource="R")["access_token"])
        self.assertEqual(3, len(self.server.requests))

    def test_bad_pin_and_endpoint_never_send_or_retry_secret(self):
        for pin, endpoint, error in [
                ("00" * 20, self.endpoint, SSLError),
                ("bad", self.endpoint, ManagedIdentityError),
                ("", self.endpoint, ManagedIdentityError),
                (self.thumbprint, self.endpoint.replace("https:", "http:"), ManagedIdentityError)]:
            with self.subTest(pin=pin), patch.dict(os.environ, {
                    "IDENTITY_SERVER_THUMBPRINT": pin, "IDENTITY_ENDPOINT": endpoint}), patch.object(
                    _ServiceFabricHTTPSConnection, "connect", autospec=True,
                    side_effect=_ServiceFabricHTTPSConnection.connect) as connect:
                with self.assertRaises(error):
                    self._app({"max_retries": 2}).acquire_token_for_client(resource="R")
                self.assertEqual(1 if error is SSLError else 0, connect.call_count)
        self.assertEqual([], self.server.requests)

    def test_redirect_status_and_destination_matrix_is_not_followed(self):
        proxy, target = self._proxy()
        other = _ServiceFabricTlsFixture()
        other.setUp()
        self.addCleanup(other.doCleanups)
        self.addCleanup(other.tearDown)
        for status in range(300, 400):
            for destination in (self.endpoint + "/next",
                    other.endpoint + "/private-target",
                    target + "/private-target"):
                with self.subTest(status=status, destination=destination):
                    self.server.status = status
                    self.server.response_headers = {"Location": destination}
                    with self.assertRaisesRegex(ManagedIdentityError, str(status)) as error:
                        self._app({"max_retries": 2}).acquire_token_for_client(resource="R")
                    self.assertNotIn("private-target", str(error.exception))
        self.assertEqual(300, len(self.server.requests))
        self.assertEqual([], proxy.connects)
        self.assertEqual([], proxy.requests)
        self.assertEqual([], other.server.requests)

    def test_explicit_http_and_https_connect_proxy_pins_endpoint(self):
        for tls in (False, True):
            proxy, address = self._proxy(tls=tls)
            address = address.replace("://", "://proxy-user:proxy-password@")
            with patch("requests.adapters.DEFAULT_CA_BUNDLE_PATH", proxy.ca_path) if tls else nullcontext():
                for key in ("https", "all", "https://localhost", "all://localhost"):
                    with self.subTest(tls=tls, key=key):
                        self.assertEqual("AT", self._app({"proxies": {key: address}}
                            ).acquire_token_for_client(resource="R")["access_token"])
                before = len(self.server.requests)
                with patch.dict(os.environ, {
                        "IDENTITY_SERVER_THUMBPRINT": proxy.thumbprint if tls else "00" * 20}):
                    with self.assertRaises((SSLError, requests.exceptions.ProxyError)):
                        self._app({"proxies": {"https": address}, "max_retries": 2}
                            ).acquire_token_for_client(resource="R")
            self.assertEqual(before, len(self.server.requests))
            self.assertEqual(5, len(proxy.connects))
            self.assertTrue(all(headers["Proxy-Authorization"] == requests.auth._basic_auth_str(
                "proxy-user", "proxy-password") for _, headers in proxy.connects))
            self.assertNotIn(b"service-fabric-secret", b"".join(proxy.tunnel_data))
            self.assertTrue(all("Secret" not in headers for _, headers in proxy.connects))
            self.assertTrue(all("Proxy-Authorization" not in request["headers"]
                for request in self.server.requests))

    def test_https_proxy_authenticates_proxy_hostname_not_endpoint_hostname(self):
        proxy, address = self._proxy(tls=True)
        server_names = []
        proxy.socket.context.set_servername_callback(
            lambda sock, name, context: server_names.append(name))
        address = address.replace("://", "://proxy-user:proxy-password@")
        endpoint = self.endpoint.replace("localhost", "127.0.0.1")
        options = {"proxies": {"https": address}, "timeout": 2, "max_retries": 2}
        with patch.dict(os.environ, {"IDENTITY_ENDPOINT": endpoint}), patch(
                "requests.adapters.DEFAULT_CA_BUNDLE_PATH", proxy.ca_path):
            self.assertEqual("AT", self._app(options).acquire_token_for_client(
                resource="R")["access_token"])
            with patch.dict(os.environ, {"IDENTITY_SERVER_THUMBPRINT": proxy.thumbprint}):
                with self.assertRaises((SSLError, requests.exceptions.ProxyError)):
                    self._app(options).acquire_token_for_client(resource="R")
        self.assertEqual(["localhost", "localhost"], server_names)
        self.assertEqual(2, len(proxy.connects))
        for target, headers in proxy.connects:
            self.assertEqual("127.0.0.1:{}".format(self.server.server_port), target)
            self.assertEqual(requests.auth._basic_auth_str("proxy-user", "proxy-password"),
                headers["Proxy-Authorization"])
            self.assertNotIn("Secret", headers)
        self.assertEqual([], proxy.requests)
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual("service-fabric-secret", self.server.requests[0]["headers"]["Secret"])
        self.assertNotIn("Proxy-Authorization", self.server.requests[0]["headers"])
        self.assertNotIn(b"service-fabric-secret", b"".join(proxy.tunnel_data))

    def test_https_proxy_hostname_is_independent_of_tls_hook_argument(self):
        session = _create_owned_service_fabric_http_client(
            {"headers": {}, "proxies": {}, "trust_env": False, "max_retries": 0},
            self.thumbprint)
        self.addCleanup(session.close)
        adapter = session.get_adapter(self.endpoint)
        with patch("msal.managed_identity.ssl.create_default_context") as context, patch.object(
                HTTPAdapter, "send"):
            adapter.send(requests.Request("GET", self.endpoint).prepare(),
                proxies={"https": "https://proxy.example:8443"})
        connection = adapter._connection_pool_class.ConnectionCls("origin.example")
        sock = Mock()
        self.assertIs(context.return_value.wrap_socket.return_value,
            connection._connect_tls_proxy("origin.example", sock))
        context.return_value.wrap_socket.assert_called_once_with(
            sock, server_hostname="proxy.example")

    def test_https_proxy_strict_verification_accepts_valid_chain_and_checks_hostname(self):
        create_default_context = ssl.create_default_context
        contexts = []

        def strict_context(*args, **kwargs):
            context = create_default_context(*args, **kwargs)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_flags |= ssl.VERIFY_X509_STRICT
            contexts.append(context)
            return context

        for hostname in ("localhost", "wrong.invalid"):
            with self.subTest(hostname=hostname):
                proxy, address = self._proxy(tls=True, hostname=hostname)
                address = address.replace("://", "://proxy-user:proxy-password@")
                context = strict_context(cafile=proxy.ca_path)
                # Verify even the hostname-negative fixture's chain with its own
                # valid name before testing the actual proxy hostname below.
                with socket.create_connection(("localhost", proxy.server_port), timeout=2) as sock:
                    with context.wrap_socket(sock, server_hostname=hostname) as tls_socket:
                        self.assertEqual(proxy.certificate_der,
                            tls_socket.getpeercert(binary_form=True))
                self.assertEqual([], proxy.connects)
                self.assertEqual([], proxy.requests)
                before = len(self.server.requests)
                with patch("requests.adapters.DEFAULT_CA_BUNDLE_PATH", proxy.ca_path), patch(
                        "msal.managed_identity.ssl.create_default_context",
                        side_effect=strict_context) as create_context:
                    app = self._app({"proxies": {"https": address}, "max_retries": 2})
                    if hostname == "localhost":
                        self.assertEqual("AT", app.acquire_token_for_client(
                            resource="R")["access_token"])
                    else:
                        with self.assertRaises((SSLError, requests.exceptions.ProxyError)) as error:
                            app.acquire_token_for_client(resource="R")
                        message = "".join(traceback.format_exception(
                            type(error.exception), error.exception, error.exception.__traceback__))
                        self.assertIn("CERTIFICATE_VERIFY_FAILED", message)
                        self.assertRegex(message, "[Hh]ostname mismatch")
                        self.assertIn("certificate is not valid for 'localhost'", message)
                    create_context.assert_called_once_with(capath=None, cafile=proxy.ca_path)
                self.assertTrue(contexts[-1].verify_flags & ssl.VERIFY_X509_STRICT)
                self.assertEqual(ssl.CERT_REQUIRED, contexts[-1].verify_mode)
                self.assertTrue(contexts[-1].check_hostname)
                expected_requests = int(hostname == "localhost")
                self.assertEqual(before + expected_requests, len(self.server.requests))
                self.assertEqual(expected_requests, len(proxy.connects))
                self.assertEqual([], proxy.requests)
                if hostname == "localhost":
                    self.assertEqual(requests.auth._basic_auth_str("proxy-user", "proxy-password"),
                        proxy.connects[0][1]["Proxy-Authorization"])
                    self.assertNotIn("Secret", proxy.connects[0][1])
                    self.assertEqual("service-fabric-secret",
                        self.server.requests[-1]["headers"]["Secret"])
                    self.assertNotIn("Proxy-Authorization", self.server.requests[-1]["headers"])
                else:
                    self.assertEqual([], proxy.tunnel_data)

    def test_https_proxy_rejects_untrusted_chain_and_wrong_hostname_before_connect(self):
        factory = requests.Session
        private = "synthetic-https-proxy-password"
        authorization = requests.auth._basic_auth_str("proxy-user", private)
        for failure in ("untrusted", "hostname"):
            proxy, address = self._proxy(tls=True,
                hostname="wrong.invalid" if failure == "hostname" else "localhost")
            address = address.replace("://", "://proxy-user:{}@".format(private))
            for environment in (False, True):
                for cached in (False, True):
                    session = factory()
                    self.addCleanup(session.close)
                    trust = (patch("requests.adapters.DEFAULT_CA_BUNDLE_PATH", proxy.ca_path)
                        if failure == "hostname" else nullcontext())
                    with self.subTest(failure=failure, environment=environment, cached=cached), trust, patch.dict(
                            os.environ, dict(
                                {"HTTPS_PROXY": address} if environment else {},
                                IDENTITY_ENDPOINT=self.endpoint.replace("localhost", "wrong.invalid"))), patch(
                            "msal.managed_identity.requests.Session", return_value=session), patch.object(
                            session, "close", wraps=session.close) as close, patch.object(
                            _ServiceFabricHTTPSConnection, "connect", autospec=True,
                            side_effect=_ServiceFabricHTTPSConnection.connect) as connect, patch(
                            "urllib3.util.retry.time.sleep") as sleep, self.assertLogs(level="DEBUG") as logs:
                        app = self._app({"trust_env": environment, "max_retries": 2,
                            "proxies": {} if environment else {"https": address}})
                        if cached:
                            self._seed_cache(app, refresh=True)
                            self.assertEqual("cached", app.acquire_token_for_client(resource="R")["access_token"])
                        else:
                            with self.assertRaises((SSLError, requests.exceptions.ProxyError)) as error:
                                app.acquire_token_for_client(resource="R")
                            message = "".join(traceback.format_exception(
                                type(error.exception), error.exception, error.exception.__traceback__))
                            self.assertIn("CERTIFICATE_VERIFY_FAILED", message)
                            for secret in (private, authorization, "service-fabric-secret"):
                                self.assertNotIn(secret, message)
                        self.assertEqual(1, connect.call_count)
                        sleep.assert_not_called()
                        close.assert_called_once_with()
                        adapter = session.get_adapter(self.endpoint)
                        self.assertTrue(all(len(manager.pools) == 0
                            for manager in adapter.proxy_manager.values()))
                    for secret in (private, authorization, "service-fabric-secret"):
                        self.assertNotIn(secret, "\n".join(logs.output))
                    self.assertEqual([], proxy.connects)
                    self.assertEqual([], proxy.requests)
                    self.assertEqual([], proxy.tunnel_data)
        self.assertEqual([], self.server.requests)

    def test_https_proxy_ca_environment_selection_does_not_change_endpoint_pin(self):
        proxy, address = self._proxy(tls=True)
        address = address.replace("://", "://proxy-user:proxy-password@")
        for route in ("explicit", "HTTPS_PROXY", "ALL_PROXY"):
            for bundle in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                for trust_env in (False, True):
                    environment = {bundle: proxy.ca_path}
                    if bundle == "REQUESTS_CA_BUNDLE":
                        environment["CURL_CA_BUNDLE"] = "unused-lower-priority-bundle"
                    if route != "explicit":
                        environment[route] = address
                    options = {"trust_env": trust_env,
                        "proxies": {"https": address} if route == "explicit" else {}}
                    before = len(proxy.connects)
                    with self.subTest(route=route, bundle=bundle, trust_env=trust_env), patch.dict(
                            os.environ, environment), self.assertLogs(level="DEBUG") as logs:
                        if route == "explicit" and not trust_env:
                            with self.assertRaises((SSLError, requests.exceptions.ProxyError)):
                                self._app(options).acquire_token_for_client(resource="R")
                            self.assertEqual(before, len(proxy.connects))
                        else:
                            self.assertEqual("AT", self._app(options).acquire_token_for_client(
                                resource="R")["access_token"])
                            self.assertEqual(before + int(trust_env), len(proxy.connects))
                        if trust_env:
                            for cached in (False, True):
                                with patch.dict(os.environ, {"IDENTITY_SERVER_THUMBPRINT": proxy.thumbprint}):
                                    app = self._app(dict(options, max_retries=2))
                                    if cached:
                                        self._seed_cache(app, refresh=True)
                                        self.assertEqual("cached", app.acquire_token_for_client(
                                            resource="R")["access_token"])
                                    else:
                                        with self.assertRaises((SSLError, requests.exceptions.ProxyError)):
                                            app.acquire_token_for_client(resource="R")
                            self.assertEqual(before + 3, len(proxy.connects))
                    for secret in ("proxy-password", "service-fabric-secret",
                            requests.auth._basic_auth_str("proxy-user", "proxy-password")):
                        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual(10, len(self.server.requests))
        self.assertTrue(all(request["headers"]["Secret"] == "service-fabric-secret"
            and "Proxy-Authorization" not in request["headers"] for request in self.server.requests))
        self.assertTrue(all("Secret" not in headers for _, headers in proxy.connects))
        self.assertNotIn(b"service-fabric-secret", b"".join(proxy.tunnel_data))

    def test_https_proxy_missing_tls_tunneling_capability_fails_before_io(self):
        factory = requests.Session
        proxy, address = self._proxy()
        private = "synthetic-unsupported-proxy-password"
        secure_address = address.replace("http://", "https://proxy-user:{}@".format(private))
        for missing in ("urllib3.connection.HTTPSConnection._connect_tls_proxy",
                "urllib3.util.ssl_.SSLTransport", "urllib3.util.ssl_.SSLContext.wrap_bio"):
            for environment in (False, True):
                for cached in (False, True):
                    session = factory()
                    self.addCleanup(session.close)
                    with self.subTest(missing=missing, environment=environment, cached=cached), patch(
                            missing, None, create=True), patch.dict(os.environ,
                            {"HTTPS_PROXY": secure_address} if environment else {}), patch(
                            "msal.managed_identity.requests.Session", return_value=session), patch.object(
                            session, "close", wraps=session.close) as close, patch.object(
                            _ServiceFabricHTTPSConnection, "_new_conn") as connect, self.assertLogs(
                            level="DEBUG") as logs:
                        app = self._app({"trust_env": environment, "max_retries": 2,
                            "proxies": {} if environment else {"https": secure_address}})
                        if cached:
                            self._seed_cache(app, refresh=True)
                            self.assertEqual("cached", app.acquire_token_for_client(resource="R")["access_token"])
                        else:
                            with self.assertRaisesRegex(ManagedIdentityError, "authenticated HTTPS proxy") as error:
                                app.acquire_token_for_client(resource="R")
                            self.assertNotIn(private, "".join(traceback.format_exception(
                                type(error.exception), error.exception, error.exception.__traceback__)))
                        connect.assert_not_called()
                        close.assert_called_once_with()
                    self.assertNotIn(private, "\n".join(logs.output))
        self.assertEqual([], proxy.connects)
        self.assertEqual([], self.server.requests)
        with patch("urllib3.connection.HTTPSConnection._connect_tls_proxy", None, create=True):
            self.assertEqual("AT", self._app({"proxies": {"https": address}}
                ).acquire_token_for_client(resource="R")["access_token"])
        self.assertEqual(1, len(proxy.connects))

    def test_reconnection_reauthenticates_changed_endpoint_certificate(self):
        other = _ServiceFabricTlsFixture()
        other.setUp()
        self.addCleanup(other.doCleanups)
        self.addCleanup(other.tearDown)
        app = self._app({"max_retries": 2})
        app.acquire_token_for_client(resource="R")
        self.tls_context.set_servername_callback(
            lambda sock, name, context: setattr(sock, "context", other.tls_context))
        with patch.object(_ServiceFabricHTTPSConnection, "connect", autospec=True,
                side_effect=_ServiceFabricHTTPSConnection.connect) as connect:
            with self.assertRaises(SSLError):
                app.acquire_token_for_client(resource="R", claims_challenge="refresh")
            self.assertEqual(1, connect.call_count)
        self.assertEqual(1, len(self.server.requests))

    def test_dict_subclasses_are_accepted_and_snapshotted(self):
        class Options(dict):
            pass
        options = Options(headers=Options({"X-Subclass": "original"}))
        app = self._app(options)
        options["headers"]["X-Subclass"] = "mutated"
        app.acquire_token_for_client(resource="R")
        self.assertEqual("original", self.server.requests[-1]["headers"]["X-Subclass"])

    def test_proxy_selection_precedence_and_credentials(self):
        proxy, address = self._proxy()
        credentials = address.replace("://", "://proxy-user:proxy-password@")
        proxies = {"http": "http://unreachable.invalid", "all": "http://unreachable.invalid",
            "https": "http://unreachable.invalid", "https://localhost": credentials}
        self._app({"proxies": proxies}).acquire_token_for_client(resource="R")
        self.assertEqual(1, len(proxy.connects))
        self.assertTrue(proxy.connects[0][1]["Proxy-Authorization"].startswith("Basic "))
        self.assertNotIn("Proxy-Authorization", self.server.requests[-1]["headers"])
        from msal.managed_identity import _validate_service_fabric_options
        options = _validate_service_fabric_options({
            "proxies": {"https://::1": "http://[::1]:8080"}})
        self.assertEqual("http://[::1]:8080", requests.utils.select_proxy(
            "https://[::1]/token", options["proxies"]))

    def test_forwarding_mode_fails_closed(self):
        connection = _ServiceFabricHTTPSConnection("localhost")
        connection._server_thumbprint = self.thumbprint
        connection.sock = Mock()
        with patch("urllib3.connection.HTTPSConnection.connect"), patch.object(
                _ServiceFabricHTTPSConnection, "proxy_is_forwarding",
                new_callable=lambda: property(lambda self: True), create=True):
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        self.assertIsNone(connection.sock)
        self.assertEqual([], self.server.requests)

    def test_environment_routing_and_netrc_are_explicit_opt_in(self):
        proxy, address = self._proxy()
        for variable in ("HTTPS_PROXY", "ALL_PROXY"):
            with self.subTest(variable=variable), patch.dict(os.environ, {
                    variable: address, "HTTP_PROXY": address}), patch(
                    "requests.sessions.get_netrc_auth", return_value=("user", "password")) as netrc:
                self._app().acquire_token_for_client(resource="R")
                netrc.assert_not_called()
                before = len(proxy.connects)
                self._app({"trust_env": True}).acquire_token_for_client(resource="R")
                self.assertEqual(before + 1, len(proxy.connects))
                self.assertTrue(netrc.called)
                self.assertTrue(self.server.requests[-1]["headers"]["Authorization"].startswith("Basic "))
                with patch.dict(os.environ, {"NO_PROXY": "localhost"}):
                    self._app({"trust_env": True}).acquire_token_for_client(resource="R")
                self.assertEqual(before + 1, len(proxy.connects))
        with patch.dict(os.environ, {"HTTPS_PROXY": address, "REQUESTS_CA_BUNDLE": "unused-ca"}):
            self._app({"trust_env": True, "proxies": {"https": "http://unused.invalid"}}
                ).acquire_token_for_client(resource="R")
        before = len(self.server.requests)
        with patch.dict(os.environ, {"HTTPS_PROXY": "socks5://user:private@localhost:9"}):
            with self.assertRaisesRegex(ManagedIdentityError, "proxy") as error:
                self._app({"trust_env": True}).acquire_token_for_client(resource="R")
            self.assertNotIn("private", str(error.exception))
        self.assertEqual(before, len(self.server.requests))

    def test_malformed_selected_environment_proxies_have_private_errors(self):
        private = "synthetic-env-proxy-password"
        factory = requests.Session
        original_send = HTTPAdapter.send
        for variable in ("HTTPS_PROXY", "ALL_PROXY"):
            for scheme in ("http", "https"):
                for authority in ("localhost:invalid", "[::1", "\u2603.example"):
                    for cached in (False, True):
                        with self.subTest(variable=variable, scheme=scheme,
                                authority=authority, cached=cached), patch.dict(os.environ, {
                                variable: "{}://user:{}@{}".format(scheme, private, authority)
                                }), patch.object(_ServiceFabricHTTPSConnection, "_new_conn") as connect, patch.object(
                                HTTPAdapter, "send", autospec=True, side_effect=original_send
                                ) as send, self.assertLogs(
                                level="DEBUG") as logs:
                            session = factory()
                            self.addCleanup(session.close)
                            with patch("msal.managed_identity.requests.Session",
                                    return_value=session), patch.object(
                                    session, "close", wraps=session.close) as close:
                                app = self._app({"trust_env": True})
                                if cached:
                                    self._seed_cache(app, refresh=True)
                                    self.assertEqual("cached", app.acquire_token_for_client(
                                        resource="R")["access_token"])
                                else:
                                    with self.assertRaisesRegex(
                                            ManagedIdentityError, "^Unsupported Service Fabric proxy configuration\\.$"
                                            ) as error:
                                        app.acquire_token_for_client(resource="R")
                                    self.assertIsNone(error.exception.__cause__)
                                    self.assertIsNone(error.exception.__context__)
                                    self.assertNotIn(private, "".join(traceback.format_exception(
                                        type(error.exception), error.exception, error.exception.__traceback__)))
                                close.assert_called_once_with()
                            connect.assert_not_called()
                            send.assert_not_called()
                        self.assertNotIn(private, "\n".join(logs.output))
        self.assertEqual([], self.server.requests)

    def test_only_selected_environment_proxy_is_validated(self):
        proxy, address = self._proxy()
        invalid = "http://user:synthetic-env-proxy-password@localhost:invalid"
        for options, environment, proxied in (
                ({}, {"HTTPS_PROXY": invalid}, False),
                ({"trust_env": True}, {"HTTPS_PROXY": invalid, "NO_PROXY": "localhost"}, False),
                ({"trust_env": True}, {"HTTPS_PROXY": address, "ALL_PROXY": invalid,
                    "HTTP_PROXY": invalid}, True),
                ({"trust_env": True}, {"HTTPS_PROXY": address.replace(
                    "http://localhost", "127.0.0.1")}, True),
                ({"trust_env": True, "proxies": {"https://localhost": address}},
                    {"HTTPS_PROXY": invalid}, True)):
            with self.subTest(options=options, environment=environment), patch.dict(
                    os.environ, environment):
                before = len(proxy.connects)
                self.assertEqual("AT", self._app(options).acquire_token_for_client(
                    resource="R")["access_token"])
                self.assertEqual(before + int(proxied), len(proxy.connects))
        self.assertEqual(5, len(self.server.requests))

    def test_environment_proxy_transport_error_retains_retry_and_fallback(self):
        from urllib3.exceptions import NewConnectionError
        for cached in (False, True):
            with self.subTest(cached=cached), patch.dict(os.environ, {
                    "HTTPS_PROXY": "http://localhost:9"}), patch.object(
                    _ServiceFabricHTTPSConnection, "_new_conn",
                    side_effect=NewConnectionError(None, "controlled connection failure")) as connect:
                app = self._app({"trust_env": True, "max_retries": 1})
                if cached:
                    self._seed_cache(app, refresh=True)
                    self.assertEqual("cached", app.acquire_token_for_client(
                        resource="R")["access_token"])
                else:
                    with self.assertRaises(requests.exceptions.ProxyError):
                        app.acquire_token_for_client(resource="R")
                self.assertEqual(2, connect.call_count)
        self.assertEqual([], self.server.requests)

    def test_real_netrc_and_environment_proxy_still_require_endpoint_pin(self):
        netrc_path = os.path.abspath(".service-fabric-netrc-" + uuid.uuid4().hex)
        with open(netrc_path, "w") as netrc:
            netrc.write("machine localhost login netrc-user password netrc-password\n")
        self.addCleanup(os.remove, netrc_path)
        proxy, address = self._proxy(tls=True)
        with patch.dict(os.environ, {
                "NETRC": netrc_path, "HTTPS_PROXY": address, "REQUESTS_CA_BUNDLE": proxy.ca_path}):
            self._app().acquire_token_for_client(resource="R")
            self.assertNotIn("Authorization", self.server.requests[-1]["headers"])
            self.assertEqual([], proxy.connects)
            self._app({"trust_env": True}).acquire_token_for_client(resource="R")
            self.assertEqual(requests.auth._basic_auth_str("netrc-user", "netrc-password"),
                self.server.requests[-1]["headers"]["Authorization"])
            with patch.dict(os.environ, {"IDENTITY_SERVER_THUMBPRINT": "00" * 20}):
                with self.assertRaises((SSLError, requests.exceptions.ProxyError)):
                    self._app({"trust_env": True}).acquire_token_for_client(resource="R")
        self.assertEqual(2, len(self.server.requests))
        self.assertEqual(2, len(proxy.connects))
        self.assertNotIn(b"service-fabric-secret", b"".join(proxy.tunnel_data))

    def test_connect_retry_boundaries_timeouts_and_eventual_pin_success(self):
        from urllib3.exceptions import NewConnectionError, ConnectTimeoutError
        original = _ServiceFabricHTTPSConnection._new_conn
        for retries in (0, 1, 2):
            for eventual_success in (False, True):
                for exception_type in (NewConnectionError, ConnectTimeoutError):
                    attempts = []
                    def connect(connection):
                        attempts.append(connection.timeout)
                        if eventual_success and len(attempts) == retries + 1:
                            return original(connection)
                        raise exception_type(connection, "controlled pre-send failure")
                    with self.subTest(retries=retries, success=eventual_success, error=exception_type), patch.object(
                            _ServiceFabricHTTPSConnection, "_new_conn", connect), patch(
                            "urllib3.util.retry.time.sleep") as sleep:
                        app = self._app({"max_retries": retries, "timeout": (1.25, 2)})
                        if eventual_success:
                            self.assertEqual("AT", app.acquire_token_for_client(resource="R")["access_token"])
                        else:
                            with self.assertRaises(requests.exceptions.ConnectionError):
                                app.acquire_token_for_client(resource="R")
                        self.assertEqual([1.25] * (1 + retries), attempts)
                        sleep.assert_not_called()

    def test_non_connection_errors_are_not_retried_or_reclassified(self):
        from urllib3.exceptions import (
            NewConnectionError, ProtocolError, ProxyError, ReadTimeoutError,
            SSLError as Urllib3SSLError)
        for failure, expected in (
                (Urllib3SSLError("controlled TLS failure"), SSLError),
                (ProxyError("controlled proxy TLS failure", Urllib3SSLError("TLS")),
                    requests.exceptions.ProxyError),
                (ProxyError("controlled other proxy failure", ValueError("other")),
                    requests.exceptions.ProxyError),
                (ReadTimeoutError(None, None, "controlled read failure"),
                    requests.exceptions.ReadTimeout),
                (ProtocolError("controlled protocol failure"),
                    requests.exceptions.ConnectionError)):
            for first_connection_fails in (False, True):
                for cached in (False, True):
                    failures = ([NewConnectionError(None, "controlled connection failure")]
                        if first_connection_fails else []) + [failure, failure, failure]
                    with self.subTest(failure=type(failure), cached=cached,
                            first_connection_fails=first_connection_fails), patch.object(
                            _ServiceFabricHTTPSConnection, "_new_conn", side_effect=failures) as connect, patch(
                            "urllib3.util.retry.time.sleep") as sleep:
                        app = self._app({"max_retries": 2})
                        if cached:
                            self._seed_cache(app, refresh=True)
                            self.assertEqual("cached", app.acquire_token_for_client(
                                resource="R")["access_token"])
                        else:
                            with self.assertRaises(expected):
                                app.acquire_token_for_client(resource="R")
                        self.assertEqual(1 + int(first_connection_fails), connect.call_count)
                        sleep.assert_not_called()
        self.assertEqual([], self.server.requests)

    def test_response_failures_and_retry_after_are_not_retried(self):
        for status in (404, 429, 500):
            self.server.status = status
            self.server.body = '{"error":{"code":"ManagedIdentityNotFound"}}'
            self.server.response_headers = {"Retry-After": "1"}
            before = len(self.server.requests)
            result = self._app({"max_retries": 2}).acquire_token_for_client(resource="R")
            self.assertEqual("invalid_client", result["error"])
            self.assertEqual(before + 1, len(self.server.requests))

    def test_other_tls_handshake_failure_is_not_retried(self):
        server, address = self._proxy()
        with patch.dict(os.environ, {"IDENTITY_ENDPOINT": address.replace("http:", "https:")}), patch.object(
                _ServiceFabricHTTPSConnection, "connect", autospec=True,
                side_effect=_ServiceFabricHTTPSConnection.connect) as connect:
            with self.assertRaises(SSLError):
                self._app({"max_retries": 2}).acquire_token_for_client(resource="R")
            self.assertEqual(1, connect.call_count)
        self.assertEqual([], server.requests)
        self.assertEqual([], self.server.requests)

    def test_timeouts_apply_to_each_wait_and_read_failure_is_not_retried(self):
        original = HTTPAdapter.send
        for timeout in ((5, 30), 0.5, (0.5, 1)):
            with self.subTest(timeout=timeout), patch.object(
                    HTTPAdapter, "send", autospec=True, side_effect=original) as send:
                options = {} if timeout == (5, 30) else {"timeout": timeout}
                self._app(options).acquire_token_for_client(resource="R")
                self.assertEqual(timeout, send.call_args.kwargs["timeout"])
        self.server.stall = threading.Event()
        try:
            before = len(self.server.requests)
            with self.assertRaises(requests.exceptions.ConnectionError):
                self._app({"timeout": (1, 0.05), "max_retries": 2}
                    ).acquire_token_for_client(resource="R")
            self.assertEqual(before + 1, len(self.server.requests))
        finally:
            self.server.stall.set()

    def test_owned_resources_close_for_success_and_all_failure_paths(self):
        factory = requests.Session
        for outcome in ("success", "setup", "transport", "tls", "redirect",
                "json", "expiry", "interrupt", "response-close"):
            sessions, responses = [], []
            def session_factory():
                session = factory()
                session.close = Mock(wraps=session.close)
                sessions.append(session)
                return session
            build_response = HTTPAdapter.build_response
            def capture(adapter, request, raw):
                response = build_response(adapter, request, raw)
                response.close = Mock(wraps=response.close)
                if outcome == "response-close":
                    response.close.side_effect = RuntimeError("close failure")
                    self.addCleanup(requests.Response.close, response)
                responses.append(response)
                return response
            self.server.status, self.server.body = 200, json.dumps({
                "access_token": "AT", "expires_on": int(time.time()) + 3600,
                "token_type": "Bearer"})
            if outcome == "redirect":
                self.server.status = 302
            elif outcome == "json":
                self.server.body = "not json"
            elif outcome == "expiry":
                self.server.body = '{"access_token":"AT","expires_on":"not an expiry"}'
            failure = {"setup": RuntimeError("setup"), "transport": requests.exceptions.ConnectionError(),
                "tls": SSLError(), "interrupt": KeyboardInterrupt()}.get(outcome)
            target = ("msal.managed_identity._ServiceFabricHTTPAdapter" if outcome == "setup"
                else "requests.sessions.Session.get")
            with self.subTest(outcome=outcome), patch(
                    "msal.managed_identity.requests.Session", side_effect=session_factory), patch.object(
                    HTTPAdapter, "build_response", capture):
                if failure:
                    with patch(target, side_effect=failure), self.assertRaises(type(failure)):
                        self._app().acquire_token_for_client(resource="R")
                elif outcome == "success":
                    self._app().acquire_token_for_client(resource="R")
                else:
                    with self.assertRaises((ManagedIdentityError, ValueError, RuntimeError)):
                        self._app().acquire_token_for_client(resource="R")
                self.assertEqual(1, len(sessions))
                sessions[0].close.assert_called_once_with()
                for response in responses:
                    response.close.assert_called_once_with()

    def test_fallback_matrix_and_diagnostic_privacy(self):
        for cached in (False, True):
            for failure in ("transport", "tls", "endpoint", "redirect", "json", "expiry", "options"):
                with self.subTest(cached=cached, failure=failure):
                    app = self._app({"unknown-private-key": "private-value"} if failure == "options" else {})
                    if cached:
                        self._seed_cache(app, refresh=True)
                    self.server.status = 302 if failure == "redirect" else 200
                    self.server.response_headers = {"Location": "https://private-location.invalid"}
                    self.server.body = ("invalid-json" if failure == "json"
                        else '{"access_token":"AT","expires_on":"invalid"}')
                    environment = dict(os.environ)
                    if failure == "tls":
                        environment["IDENTITY_SERVER_THUMBPRINT"] = "00" * 20
                    if failure == "endpoint":
                        environment["IDENTITY_ENDPOINT"] = "http://localhost"
                    transport = (patch.object(_ServiceFabricHTTPSConnection, "_new_conn",
                        side_effect=requests.exceptions.ConnectionError())
                        if failure == "transport" else nullcontext())
                    with patch.dict(os.environ, environment, clear=True), transport:
                        if cached and failure != "options":
                            self.assertEqual("cached", app.acquire_token_for_client(resource="R")["access_token"])
                        else:
                            with self.assertRaises(Exception) as error:
                                app.acquire_token_for_client(resource="R")
                            for private in ("private-location", "private-value", "unknown-private-key",
                                    "service-fabric-secret"):
                                self.assertNotIn(private, str(error.exception))

    def test_concurrent_and_sequential_acquisitions_do_not_share_configuration(self):
        from urllib3.poolmanager import pool_classes_by_scheme
        before = pool_classes_by_scheme.copy()
        proxy, address = self._proxy()
        clients = [self._app({"headers": {"X-Client": str(index)},
            "proxies": {"https": address} if index else {}}) for index in range(2)]
        sessions = []
        factory = _create_owned_service_fabric_http_client
        def capture(*args):
            session = factory(*args)
            sessions.append(session)
            return session
        with patch("msal.managed_identity._create_owned_service_fabric_http_client", side_effect=capture):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda app: app.acquire_token_for_client(resource="R"), clients))
            for app in clients:
                app.acquire_token_for_client(resource="R", claims_challenge="refresh")
        self.assertEqual(["AT", "AT"], [result["access_token"] for result in results])
        self.assertEqual(4, len({id(session) for session in sessions}))
        self.assertEqual(4, len({id(session.headers) for session in sessions}))
        self.assertEqual(4, len({id(session.proxies) for session in sessions}))
        self.assertEqual(["0", "0", "1", "1"],
            sorted(request["headers"]["X-Client"] for request in self.server.requests))
        self.assertEqual(2, len(proxy.connects))
        self.assertEqual(before, pool_classes_by_scheme)

    def test_distinct_endpoint_pins_are_isolated_during_concurrent_acquisitions(self):
        other = _ServiceFabricTlsFixture()
        other.setUp()
        self.addCleanup(other.doCleanups)
        self.addCleanup(other.tearDown)
        barrier = threading.Barrier(2)
        def acquire(fixture):
            barrier.wait(timeout=5)
            return _obtain_token_on_service_fabric(
                _UnopenedHttpClient(), fixture.endpoint, "service-fabric-secret",
                fixture.thumbprint, "R", service_fabric_http_options={
                    "headers": {"X-Pin": fixture.thumbprint}})
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(acquire, (self, other)))
        self.assertEqual(["AT", "AT"], [result["access_token"] for result in results])
        for fixture in (self, other):
            self.assertEqual(1, len(fixture.server.requests))
            self.assertEqual(fixture.thumbprint, fixture.server.requests[0]["headers"]["X-Pin"])

    def test_error_mapping_and_http_cache_parity(self):
        for code, error in [("SecretHeaderNotFound", "unauthorized_client"),
                ("ManagedIdentityNotFound", "invalid_client"),
                ("ArgumentNullOrEmpty", "invalid_scope"), ("Other", "invalid_request")]:
            self.server.status = 500
            self.server.body = json.dumps({"error": {"code": code}})
            cache = {}
            result = self._app(http_cache=cache).acquire_token_for_client(resource="R")
            self.assertEqual({"error": error, "error_description": self.server.body}, result)
            self.assertEqual({}, cache, "Legacy Service Fabric also bypasses HTTP throttling")

    def test_new_diagnostics_do_not_log_private_configuration_or_redirect_values(self):
        for options in ({"private-key": "private-value"},
                {"headers": {"Secret": "private-header"}},
                {"proxies": {"https": "socks5://user:private-password@host"}}):
            with self.assertLogs("msal", level="DEBUG") as logs, self.assertRaises(
                    ManagedIdentityError) as error:
                self._app(options).acquire_token_for_client(resource="R")
            self.assertNotIn("private-", str(error.exception) + "\n".join(logs.output))
        self.server.status = 302
        self.server.response_headers = {"Location": "https://private-location.invalid"}
        with self.assertLogs("msal", level="DEBUG") as logs, self.assertRaises(
                ManagedIdentityError) as error:
            self._app().acquire_token_for_client(resource="R")
        self.assertNotIn("private-", str(error.exception) + "\n".join(logs.output))

    def test_explicit_none_preserves_source_session_and_adapter_subclasses(self):
        for adapter_type in (HTTPAdapter, _ServiceFabricSourceHTTPAdapter):
            source = self._new_session()
            source.headers["X-Legacy"] = "legacy"
            source.params["legacy"] = "parameter"
            source.mount("https://", adapter_type())
            source.close = Mock(wraps=source.close)
            app = ManagedIdentityClient(SystemAssignedManagedIdentity(),
                http_client=source, service_fabric_http_options=None)
            app.acquire_token_for_client(resource="R")
            self.assertEqual("legacy", self.server.requests[-1]["headers"]["X-Legacy"])
            self.assertIn("legacy=parameter", self.server.requests[-1]["path"])
            source.close.assert_not_called()
        source = self._new_session()
        source.mount("https://", Mock(spec=BaseAdapter))
        with self.assertRaises(ManagedIdentityError):
            ManagedIdentityClient(SystemAssignedManagedIdentity(), http_client=source,
                service_fabric_http_options=None).acquire_token_for_client(resource="R")


class ServiceFabricOptionsCompatibilityTestCase(unittest.TestCase):
    def test_omission_and_none_preserve_legacy_contract(self):
        with patch.dict(os.environ, {
                "IDENTITY_ENDPOINT": "https://localhost", "IDENTITY_HEADER": "secret",
                "IDENTITY_SERVER_THUMBPRINT": "ab" * 20}, clear=True):
            for kwargs in ({}, {"service_fabric_http_options": None}):
                app = ManagedIdentityClient(SystemAssignedManagedIdentity(),
                    http_client=_UnopenedHttpClient(), **kwargs)
                with self.assertRaisesRegex(ManagedIdentityError, "requests.Session"):
                    app.acquire_token_for_client(resource="R")

    def test_other_providers_ignore_all_service_fabric_options(self):
        environments = [
            {},
            {"IDENTITY_ENDPOINT": "http://localhost", "IDENTITY_HEADER": "secret"},
            {"MSI_ENDPOINT": "http://localhost", "MSI_SECRET": "secret"},
            {"IDENTITY_ENDPOINT": "http://localhost", "IMDS_ENDPOINT": "http://localhost"},
        ]
        for environment in environments:
            for options in ({}, {"timeout": 2}, {"timeout": None}, False):
                with self.subTest(environment=environment, options=options), patch.dict(
                        os.environ, environment, clear=True), patch(
                        "msal.managed_identity.os.path.exists", return_value=False), patch(
                        "msal.managed_identity.requests.Session") as allocation:
                    consumer = Mock()
                    consumer.get.return_value = MinimalResponse(status_code=400, text='{"error":"expected"}')
                    app = ManagedIdentityClient(SystemAssignedManagedIdentity(),
                        http_client=consumer, service_fabric_http_options=options)
                    result = app.acquire_token_for_client(resource="R")
                    self.assertIn("error", result)
                    consumer.get.assert_called_once()
                    consumer.close.assert_not_called()
                    allocation.assert_not_called()

    def test_other_provider_success_protocol_and_cache_are_unchanged(self):
        environments = [
            ({}, "2018-02-01", "msi_res_id"),
            ({"IDENTITY_ENDPOINT": "http://localhost", "IDENTITY_HEADER": "secret"},
                "2019-08-01", "mi_res_id"),
            ({"MSI_ENDPOINT": "http://localhost", "MSI_SECRET": "secret"},
                "2017-09-01", "msi_res_id"),
            ({"IDENTITY_ENDPOINT": "http://localhost", "IMDS_ENDPOINT": "http://localhost"},
                "2020-06-01", "msi_res_id"),
        ]
        for environment, version, selector in environments:
            for options in ({}, {"timeout": 2}, {"timeout": None}):
                with self.subTest(environment=environment, options=options), patch.dict(
                        os.environ, environment, clear=True), patch(
                        "msal.managed_identity.os.path.exists", return_value=False), patch(
                        "msal.managed_identity.requests.Session") as allocation:
                    consumer = Mock()
                    success = MinimalResponse(status_code=200, text=json.dumps({
                        "access_token": "AT", "expires_in": 3600, "token_type": "Bearer",
                        "expires_on": int(time.time()) + 3600, "msi_res_id": "resource-id"}))
                    consumer.get.side_effect = ([MinimalResponse(status_code=401,
                        text="", headers={"www-authenticate": "Basic realm=challenge"}), success]
                        if version == "2020-06-01" else [success])
                    app = ManagedIdentityClient(UserAssignedManagedIdentity(resource_id="resource-id"),
                        http_client=consumer, service_fabric_http_options=options)
                    with patch.dict(_supported_arc_platforms_and_their_prefixes,
                            {sys.platform: os.getcwd()}), patch("builtins.open",
                            mock_open(read_data="secret")), patch(
                            "msal.managed_identity.os.stat", return_value=Mock(st_size=6)):
                        self.assertEqual("AT", app.acquire_token_for_client(resource="R")["access_token"])
                    self.assertEqual({"api-version": version, "resource": "R",
                        selector: "resource-id"}, consumer.get.call_args.kwargs["params"])
                    self.assertEqual("cache", app.acquire_token_for_client(resource="R")["token_source"])
                    consumer.close.assert_not_called()
                    allocation.assert_not_called()


@patch.dict(os.environ, {
    "IDENTITY_ENDPOINT": "http://localhost/token",
    "IMDS_ENDPOINT": "http://localhost",
})
@patch(
    "builtins.open" if sys.version_info.major >= 3 else "__builtin__.open",
    new=mock_open(read_data="secret"),  # `new` requires no extra argument on the decorated function.
        #  https://docs.python.org/3/library/unittest.mock.html#unittest.mock.patch
)
@patch("os.stat", return_value=Mock(st_size=4096))
class ArcTestCase(ClientTestCase):
    challenge = MinimalResponse(status_code=401, text="", headers={
        "WWW-Authenticate": "Basic realm=/tmp/foo",
        })

    def test_error_out_on_invalid_input(self, mocked_stat):
        return super(ArcTestCase, self).test_error_out_on_invalid_input()

    def test_happy_path(self, mocked_stat):
        expires_in = 1234
        with patch.object(self.app._http_client, "get", side_effect=[
            self.challenge,
            MinimalResponse(
                status_code=200,
                text='{"access_token": "AT", "expires_in": "%s", "resource": "R"}' % expires_in,
                ),
            ] * 2,  # Duplicate a pair of mocks for _test_happy_path()'s CAE check
        ) as mocked_method:
            try:
                self._test_happy_path(self.app, mocked_method, expires_in)
                mocked_stat.assert_called_with(os.path.join(
                    _supported_arc_platforms_and_their_prefixes[sys.platform],
                    "foo.key"))
            except ArcPlatformNotSupportedError:
                if sys.platform in _supported_arc_platforms_and_their_prefixes:
                    self.fail("Should not raise ArcPlatformNotSupportedError")

    def test_arc_error_should_be_normalized(self, mocked_stat):
        with patch.object(self.app._http_client, "get", side_effect=[
            self.challenge,
            MinimalResponse(status_code=400, text="undefined"),
        ]) as mocked_method:
            try:
                self.assertEqual({
                    "error": "invalid_request",
                    "error_description": "undefined",
                }, self.app.acquire_token_for_client(resource="R"))
                self.assertEqual({}, self.app._token_cache._cache)
            except ArcPlatformNotSupportedError:
                if sys.platform in _supported_arc_platforms_and_their_prefixes:
                    self.fail("Should not raise ArcPlatformNotSupportedError")

    def test_arc_error_before_challenge_should_be_normalized(self, mocked_stat):
        error = '{"error":"invalid_request","error_description":"The requested identity was not found"}'
        app = ManagedIdentityClient(
            UserAssignedManagedIdentity(client_id="system-assigned-client-id"),
            http_client=requests.Session())
        with patch.object(app._http_client, "get", return_value=MinimalResponse(
            status_code=400,
            text=error,
            headers={"content-type": "application/json"},
        )) as mocked_method:
            self.assertEqual({
                "error": "invalid_request",
                "error_description": error,
            }, app.acquire_token_for_client(resource="R"))
            mocked_method.assert_called_once_with(
                "http://localhost/token",
                params={
                    "api-version": "2020-06-01",
                    "resource": "R",
                    "client_id": "system-assigned-client-id",
                },
                headers={"Metadata": "true"},
            )
            self.assertEqual({}, app._token_cache._cache)

    def _assert_user_assigned_selector(self, managed_identity, selector_name, selector_value):
        app = ManagedIdentityClient(managed_identity, http_client=requests.Session())
        with patch.object(app._http_client, "get", side_effect=[
            self.challenge,
            MinimalResponse(
                status_code=200,
                # A compliant Azure Arc agent echoes the identity it used; MSAL verifies it (fail closed).
                text='{"access_token": "AT", "expires_in": "1234", "resource": "R", "%s": "%s"}' % (
                    selector_name, selector_value),
            ),
        ]) as mocked_method:
            try:
                result = app.acquire_token_for_client(resource="R")
                self.assertEqual("AT", result["access_token"])
                expected_params = {
                    "api-version": "2020-06-01",
                    "resource": "R",
                    selector_name: selector_value,
                }
                self.assertEqual(expected_params, mocked_method.call_args_list[0].kwargs["params"])
                self.assertEqual(expected_params, mocked_method.call_args_list[1].kwargs["params"])
            except ArcPlatformNotSupportedError:
                if sys.platform in _supported_arc_platforms_and_their_prefixes:
                    self.fail("Should not raise ArcPlatformNotSupportedError")

    def test_arc_user_assigned_client_id_should_be_forwarded(self, mocked_stat):
        self._assert_user_assigned_selector(
            UserAssignedManagedIdentity(client_id="client-id"),
            "client_id",
            "client-id",
        )

    def test_arc_user_assigned_resource_id_should_be_forwarded_as_msi_res_id(self, mocked_stat):
        self._assert_user_assigned_selector(
            UserAssignedManagedIdentity(resource_id="resource-id"),
            "msi_res_id",
            "resource-id",
        )

    def test_arc_user_assigned_object_id_should_be_forwarded(self, mocked_stat):
        self._assert_user_assigned_selector(
            UserAssignedManagedIdentity(object_id="object-id"),
            "object_id",
            "object-id",
        )

    def test_arc_user_assigned_identity_not_confirmed_should_fail_closed(self, mocked_stat):
        # A legacy Azure Arc agent ignores the selector and returns the system-assigned identity:
        # the token response echoes a different identity than the one requested. MSAL must fail
        # closed rather than hand back a token for a different identity than requested.
        app = ManagedIdentityClient(
            UserAssignedManagedIdentity(client_id="client-id"),
            http_client=requests.Session())
        with patch.object(app._http_client, "get", side_effect=[
            self.challenge,
            MinimalResponse(
                status_code=200,
                text='{"access_token": "AT", "expires_in": "1234", "resource": "R", "client_id": "a-different-id"}',
            ),
        ]):
            with self.assertRaises(ManagedIdentityError):
                app.acquire_token_for_client(resource="R")
        self.assertEqual(
            {}, app._token_cache._cache,
            "An unconfirmed identity token must not be cached")


class GetManagedIdentitySourceTestCase(unittest.TestCase):

    @patch.dict(os.environ, {
        "IDENTITY_ENDPOINT": "http://localhost",
        "IDENTITY_HEADER": "foo",
        "IDENTITY_SERVER_THUMBPRINT": "bar",
    })
    def test_service_fabric(self):
        self.assertEqual(get_managed_identity_source(), SERVICE_FABRIC)

    @patch.dict(os.environ, {
        "IDENTITY_ENDPOINT": "http://localhost",
        "IDENTITY_HEADER": "foo",
    })
    def test_app_service(self):
        self.assertEqual(get_managed_identity_source(), APP_SERVICE)

    @patch.dict(os.environ, {
        "MSI_ENDPOINT": "http://localhost",
        "MSI_SECRET": "foo",
    })
    def test_machine_learning(self):
        self.assertEqual(get_managed_identity_source(), MACHINE_LEARNING)

    @patch.dict(os.environ, {
        "IDENTITY_ENDPOINT": "http://localhost",
        "IMDS_ENDPOINT": "http://localhost",
    })
    def test_arc_by_env_var(self):
        self.assertEqual(get_managed_identity_source(), AZURE_ARC)

    @patch("msal.managed_identity.os.path.exists", return_value=True)
    @patch("msal.managed_identity.sys.platform", new="linux")
    def test_arc_by_file_existence_on_linux(self, mocked_exists):
        self.assertEqual(get_managed_identity_source(), AZURE_ARC)
        mocked_exists.assert_called_with("/opt/azcmagent/bin/himds")

    @patch("msal.managed_identity.os.path.exists", return_value=True)
    @patch("msal.managed_identity.sys.platform", new="win32")
    @patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files"})
    def test_arc_by_file_existence_on_windows(self, mocked_exists):
        self.assertEqual(get_managed_identity_source(), AZURE_ARC)
        mocked_exists.assert_called_with(
            r"C:\Program Files\AzureConnectedMachineAgent\himds.exe")

    @patch.dict(os.environ, {
        "AZUREPS_HOST_ENVIRONMENT": "cloud-shell-foo",
    })
    def test_cloud_shell(self):
        self.assertEqual(get_managed_identity_source(), CLOUD_SHELL)

    def test_default_to_vm(self):
        self.assertEqual(get_managed_identity_source(), DEFAULT_TO_VM)
