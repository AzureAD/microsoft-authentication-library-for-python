import os
import json
import logging
try:  # Python 2
    from urlparse import urljoin
except:  # Python 3
    from urllib.parse import urljoin
import time

import requests

from oauth2cli.oauth2 import Client
from oauth2cli.authcode import obtain_auth_code
from oauth2cli.assertion import JwtSigner
from tests import unittest, Oauth2TestCase


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__file__)

CONFIG_FILENAME = "config.json"

def load_conf(filename):
    """
    Example of a configuration file:

    {
        "Note": "the OpenID Discovery will be updated by following optional content",
        "openid_configuration": {
            "authorization_endpoint": "https://example.com/tenant/oauth2/authorize",
            "token_endpoint": "https://example.com/tenant/oauth2/token",
            "device_authorization_endpoint": "device_authorization"
        },

        "client_id": "289a413d-284b-4303-9c79-94380abe5d22",
        "client_secret": "your_secret",

        "scope": ["your_scope"],
        "resource": "Some IdP needs this",

        "oidp": "https://example.com/tenant/",
        "username": "you@example.com",
        "password": "I could tell you but then I would have to kill you",

        "placeholder": null
    }
    """
    try:
        with open(filename) as f:
            conf = json.load(f)
    except:
        logger.warn("Unable to open/read JSON configuration %s" % filename)
        raise
    openid_configuration = {}
    try:
        # The following line may duplicate a '/' at the joining point,
        # but requests.get(...) would still work.
        # Besides, standard urljoin(...) is picky on insisting oidp ends with '/'
        discovery_uri = conf["oidp"] + '/.well-known/openid-configuration'
        openid_configuration.update(requests.get(discovery_uri).json())
    except:
        logger.warn("openid-configuration uri not accesible: %s", discovery_uri)
    openid_configuration.update(conf.get("openid_configuration", {}))
    if openid_configuration.get("device_authorization_endpoint"):
        # The following urljoin(..., ...) trick allows a "path_name" shorthand
        openid_configuration["device_authorization_endpoint"] = urljoin(
            openid_configuration.get("token_endpoint", ""),
            openid_configuration.get("device_authorization_endpoint", ""))
    conf["openid_configuration"] = openid_configuration
    return conf

THIS_FOLDER = os.path.dirname(__file__)
CONFIG = load_conf(os.path.join(THIS_FOLDER, CONFIG_FILENAME)) or {}


# Since the OAuth2 specs uses snake_case, this test config also uses snake_case
@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestClient(Oauth2TestCase):

    @classmethod
    def setUpClass(cls):
        if "client_certificate" in CONFIG:
            private_key_path = CONFIG["client_certificate"]["private_key_path"]
            with open(os.path.join(THIS_FOLDER, private_key_path)) as f:
                private_key = f.read()  # Expecting PEM format
            cls.client = Client(
                CONFIG["openid_configuration"],
                CONFIG['client_id'],
                client_assertion=JwtSigner(
                        private_key,
                        algorithm="RS256",
                        sha1_thumbprint=CONFIG["client_certificate"]["thumbprint"]
                    ).sign_assertion(
                        audience=CONFIG["openid_configuration"]["token_endpoint"],
                        issuer=CONFIG["client_id"],
                    ),
                )
        else:
            cls.client = Client(
                CONFIG["openid_configuration"], CONFIG['client_id'],
                client_secret=CONFIG.get('client_secret'))

    @unittest.skipUnless("client_secret" in CONFIG, "client_secret missing")
    def test_client_credentials(self):
        result = self.client.obtain_token_for_client(CONFIG.get('scope'))
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
        "authorization_endpoint" in CONFIG.get("openid_configuration", {}),
        "authorization_endpoint missing")
    def test_auth_code(self):
        port = CONFIG.get("listen_port", 44331)
        redirect_uri = "http://localhost:%s" % port
        auth_request_uri = self.client.build_auth_request_uri(
            "code", redirect_uri=redirect_uri, scope=CONFIG.get("scope"))
        ac = obtain_auth_code(port, auth_uri=auth_request_uri)
        self.assertNotEqual(ac, None)
        result = self.client.obtain_token_by_authorization_code(
            ac,
            data={
                "scope": CONFIG.get("scope"),
                "resource": CONFIG.get("resource"),
                },  # MSFT AAD only
            redirect_uri=redirect_uri)
        self.assertLoosely(result, lambda: self.assertIn('access_token', result))

    @unittest.skipUnless(
        CONFIG.get("openid_configuration", {}).get("device_authorization_endpoint"),
        "device_authorization_endpoint is missing")
    def test_device_flow(self):
        flow = self.client.initiate_device_flow(scope=CONFIG.get("scope"))
        try:
            msg = ("Use a web browser to open the page {verification_uri} and "
                "enter the code {user_code} to authenticate.".format(**flow))
        except KeyError:  # Some IdP might not be standard compliant
            msg = flow["message"]  # Not a standard parameter though
        logger.warn(msg)  # We avoid print(...) b/c its output would be buffered

        duration = 30
        logger.warn("We will wait up to %d seconds for you to sign in" % duration)
        result = self.client.obtain_token_by_device_flow(
            flow,
            exit_condition=lambda end=time.time() + duration: time.time() > end)

        self.assertLoosely(
                result,
                assertion=lambda: self.assertIn('access_token', result),
                skippable_errors=self.client.DEVICE_FLOW_RETRIABLE_ERRORS)

