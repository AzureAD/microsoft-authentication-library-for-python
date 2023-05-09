import json
import os
import sys
import time
import unittest
try:
    from unittest.mock import patch, ANY, mock_open
except:
    from mock import patch, ANY, mock_open
import requests

from tests.http_client import MinimalResponse
from msal import (
    ConfidentialClientApplication,
    SystemAssignedManagedIdentity, UserAssignedManagedIdentity,
    )


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
        with self.assertRaises(ValueError):
            UserAssignedManagedIdentity()
        with self.assertRaises(ValueError):
            UserAssignedManagedIdentity(client_id="foo", resource_id="bar")
        self.assertEqual(
            SystemAssignedManagedIdentity(),
            {"ManagedIdentityIdType": "SystemAssigned", "Id": None})


class ClientTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        system_assigned = {"ManagedIdentityIdType": "SystemAssigned", "Id": None}
        self.app = ConfidentialClientApplication(client_id=system_assigned)

    def _test_token_cache(self, app):
        cache = app.token_cache._cache
        self.assertEqual(1, len(cache.get("AccessToken", [])), "Should have 1 AT")
        at = list(cache["AccessToken"].values())[0]
        self.assertEqual(
            app.client_id.get("Id", "SYSTEM_ASSIGNED_MANAGED_IDENTITY"),
            at["client_id"],
            "Should have expected client_id")
        self.assertEqual("managed_identity", at["realm"], "Should have expected realm")

    def _test_happy_path(self, app, mocked_http):
        #result = app.acquire_token_for_client(resource="R")
        result = app.acquire_token_for_client(["R"])
        mocked_http.assert_called()
        self.assertEqual({
            "access_token": "AT",
            "expires_in": 1234,
            "resource": "R",
            "token_type": "Bearer",
        }, result, "Should obtain a token response")
        self.assertEqual(
            result["access_token"],
            app.acquire_token_for_client(["R"]).get("access_token"),
            "Should hit the same token from cache")
        self._test_token_cache(app)


class VmTestCase(ClientTestCase):

    def test_happy_path(self):
        with patch.object(self.app.http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_in": "1234", "resource": "R"}',
        )) as mocked_method:
            self._test_happy_path(self.app, mocked_method)

    def test_vm_error_should_be_returned_as_is(self):
        raw_error = '{"raw": "error format is undefined"}'
        with patch.object(self.app.http_client, "get", return_value=MinimalResponse(
            status_code=400,
            text=raw_error,
        )) as mocked_method:
            self.assertEqual(
                json.loads(raw_error), self.app.acquire_token_for_client(["R"]))
            self.assertEqual({}, self.app.token_cache._cache)


@patch.dict(os.environ, {"IDENTITY_ENDPOINT": "http://localhost", "IDENTITY_HEADER": "foo"})
class AppServiceTestCase(ClientTestCase):

    def test_happy_path(self):
        with patch.object(self.app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": "%s", "resource": "R"}' % (
                int(time.time()) + 1234),
        )) as mocked_method:
            self._test_happy_path(self.app, mocked_method)

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


@patch.dict(os.environ, {
    "IDENTITY_ENDPOINT": "http://localhost",
    "IDENTITY_HEADER": "foo",
    "IDENTITY_SERVER_THUMBPRINT": "bar",
})
class ServiceFabricTestCase(ClientTestCase):

    def _test_happy_path(self, app):
        with patch.object(app._http_client, "get", return_value=MinimalResponse(
            status_code=200,
            text='{"access_token": "AT", "expires_on": %s, "resource": "R", "token_type": "Bearer"}' % (
                int(time.time()) + 1234),
        )) as mocked_method:
            super(ServiceFabricTestCase, self)._test_happy_path(app, mocked_method)

    def test_happy_path(self):
        self._test_happy_path(self.app)

    def test_unified_api_service_should_ignore_unnecessary_client_id(self):
        self._test_happy_path(ManagedIdentityClient(
            {"ManagedIdentityIdType": "ClientId", "Id": "foo"},
            requests.Session(),
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
    mock_open(read_data="secret")
)
class ArcTestCase(ClientTestCase):
    challenge = MinimalResponse(status_code=401, text="", headers={
        "WWW-Authenticate": "Basic realm=/tmp/foo",
        })

    def test_happy_path(self):
        with patch.object(self.app._http_client, "get", side_effect=[
            self.challenge,
            MinimalResponse(
                status_code=200,
                text='{"access_token": "AT", "expires_in": "1234", "resource": "R"}',
                ),
        ]) as mocked_method:
            super(ArcTestCase, self)._test_happy_path(self.app, mocked_method)

    def test_arc_error_should_be_normalized(self):
        with patch.object(self.app._http_client, "get", side_effect=[
            self.challenge,
            MinimalResponse(status_code=400, text="undefined"),
        ]) as mocked_method:
            self.assertEqual({
                "error": "invalid_request",
                "error_description": "undefined",
            }, self.app.acquire_token_for_client(resource="R"))
            self.assertEqual({}, self.app._token_cache._cache)

