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
        # You can filter by predefined username, or let end user to choose one
        accounts = self.app.get_accounts(username=CONFIG.get("username"))
        self.assertNotEqual(0, len(accounts))
        account = accounts[0]
        # Going to test acquire_token_silent(...) to locate an AT from cache
        result_from_cache = self.app.acquire_token_silent(
                CONFIG["scope"], account=account)
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(result['access_token'], result_from_cache['access_token'],
                "We should get a cached AT")

        # Going to test acquire_token_silent(...) to obtain an AT by a RT from cache
        self.app.token_cache._cache["AccessToken"] = {}  # A hacky way to clear ATs
        result_from_cache = self.app.acquire_token_silent(
                CONFIG["scope"], account=account)
        self.assertIsNotNone(result_from_cache,
                "We should get a result from acquire_token_silent(...) call")
        self.assertNotEqual(result['access_token'], result_from_cache['access_token'],
                "We should get a fresh AT (via RT)")


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestConfidentialClientApplication(unittest.TestCase):

    def assertCacheWorks(self, result_from_wire, result_from_cache):
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(
            result_from_wire['access_token'], result_from_cache['access_token'])

    @unittest.skipUnless("client_secret" in CONFIG, "Missing client secret")
    def test_client_secret(self):
        app = ConfidentialClientApplication(
            CONFIG["client_id"], client_credential=CONFIG.get("client_secret"),
            authority=CONFIG.get("authority"))
        scope = CONFIG.get("scope", [])
        result = app.acquire_token_for_client(scope)
        self.assertIn('access_token', result)
        self.assertCacheWorks(result, app.acquire_token_silent(scope, account=None))

    @unittest.skipUnless("client_certificate" in CONFIG, "Missing client cert")
    def test_client_certificate(self):
        client_certificate = CONFIG["client_certificate"]
        assert ("private_key_path" in client_certificate
                and "thumbprint" in client_certificate)
        key_path = os.path.join(THIS_FOLDER, client_certificate['private_key_path'])
        with open(key_path) as f:
            pem = f.read()
        app = ConfidentialClientApplication(
            CONFIG['client_id'],
            {"private_key": pem, "thumbprint": client_certificate["thumbprint"]})
        scope = CONFIG.get("scope", [])
        result = app.acquire_token_for_client(scope)
        self.assertIn('access_token', result)
        self.assertCacheWorks(result, app.acquire_token_silent(scope, account=None))


@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestPublicClientApplication(Oauth2TestCase):

    @unittest.skipUnless("username" in CONFIG and "password" in CONFIG, "Missing U/P")
    def test_username_password(self):
        self.app = PublicClientApplication(
                CONFIG["client_id"], authority=CONFIG["authority"])
        result = self.app.acquire_token_by_username_password(
                CONFIG["username"], CONFIG["password"], scopes=CONFIG.get("scope"))
        self.assertLoosely(result)
        self.assertCacheWorks(result)

    def test_device_flow(self):
        self.app = PublicClientApplication(
            CONFIG["client_id"], authority=CONFIG["authority"])
        flow = self.app.initiate_device_flow(scopes=CONFIG.get("scope"))
        logging.warn(flow["message"])

        duration = 30
        logging.warn("We will wait up to %d seconds for you to sign in" % duration)
        flow["expires_at"] = time.time() + duration  # Shorten the time for quick test
        result = self.app.acquire_token_by_device_flow(flow)
        self.assertLoosely(
                result,
                assertion=lambda: self.assertIn('access_token', result),
                skippable_errors=self.app.client.DEVICE_FLOW_RETRIABLE_ERRORS)

        if "access_token" in result:
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

        result = self.app.acquire_token_by_authorization_code(
            ac, CONFIG["scope"], redirect_uri=redirect_uri)
        logging.debug("cache = %s", json.dumps(self.app.token_cache._cache, indent=4))
        self.assertIn(
            "access_token", result,
            "{error}: {error_description}".format(
                # Note: No interpolation here, cause error won't always present
                error=result.get("error"),
                error_description=result.get("error_description")))
        self.assertCacheWorks(result)

