from tests import unittest
import msal
import logging
import sys

# from tests.test_e2e import LabBasedTestCase

if not sys.platform.startswith("win"):
    raise unittest.SkipTest("Currently, our broker supports Windows")

SCOPE_ARM = "https://management.azure.com/.default"
_AZURE_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
pca = msal.PublicClientApplication(
    _AZURE_CLI,
    authority="https://login.microsoftonline.com/organizations",
    enable_broker_on_mac=True,
    enable_broker_on_windows=True)


# class ForceRefreshTestCase(LabBasedTestCase):
#     def test_silent_with_force_refresh(self):
#         # acquire token using username and password
#         print("Testing silent flow with force_refresh=True")
#         config = self.get_lab_user(usertype="cloud")
#         config["password"] = self.get_lab_user_secret(config["lab_name"])
#         result = pca.acquire_token_by_username_password(username=config["lab_name"], password=config["password"], scopes=config["scope"])
#         # assert username and password, "You need to provide a test account and its password"
        
#         ropcToken = result.get("access_token")
#         accounts = pca.get_accounts()
#         account = accounts[0]
#         assert account, "The logged in account should have been established by interactive flow"
        
#         result = pca.acquire_token_silent(
#             config["scope"],
#             account=account,
#             force_refresh=False,
#               auth_scheme=None, data=None) 

#         assert result.get("access_token") == ropcToken, "Token should not be refreshed" 


class ForceRefreshTestCase(unittest.TestCase):
    def test_silent_with_force_refresh(self):
        # acquire token using username and password
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

        # This token should be recieved from cache
        assert result.get("access_token") == oldToken, "Token should not be refreshed" 


        result = pca.acquire_token_silent(
            scopes=[SCOPE_ARM],
            account=account,
            force_refresh=True)
        
        # Token will be different proving it is not from cache and was renewed
        assert result.get("access_token") != oldToken, "Token should be refreshed"