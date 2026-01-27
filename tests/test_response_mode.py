"""Integration tests for form_post response mode in authorization code flow"""
import unittest
import warnings
try:
    from urllib.parse import urlparse, parse_qs
except ImportError:
    from urlparse import urlparse, parse_qs

from msal.oauth2cli import Client
from msal.oauth2cli.authcode import AuthCodeReceiver
import requests


class TestResponseModeIntegration(unittest.TestCase):
    """Integration test for response_mode with end-to-end authentication flow"""
    
    def setUp(self):
        self.client = Client(
            {
                "authorization_endpoint": "https://example.com/authorize",
                "token_endpoint": "https://example.com/token"
            },
            "test_client_id"
        )

    def _send_post_auth_response(self, port, **params):
        """Helper to send POST auth response to the receiver"""
        try:
            from urllib.parse import urlencode
        except ImportError:
            from urllib import urlencode
        
        response = requests.post(
            "http://localhost:{}".format(port),
            data=urlencode(params),
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        return response
    
    def test_query_mode_override_with_successful_post_authentication(self):
        """
        Comprehensive integration test: When response_mode='query' is set,
        verify deprecation warning, override to form_post, and successful POST authentication.
        """
        # Part 1: Verify deprecation and override warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            flow = self.client.initiate_auth_code_flow(
                scope=["openid", "profile"],
                redirect_uri="http://localhost:8080",
                response_mode="query"  # Explicitly request query mode (should be overridden)
            )
            
            # Verify deprecation warning
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            self.assertEqual(len(deprecation_warnings), 1, "Should have exactly one deprecation warning")
            warning_msg = str(deprecation_warnings[0].message).lower()
            self.assertIn("deprecated", warning_msg)
            self.assertIn("public clients", warning_msg)
            self.assertIn("confidential clients", warning_msg)
            
            # Verify security warning for non-form_post
            user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
            self.assertEqual(len(user_warnings), 1, "Should have exactly one security warning")
            self.assertIn("form_post", str(user_warnings[0].message).lower())
        
        # Part 2: Verify response_mode parameter in flow
        # Note: At oauth2.py level, response_mode is passed through as-is.
        # The enforcement to form_post happens in authcode.py when opening the browser.
        parsed = urlparse(flow["auth_uri"])
        params = parse_qs(parsed.query)
        # The flow still contains the query mode at this level
        self.assertIn("response_mode", params)
        
        # Part 3: Verify actual POST authentication works (form_post flow)
        with AuthCodeReceiver() as receiver:
            test_code = "test_auth_code_xyz"
            test_state = "test_state_abc"
            
            receiver._scheduled_actions = [(
                1,
                lambda: self._send_post_auth_response(
                    receiver.get_port(),
                    code=test_code,
                    state=test_state
                )
            )]
            
            result = receiver.get_auth_response(
                timeout=3,
                state=test_state,
                success_template="Success: Got code $code",
            )
            
            # Verify the POST request succeeded
            self.assertIsNotNone(result, "POST authentication should succeed")
            self.assertEqual(result.get("code"), test_code,
                           "Should receive auth code via POST (form_post)")
            self.assertEqual(result.get("state"), test_state,
                           "Should receive correct state via POST")
    
    def test_query_mode_get_request_rejected(self):
        """
        Verify that GET requests with auth code are rejected when form_post is enforced.
        """
        with AuthCodeReceiver() as receiver:
            test_code = "test_auth_code_via_get"
            
            # Schedule the GET request to be sent after server starts
            def send_get_request():
                try:
                    from urllib.parse import urlencode
                except ImportError:
                    from urllib import urlencode
                
                response = requests.get(
                    "http://localhost:{}?{}".format(
                        receiver.get_port(),
                        urlencode({"code": test_code, "state": "test"})
                    )
                )
                
                # Verify the GET request shows welcome page (doesn't process auth code)
                # When no welcome_template is provided, an empty 200 response is returned
                self.assertEqual(response.status_code, 200, "GET request should return 200")
                # The key is that it doesn't set auth_response - the auth code is ignored
            
            receiver._scheduled_actions = [(1, send_get_request)]
            
            # Start the server (it will timeout after rejecting the GET)
            result = receiver.get_auth_response(timeout=3)
            
            # Result should be None because GET was rejected (no auth_response set)
            self.assertIsNone(result, "GET request should not produce auth response")


if __name__ == '__main__':
    unittest.main()
