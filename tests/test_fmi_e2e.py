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
    result = app.acquire_token_for_client_with_fmi_path(
        [_FMI_SCOPE_FOR_RMA], _FMI_PATH)
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
        result = app.acquire_token_for_client_with_fmi_path(scopes, _FMI_PATH)
        self.assertIn("access_token", result,
            "acquire_token_for_client_with_fmi_path() failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
        self.assertNotEqual("", result["access_token"],
            "acquire_token_for_client_with_fmi_path() returned empty access token")

        first_token = result["access_token"]

        # 2. Verify silent token acquisition works (should retrieve from cache)
        cache_result = app.acquire_token_for_client_with_fmi_path(scopes, _FMI_PATH)
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
        result = app.acquire_token_for_client_with_fmi_path(scopes, fmi_path)
        self.assertIn("access_token", result,
            "acquire_token_for_client_with_fmi_path() failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
        self.assertNotEqual("", result["access_token"],
            "acquire_token_for_client_with_fmi_path() returned empty access token")
        first_token = result["access_token"]
        
        # 2. Verify cached token acquisition works
        cache_result = app.acquire_token_for_client_with_fmi_path(scopes, fmi_path)
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


if __name__ == "__main__":
    unittest.main()
