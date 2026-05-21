"""End-to-end tests for agentic (agent identity) scenarios.

These tests verify the full agent identity flow using MSAL Python APIs:
1. Assertion callback context propagation (fmi_path flows to callback)
2. Agent app-only token acquisition using FMI-sourced client assertion (Leg 2)
3. Full 3-leg flow: FMI → assertion → user_fic → user-scoped token
4. Cache isolation between app-only and user-scoped tokens

Test configuration uses the same lab infrastructure as test_fmi_e2e.py.
Requires LAB_APP_CLIENT_CERT_PFX_PATH environment variable.
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

# =============================================================================
# Test configuration — shared lab app registrations for agentic scenarios
# =============================================================================
_TENANT_ID = "10c419d4-4a50-45b2-aa4e-919fb84df24f"
_BLUEPRINT_CLIENT_ID = "aab5089d-e764-47e3-9f28-cc11c2513821"
_AGENT_APP_ID = "ab18ca07-d139-4840-8b3b-4be9610c6ed5"
_RMA_CLIENT_ID = "3bf56293-fbb5-42bd-a407-248ba7431a8c"
_USER_UPN = "agentuser1@id4slab1.onmicrosoft.com"
_TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
_FMI_EXCHANGE_SCOPE = "api://AzureFMITokenExchange/.default"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_FMI_PATH = "SomeFmiPath/FmiCredentialPath"
_AUTHORITY = "https://login.microsoftonline.com/" + _TENANT_ID


# =============================================================================
# Helpers
# =============================================================================

def _acquire_fmi_credential_for_agent(agent_app_id):
    """Leg 1: Blueprint app acquires FMI credential (T1) for the given agent.

    Uses certificate authentication with SNI (sendX5C) and fmi_path set to
    the agent app ID.
    """
    blueprint_app = msal.ConfidentialClientApplication(
        _BLUEPRINT_CLIENT_ID,
        client_credential=get_client_certificate(),
        authority=_AUTHORITY,
        http_client=MinimalHttpClient(),
    )
    result = blueprint_app.acquire_token_for_client(
        [_TOKEN_EXCHANGE_SCOPE], fmi_path=agent_app_id)
    if "access_token" not in result:
        raise RuntimeError(
            "Leg 1 failed — could not acquire FMI credential: {}: {}".format(
                result.get("error"), result.get("error_description")))
    return result["access_token"]


def _acquire_fmi_credential_from_rma():
    """Acquire an FMI credential from the RMA app using certificate credentials.

    Uses the same RMA pattern as test_fmi_e2e._get_fmi_credential_from_rma().
    Used for assertion callback context tests where the callback just needs to
    return a valid FMI token (not specifically for an agent app).
    """
    rma_app = msal.ConfidentialClientApplication(
        _RMA_CLIENT_ID,
        client_credential=get_client_certificate(),
        authority=_AUTHORITY,
        http_client=MinimalHttpClient(),
    )
    result = rma_app.acquire_token_for_client(
        [_FMI_EXCHANGE_SCOPE], fmi_path=_FMI_PATH)
    if "access_token" not in result:
        raise RuntimeError(
            "RMA FMI credential acquisition failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
    return result["access_token"]


def _acquire_instance_token_for_agent():
    """Leg 1 + Leg 2: Acquire an instance token (T2) for the agent app.

    1. Blueprint → T1 (FMI credential via fmi_path)
    2. Agent uses T1 as client_assertion → T2 (instance token)

    T2 is used as user_federated_identity_credential in Leg 3 (user_fic).
    """
    t1 = _acquire_fmi_credential_for_agent(_AGENT_APP_ID)

    agent_app = msal.ConfidentialClientApplication(
        _AGENT_APP_ID,
        client_credential={"client_assertion": t1},
        authority=_AUTHORITY,
        http_client=MinimalHttpClient(),
    )
    result = agent_app.acquire_token_for_client([_TOKEN_EXCHANGE_SCOPE])
    if "access_token" not in result:
        raise RuntimeError(
            "Leg 2 failed — could not acquire instance token: {}: {}".format(
                result.get("error"), result.get("error_description")))
    return result["access_token"]


# =============================================================================
# Tests
# =============================================================================

class TestAssertionCallbackContext(LabBasedTestCase):
    """Verify assertion callback receives correct context when fmi_path is set."""

    def test_assertion_callback_receives_fmi_path(self):
        captured_context = {}

        def assertion_callback(context):
            captured_context.update(context)
            return _acquire_fmi_credential_from_rma()

        app = msal.ConfidentialClientApplication(
            "urn:microsoft:identity:fmi",
            client_credential={"client_assertion": assertion_callback},
            authority=_AUTHORITY,
            http_client=MinimalHttpClient(),
        )

        result = app.acquire_token_for_client(
            [_FMI_EXCHANGE_SCOPE], fmi_path=_AGENT_APP_ID)
        self.assertIn("access_token", result,
            "acquire_token_for_client failed: {}: {}".format(
                result.get("error"), result.get("error_description")))

        # Verify context was passed to callback
        self.assertEqual(_AGENT_APP_ID, captured_context.get("fmi_path"),
            "fmi_path should flow to assertion callback context")
        self.assertEqual("urn:microsoft:identity:fmi", captured_context.get("client_id"),
            "client_id should be in assertion callback context")
        self.assertTrue(captured_context.get("token_endpoint"),
            "token_endpoint should be in assertion callback context")


class TestAgentAppToken(LabBasedTestCase):
    """Agent acquires app-only token for Graph using FMI-sourced assertion.

    Flow: Blueprint → T1 (assertion callback) → Agent CCA → app token

    Disabled in CI: The blueprint app (aab5089d) requires SNI authentication,
    but the CI pipeline's PFX-based cert loading does not include intermediate
    certs in the x5c chain, causing AADSTS700027. These tests pass locally
    where the OS cert store can resolve the chain.
    """

    @unittest.skipUnless(
        os.environ.get("MSAL_RUN_LOCAL_ONLY_TESTS"),
        "Requires local cert store for SNI — set MSAL_RUN_LOCAL_ONLY_TESTS=1")
    def test_agent_gets_app_token_for_graph(self):
        def assertion_provider(context):
            return _acquire_fmi_credential_for_agent(_AGENT_APP_ID)

        agent_app = msal.ConfidentialClientApplication(
            _AGENT_APP_ID,
            client_credential={"client_assertion": assertion_provider},
            authority=_AUTHORITY,
            http_client=MinimalHttpClient(),
        )

        result = agent_app.acquire_token_for_client([_GRAPH_SCOPE])
        self.assertIn("access_token", result,
            "Agent app token acquisition failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
        self.assertTrue(result["access_token"],
            "Access token should not be empty")


class TestAgentUserIdentity(LabBasedTestCase):
    """Full 3-leg agent identity flow: FMI → assertion → user_fic → user token.

    Flow:
    1. Blueprint → T1 (FMI credential)
    2. Agent uses T1 → T2 (instance token)
    3. Agent exchanges T2 via user_fic → user-scoped Graph token
    4. Verify token is cached and retrievable via acquire_token_silent

    Disabled in CI: see TestAgentAppToken docstring.
    """

    @unittest.skipUnless(
        os.environ.get("MSAL_RUN_LOCAL_ONLY_TESTS"),
        "Requires local cert store for SNI — set MSAL_RUN_LOCAL_ONLY_TESTS=1")
    def test_agent_user_identity_gets_token_for_graph(self):
        # Get instance token (T2) for user_fic exchange
        t2 = _acquire_instance_token_for_agent()

        # Build agent CCA with assertion callback
        def assertion_provider(context):
            return _acquire_fmi_credential_for_agent(_AGENT_APP_ID)

        agent_app = msal.ConfidentialClientApplication(
            _AGENT_APP_ID,
            client_credential={"client_assertion": assertion_provider},
            authority=_AUTHORITY,
            http_client=MinimalHttpClient(),
        )

        # Exchange T2 for user-scoped token via user_fic grant
        result = agent_app.acquire_token_by_user_federated_identity_credential(
            [_GRAPH_SCOPE], assertion=t2, username=_USER_UPN)
        self.assertIn("access_token", result,
            "user_fic token acquisition failed: {}: {}".format(
                result.get("error"), result.get("error_description")))
        self.assertTrue(result["access_token"],
            "Access token should not be empty")

        # Verify account was created
        accounts = agent_app.get_accounts()
        self.assertTrue(len(accounts) > 0,
            "Account should be created from user_fic response")

        # Verify silent retrieval works (token should be cached)
        account = accounts[0]
        silent_result = agent_app.acquire_token_silent(
            [_GRAPH_SCOPE], account=account)
        self.assertIsNotNone(silent_result,
            "acquire_token_silent should return cached token")
        self.assertIn("access_token", silent_result)
        self.assertEqual(result["access_token"], silent_result["access_token"],
            "Silent call should return the same cached token")


class TestAgentCacheIsolation(LabBasedTestCase):
    """App-only and user-scoped tokens are isolated in cache on the same CCA.

    Disabled in CI: see TestAgentAppToken docstring.
    """

    @unittest.skipUnless(
        os.environ.get("MSAL_RUN_LOCAL_ONLY_TESTS"),
        "Requires local cert store for SNI — set MSAL_RUN_LOCAL_ONLY_TESTS=1")
    def test_app_and_user_tokens_are_isolated(self):
        def assertion_provider(context):
            return _acquire_fmi_credential_for_agent(_AGENT_APP_ID)

        agent_app = msal.ConfidentialClientApplication(
            _AGENT_APP_ID,
            client_credential={"client_assertion": assertion_provider},
            authority=_AUTHORITY,
            http_client=MinimalHttpClient(),
        )

        # Acquire app-only token
        app_result = agent_app.acquire_token_for_client([_GRAPH_SCOPE])
        self.assertIn("access_token", app_result,
            "App token acquisition failed: {}: {}".format(
                app_result.get("error"), app_result.get("error_description")))

        # Acquire user token via user_fic
        t2 = _acquire_instance_token_for_agent()
        user_result = agent_app.acquire_token_by_user_federated_identity_credential(
            [_GRAPH_SCOPE], assertion=t2, username=_USER_UPN)
        self.assertIn("access_token", user_result,
            "User token acquisition failed: {}: {}".format(
                user_result.get("error"), user_result.get("error_description")))

        # Tokens should be different (app-scoped vs user-scoped)
        self.assertNotEqual(app_result["access_token"], user_result["access_token"],
            "App token and user token should be different")

        # Verify both are independently retrievable
        # App token: second call should return from cache
        app_cached = agent_app.acquire_token_for_client([_GRAPH_SCOPE])
        self.assertEqual("cache", app_cached.get("token_source"),
            "App token should be returned from cache on second call")
        self.assertEqual(app_result["access_token"], app_cached["access_token"])

        # User token: silent call should return from cache
        accounts = agent_app.get_accounts()
        self.assertTrue(len(accounts) > 0)
        user_cached = agent_app.acquire_token_silent(
            [_GRAPH_SCOPE], account=accounts[0])
        self.assertIsNotNone(user_cached)
        self.assertEqual(user_result["access_token"], user_cached["access_token"])


if __name__ == "__main__":
    unittest.main()
