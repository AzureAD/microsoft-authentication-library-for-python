import os
import json
import logging

from msal.application import *
from tests import unittest


THIS_FOLDER = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(THIS_FOLDER, 'config.json')
CONFIG = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as conf:
        CONFIG = json.load(conf)


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


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestPublicClientApplication(unittest.TestCase):

    @unittest.skipUnless("username" in CONFIG and "password" in CONFIG, "Missing U/P")
    def test_username_password(self):
        app = PublicClientApplication(
                CONFIG["client_id"], authority=CONFIG["authority"])
        result = app.acquire_token_with_username_password(
                CONFIG["username"], CONFIG["password"], scope=CONFIG.get("scope"))
        if "error" in result:
            if result["error"] == "invalid_grant":
                raise unittest.SkipTest(result.get("error_description"))
            self.assertEqual(result["error"], "interaction_required")
        else:
            self.assertIn('access_token', result)


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestClientApplication(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = ClientApplication(
            CONFIG["client_id"], client_credential=CONFIG.get("client_secret"))

    def assertLoosely(self, result):
        if "error" in result:
            # Some of these errors are configuration issues, not library issues
            if result["error"] == "invalid_grant":
                raise unittest.SkipTest(result.get("error_description"))
            self.assertEqual(result["error"], "interaction_required")
        else:
            self.assertIn('access_token', result)

    @unittest.skipUnless("scope" in CONFIG, "Missing scope")
    def test_auth_code(self):
        from oauth2cli.authcode import obtain_auth_code
        port = CONFIG.get("listen_port", 44331)
        redirect_uri = "http://localhost:%s" % port
        auth_request_uri = self.app.get_authorization_request_url(
            CONFIG["scope"], redirect_uri=redirect_uri)
        ac = obtain_auth_code(port, auth_uri=auth_request_uri)
        self.assertNotEqual(ac, None)
        result = self.app.acquire_token_with_authorization_code(
            ac, CONFIG["scope"], redirect_uri=redirect_uri)
        self.assertLoosely(result)

