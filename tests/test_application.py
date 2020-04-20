# Note: Since Aug 2019 we move all e2e tests into test_e2e.py,
# so this test_application file contains only unit tests without dependency.
from msal.application import *
import msal
from tests import unittest
from tests.test_token_cache import TokenCacheTestCase
from tests.http_client import MinimalHttpClient, MinimalResponse


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class TestHelperExtractCerts(unittest.TestCase):  # It is used by SNI scenario

    def test_extract_a_tag_less_public_cert(self):
        pem = "my_cert"
        self.assertEqual(["my_cert"], extract_certs(pem))

    def test_extract_a_tag_enclosed_cert(self):
        pem = """
        -----BEGIN CERTIFICATE-----
        my_cert
        -----END CERTIFICATE-----
        """
        self.assertEqual(["my_cert"], extract_certs(pem))

    def test_extract_multiple_tag_enclosed_certs(self):
        pem = """
        -----BEGIN CERTIFICATE-----
        my_cert1
        -----END CERTIFICATE-----

        -----BEGIN CERTIFICATE-----
        my_cert2
        -----END CERTIFICATE-----
        """
        self.assertEqual(["my_cert1", "my_cert2"], extract_certs(pem))


class TestClientApplicationAcquireTokenSilentErrorBehaviors(unittest.TestCase):

    def setUp(self):
        self.authority_url = "https://login.microsoftonline.com/common"
        self.authority = msal.authority.Authority(
            self.authority_url, MinimalHttpClient())
        self.scopes = ["s1", "s2"]
        self.uid = "my_uid"
        self.utid = "my_utid"
        self.account = {"home_account_id": "{}.{}".format(self.uid, self.utid)}
        self.rt = "this is a rt"
        self.cache = msal.SerializableTokenCache()
        self.client_id = "my_app"
        self.cache.add({  # Pre-populate the cache
            "client_id": self.client_id,
            "scope": self.scopes,
            "token_endpoint": "{}/oauth2/v2.0/token".format(self.authority_url),
            "response": TokenCacheTestCase.build_response(
                access_token="an expired AT to trigger refresh", expires_in=-99,
                uid=self.uid, utid=self.utid, refresh_token=self.rt),
            })  # The add(...) helper populates correct home_account_id for future searching
        self.app = ClientApplication(
            self.client_id, authority=self.authority_url, token_cache=self.cache)

    def test_cache_empty_will_be_returned_as_None(self):
        self.assertEqual(
            None, self.app.acquire_token_silent(['cache_miss'], self.account))
        self.assertEqual(
            None, self.app.acquire_token_silent_with_error(['cache_miss'], self.account))

    def test_acquire_token_silent_will_suppress_error(self):
        error_response = '{"error": "invalid_grant", "suberror": "xyz"}'
        def tester(url, **kwargs):
            return MinimalResponse(status_code=400, text=error_response)
        self.assertEqual(None, self.app.acquire_token_silent(
            self.scopes, self.account, post=tester))

    def test_acquire_token_silent_with_error_will_return_error(self):
        error_response = '{"error": "invalid_grant", "error_description": "xyz"}'
        def tester(url, **kwargs):
            return MinimalResponse(status_code=400, text=error_response)
        self.assertEqual(json.loads(error_response), self.app.acquire_token_silent_with_error(
            self.scopes, self.account, post=tester))

    def test_atswe_will_map_some_suberror_to_classification_as_is(self):
        error_response = '{"error": "invalid_grant", "suberror": "basic_action"}'
        def tester(url, **kwargs):
            return MinimalResponse(status_code=400, text=error_response)
        result = self.app.acquire_token_silent_with_error(
            self.scopes, self.account, post=tester)
        self.assertEqual("basic_action", result.get("classification"))

    def test_atswe_will_map_some_suberror_to_classification_to_empty_string(self):
        error_response = '{"error": "invalid_grant", "suberror": "client_mismatch"}'
        def tester(url, **kwargs):
            return MinimalResponse(status_code=400, text=error_response)
        result = self.app.acquire_token_silent_with_error(
            self.scopes, self.account, post=tester)
        self.assertEqual("", result.get("classification"))

class TestClientApplicationAcquireTokenSilentFociBehaviors(unittest.TestCase):

    def setUp(self):
        self.authority_url = "https://login.microsoftonline.com/common"
        self.authority = msal.authority.Authority(
            self.authority_url, MinimalHttpClient())
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
                id_token=TokenCacheTestCase.build_id_token(aud=self.preexisting_family_app_id),
                uid=self.uid, utid=self.utid, refresh_token=self.frt, foci="1"),
            })  # The add(...) helper populates correct home_account_id for future searching

    def test_unknown_orphan_app_will_attempt_frt_and_not_remove_it(self):
        app = ClientApplication(
            "unknown_orphan", authority=self.authority_url, token_cache=self.cache)
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        error_response = '{"error": "invalid_grant","error_description": "Was issued to another client"}'
        def tester(url, data=None, **kwargs):
            self.assertEqual(self.frt, data.get("refresh_token"), "Should attempt the FRT")
            return MinimalResponse(status_code=400, text=error_response)
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
            return MinimalResponse(status_code=200, text='{}')
        app._acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
            self.authority, self.scopes, self.account, post=tester)

    def test_unknown_family_app_will_attempt_frt_and_join_family(self):
        def tester(url, data=None, **kwargs):
            self.assertEqual(
                self.frt, data.get("refresh_token"), "Should attempt the FRT")
            return MinimalResponse(
                status_code=200, text=json.dumps(TokenCacheTestCase.build_response(
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

