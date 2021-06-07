import os
import json
import logging
try:  # Python 2
    from urlparse import urljoin
except:  # Python 3
    from urllib.parse import urljoin
import time

import requests

from msal.oauth2cli import Client, JwtSigner, AuthCodeReceiver
from msal.oauth2cli.authcode import obtain_auth_code
from tests import unittest, Oauth2TestCase
from tests.http_client import MinimalHttpClient, MinimalResponse


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__file__)

CONFIG_FILENAME = "config.json"

def load_conf(filename):
    """
    Example of a configuration file:

    {
        "Note": "the OpenID Discovery will be updated by following optional content",
        "additional_openid_configuration": {
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
    conf = {}
    if os.path.exists(filename):
        with open(filename) as f:
            conf = json.load(f)
    else:
        # Do not raise unittest.SkipTest(...) here,
        # because it would still be considered as Test Error in Python 2
        logger.warning("Unable to locate JSON configuration %s" % filename)
    openid_configuration = {}
    if "oidp" in conf:
        try:
            # The following line may duplicate a '/' at the joining point,
            # but requests.get(...) would still work.
            # Besides, standard urljoin(...) is picky on insisting oidp ends with '/'
            discovery_uri = conf["oidp"] + '/.well-known/openid-configuration'
            openid_configuration.update(requests.get(discovery_uri).json())
        except:
            logger.warning(
                "openid configuration uri not accesible: %s", discovery_uri)
    openid_configuration.update(conf.get("additional_openid_configuration", {}))
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
@unittest.skipUnless(CONFIG.get("openid_configuration"), "openid_configuration missing")
class TestClient(Oauth2TestCase):

    @classmethod
    def setUpClass(cls):
        http_client = MinimalHttpClient()
        if "client_assertion" in CONFIG:
            cls.client = Client(
                CONFIG["openid_configuration"],
                CONFIG['client_id'],
                http_client=http_client,
                client_assertion=CONFIG["client_assertion"],
                client_assertion_type=Client.CLIENT_ASSERTION_TYPE_JWT,
                )
        elif "client_certificate" in CONFIG:
            private_key_path = CONFIG["client_certificate"]["private_key_path"]
            with open(os.path.join(THIS_FOLDER, private_key_path)) as f:
                private_key = f.read()  # Expecting PEM format
            cls.client = Client(
                CONFIG["openid_configuration"],
                CONFIG['client_id'],
                http_client=http_client,
                client_assertion=JwtSigner(
                        private_key,
                        algorithm="RS256",
                        sha1_thumbprint=CONFIG["client_certificate"]["thumbprint"]
                    ).sign_assertion(
                        audience=CONFIG["openid_configuration"]["token_endpoint"],
                        issuer=CONFIG["client_id"],
                    ),
                client_assertion_type=Client.CLIENT_ASSERTION_TYPE_JWT,
                )
        else:
            cls.client = Client(
                CONFIG["openid_configuration"], CONFIG['client_id'],
                http_client=http_client,
                client_secret=CONFIG.get('client_secret'))

    @unittest.skipIf(
        "token_endpoint" not in CONFIG.get("openid_configuration", {}),
        "token_endpoint missing")
    @unittest.skipIf("client_secret" not in CONFIG, "client_secret missing")
    def test_client_credentials(self):
        result = self.client.obtain_token_for_client(CONFIG.get('scope'))
        self.assertIn('access_token', result)

    @unittest.skipIf(
        "token_endpoint" not in CONFIG.get("openid_configuration", {}),
        "token_endpoint missing")
    @unittest.skipIf(
        not ("username" in CONFIG and "password" in CONFIG),
        "username/password missing")
    def test_username_password(self):
        result = self.client.obtain_token_by_username_password(
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
        nonce = "nonce should contain sufficient entropy"
        auth_request_uri = self.client.build_auth_request_uri(
            "code",
            nonce=nonce,
            redirect_uri=redirect_uri, scope=CONFIG.get("scope"))
        ac = obtain_auth_code(port, auth_uri=auth_request_uri)
        self.assertNotEqual(ac, None)
        result = self.client.obtain_token_by_authorization_code(
            ac,
            data={
                "scope": CONFIG.get("scope"),
                "resource": CONFIG.get("resource"),
                },  # MSFT AAD only
            nonce=nonce,
            redirect_uri=redirect_uri)
        self.assertLoosely(result, lambda: self.assertIn('access_token', result))

    @unittest.skipUnless(
        "authorization_endpoint" in CONFIG.get("openid_configuration", {}),
        "authorization_endpoint missing")
    def test_auth_code_flow(self):
        with AuthCodeReceiver(port=CONFIG.get("listen_port")) as receiver:
            flow = self.client.initiate_auth_code_flow(
                redirect_uri="http://localhost:%d" % receiver.get_port(),
                scope=CONFIG.get("scope"),
                login_hint=CONFIG.get("username"),  # To skip the account selector
                )
            auth_response = receiver.get_auth_response(
                auth_uri=flow["auth_uri"],
                state=flow["state"],  # Optional but recommended
                timeout=120,
                welcome_template="""<html><body>
                    authorization_endpoint = {a}, client_id = {i}
                    <a href="$auth_uri">Sign In</a> or <a href="$abort_uri">Abort</a>
                    </body></html>""".format(
                        a=CONFIG["openid_configuration"]["authorization_endpoint"],
                        i=CONFIG.get("client_id")),
                )
            self.assertIsNotNone(
                auth_response.get("code"), "Error: {}, Detail: {}".format(
                    auth_response.get("error"), auth_response))
            result = self.client.obtain_token_by_auth_code_flow(flow, auth_response)
                #TBD: data={"resource": CONFIG.get("resource")},  # MSFT AAD v1 only
            self.assertLoosely(result, lambda: self.assertIn('access_token', result))

    def test_auth_code_flow_error_response(self):
        with self.assertRaisesRegexp(ValueError, "state missing"):
            self.client.obtain_token_by_auth_code_flow({}, {"code": "foo"})
        with self.assertRaisesRegexp(ValueError, "state mismatch"):
            self.client.obtain_token_by_auth_code_flow({"state": "1"}, {"state": "2"})
        with self.assertRaisesRegexp(ValueError, "scope"):
            self.client.obtain_token_by_auth_code_flow(
                {"state": "s", "scope": ["foo"]}, {"state": "s"}, scope=["bar"])
        self.assertEqual(
            {"error": "foo", "error_uri": "bar"},
            self.client.obtain_token_by_auth_code_flow(
                {"state": "s"},
                {"state": "s", "error": "foo", "error_uri": "bar", "access_token": "fake"}),
            "We should not leak malicious input into our output")

    @unittest.skipUnless(
        "authorization_endpoint" in CONFIG.get("openid_configuration", {}),
        "authorization_endpoint missing")
    def test_obtain_token_by_browser(self):
        result = self.client.obtain_token_by_browser(
            scope=CONFIG.get("scope"),
            redirect_uri=CONFIG.get("redirect_uri"),
            welcome_template="""<html><body>
                authorization_endpoint = {a}, client_id = {i}
                <a href="$auth_uri">Sign In</a> or <a href="$abort_uri">Abort</a>
                </body></html>""".format(
                    a=CONFIG["openid_configuration"]["authorization_endpoint"],
                    i=CONFIG.get("client_id")),
            success_template="<strong>Done. You can close this window now.</strong>",
            login_hint=CONFIG.get("username"),  # To skip the account selector
            timeout=60,
            )
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
        logger.warning(msg)  # Avoid print(...) b/c its output would be buffered

        duration = 30
        logger.warning("We will wait up to %d seconds for you to sign in" % duration)
        flow["expires_at"] = time.time() + duration  # Shorten the time for quick test
        result = self.client.obtain_token_by_device_flow(flow)
        self.assertLoosely(
                result,
                assertion=lambda: self.assertIn('access_token', result),
                skippable_errors=self.client.DEVICE_FLOW_RETRIABLE_ERRORS)


class TestRefreshTokenCallbacks(unittest.TestCase):

    def _dummy(self, url, **kwargs):
        return MinimalResponse(status_code=200, text='{"refresh_token": "new"}')

    def test_rt_being_added(self):
        client = Client(
            {"token_endpoint": "http://example.com/token"},
            "client_id",
            http_client=MinimalHttpClient(),
            on_obtaining_tokens=lambda event:
                self.assertEqual("new", event["response"].get("refresh_token")),
            on_updating_rt=lambda rt_item, new_rt:
                self.fail("This should not be called here"),
            )
        client.obtain_token_by_authorization_code("code", post=self._dummy)

    def test_rt_being_updated(self):
        old_rt = {"refresh_token": "old"}
        client = Client(
            {"token_endpoint": "http://example.com/token"},
            "client_id",
            http_client=MinimalHttpClient(),
            on_obtaining_tokens=lambda event:
                self.assertNotIn("refresh_token", event["response"]),
            on_updating_rt=lambda old, new:  # TODO: ensure it being called
                (self.assertEqual(old_rt, old), self.assertEqual("new", new)),
            )
        client.obtain_token_by_refresh_token(
            {"refresh_token": "old"}, post=self._dummy)

    def test_rt_being_migrated(self):
        old_rt = {"refresh_token": "old"}
        client = Client(
            {"token_endpoint": "http://example.com/token"},
            "client_id",
            http_client=MinimalHttpClient(),
            on_obtaining_tokens=lambda event:
                self.assertEqual("new", event["response"].get("refresh_token")),
            on_updating_rt=lambda rt_item, new_rt:
                self.fail("This should not be called here"),
            )
        client.obtain_token_by_refresh_token(
            {"refresh_token": "old"}, on_updating_rt=False, post=self._dummy)


class TestSessionAccessibility(unittest.TestCase):
    def test_accessing_session_property_for_backward_compatibility(self):
        client = Client({"token_endpoint": "https://example.com"}, "client_id")
        client.session
        client.session.close()
        client.session = "something"

