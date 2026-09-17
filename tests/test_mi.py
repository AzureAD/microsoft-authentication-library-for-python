import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
import unittest
try:
    from unittest.mock import patch, ANY, mock_open, Mock
except:
    from mock import patch, ANY, mock_open, Mock
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.test_throttled_http_client import (
    MinimalResponse, ThrottledHttpClientBaseTestCase, DummyHttpClient)
from msal import (
    SystemAssignedManagedIdentity, UserAssignedManagedIdentity,
    ManagedIdentityClient,
    ManagedIdentityError,
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
    _obtain_token_on_service_fabric,
    _request_service_fabric_token,
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
                "msal.managed_identity._request_service_fabric_token",
                return_value=MinimalResponse(
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
            "ab" * 20,
            {
                'api-version': '2019-07-01-preview',
                'resource': 'R',
                'token_sha256_to_refresh': self.access_token_sha256,
                "xms_cc": "foo,bar",
            },
            'foo',
        )

    def test_happy_path_with_claim_challenge_should_send_sha256_to_provider(self):
        self._test_happy_path(
            self._build_app(client_capabilities=[]),  # Test empty client_capabilities
            claims_challenge='{"access_token": {"nbf": {"essential": true, "value": "1563308371"}}}',
        ).assert_called_with(
            'https://localhost',
            "ab" * 20,
            {
                'api-version': '2019-07-01-preview',
                'resource': 'R',
                'token_sha256_to_refresh': self.access_token_sha256,
                # There is no xms_cc in this case
            },
            'foo',
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
                "msal.managed_identity._request_service_fabric_token",
                return_value=MinimalResponse(
                status_code=404,
                text=raw_error,
            )):
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
        status_codes = getattr(self.server, "status_codes", None)
        if status_codes:
            status_code = status_codes.pop(0)
            if status_code != 200:
                body = json.dumps({"error": {"code": "transient"}}).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)
                return
        redirect_url = getattr(self.server, "redirect_url", None)
        if redirect_url:
            self.send_response(302)
            self.send_header("Location", redirect_url)
            self.end_headers()
            return
        body = json.dumps({
            "access_token": "AT",
            "expires_on": str(int(time.time()) + 3600),
            "resource": "R",
            "token_type": "Bearer",
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        trickle_interval = getattr(self.server, "trickle_interval", None)
        if trickle_interval is None:
            self.wfile.write(body)
            return
        try:
            for byte in body:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(trickle_interval)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def log_message(self, format, *args):
        pass


class ServiceFabricTlsValidationTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
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
        certificate_path = os.path.join(self._temporary_directory.name, "server.pem")
        private_key_path = os.path.join(self._temporary_directory.name, "server.key")
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
        self.server.socket = tls_context.wrap_socket(self.server.socket, server_side=True)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.endpoint = "https://localhost:{}/token".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self._temporary_directory.cleanup()

    def _new_session(self):
        session = requests.Session()
        session.trust_env = False
        return session

    def test_matching_thumbprint_sends_secret_after_validating_certificate(self):
        result = _obtain_token_on_service_fabric(
            self._new_session(),
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
        with self.assertRaises(SSLError):
            _obtain_token_on_service_fabric(
                self._new_session(),
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

    def test_service_fabric_request_uses_a_finite_timeout(self):
        connection = Mock()
        connection.sock.getpeercert.return_value = b"certificate"
        response = Mock()
        response.status = 200
        response.read.return_value = b"{}"
        response.headers.get_content_charset.return_value = "utf-8"
        response.getheaders.return_value = []
        connection.getresponse.return_value = response
        with patch(
                "msal.managed_identity._ServiceFabricHTTPSConnection",
                return_value=connection) as mocked_connection:
            with patch(
                    "msal.managed_identity._resolve_service_fabric_endpoint",
                    return_value=[Mock()]) as mocked_resolve:
                with patch("msal.managed_identity.hashlib.sha1") as mocked_sha1:
                    mocked_sha1.return_value.hexdigest.return_value = self.thumbprint
                    _request_service_fabric_token(
                        "https://localhost/token",
                        self.thumbprint,
                        {"resource": "R"},
                        "service-fabric-secret",
                    )

        mocked_connection.assert_called_once_with(
            "localhost",
            port=None,
            resolved_addresses=[ANY],
            timeout=5,
            context=ANY,
        )
        mocked_resolve.assert_called_once_with("localhost", 443)
        tls_context = mocked_connection.call_args.kwargs["context"]
        self.assertFalse(tls_context.check_hostname)
        self.assertEqual(ssl.CERT_NONE, tls_context.verify_mode)
        self.assertEqual(ssl.TLSVersion.TLSv1_2, tls_context.minimum_version)
        connection.close.assert_called_once_with()

    def test_service_fabric_request_has_a_wall_clock_deadline(self):
        self.server.trickle_interval = 0.05
        started_at = time.monotonic()
        try:
            with patch(
                    "msal.managed_identity._SERVICE_FABRIC_REQUEST_TIMEOUT",
                    new=0.1):
                with patch(
                        "msal.managed_identity._SERVICE_FABRIC_MAX_ATTEMPTS",
                        new=1):
                    with self.assertRaises(socket.timeout):
                        _obtain_token_on_service_fabric(
                            self._new_session(),
                            self.endpoint,
                            "service-fabric-secret",
                            self.thumbprint,
                            "R",
                        )
        finally:
            del self.server.trickle_interval

        self.assertLess(time.monotonic() - started_at, 1)

    def test_dns_timeout_cannot_later_send_the_secret(self):
        original_getaddrinfo = socket.getaddrinfo

        def delayed_getaddrinfo(*args, **kwargs):
            time.sleep(0.2)
            return original_getaddrinfo(*args, **kwargs)

        with patch(
                "msal.managed_identity._SERVICE_FABRIC_REQUEST_TIMEOUT",
                new=0.05):
            with patch(
                    "msal.managed_identity.socket.getaddrinfo",
                    side_effect=delayed_getaddrinfo):
                with self.assertRaises(socket.timeout):
                    _obtain_token_on_service_fabric(
                        self._new_session(),
                        self.endpoint,
                        "service-fabric-secret",
                        self.thumbprint,
                        "R",
                    )

        time.sleep(0.3)
        self.assertEqual([], self.server.requests)

    def test_service_fabric_does_not_construct_a_requests_session(self):
        class CustomHttpClient:
            pass

        with patch(
                "msal.managed_identity.requests.Session",
                side_effect=AssertionError("Requests must not own this transport")):
            result = _obtain_token_on_service_fabric(
                CustomHttpClient(),
                self.endpoint,
                "service-fabric-secret",
                self.thumbprint,
                "R",
            )

        self.assertEqual("AT", result["access_token"])

    def test_custom_adapter_does_not_block_service_fabric(self):
        source = self._new_session()

        class CustomAdapter(HTTPAdapter):
            def send(self, *args, **kwargs):
                raise AssertionError("The caller's adapter must not be used")

        source.mount("https://", CustomAdapter())
        result = _obtain_token_on_service_fabric(
            source,
            self.endpoint,
            "service-fabric-secret",
            self.thumbprint,
            "R",
        )

        self.assertEqual("AT", result["access_token"])
        self.assertEqual(
            "service-fabric-secret",
            self.server.requests[0]["headers"]["Secret"])

    def test_custom_http_client_does_not_block_service_fabric(self):
        class CustomHttpClient:
            def get(self, *args, **kwargs):
                raise AssertionError("The caller's transport must not be used")

        result = _obtain_token_on_service_fabric(
            CustomHttpClient(),
            self.endpoint,
            "service-fabric-secret",
            self.thumbprint,
            "R",
        )

        self.assertEqual("AT", result["access_token"])
        self.assertEqual(
            "service-fabric-secret",
            self.server.requests[0]["headers"]["Secret"])

    def test_transient_responses_are_retried(self):
        for transient_status in (429, 503):
            with self.subTest(status_code=transient_status):
                self.server.requests = []
                self.server.status_codes = [transient_status, 200]
                result = _obtain_token_on_service_fabric(
                    self._new_session(),
                    self.endpoint,
                    "service-fabric-secret",
                    self.thumbprint,
                    "R",
                )

                self.assertEqual("AT", result["access_token"])
                self.assertEqual(2, len(self.server.requests))
                self.assertEqual([], self.server.status_codes)
        del self.server.status_codes

    def test_non_retryable_response_is_not_retried(self):
        self.server.status_codes = [404, 200]
        try:
            result = _obtain_token_on_service_fabric(
                self._new_session(),
                self.endpoint,
                "service-fabric-secret",
                self.thumbprint,
                "R",
            )
        finally:
            del self.server.status_codes

        self.assertEqual("invalid_request", result["error"])
        self.assertEqual(1, len(self.server.requests))

    def test_custom_http_client_gets_internal_retry_behavior(self):
        class CustomHttpClient:
            def get(self, *args, **kwargs):
                raise AssertionError("The caller's transport must not be used")

        self.server.status_codes = [503, 200]
        try:
            result = _obtain_token_on_service_fabric(
                CustomHttpClient(),
                self.endpoint,
                "service-fabric-secret",
                self.thumbprint,
                "R",
            )
        finally:
            del self.server.status_codes

        self.assertEqual("AT", result["access_token"])
        self.assertEqual(2, len(self.server.requests))

    def test_redirect_does_not_forward_the_secret(self):
        redirect_target = ThreadingHTTPServer(
            ("localhost", 0), _ServiceFabricTlsRequestHandler)
        redirect_target.requests = []
        redirect_target_thread = threading.Thread(
            target=redirect_target.serve_forever)
        redirect_target_thread.start()
        try:
            self.server.redirect_url = "http://localhost:{}/token".format(
                redirect_target.server_port)
            result = _obtain_token_on_service_fabric(
                self._new_session(),
                self.endpoint,
                "service-fabric-secret",
                self.thumbprint,
                "R",
            )

            self.assertEqual("invalid_request", result["error"])
            self.assertEqual(
                "Service Fabric managed identity endpoint redirects are not supported.",
                result["error_description"])
            self.assertEqual([], redirect_target.requests)
        finally:
            del self.server.redirect_url
            redirect_target.shutdown()
            redirect_target.server_close()
            redirect_target_thread.join()


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
