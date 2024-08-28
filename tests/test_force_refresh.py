from tests import unittest
import msal
import logging
import sys

if not sys.platform.startswith("win"):
    raise unittest.SkipTest("Currently, our broker supports Windows")

SCOPE_ARM = "https://management.azure.com/.default"
_AZURE_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
pca = msal.PublicClientApplication(
    _AZURE_CLI,
    authority="https://login.microsoftonline.com/organizations",
    enable_broker_on_mac=True,
    enable_broker_on_windows=True) 

class ForceRefreshTestCase(unittest.TestCase):
    def test_silent_with_force_refresh(self):
        print("Testing silent flow with force_refresh=True")
        result = pca.acquire_token_interactive(scopes=[SCOPE_ARM], prompt="select_account", parent_window_handle=pca.CONSOLE_WINDOW_HANDLE, enable_msa_passthrough=True)
        accounts = pca.get_accounts()
        account = accounts[0]
        assert account, "The logged in account should have been established by interactive flow"
        oldToken = result.get("access_token")
        
        
        result = pca.acquire_token_silent(
            scopes=[SCOPE_ARM],
            account=account,
            force_refresh=False) 

        # This token should have been recieved from cache
        assert result.get("access_token") == oldToken, "Token should not be refreshed" 


        result = pca.acquire_token_silent(
            scopes=[SCOPE_ARM],
            account=account,
            force_refresh=True)
        
        # Token will be different proving it is not token from cache and was renewed
        assert result.get("access_token") != oldToken, "Token should be refreshed"