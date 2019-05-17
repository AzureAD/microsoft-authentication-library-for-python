import os
import json
import logging

try:
    from unittest.mock import *  # Python 3
except:
    from mock import *  # Need an external mock package

from msal.application import *
import msal
from tests import unittest
from tests.test_token_cache import TokenCacheTestCase


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
        assert "user_code" in flow, str(flow)  # Provision or policy might block DF
        logging.warning(flow["message"])

        duration = 30
        logging.warning("We will wait up to %d seconds for you to sign in" % duration)
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
        from msal.oauth2cli.authcode import obtain_auth_code
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


class TestClientApplicationAcquireTokenSilentFociBehaviors(unittest.TestCase):

    def setUp(self):
        self.authority_url = "https://login.microsoftonline.com/common"
        self.authority = msal.authority.Authority(self.authority_url)
        self.scopes = ["s1", "s2"]
        self.uid = "my_uid"
        self.utid = "my_utid"
        self.account = {"home_account_id": "{}.{}".format(self.uid, self.utid)}
        self.frt = "what the frt"
        self.cache = msal.SerializableTokenCache()
        self.preexisting_family_app_id = "preexisting_family_app"
        self.cache.add({  # Pre-populate a FRT
            "client_id": self.preexisting_family_app_id,
            "scope": self.scopes,
            "token_endpoint": "{}/oauth2/v2.0/token".format(self.authority_url),
            "response": TokenCacheTestCase.build_response(
                access_token="Siblings won't share AT. test_remove_account() will.",
                id_token=TokenCacheTestCase.build_id_token(),
                uid=self.uid, utid=self.utid, refresh_token=self.frt, foci="1"),
            })  # The add(...) helper populates correct home_account_id for future searching

    def test_unknown_orphan_app_will_attempt_frt_and_not_remove_it(self):
        app = ClientApplication(
            "unknown_orphan", authority=self.authority_url, token_cache=self.cache)
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        def tester(url, data=None, **kwargs):
            self.assertEqual(self.frt, data.get("refresh_token"), "Should attempt the FRT")
            return Mock(status_code=200, json=Mock(return_value={
                "error": "invalid_grant",
                "error_description": "Was issued to another client"}))
        app._acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
            self.authority, self.scopes, self.account, post=tester)
        self.assertNotEqual([], app.token_cache.find(
            msal.TokenCache.CredentialType.REFRESH_TOKEN, query={"secret": self.frt}),
            "The FRT should not be removed from the cache")

    def test_known_orphan_app_will_skip_frt_and_only_use_its_own_rt(self):
        app = ClientApplication(
            "known_orphan", authority=self.authority_url, token_cache=self.cache)
        rt = "RT for this orphan app. We will check it being used by this test case."
        self.cache.add({  # Populate its RT and AppMetadata, so it becomes a known orphan app
            "client_id": app.client_id,
            "scope": self.scopes,
            "token_endpoint": "{}/oauth2/v2.0/token".format(self.authority_url),
            "response": TokenCacheTestCase.build_response(
                uid=self.uid, utid=self.utid, refresh_token=rt),
            })
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        def tester(url, data=None, **kwargs):
            self.assertEqual(rt, data.get("refresh_token"), "Should attempt the RT")
            return Mock(status_code=200, json=Mock(return_value={}))
        app._acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
            self.authority, self.scopes, self.account, post=tester)

    def test_unknown_family_app_will_attempt_frt_and_join_family(self):
        def tester(url, data=None, **kwargs):
            self.assertEqual(
                self.frt, data.get("refresh_token"), "Should attempt the FRT")
            return Mock(
                status_code=200,
                json=Mock(return_value=TokenCacheTestCase.build_response(
                    uid=self.uid, utid=self.utid, foci="1", access_token="at")))
        app = ClientApplication(
            "unknown_family_app", authority=self.authority_url, token_cache=self.cache)
        at = app._acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
            self.authority, self.scopes, self.account, post=tester)
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        self.assertEqual("at", at.get("access_token"), "New app should get a new AT")
        app_metadata = app.token_cache.find(
            msal.TokenCache.CredentialType.APP_METADATA,
            query={"client_id": app.client_id})
        self.assertNotEqual([], app_metadata, "Should record new app's metadata")
        self.assertEqual("1", app_metadata[0].get("family_id"),
            "The new family app should be recorded as in the same family")
    # Known family app will simply use FRT, which is largely the same as this one

    # Will not test scenario of app leaving family. Per specs, it won't happen.

    def test_family_app_remove_account(self):
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        app = ClientApplication(
            self.preexisting_family_app_id,
            authority=self.authority_url, token_cache=self.cache)
        account = app.get_accounts()[0]
        mine = {"home_account_id": account["home_account_id"]}

        self.assertNotEqual([], self.cache.find(
            self.cache.CredentialType.ACCESS_TOKEN, query=mine))
        self.assertNotEqual([], self.cache.find(
            self.cache.CredentialType.REFRESH_TOKEN, query=mine))
        self.assertNotEqual([], self.cache.find(
            self.cache.CredentialType.ID_TOKEN, query=mine))
        self.assertNotEqual([], self.cache.find(
            self.cache.CredentialType.ACCOUNT, query=mine))

        app.remove_account(account)

        self.assertEqual([], self.cache.find(
            self.cache.CredentialType.ACCESS_TOKEN, query=mine))
        self.assertEqual([], self.cache.find(
            self.cache.CredentialType.REFRESH_TOKEN, query=mine))
        self.assertEqual([], self.cache.find(
            self.cache.CredentialType.ID_TOKEN, query=mine))
        self.assertEqual([], self.cache.find(
            self.cache.CredentialType.ACCOUNT, query=mine))


class TestClientApplicationForAuthorityMigration(unittest.TestCase):

    @classmethod
    def setUp(self):
        self.environment_in_cache = "sts.windows.net"
        self.authority_url_in_app = "https://login.microsoftonline.com/common"
        self.scopes = ["s1", "s2"]
        uid = "uid"
        utid = "utid"
        self.account = {"home_account_id": "{}.{}".format(uid, utid)}
        self.client_id = "my_app"
        self.access_token = "access token for testing authority aliases"
        self.cache = msal.SerializableTokenCache()
        self.cache.add({
            "client_id": self.client_id,
            "scope": self.scopes,
            "token_endpoint": "https://{}/common/oauth2/v2.0/token".format(
                self.environment_in_cache),
            "response": TokenCacheTestCase.build_response(
                uid=uid, utid=utid,
                access_token=self.access_token, refresh_token="some refresh token"),
        })  # The add(...) helper populates correct home_account_id for future searching

    def test_get_accounts(self):
        app = ClientApplication(
            self.client_id,
            authority=self.authority_url_in_app, token_cache=self.cache)
        accounts = app.get_accounts()
        self.assertNotEqual([], accounts)
        self.assertEqual(self.environment_in_cache, accounts[0].get("environment"),
            "We should be able to find an account under an authority alias")

    def test_acquire_token_silent(self):
        app = ClientApplication(
            self.client_id,
            authority=self.authority_url_in_app, token_cache=self.cache)
        at = app.acquire_token_silent(self.scopes, self.account)
        self.assertNotEqual(None, at)
        self.assertEqual(self.access_token, at.get('access_token'))

