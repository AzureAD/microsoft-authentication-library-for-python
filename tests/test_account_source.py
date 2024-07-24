import json
try:
    from unittest.mock import patch
except:
    from mock import patch
try:
    import pymsalruntime
    broker_available = True
except ImportError:
    broker_available = False
import msal
from tests import unittest
from tests.test_token_cache import build_response
from tests.http_client import MinimalResponse


SCOPE = "scope_foo"
TOKEN_RESPONSE = build_response(
    access_token="at",
    uid="uid", utid="utid",  # So that it will create an account
    scope=SCOPE, refresh_token="rt",  # So that non-broker's acquire_token_silent() would work
)

def _mock_post(url, headers=None, *args, **kwargs):
    return MinimalResponse(status_code=200, text=json.dumps(TOKEN_RESPONSE))

@unittest.skipUnless(broker_available, "These test cases need pip install msal[broker]")
@patch("msal.broker._acquire_token_silently", return_value=dict(
    TOKEN_RESPONSE, _account_id="placeholder"))
@patch.object(msal.authority, "tenant_discovery", return_value={
    "authorization_endpoint": "https://contoso.com/placeholder",
    "token_endpoint": "https://contoso.com/placeholder",
})  # Otherwise it would fail on OIDC discovery
class TestAccountSourceBehavior(unittest.TestCase):

    def test_device_flow_and_its_silent_call_should_bypass_broker(self, _, mocked_broker_ats):
        app = msal.PublicClientApplication("client_id", enable_broker_on_windows=True)
        result = app.acquire_token_by_device_flow({"device_code": "123"}, post=_mock_post)
        self.assertEqual(result["token_source"], "identity_provider")

        account = app.get_accounts()[0]
        self.assertEqual(account["account_source"], "urn:ietf:params:oauth:grant-type:device_code")

        result = app.acquire_token_silent_with_error(
            [SCOPE], account, force_refresh=True, post=_mock_post)
        mocked_broker_ats.assert_not_called()
        self.assertEqual(result["token_source"], "identity_provider")

    def test_ropc_flow_and_its_silent_call_should_invoke_broker(self, _, mocked_broker_ats):
        app = msal.PublicClientApplication("client_id", enable_broker_on_windows=True)
        with patch("msal.broker._signin_silently", return_value=dict(TOKEN_RESPONSE, _account_id="placeholder")):
            result = app.acquire_token_by_username_password(
                "username", "placeholder", [SCOPE], post=_mock_post)
        self.assertEqual(result["token_source"], "broker")

        account = app.get_accounts()[0]
        self.assertEqual(account["account_source"], "broker")

        result = app.acquire_token_silent_with_error(
            [SCOPE], account, force_refresh=True, post=_mock_post)
        self.assertEqual(result["token_source"], "broker")

    def test_interactive_flow_and_its_silent_call_should_invoke_broker(self, _, mocked_broker_ats):
        app = msal.PublicClientApplication("client_id", enable_broker_on_windows=True)
        with patch.object(app, "_acquire_token_interactive_via_broker", return_value=dict(
                TOKEN_RESPONSE, _account_id="placeholder")):
            result = app.acquire_token_interactive(
                [SCOPE], parent_window_handle=app.CONSOLE_WINDOW_HANDLE)
        self.assertEqual(result["token_source"], "broker")

        account = app.get_accounts()[0]
        self.assertEqual(account["account_source"], "broker")

        result = app.acquire_token_silent_with_error(
            [SCOPE], account, force_refresh=True, post=_mock_post)
        mocked_broker_ats.assert_called_once()
        self.assertEqual(result["token_source"], "broker")

