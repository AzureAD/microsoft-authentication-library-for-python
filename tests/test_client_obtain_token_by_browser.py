"""Integration tests for form_post response mode in authorization code flow"""
import json
import unittest
try:
    from urllib.parse import urlparse, parse_qs
except ImportError:
    from urlparse import urlparse, parse_qs

from msal.oauth2cli import Client
from msal.oauth2cli.authcode import AuthCodeReceiver
import requests


class _BrowserlessAuthCodeReceiver(AuthCodeReceiver):
    """Subclass that bypass browser opening behavior for testing.

    :param scheduled_action:
        Optional callable with signature: (*, state: str, port: int) -> Callable[[], Any]
        It receives state and port as keyword arguments, and should return a callable
        that will be executed as the scheduled action.

        This allows the test cases to define mocked http requests with the correct state and port.
    """
    def __init__(self, *args, scheduled_action=None, **kwargs):
        super(_BrowserlessAuthCodeReceiver, self).__init__(*args, **kwargs)
        self.scheduled_action = scheduled_action

    def get_auth_response(self, **kwargs):
        """Override to strip auth_uri, preventing browser launch, and optionally inject scheduled actions."""
        kwargs.pop('auth_uri', None)  # Remove auth_uri to skip browser behavior
        kwargs.pop('auth_uri_callback', None)  # Also remove callback

        if self.scheduled_action:
            state = kwargs.get('state')
            port = self.get_port()
            self._scheduled_actions = [(
                1,
                self.scheduled_action(state=state, port=port)
            )]

        return super(_BrowserlessAuthCodeReceiver, self).get_auth_response(**kwargs)


class TestResponseModeIntegration(unittest.TestCase):
    """Integration test for response_mode with end-to-end authentication flow"""
    fake_access_token = "fake_token_xyz"

    def setUp(self):
        # Mock http_client that returns fake token
        class MockResponse:
            def __init__(self):
                self.status_code = 200
                self.text = json.dumps({
                    "access_token": TestResponseModeIntegration.fake_access_token,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                })

        class MockHttpClient:
            def post(self, url, **kwargs):
                return MockResponse()

        self.client = Client(
            {
                "authorization_endpoint": "https://example.com/authorize",
                "token_endpoint": "https://example.com/token",
            },
            "test_client_id",
            http_client=MockHttpClient(),
        )

    def test_initiate_auth_code_flow_with_non_form_post_response_mode_should_warn(self):
        """Test that initiating auth code flow warns for non-form_post response modes"""
        for mode in ['query', 'fragment', None]:
            with self.assertWarns(UserWarning) as cm:
                flow = self.client.initiate_auth_code_flow(
                    scope=["openid", "profile"],
                    redirect_uri="http://localhost:8080",
                    response_mode=mode,
                )
            self.assertIn(
                "form_post", str(cm.warning).lower(), "Warning should mention form_post requirement")
            if mode is not None:
                # Verify response_mode in the auth_uri (if it was explicitly set)
                params = parse_qs(urlparse(flow["auth_uri"]).query)
                self.assertIn(
                    "response_mode", params, "response_mode should be in auth_uri when explicitly set")
                self.assertEqual(
                    params.get("response_mode", [None])[0], mode,
                    f"response_mode should be set as requested")

    def test_http_post_should_work_with_obtain_token_by_browser(self):
        def action_builder(*, state, port):
            """Build an action that sends the auth response via HTTP POST"""
            return lambda: requests.post(
                "http://localhost:{}".format(port),
                data={"code": "auth_code_from_server", "state": state},
            )

        with _BrowserlessAuthCodeReceiver(scheduled_action=action_builder) as receiver:
            try:
                # This will use form_post internally (as required by obtain_token_by_browser)
                result = self.client.obtain_token_by_browser(
                    redirect_uri="http://localhost:{}".format(receiver.get_port()),
                    auth_code_receiver=receiver,
                    scope=["openid", "profile"],
                    timeout=3,
                )
                self.assertIsNotNone(result, "obtain_token_by_browser should return result")
                self.assertIn("access_token", result, "Result should contain access_token")
                self.assertEqual(result["access_token"], self.fake_access_token)
            except Exception as e:
                self.fail(f"obtain_token_by_browser failed: {e}")


if __name__ == '__main__':
    unittest.main()
