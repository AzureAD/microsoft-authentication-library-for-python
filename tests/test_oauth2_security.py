import unittest
from msal.oauth2cli import oauth2
import string

class TestTokenGenerationSecurity(unittest.TestCase):
    """Verify secure token generation using secrets module."""
    
    def test_pkce_verifier_format(self):
        """Verify PKCE verifier is valid and uses correct alphabet."""
        pkce = oauth2._generate_pkce_code_verifier(length=43)
        verifier = pkce["code_verifier"]
        
        # Check length
        self.assertEqual(len(verifier), 43)
        
        # Check alphabet compliance (RFC 7636)
        allowed_chars = set(string.ascii_letters + string.digits + "-._~")
        self.assertTrue(all(c in allowed_chars for c in verifier))
        
        # Verify code_challenge exists and is base64url encoded (no padding)
        self.assertIn("code_challenge", pkce)
        self.assertNotIn(b"=", pkce["code_challenge"])
    
    def test_pkce_verifier_custom_length(self):
        """Test PKCE verifier with valid custom lengths."""
        for length in [43, 64, 128]:
            pkce = oauth2._generate_pkce_code_verifier(length=length)
            self.assertEqual(len(pkce["code_verifier"]), length)
    
    def test_token_uniqueness(self):
        """Verify generated tokens are unique (randomness test)."""
        # Generate multiple tokens - should all be different
        verifiers = [oauth2._generate_pkce_code_verifier()["code_verifier"] 
                     for _ in range(10)]
        self.assertEqual(len(set(verifiers)), 10, "Generated tokens should be unique")
        
        # Also test state generation
        from msal.oauth2cli.oauth2 import Client
        from unittest.mock import Mock
        
        client = Client(
            server_configuration={"authorization_endpoint": "https://example.com/auth"},
            client_id="test-client",
            http_client=Mock()
        )
        
        states = [client.initiate_auth_code_flow(scope=["test"])["state"] 
                  for _ in range(10)]
        self.assertEqual(len(set(states)), 10, "Generated states should be unique")