import json
import os
import sys
import time
import unittest
try:
    from unittest.mock import patch, ANY, mock_open, Mock
except:
    from mock import patch, ANY, mock_open, Mock
import requests

from tests.http_client import MinimalResponse
from msal import (
    SystemAssignedManagedIdentity, UserAssignedManagedIdentity,
    ManagedIdentityClient,
    ManagedIdentityError,
    ArcPlatformNotSupportedError,
)
from msal.managed_identity import (
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


class ClientTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.app = ManagedIdentityClient(
            {   # Here we test it with the raw dict form, to test that
                # the client has no hard dependency on ManagedIdentity object
                "ManagedIdentityIdType": "SystemAssigned", "Id": None,
            },
            http_client=requests.Session(),
            )

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

    def _test_happy_path(self, app, mocked_http, expires_in, resource="R"):
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

        result = app.acquire_token_for_client(resource=resource, claims_challenge="foo")
        self.assertEqual("identity_provider", result["token_source"], "Should miss cache")


class VmTestCase(ClientTestCase):

    def test_happy_path(self):
        expires_in = 7890  # We test a bigger than 7200 value here
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_in": "%s", "resource": "R"}' % expires_in,
        )) as mocked_method:
            self._test_happy_path(self.app, mocked_method, expires_in)

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

    def _test_happy_path(self, app):
        expires_in = 1234
        with patch.object(app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": %s, "resource": "R", "token_type": "Bearer"}' % (
                int(time.time()) + expires_in),
        )) as mocked_method:
            super(ServiceFabricTestCase, self)._test_happy_path(
                app, mocked_method, expires_in)

    def test_happy_path(self):
        self._test_happy_path(self.app)

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

