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

logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.DEBUG)


class Oauth2TestCase(unittest.TestCase):

    def assertLoosely(self, response, assertion=None,
            skippable_errors=("invalid_grant", "interaction_required")):
        if response.get("error") in skippable_errors:
            logger.debug("Response = %s", response)
            # Some of these errors are configuration issues, not library issues
            raise unittest.SkipTest(response.get("error_description"))
        else:
            if assertion is None:
                assertion = lambda: self.assertIn(
                    "access_token", response,
                    "{error}: {error_description}".format(
                        # Do explicit response.get(...) rather than **response
                        error=response.get("error"),
                        error_description=response.get("error_description")))
            assertion()

    def assertCacheWorks(self, result_from_wire):
        result = result_from_wire
        # Going to test acquire_token_silent(...) to locate an AT from cache
        # In practice, you may want to filter based on its "username" field
        accounts = self.app.get_accounts()
        self.assertNotEqual(0, len(accounts))
        result_from_cache = self.app.acquire_token_silent(
                CONFIG["scope"], account=accounts[0])
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(result['access_token'], result_from_cache['access_token'],
                "We should get a cached AT")

        # Going to test acquire_token_silent(...) to obtain an AT by a RT from cache
        self.app.token_cache._cache["AccessToken"] = {}  # A hacky way to clear ATs
        result_from_cache = self.app.acquire_token_silent(
                CONFIG["scope"], account=accounts[0])
        self.assertIsNotNone(result_from_cache,
                "We should get a result from acquire_token_silent(...) call")
        self.assertNotEqual(result['access_token'], result_from_cache['access_token'],
                "We should get a fresh AT (via RT)")


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestConfidentialClientApplication(unittest.TestCase):

    @unittest.skipUnless("client_secret" in CONFIG, "Missing client secret")
    def test_confidential_client_using_secret(self):
        app = ConfidentialClientApplication(
            CONFIG["client_id"], client_credential=CONFIG.get("client_secret"),
            authority=CONFIG.get("authority"))
        scope = CONFIG.get("scope", [])
        result = app.acquire_token_for_client(scope)
        self.assertIn('access_token', result)

        result_from_cache = app.acquire_token_silent(scope, account=None)
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(result['access_token'], result_from_cache['access_token'])

    @unittest.skipUnless("private_key" in CONFIG, "Missing client cert")
    def test_confidential_client_using_certificate(self):
        private_key = os.path.join(THIS_FOLDER, CONFIG['private_key'])
        with open(private_key) as f: pem = f.read()
        certificate = {'certificate': pem, "thumbprint": CONFIG['thumbprint']}
        app = ConfidentialClientApplication(CONFIG['client_id'], certificate)
        result = app.acquire_token_for_client(self.scope)
        self.assertIn('access_token', result)


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestPublicClientApplication(Oauth2TestCase):

    @unittest.skipUnless("username" in CONFIG and "password" in CONFIG, "Missing U/P")
    def test_username_password(self):
        self.app = PublicClientApplication(
                CONFIG["client_id"], authority=CONFIG["authority"])
        result = self.app.acquire_token_with_username_password(
                CONFIG["username"], CONFIG["password"], scope=CONFIG.get("scope"))
        self.assertLoosely(result)
        self.assertCacheWorks(result)


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestClientApplication(Oauth2TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = ClientApplication(
            CONFIG["client_id"], client_credential=CONFIG.get("client_secret"),
            authority=CONFIG.get("authority"))

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
        logging.debug("cache = %s", json.dumps(self.app.token_cache._cache, indent=4))
        self.assertIn(
            "access_token", result,
            "{error}: {error_description}".format(
                # Note: No interpolation here, cause error won't always present
                error=result.get("error"),
                error_description=result.get("error_description")))
        self.assertCacheWorks(result)

    def test_device_flow(self):
        flow = self.app.initiate_device_flow(scope=CONFIG.get("scope"))
        logging.warn(flow["message"])

        duration = 30
        logging.warn("We will wait up to %d seconds for you to sign in" % duration)
        result = self.app.acquire_token_by_device_flow(
            flow,
            exit_condition=lambda end=time.time() + duration: time.time() > end)
        self.assertLoosely(
                result,
                assertion=lambda: self.assertIn('access_token', result),
                skippable_errors=self.app.client.DEVICE_FLOW_RETRIABLE_ERRORS)

        if "access_token" in result:
            self.assertCacheWorks(result)

