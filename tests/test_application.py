from msal.application import ConfidentialClientApplication

from tests import unittest


class TestConfidentialClientApplication(unittest.TestCase):
    def test_confidential_client_using_secret(self):
        app = ConfidentialClientApplication(
            "client_id", "client_secret", "TBD: TokenCache()")
        result = app.acquire_token_for_client(
            ["r1/scope1", "r1/scope2"], "policy")
        self.assertIsNone(result)

