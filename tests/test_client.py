import os
import json
import logging

import requests

from oauth2cli.oauth2 import Client
from oauth2cli.authcode import obtain_auth_code
from tests import unittest


THIS_FOLDER = os.path.dirname(__file__)
CONFIG_FILENAME = "config.json"

def load_conf(filename):
    """
    Example of a configuration file:

    {
        "Note": "the following server_configuration is optional",
        "server_configuration": {
            "authorization_endpoint": "https://example.com/tenant/oauth2/authorize",
            "token_endpoint": "https://example.com/tenant/oauth2/token"
        },

        "client_id": "289a413d-284b-4303-9c79-94380abe5d22",
        "client_secret": "your_secret",

        "scope": ["your_scope"],
        "resource": "Some IdP needs this",

        "authority": "https://example.com/tenant/",
        "username": "you@example.com",
        "password": "I could tell you but then I would have to kill you",

        "placeholder": null
    }
    """
    try:
        with open(filename) as f:
            conf = json.load(f)
    except:
        logging.warn("Unable to open/read JSON configuration %s" % filename)
        raise
    if not conf.get("server_configuration"):  # Then we do a discovery
        # The following line may duplicate a '/' at the joining point,
        # but requests.get(...) would still work.
        # Besides, standard urljoin(...) is picky on insisting authority ends with '/'
        discovery_uri = conf["authority"] + '/.well-known/openid-configuration'
        conf["server_configuration"] = requests.get(discovery_uri).json()
    return conf

CONFIG = load_conf(os.path.join(THIS_FOLDER, 'config.json')) or {}


class Oauth2TestCase(unittest.TestCase):

    def assertLoosely(self, response, assertion=None,
            skippable_errors=("invalid_grant", "interaction_required")):
        if response.get("error") in skippable_errors:
            # Some of these errors are configuration issues, not library issues
            raise unittest.SkipTest(response.get("error_description"))
        else:
            if assertion is None:
                assertion = lambda: self.assertIn("access_token", response)
            assertion()


# Since the OAuth2 specs uses snake_case, this test config also uses snake_case
@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestClient(Oauth2TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = Client(
            CONFIG['client_id'],
            client_secret=CONFIG.get('client_secret'),
            configuration=CONFIG["server_configuration"])

    @unittest.skipUnless("client_secret" in CONFIG, "client_secret missing")
    def test_client_credentials(self):
        result = self.client.obtain_token_with_client_credentials(
            CONFIG.get('scope'))
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
        "authorization_endpoint" in CONFIG.get("server_configuration", {}),
        "authorization_endpoint missing")
    def test_auth_code(self):
        port = CONFIG.get("listen_port", 44331)
        redirect_uri = "http://localhost:%s" % port
        auth_request_uri = self.client.build_auth_request_uri(
            "code", redirect_uri=redirect_uri, scope=CONFIG.get("scope"))
        ac = obtain_auth_code(port, auth_uri=auth_request_uri)
        self.assertNotEqual(ac, None)
        result = self.client.obtain_token_with_authorization_code(
            ac,
            data={
                "scope": CONFIG.get("scope"),
                "resource": CONFIG.get("resource"),
                },  # MSFT AAD only
            redirect_uri=redirect_uri)
        self.assertLoosely(result, lambda: self.assertIn('access_token', result))

