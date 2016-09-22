import os
import json

from msal.application import ConfidentialClientApplication
from tests import unittest


class TestConfidentialClientApplication(unittest.TestCase):
    same_folder = os.path.dirname(__file__)
    config = json.load(open(os.path.join(same_folder, 'config.json')))
    scope = ["https://graph.microsoft.com/.default"]
    scope2 = ["User.Read"]
    scope_rt = ["offline_access"]

    def test_confidential_client_using_secret(self):
        app = ConfidentialClientApplication(
            self.config['CLIENT_ID'], self.config['CLIENT_SECRET'])
        result = app.acquire_token_for_client(self.scope, "policy")
        self.assertIn('access_token', result)

    def test_confidential_client_using_certificate(self):
        private_key = os.path.join(self.same_folder, self.config['PRIVATE_KEY'])
        with open(private_key) as f: pem = f.read()
        certificate = {
            'certificate': pem,
            "thumbprint": self.config['THUMBPRINT'],
            }
        app = ConfidentialClientApplication(
            self.config['CLIENT_ID'], certificate)
        result = app.acquire_token_for_client(self.scope, "policy")
        self.assertIn('access_token', result)

    def test_get_authorization_request_url(self):
        app = ConfidentialClientApplication(self.config['CLIENT_ID'], "secret")
        url = app.get_authorization_request_url(self.scope2)
        print("Authorization URL:", url)
        self.assertIn("response_type=code", url)  # A weak check, for now
        # After user consent, your redirect endpoint will be hit like this:
        # http://localhost:8000/?code=blahblah&other_param=foo

    def test_acquire_token_by_authorization_code(self):
        app = ConfidentialClientApplication(
            self.config['CLIENT_ID'], self.config['CLIENT_SECRET'])
        auth_code = "OAQ...snipped..."
        token = app.acquire_token_by_authorization_code(auth_code, self.scope2)
        self.assertEqual(token.get('error_description', ""), "")  # Expired?
        # print(token)

    def test_acquire_token_silent(self):
        app = ConfidentialClientApplication(
            self.config['CLIENT_ID'], self.config['CLIENT_SECRET'])
        token = app.acquire_token_silent(
            self.scope2, refresh_token=self.config['REFRESH_TOKEN'])
        self.assertEqual(token.get('error_description', ""), "")
        #print(token)

