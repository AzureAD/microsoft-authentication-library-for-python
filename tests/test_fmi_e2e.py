"""End-to-end tests for Federated Managed Identity (FMI) functionality.

These tests verify:
1. Tokens can be acquired using certificate authentication with FMI path
2. Tokens are properly cached and returned from cache on subsequent calls
3. Tokens can be acquired using an assertion callback (RMA pattern) with FMI path

"""

import logging
import os
import sys
import unittest

import msal
from tests.http_client import MinimalHttpClient
from tests.lab_config import get_client_certificate
from tests.test_e2e import LabBasedTestCase

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.INFO)

# Test configuration
_FMI_TENANT_ID = "f645ad92-e38d-4d1a-b510-d1b09a74a8ca"
_FMI_CLIENT_ID = "4df2cbbb-8612-49c1-87c8-f334d6d065ad"
_FMI_SCOPE = "3091264c-7afb-45d4-b527-39737ee86187/.default"
_FMI_PATH = "SomeFmiPath/FmiCredentialPath"
_FMI_CLIENT_ID_URN = "urn:microsoft:identity:fmi"
_FMI_SCOPE_FOR_RMA = "api://AzureFMITokenExchange/.default"
_AUTHORITY_URL = "https://login.microsoftonline.com/" + _FMI_TENANT_ID


def _get_fmi_credential_from_rma():
    """Acquire an FMI token from RMA service using certificate credentials.

    This mirrors the Go function GetFmiCredentialFromRma:
    1. Create a confidential client with certificate credential
    2. Acquire a token for the FMI scope with the FMI path
    3. Return the access token as an assertion string
    """

    app = msal.ConfidentialClientApplication(
        _FMI_CLIENT_ID,
        client_credential=get_client_certificate(),
        authority=_AUTHORITY_URL,
        http_client=MinimalHttpClient(),
    )
    result = app.acquire_token_for_client(
        [_FMI_SCOPE_FOR_RMA], fmi_path=_FMI_PATH)
    if "access_token" not in result:
        raise RuntimeError(
            "Failed to acquire FMI token from RMA: {}: {}".format(
                result.get("error"), result.get("error_description")))
    return result["access_token"]


class TestFMIBasicFunctionality(LabBasedTestCase):
    """Test basic FMI token acquisition with certificate credential.

    Mirrors TestFMIBasicFunctionality from Go:
    1. Acquire token by credential with FMI path
    2. Verify silent (cached) token acquisition works
    3. Validate tokens match (proving cache was used)
    """

    def test_acquire_and_cache_with_fmi_path(self):
        app = msal.ConfidentialClientApplication(
            _FMI_CLIENT_ID,
            client_credential=get_client_certificate(),
            authority=_AUTHORITY_URL,
            http_client=MinimalHttpClient(),
        )
        scopes = [_FMI_SCOPE]

        # 1. Acquire token by credential with FMI path
        result = app.acquire_token_for_client(scopes, fmi_path=_FMI_PATH)
        self.assertIn("access_token", result,
            "acquire_token_for_client(fmi_path=...) failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
        self.assertNotEqual("", result["access_token"],
            "acquire_token_for_client(fmi_path=...) returned empty access token")

        first_token = result["access_token"]

        # 2. Verify silent token acquisition works (should retrieve from cache)
        cache_result = app.acquire_token_for_client(scopes, fmi_path=_FMI_PATH)
        self.assertIn("access_token", cache_result,
            "Second call failed: {}: {}".format(
                cache_result.get("error"), cache_result.get("error_description")))
        self.assertNotEqual("", cache_result["access_token"],
            "Second call returned empty access token")
        self.assertEqual(
            cache_result.get("token_source"), "cache",
            "Second call should return token from cache")

        # 3. Validate tokens match (proving cache was used)
        self.assertEqual(first_token, cache_result["access_token"],
            "Token comparison failed - tokens don't match, "
            "cache might not be working correctly")

class TestFMIIntegration(LabBasedTestCase):
    """Test FMI with assertion callback (RMA pattern).

    Mirrors TestFMIIntegration from Go:
    1. Get credentials from RMA via assertion callback
    2. Acquire token by credential with FMI path
    3. Verify cached token acquisition works
    4. Compare tokens to verify cache was used
    """

    def test_acquire_with_assertion_callback_and_fmi_path(self):
        # Create credential from assertion callback (mirrors Go's NewCredFromAssertionCallback)
        client_credential = {
            "client_assertion": lambda: _get_fmi_credential_from_rma(),
        }

        app = msal.ConfidentialClientApplication(
            _FMI_CLIENT_ID_URN,
            client_credential=client_credential,
            authority=_AUTHORITY_URL,
            http_client=MinimalHttpClient(),
        )
        scopes = [_FMI_SCOPE]
        fmi_path = "SomeFmiPath/Path"

        # 1. Acquire token by credential with FMI path
        result = app.acquire_token_for_client(scopes, fmi_path=fmi_path)
        self.assertIn("access_token", result,
            "acquire_token_for_client(fmi_path=...) failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
        self.assertNotEqual("", result["access_token"],
            "acquire_token_for_client(fmi_path=...) returned empty access token")
        first_token = result["access_token"]
        
        # 2. Verify cached token acquisition works
        cache_result = app.acquire_token_for_client(scopes, fmi_path=fmi_path)
        self.assertIn("access_token", cache_result,
            "Second call failed: {}: {}".format(
                cache_result.get("error"), cache_result.get("error_description")))
        self.assertNotEqual("", cache_result["access_token"],
            "Second call returned empty access token")
        self.assertEqual(
            cache_result.get("token_source"), "cache",
            "Second call should return token from cache")

        # 3. Compare tokens to verify cache was used
        self.assertEqual(first_token, cache_result["access_token"],
            "Token comparison failed - tokens don't match, "
            "cache might not be working correctly")


class TestFMICacheIsolation(LabBasedTestCase):
    """Test that tokens acquired with different FMI paths are cached separately.

    This verifies the cache key extensibility: two calls with different fmi_path
    values should NOT return each other's cached tokens.
    """

    def test_different_fmi_paths_are_cached_separately(self):
        app = msal.ConfidentialClientApplication(
            _FMI_CLIENT_ID,
            client_credential=get_client_certificate(),
            authority=_AUTHORITY_URL,
            http_client=MinimalHttpClient(),
        )
        scopes = [_FMI_SCOPE]

        # Acquire token with path A
        result_a = app.acquire_token_for_client(
            scopes, fmi_path="PathA/credential")
        self.assertIn("access_token", result_a,
            "Path A acquisition failed: {}: {}".format(
                result_a.get("error"), result_a.get("error_description")))

        # Acquire token with path B — should NOT get path A's cached token
        result_b = app.acquire_token_for_client(
            scopes, fmi_path="PathB/credential")
        self.assertIn("access_token", result_b,
            "Path B acquisition failed: {}: {}".format(
                result_b.get("error"), result_b.get("error_description")))
        self.assertNotEqual(
            result_b.get("token_source"), "cache",
            "Different FMI path should NOT return cached token from another path")

        # Verify path A still returns its own cached token
        result_a2 = app.acquire_token_for_client(
            scopes, fmi_path="PathA/credential")
        self.assertIn("access_token", result_a2)
        self.assertEqual(
            result_a2.get("token_source"), "cache",
            "Same FMI path should return cached token")
        self.assertEqual(result_a["access_token"], result_a2["access_token"])

    def test_fmi_token_does_not_interfere_with_non_fmi_token(self):
        app = msal.ConfidentialClientApplication(
            _FMI_CLIENT_ID,
            client_credential=get_client_certificate(),
            authority=_AUTHORITY_URL,
            http_client=MinimalHttpClient(),
        )
        scopes = [_FMI_SCOPE]

        # Cache a token via FMI path
        fmi_result = app.acquire_token_for_client(scopes, fmi_path=_FMI_PATH)
        self.assertIn("access_token", fmi_result)

        # Regular acquire_token_for_client should NOT get the FMI token
        regular_result = app.acquire_token_for_client(scopes)
        self.assertIn("access_token", regular_result,
            "Regular call failed: {}: {}".format(
                regular_result.get("error"), regular_result.get("error_description")))
        self.assertNotEqual(
            regular_result.get("token_source"), "cache",
            "Non-FMI call should not return FMI-cached token")


class TestFMICacheInspection(LabBasedTestCase):
    """Acquire tokens with two different FMI paths and inspect the underlying
    cache to verify the entries are correctly isolated."""

    def test_two_fmi_paths_produce_separate_cache_entries(self):
        app = msal.ConfidentialClientApplication(
            _FMI_CLIENT_ID,
            client_credential=get_client_certificate(),
            authority=_AUTHORITY_URL,
            http_client=MinimalHttpClient(),
        )
        scopes = [_FMI_SCOPE]
        path_a = "PathAlpha/Credential"
        path_b = "PathBeta/Credential"

        # 1. Acquire token with path A
        result_a = app.acquire_token_for_client(scopes, fmi_path=path_a)
        self.assertIn("access_token", result_a,
            "Path A acquisition failed: {}: {}".format(
                result_a.get("error"), result_a.get("error_description")))
        token_a = result_a["access_token"]

        # 2. Acquire token with path B
        result_b = app.acquire_token_for_client(scopes, fmi_path=path_b)
        self.assertIn("access_token", result_b,
            "Path B acquisition failed: {}: {}".format(
                result_b.get("error"), result_b.get("error_description")))
        token_b = result_b["access_token"]

        # Tokens should be different (different paths go to different resources)
        self.assertNotEqual(token_a, token_b,
            "Tokens for different FMI paths should differ")

        # 3. Inspect cache: there should be exactly 2 AccessToken entries
        cache = app.token_cache._cache
        at_entries = cache.get("AccessToken", {})
        # Filter to our client_id + scope to avoid noise
        our_entries = {
            k: v for k, v in at_entries.items()
            if v.get("client_id") == _FMI_CLIENT_ID
            and _FMI_SCOPE.split("/")[0] in v.get("target", "")
        }
        self.assertEqual(2, len(our_entries),
            "Cache should contain exactly 2 AT entries for our client, "
            "got {}: {}".format(len(our_entries), list(our_entries.keys())))

        # 4. Each entry must have a non-empty ext_cache_key, and they must differ
        ext_keys = [v.get("ext_cache_key") for v in our_entries.values()]
        for ek in ext_keys:
            self.assertTrue(ek, "Each FMI cache entry must have a non-empty ext_cache_key")
        self.assertNotEqual(ext_keys[0], ext_keys[1],
            "ext_cache_key values for different FMI paths must differ")

        # 5. Verify each path still returns its own cached token
        cached_a = app.acquire_token_for_client(scopes, fmi_path=path_a)
        self.assertEqual("cache", cached_a.get("token_source"))
        self.assertEqual(token_a, cached_a["access_token"],
            "Path A should return its own cached token")

        cached_b = app.acquire_token_for_client(scopes, fmi_path=path_b)
        self.assertEqual("cache", cached_b.get("token_source"))
        self.assertEqual(token_b, cached_b["access_token"],
            "Path B should return its own cached token")


if __name__ == "__main__":
    unittest.main()
