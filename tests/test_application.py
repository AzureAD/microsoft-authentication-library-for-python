import os
import json
import logging

from msal.application import ConfidentialClientApplication
from tests import unittest

from authcode import AuthCodeReceiver


THIS_FOLDER = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(THIS_FOLDER, 'config.json')
CONFIG = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as conf:
        CONFIG = json.load(conf)

def acquire_token_with_authorization_code(app, redirect_port, scope):
    # Note: This func signature does not and should not require client_secret
    fresh_auth_code = AuthCodeReceiver.acquire(
        app.get_authorization_request_url(scope), redirect_port)
    return app.acquire_token_with_authorization_code(fresh_auth_code, scope)


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestConfidentialClientApplication(unittest.TestCase):

    @unittest.skipUnless("client_secret" in CONFIG, "Missing client secret")
    def test_confidential_client_using_secret(self):
        app = ConfidentialClientApplication(
                CONFIG['client_id'], CONFIG['client_secret'])
        result = app.acquire_token_for_client(CONFIG.get("scope"))
        self.assertIn('access_token', result)

    @unittest.skipUnless("private_key" in CONFIG, "Missing client cert")
    def test_confidential_client_using_certificate(self):
        private_key = os.path.join(THIS_FOLDER, CONFIG['private_key'])
        with open(private_key) as f: pem = f.read()
        certificate = {'certificate': pem, "thumbprint": CONFIG['thumbprint']}
        app = ConfidentialClientApplication(CONFIG['client_id'], certificate)
        result = app.acquire_token_for_client(self.scope)
        self.assertIn('access_token', result)


# Note: This test case requires human interaction to obtain authorization code
@unittest.skipUnless(os.path.exists(CONFIG_FILE), "%s missing" % CONFIG_FILE)
class ToDo_TestConfidentialClientApplication(unittest.TestCase):
    scope = ["https://graph.microsoft.com/.default"]
    scope2 = ["User.Read"]
    scope_rt = ["offline_access"]

    @classmethod
    def setUpClass(cls):
        cls.config = json.load(open(CONFIG_FILE))
        cls.app = ConfidentialClientApplication(
            cls.config['CLIENT_ID'], cls.config['CLIENT_SECRET'])
        cls.token = acquire_token_with_authorization_code(
            # Prepare a token. It will be shared among multiple test cases.
            cls.app, cls.config.get('REDIRECTION_PORT', 8000), cls.scope2)
    def test_get_authorization_request_url(self):
        app = ConfidentialClientApplication(self.config['CLIENT_ID'], "secret")
        url = app.get_authorization_request_url(self.scope2)
        print("Authorization URL: {}".format(url))
        self.assertIn("response_type=code", url)  # A weak check, for now
        # After user consent, your redirect endpoint will be hit like this:
        # http://localhost:8000/?code=blahblah&other_param=foo

    @classmethod
    def beautify(cls, json_payload):
        return json.dumps(json_payload, indent=2)

    def test_acquire_token_with_authorization_code(self):
        # Actually we already obtain a token during this TestCase initialization
        self.assertEqual(self.token.get('error_description'), None)
        logging.info("Authorization Code Grant: %s", self.beautify(self.token))

    def test_acquire_token_silent(self):
        if 'refresh_token' not in self.token:
            raise unittest.SkipTest("refresh_token not available")
        token = self.app.acquire_token_silent(
            self.scope2, refresh_token=self.token['refresh_token'])
        self.assertEqual(token.get('error_description', ""), "")
        if 'refresh_token' in token:
            logging.warn(
                "Authorization Server also issues a new Refresh Token: %s",
                self.beautify(token))
            self.token = token  # https://tools.ietf.org/html/rfc6749#section-6

