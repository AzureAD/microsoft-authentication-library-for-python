from tests import unittest
import msal
import sys


if sys.platform not in ("win32", "darwin"):
    raise unittest.SkipTest(f"Our broker does not support {sys.platform}")

SCOPES = ["https://management.azure.com/.default"]
_AZURE_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
pca = msal.PublicClientApplication(
    _AZURE_CLI,
    authority="https://login.microsoftonline.com/organizations",
    enable_broker_on_mac=True,
    enable_broker_on_windows=True,
    )


class ForceRefreshTestCase(unittest.TestCase):
    def test_silent_with_force_refresh_should_return_a_new_token(self):
        result = pca.acquire_token_interactive(
            scopes=SCOPES,
            prompt="select_account",
            parent_window_handle=pca.CONSOLE_WINDOW_HANDLE,
            enable_msa_passthrough=True,
            )
        accounts = pca.get_accounts()
        self.assertNotEqual(
            [], accounts,
            "Interactive flow should have established a logged-in account")
        account = accounts[0]
        old_token = result.get("access_token")

        result = pca.acquire_token_silent(SCOPES, account)
        assertion = "This token should have been received from cache"
        self.assertEqual(result.get("access_token"), old_token, assertion)
        self.assertEqual(result.get("token_source"), "cache", assertion)

        result = pca.acquire_token_silent(SCOPES, account, force_refresh=True)
        assertion = "A new token should have been received from broker"
        self.assertNotEqual(result.get("access_token"), old_token, assertion)
        self.assertEqual(result.get("token_source"), "broker", assertion)

