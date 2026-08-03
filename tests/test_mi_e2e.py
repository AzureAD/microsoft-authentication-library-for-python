"""End-to-end Managed Identity tests (real token acquisition).

These tests perform REAL token acquisition and therefore only run on the
self-hosted Azure DevOps pools that are actual Azure VM / Azure Arc machines with
the lab managed identities assigned:

  * IMDS tests -> the "MISEManagedIdentity" pool (an Azure VM). Gated on the
                  MSAL_TEST_MI_IMDS environment variable, which that pipeline
                  stage sets. (DEFAULT_TO_VM is also the fallback source on hosted
                  agents, so an explicit flag is used instead of source detection.)
  * Azure Arc  -> the "MISEAZUREARC" pool (an Azure Arc-enabled machine). Gated on
                  the Azure Arc source being detected on the machine.

They mirror the MSAL Go E2E tests
(apps/tests/e2e/managedidentity_e2e_test.go and managedidentity_arc_e2e_test.go)
and use the SAME lab identities and ARM resource, so both SDKs exercise the same
lab configuration on the same machines.

Everywhere else (hosted agents, local dev) the tests self-skip.
"""
import hashlib
import os
import unittest

import requests

from msal import (
    ManagedIdentityClient,
    SystemAssignedManagedIdentity,
    UserAssignedManagedIdentity,
)
from msal.managed_identity import get_managed_identity_source, AZURE_ARC


# Azure Resource Manager resource. Matches the ARM scope used by the MSAL .NET and
# Go managed identity E2E tests.
_ARM_RESOURCE = "https://management.azure.com"

# User-assigned managed identities assigned to the MISEManagedIdentity VM. These are
# the SAME values used by the MSAL Go / .NET IMDS E2E tests, so all SDKs exercise the
# same lab configuration on the same VM.
_UAMI_CLIENT_ID = "6325cd32-9911-41f3-819c-416cdf9104e7"
_UAMI_OBJECT_ID = "ecb2ad92-3e30-4505-b79f-ac640d069f24"
_UAMI_RESOURCE_ID = (
    "/subscriptions/c1686c51-b717-4fe0-9af3-24a20a41fb0c/resourcegroups/"
    "MSIV2-Testing-MSALNET/providers/Microsoft.ManagedIdentity/userAssignedIdentities/msiv2uami"
)


def _safe_error(result):
    """Return a log-safe summary of a failed result.

    Only the non-sensitive error fields are surfaced, so an assertion failure can
    never spill an access token (or the whole result dict) into the CI logs.
    """
    return {
        key: result[key]
        for key in ("error", "error_description", "correlation_id")
        if key in result
    }


def _acquire_token_twice_assert_caching(test, managed_identity):
    """Acquire an ARM token twice for the given managed identity and assert the first
    call reaches the identity provider while the second is served from the token cache.

    Shared by the IMDS and Azure Arc E2E tests, mirroring the Go helper of the same name.
    """
    http_client = requests.Session()
    client = ManagedIdentityClient(managed_identity, http_client=http_client)
    try:
        first = client.acquire_token_for_client(resource=_ARM_RESOURCE)
        test.assertNotIn(
            "error", first, "first acquisition failed: {}".format(_safe_error(first)))
        test.assertIn("access_token", first)
        test.assertEqual(
            "identity_provider", first.get("token_source"),
            "first call should reach the identity provider")

        second = client.acquire_token_for_client(resource=_ARM_RESOURCE)
        test.assertNotIn(
            "error", second, "second acquisition failed: {}".format(_safe_error(second)))
        test.assertIn("access_token", second)
        test.assertEqual(
            "cache", second.get("token_source"),
            "second call should be served from the token cache")
        # Compare tokens by SHA-256 digest so a mismatch never prints the actual
        # token material into CI logs.
        test.assertEqual(
            hashlib.sha256(first["access_token"].encode("utf-8")).hexdigest(),
            hashlib.sha256(second["access_token"].encode("utf-8")).hexdigest(),
            "cached token should match the original token")
    finally:
        http_client.close()


@unittest.skipUnless(
    os.getenv("MSAL_TEST_MI_IMDS"),
    "Set MSAL_TEST_MI_IMDS to run on the MISEManagedIdentity Azure VM (IMDS) pool")
class ManagedIdentityImdsE2ETestCase(unittest.TestCase):
    """Acquires ARM tokens over IMDS v1 for the system-assigned identity and each
    user-assigned identity binding (client id / resource id / object id). Each test
    asserts the first call reaches the identity provider and the second is cached."""

    def test_system_assigned(self):
        _acquire_token_twice_assert_caching(self, SystemAssignedManagedIdentity())

    def test_user_assigned_client_id(self):
        _acquire_token_twice_assert_caching(
            self, UserAssignedManagedIdentity(client_id=_UAMI_CLIENT_ID))

    def test_user_assigned_resource_id(self):
        _acquire_token_twice_assert_caching(
            self, UserAssignedManagedIdentity(resource_id=_UAMI_RESOURCE_ID))

    def test_user_assigned_object_id(self):
        _acquire_token_twice_assert_caching(
            self, UserAssignedManagedIdentity(object_id=_UAMI_OBJECT_ID))


@unittest.skipUnless(
    get_managed_identity_source() == AZURE_ARC,
    "Runs only on an Azure Arc-enabled machine (the MISEAZUREARC pool)")
class ManagedIdentityAzureArcE2ETestCase(unittest.TestCase):
    """Azure Arc supports the system-assigned identity only, so unlike the IMDS tests
    there are no user-assigned variants."""

    def test_system_assigned(self):
        _acquire_token_twice_assert_caching(self, SystemAssignedManagedIdentity())


if __name__ == "__main__":
    unittest.main()
