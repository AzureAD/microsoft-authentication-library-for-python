import hashlib
import json
import os
import sys
import time
from typing import List, Optional
import unittest
try:
    from unittest.mock import patch, ANY, mock_open, Mock
except:
    from mock import patch, ANY, mock_open, Mock
import requests

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
)
from msal.token_cache import is_subdict_of


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
        self._test_happy_path().assert_called_with(
            # The last call contained claims_challenge
            # but since IMDS doesn't support token_sha256_to_refresh,
            # the request shall remain the same as before
            'http://169.254.169.254/metadata/identity/oauth2/token',
            params={'api-version': '2018-02-01', 'resource': 'R'},
            headers={'Metadata': 'true'},
            )

    @patch.dict(os.environ, {"AZURE_POD_IDENTITY_AUTHORITY_HOST": "http://localhost:1234//"})
    def test_happy_path_of_pod_identity(self):
        self._test_happy_path().assert_called_with(
            'http://localhost:1234/metadata/identity/oauth2/token',
            params={'api-version': '2018-02-01', 'resource': 'R'},
            headers={'Metadata': 'true'},
            )

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
                headers={'Metadata': 'true'},
                )


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
    "IDENTITY_ENDPOINT": "http://localhost",
    "IDENTITY_HEADER": "foo",
    "IDENTITY_SERVER_THUMBPRINT": "bar",
})
class ServiceFabricTestCase(ClientTestCase):
    access_token = "AT"
    access_token_sha256 = hashlib.sha256(access_token.encode()).hexdigest()

    def _test_happy_path(self, app, *, claims_challenge=None) -> callable:
        expires_in = 1234
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
            'http://localhost',
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
            'http://localhost',
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
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=404,
            text=raw_error,
        )) as mocked_method:
            self.assertEqual({
                "error": "unauthorized_client",
                "error_description": raw_error,
            }, self.app.acquire_token_for_client(resource="R"))
            self.assertEqual({}, self.app._token_cache._cache)


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
    @patch.dict(os.environ, {"ProgramFiles": "C:\Program Files"})
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

