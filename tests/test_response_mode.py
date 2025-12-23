"""Tests for form_post response mode in authorization code flow"""
import unittest
import warnings
try:
    from urllib.parse import urlparse, parse_qs
except ImportError:
    from urlparse import urlparse, parse_qs

from msal.oauth2cli import Client


class TestResponseMode(unittest.TestCase):
    """Test response_mode parameter in authorization code flow"""
    
    def setUp(self):
        self.client = Client(
            {
                "authorization_endpoint": "https://example.com/authorize",
                "token_endpoint": "https://example.com/token"
            },
            "test_client_id"
        )
    
    def test_default_response_mode_is_form_post(self):
        """Test that response_mode defaults to form_post for security"""
        flow = self.client.initiate_auth_code_flow(
            scope=["openid", "profile"],
            redirect_uri="http://localhost:8080"
        )
        
        # Parse the auth_uri to check query parameters
        parsed = urlparse(flow["auth_uri"])
        params = parse_qs(parsed.query)
        
        # Verify response_mode is set to form_post
        self.assertIn("response_mode", params)
        self.assertEqual(params["response_mode"][0], "form_post")
    
    def test_explicit_query_mode_shows_security_warning(self):
        """Test that attempting to use query mode raises a security warning"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            flow = self.client.initiate_auth_code_flow(
                scope=["openid", "profile"],
                redirect_uri="http://localhost:8080",
                response_mode="query"
            )
            
            # Verify a warning was raised
            self.assertEqual(len(w), 1)
            self.assertTrue(issubclass(w[0].category, Warning))
            self.assertIn("security", str(w[0].message).lower())
        
        # Verify form_post is still enforced despite explicit query request
        parsed = urlparse(flow["auth_uri"])
        params = parse_qs(parsed.query)
        self.assertEqual(params["response_mode"][0], "form_post")
    
    def test_explicit_fragment_mode_shows_security_warning(self):
        """Test that attempting to use fragment mode raises a security warning"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            flow = self.client.initiate_auth_code_flow(
                scope=["openid", "profile"],
                redirect_uri="http://localhost:8080",
                response_mode="fragment"
            )
            
            # Verify a warning was raised
            self.assertEqual(len(w), 1)
            self.assertIn("fragment", str(w[0].message))
        
        # Verify form_post is still enforced
        parsed = urlparse(flow["auth_uri"])
        params = parse_qs(parsed.query)
        self.assertEqual(params["response_mode"][0], "form_post")
    
    def test_build_auth_request_params_enforces_form_post(self):
        """Test that _build_auth_request_params enforces form_post"""
        params = self.client._build_auth_request_params(
            response_type="code",
            redirect_uri="http://localhost:8080",
            scope=["openid", "profile"],
            state="test_state"
        )
        
        # Verify response_mode is form_post
        self.assertIn("response_mode", params)
        self.assertEqual(params["response_mode"], "form_post")
    
    def test_build_auth_request_params_ignores_explicit_query_mode(self):
        """Test that _build_auth_request_params ignores explicit query mode"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            params = self.client._build_auth_request_params(
                response_type="code",
                redirect_uri="http://localhost:8080",
                scope=["openid", "profile"],
                state="test_state",
                response_mode="query"
            )
            
            # Verify warning was raised
            self.assertGreater(len(w), 0)
        
        # Verify form_post is enforced despite explicit request
        self.assertIn("response_mode", params)
        self.assertEqual(params["response_mode"], "form_post")


if __name__ == '__main__':
    unittest.main()
