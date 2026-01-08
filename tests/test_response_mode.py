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
            self.assertIn("deprecated", str(deprecation_warnings[0].message).lower())
            
            # Verify override warning
            user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
            self.assertEqual(len(user_warnings), 1, "Should have exactly one override warning")
            self.assertIn("overridden", str(user_warnings[0].message).lower())
        
        # Part 2: Verify form_post is enforced in auth_uri
        parsed = urlparse(flow["auth_uri"])
        params = parse_qs(parsed.query)
        self.assertIn("response_mode", params)
        self.assertEqual(params["response_mode"][0], "form_post",
                        "response_mode should be overridden to form_post")
        
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
            
            # Try to send auth code via GET (query string)
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
            
            # Verify the GET request is rejected
            self.assertEqual(response.status_code, 400, "GET with auth code should be rejected")
            self.assertIn("not supported", response.text.lower(),
                         "Error message should indicate method not supported")


if __name__ == '__main__':
    unittest.main()
