import os
import json
import logging

from oauth2cli.oauth2 import Client
from oauth2cli.authcode import obtain_auth_code
from tests import unittest


THIS_FOLDER = os.path.dirname(__file__)
CONFIG_FILENAME = "config.json"

def load_conf(filename):
    try:
        with open(filename) as f:
            return json.load(f)
    except:
        logging.warn("Unable to find/read JSON configuration %s" % filename)

CONFIG = load_conf(os.path.join(THIS_FOLDER, 'config.json')) or {}


# Since the OAuth2 specs uses snake_case, this test config also uses snake_case
@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = Client(
            CONFIG['client_id'],
            client_secret=CONFIG.get('client_secret'),
            authorization_endpoint=CONFIG.get("authorization_endpoint"),
            token_endpoint=CONFIG.get("token_endpoint"))

    @unittest.skipUnless("client_secret" in CONFIG, "client_secret missing")
    def test_client_credentials(self):
        result = self.client.obtain_token_with_client_credentials(
            CONFIG.get('scope'))
        self.assertIn('access_token', result)

    def assertLoosely(self, result):
        if "error" in result:
            # Some of these errors are configuration issues, not library issues
            if result["error"] == "invalid_grant":
                raise unittest.SkipTest(result.get("error_description"))
            self.assertEqual(result["error"], "interaction_required")
        else:
            self.assertIn('access_token', result)

    @unittest.skipUnless(
        "username" in CONFIG and "password" in CONFIG, "username/password missing")
    def test_username_password(self):
        result = self.client.obtain_token_with_username_password(
            CONFIG["username"], CONFIG["password"],
            data={"resource": CONFIG.get("resource")},  # MSFT AAD V1 only
            scope=CONFIG.get("scope"))
        self.assertLoosely(result)

    @unittest.skipUnless(
        "authorization_endpoint" in CONFIG, "authorization_endpoint missing")
    def test_auth_code(self):
        port = CONFIG.get("listen_port", 44331)
        redirect_uri = "http://localhost:%s" % port
        auth_request_uri = self.client.build_auth_request_uri(
            "code", redirect_uri=redirect_uri, scope=CONFIG.get("scope"))
        ac = obtain_auth_code(port, auth_uri=auth_request_uri)
        self.assertNotEqual(ac, None)
        result = self.client.obtain_token_with_authorization_code(
            ac,
            data={"scope": CONFIG.get("scope")},  # MSFT AAD only
            redirect_uri=redirect_uri)
        self.assertLoosely(result)

