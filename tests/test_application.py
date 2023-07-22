# Note: Since Aug 2019 we move all e2e tests into test_e2e.py,
# so this test_application file contains only unit tests without dependency.
import sys
from msal.application import *
from msal.application import _str2bytes
import msal
from msal.application import _merge_claims_challenge_and_capabilities
from tests import unittest
from tests.test_token_cache import build_id_token, build_response
from tests.http_client import MinimalHttpClient, MinimalResponse
from msal.telemetry import CLIENT_CURRENT_TELEMETRY, CLIENT_LAST_TELEMETRY


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


class TestBytesConversion(unittest.TestCase):
    def test_string_to_bytes(self):
        self.assertEqual(type(_str2bytes("some string")), type(b"bytes"))

    def test_bytes_to_bytes(self):
        self.assertEqual(type(_str2bytes(b"some bytes")), type(b"bytes"))


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
            "response": build_response(
                access_token="an expired AT to trigger refresh", expires_in=-99,
                uid=self.uid, utid=self.utid, refresh_token=self.rt),
            })  # The add(...) helper populates correct home_account_id for future searching
        self.app = ClientApplication(
            self.client_id, authority=self.authority_url, token_cache=self.cache)

    def test_cache_empty_will_be_returned_as_None(self):
        self.app.token_cache = msal.SerializableTokenCache()  # Reset it to empty
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
            "response": build_response(
                access_token="Siblings won't share AT. test_remove_account() will.",
                id_token=build_id_token(aud=self.preexisting_family_app_id),
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
            "response": build_response(uid=self.uid, utid=self.utid, refresh_token=rt),
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
                status_code=200, text=json.dumps(build_response(
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

    def test_preexisting_family_app_will_attempt_frt_and_return_error(self):
        error_response = '{"error": "invalid_grant", "error_description": "xyz"}'
        def tester(url, data=None, **kwargs):
            self.assertEqual(
                self.frt, data.get("refresh_token"), "Should attempt the FRT")
            return MinimalResponse(status_code=400, text=error_response)
        app = ClientApplication(
            "preexisting_family_app", authority=self.authority_url, token_cache=self.cache)
        resp = app._acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
            self.authority, self.scopes, self.account, post=tester)
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        self.assertEqual(json.loads(error_response), resp, "Error raised will be returned")

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
            "response": build_response(
                uid=uid, utid=utid,
                access_token=self.access_token, refresh_token="some refresh token"),
        })  # The add(...) helper populates correct home_account_id for future searching
        self.app = ClientApplication(
            self.client_id,
            authority=self.authority_url_in_app, token_cache=self.cache)

    def test_get_accounts_should_find_accounts_under_different_alias(self):
        accounts = self.app.get_accounts()
        self.assertNotEqual([], accounts)
        self.assertEqual(self.environment_in_cache, accounts[0].get("environment"),
            "We should be able to find an account under an authority alias")

    def test_acquire_token_silent_should_find_at_under_different_alias(self):
        result = self.app.acquire_token_silent(self.scopes, self.account)
        self.assertNotEqual(None, result)
        self.assertEqual(self.access_token, result.get('access_token'))

    def test_acquire_token_silent_should_find_rt_under_different_alias(self):
        self.cache._cache["AccessToken"] = {}  # A hacky way to clear ATs
        class ExpectedBehavior(Exception):
            pass
        def helper(scopes, account, authority, *args, **kwargs):
            if authority.instance == self.environment_in_cache:
                raise ExpectedBehavior("RT of different alias being attempted")
        self.app._acquire_token_silent_from_cache_and_possibly_refresh_it = helper

        with self.assertRaises(ExpectedBehavior):
            self.app.acquire_token_silent(["different scope"], self.account)


class TestApplicationForClientCapabilities(unittest.TestCase):

    def test_capabilities_and_id_token_claims_merge(self):
        client_capabilities = ["foo", "bar"]
        claims_challenge = '''{"id_token": {"auth_time": {"essential": true}}}'''
        merged_claims = '''{"id_token": {"auth_time": {"essential": true}},
                        "access_token": {"xms_cc": {"values": ["foo", "bar"]}}}'''
        # Comparing  dictionaries as JSON object order differs based on python version
        self.assertEqual(
            json.loads(merged_claims),
            json.loads(_merge_claims_challenge_and_capabilities(
                client_capabilities, claims_challenge)))

    def test_capabilities_and_id_token_claims_and_access_token_claims_merge(self):
        client_capabilities = ["foo", "bar"]
        claims_challenge = '''{"id_token": {"auth_time": {"essential": true}},
                 "access_token": {"nbf":{"essential":true, "value":"1563308371"}}}'''
        merged_claims = '''{"id_token": {"auth_time": {"essential": true}},
                        "access_token": {"nbf": {"essential": true, "value": "1563308371"},
                                        "xms_cc": {"values": ["foo", "bar"]}}}'''
        # Comparing  dictionaries as JSON object order differs based on python version
        self.assertEqual(
            json.loads(merged_claims),
            json.loads(_merge_claims_challenge_and_capabilities(
                client_capabilities, claims_challenge)))

    def test_no_capabilities_only_claims_merge(self):
        claims_challenge = '''{"id_token": {"auth_time": {"essential": true}}}'''
        self.assertEqual(
            json.loads(claims_challenge),
            json.loads(_merge_claims_challenge_and_capabilities(None, claims_challenge)))

    def test_only_client_capabilities_no_claims_merge(self):
        client_capabilities = ["foo", "bar"]
        merged_claims = '''{"access_token": {"xms_cc": {"values": ["foo", "bar"]}}}'''
        self.assertEqual(
            json.loads(merged_claims),
            json.loads(_merge_claims_challenge_and_capabilities(client_capabilities, None)))

    def test_both_claims_and_capabilities_none(self):
        self.assertEqual(_merge_claims_challenge_and_capabilities(None, None), None)


class TestApplicationForRefreshInBehaviors(unittest.TestCase):
    """The following test cases were based on design doc here
    https://identitydivision.visualstudio.com/DevEx/_git/AuthLibrariesApiReview?path=%2FRefreshAtExpirationPercentage%2Foverview.md&version=GBdev&_a=preview&anchor=scenarios
    """
    authority_url = "https://login.microsoftonline.com/common"
    scopes = ["s1", "s2"]
    uid = "my_uid"
    utid = "my_utid"
    account = {"home_account_id": "{}.{}".format(uid, utid)}
    rt = "this is a rt"
    client_id = "my_app"

    @classmethod
    def setUpClass(cls):  # Initialization at runtime, not interpret-time
        cls.app = ClientApplication(cls.client_id, authority=cls.authority_url)

    def setUp(self):
        self.app.token_cache = self.cache = msal.SerializableTokenCache()

    def populate_cache(self, access_token="at", expires_in=86400, refresh_in=43200):
        self.cache.add({
            "client_id": self.client_id,
            "scope": self.scopes,
            "token_endpoint": "{}/oauth2/v2.0/token".format(self.authority_url),
            "response": build_response(
                access_token=access_token,
                expires_in=expires_in, refresh_in=refresh_in,
                uid=self.uid, utid=self.utid, refresh_token=self.rt),
            })

    def test_fresh_token_should_be_returned_from_cache(self):
        # a.k.a. Return unexpired token that is not above token refresh expiration threshold
        access_token = "An access token prepopulated into cache"
        self.populate_cache(access_token=access_token, expires_in=900, refresh_in=450)
        result = self.app.acquire_token_silent(
            ['s1'], self.account,
            post=lambda url, *args, **kwargs:  # Utilize the undocumented test feature
                self.fail("I/O shouldn't happen in cache hit AT scenario")
            )
        self.assertEqual(access_token, result.get("access_token"))
        self.assertNotIn("refresh_in", result, "Customers need not know refresh_in")

    def test_aging_token_and_available_aad_should_return_new_token(self):
        # a.k.a. Attempt to refresh unexpired token when AAD available
        self.populate_cache(access_token="old AT", expires_in=3599, refresh_in=-1)
        new_access_token = "new AT"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,4|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": new_access_token,
                "refresh_in": 123,
                }))
        result = self.app.acquire_token_silent(['s1'], self.account, post=mock_post)
        self.assertEqual(new_access_token, result.get("access_token"))
        self.assertNotIn("refresh_in", result, "Customers need not know refresh_in")

    def test_aging_token_and_unavailable_aad_should_return_old_token(self):
        # a.k.a. Attempt refresh unexpired token when AAD unavailable
        old_at = "old AT"
        self.populate_cache(access_token=old_at, expires_in=3599, refresh_in=-1)
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,4|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=400, text=json.dumps({"error": "foo"}))
        result = self.app.acquire_token_silent(['s1'], self.account, post=mock_post)
        self.assertEqual(old_at, result.get("access_token"))

    def test_expired_token_and_unavailable_aad_should_return_error(self):
        # a.k.a. Attempt refresh expired token when AAD unavailable
        self.populate_cache(access_token="expired at", expires_in=-1, refresh_in=-900)
        error = "something went wrong"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,3|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=400, text=json.dumps({"error": error}))
        result = self.app.acquire_token_silent_with_error(
            ['s1'], self.account, post=mock_post)
        self.assertEqual(error, result.get("error"), "Error should be returned")

    def test_expired_token_and_available_aad_should_return_new_token(self):
        # a.k.a. Attempt refresh expired token when AAD available
        self.populate_cache(access_token="expired at", expires_in=-1, refresh_in=-900)
        new_access_token = "new AT"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,3|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": new_access_token,
                "refresh_in": 123,
                }))
        result = self.app.acquire_token_silent(['s1'], self.account, post=mock_post)
        self.assertEqual(new_access_token, result.get("access_token"))
        self.assertNotIn("refresh_in", result, "Customers need not know refresh_in")


class TestTelemetryMaintainingOfflineState(unittest.TestCase):
    authority_url = "https://login.microsoftonline.com/common"
    scopes = ["s1", "s2"]
    uid = "my_uid"
    utid = "my_utid"
    account = {"home_account_id": "{}.{}".format(uid, utid)}
    rt = "this is a rt"
    client_id = "my_app"

    def populate_cache(self, cache, access_token="at"):
        cache.add({
            "client_id": self.client_id,
            "scope": self.scopes,
            "token_endpoint": "{}/oauth2/v2.0/token".format(self.authority_url),
            "response": build_response(
                access_token=access_token,
                uid=self.uid, utid=self.utid, refresh_token=self.rt),
            })

    def test_maintaining_offline_state_and_sending_them(self):
        app = PublicClientApplication(
            self.client_id,
            authority=self.authority_url, token_cache=msal.SerializableTokenCache())
        cached_access_token = "cached_at"
        self.populate_cache(app.token_cache, access_token=cached_access_token)

        result = app.acquire_token_silent(
            self.scopes, self.account,
            post=lambda url, *args, **kwargs:  # Utilize the undocumented test feature
                self.fail("I/O shouldn't happen in cache hit AT scenario")
            )
        self.assertEqual(cached_access_token, result.get("access_token"))

        error1 = "error_1"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|622,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            self.assertEqual("4|1|||", (headers or {}).get(CLIENT_LAST_TELEMETRY),
                "The previous cache hit should result in success counter value as 1")
            return MinimalResponse(status_code=400, text=json.dumps({"error": error1}))
        result = app.acquire_token_by_device_flow({  # It allows customizing correlation_id
            "device_code": "123",
            PublicClientApplication.DEVICE_FLOW_CORRELATION_ID: "id_1",
            }, post=mock_post)
        self.assertEqual(error1, result.get("error"))

        error2 = "error_2"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|622,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            self.assertEqual("4|1|622,id_1|error_1|", (headers or {}).get(CLIENT_LAST_TELEMETRY),
                "The previous error should result in same success counter plus latest error info")
            return MinimalResponse(status_code=400, text=json.dumps({"error": error2}))
        result = app.acquire_token_by_device_flow({
            "device_code": "123",
            PublicClientApplication.DEVICE_FLOW_CORRELATION_ID: "id_2",
            }, post=mock_post)
        self.assertEqual(error2, result.get("error"))

        at = "ensures the successful path (which includes the mock) been used"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|622,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            self.assertEqual("4|1|622,id_1,622,id_2|error_1,error_2|", (headers or {}).get(CLIENT_LAST_TELEMETRY),
                "The previous error should result in same success counter plus latest error info")
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = app.acquire_token_by_device_flow({"device_code": "123"}, post=mock_post)
        self.assertEqual(at, result.get("access_token"))

        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|622,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            self.assertEqual("4|0|||", (headers or {}).get(CLIENT_LAST_TELEMETRY),
                "The previous success should reset all offline telemetry counters")
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = app.acquire_token_by_device_flow({"device_code": "123"}, post=mock_post)
        self.assertEqual(at, result.get("access_token"))


class TestTelemetryOnClientApplication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # Initialization at runtime, not interpret-time
        cls.app = ClientApplication(
            "client_id", authority="https://login.microsoftonline.com/common")

    def test_acquire_token_by_auth_code_flow(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|832,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        state = "foo"
        result = self.app.acquire_token_by_auth_code_flow(
            {"state": state, "code_verifier": "bar"}, {"state": state, "code": "012"},
            post=mock_post)
        self.assertEqual(at, result.get("access_token"))

    def test_acquire_token_by_refresh_token(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|85,1|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_by_refresh_token("rt", ["s"], post=mock_post)
        self.assertEqual(at, result.get("access_token"))


class TestTelemetryOnPublicClientApplication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # Initialization at runtime, not interpret-time
        cls.app = PublicClientApplication(
            "client_id", authority="https://login.microsoftonline.com/common")

    # For now, acquire_token_interactive() is verified by code review.

    def test_acquire_token_by_device_flow(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|622,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_by_device_flow(
            {"device_code": "123"}, post=mock_post)
        self.assertEqual(at, result.get("access_token"))

    def test_acquire_token_by_username_password(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|301,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_by_username_password(
            "username", "password", ["scope"], post=mock_post)
        self.assertEqual(at, result.get("access_token"))


class TestTelemetryOnConfidentialClientApplication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # Initialization at runtime, not interpret-time
        cls.app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/common")

    def test_acquire_token_for_client(self):
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|730,2|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "AT 1",
                "expires_in": 0,
                }))
        result = self.app.acquire_token_for_client(["scope"], post=mock_post)
        self.assertEqual("AT 1", result.get("access_token"), "Shall get a new token")

        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|730,3|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "AT 2",
                "expires_in": 3600,
                "refresh_in": -100,  # A hack to make sure it will attempt refresh
                }))
        result = self.app.acquire_token_for_client(["scope"], post=mock_post)
        self.assertEqual("AT 2", result.get("access_token"), "Shall get a new token")

        def mock_post(url, headers=None, *args, **kwargs):
            # 1/0  # TODO: Make sure this was called
            self.assertEqual("4|730,4|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=400, text=json.dumps({"error": "foo"}))
        result = self.app.acquire_token_for_client(["scope"], post=mock_post)
        self.assertEqual("AT 2", result.get("access_token"), "Shall get aging token")

    def test_acquire_token_on_behalf_of(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|523,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_on_behalf_of("assertion", ["s"], post=mock_post)
        self.assertEqual(at, result.get("access_token"))


class TestClientApplicationWillGroupAccounts(unittest.TestCase):
    def test_get_accounts(self):
        client_id = "my_app"
        scopes = ["scope_1", "scope_2"]
        environment = "login.microsoftonline.com"
        uid = "home_oid"
        utid = "home_tenant_guid"
        username = "Jane Doe"
        cache = msal.SerializableTokenCache()
        for tenant in ["contoso", "fabrikam"]:
            cache.add({
                "client_id": client_id,
                "scope": scopes,
                "token_endpoint":
                    "https://{}/{}/oauth2/v2.0/token".format(environment, tenant),
                "response": build_response(
                    uid=uid, utid=utid, access_token="at", refresh_token="rt",
                    id_token=build_id_token(
                        aud=client_id,
                        sub="oid_in_" + tenant,
                        preferred_username=username,
                        ),
                    ),
                })
        app = ClientApplication(
            client_id,
            authority="https://{}/common".format(environment),
            token_cache=cache)
        accounts = app.get_accounts()
        self.assertEqual(1, len(accounts), "Should return one grouped account")
        account = accounts[0]
        self.assertEqual("{}.{}".format(uid, utid), account["home_account_id"])
        self.assertEqual(environment, account["environment"])
        self.assertEqual(username, account["username"])
        self.assertIn("authority_type", account, "Backward compatibility")
        self.assertIn("local_account_id", account, "Backward compatibility")
        self.assertIn("realm", account, "Backward compatibility")


@unittest.skipUnless(
    sys.version_info[0] >= 3 and sys.version_info[1] >= 2,
    "assertWarns() is only available in Python 3.2+")
class TestClientCredentialGrant(unittest.TestCase):
    def _test_certain_authority_should_emit_warnning(self, authority):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret", authority=authority)
        def mock_post(url, headers=None, *args, **kwargs):
            return MinimalResponse(
                status_code=200, text=json.dumps({"access_token": "an AT"}))
        with self.assertWarns(DeprecationWarning):
            app.acquire_token_for_client(["scope"], post=mock_post)

    def test_common_authority_should_emit_warnning(self):
        self._test_certain_authority_should_emit_warnning(
            authority="https://login.microsoftonline.com/common")

    def test_organizations_authority_should_emit_warnning(self):
        self._test_certain_authority_should_emit_warnning(
            authority="https://login.microsoftonline.com/organizations")


class TestScopeDecoration(unittest.TestCase):
    def _test_client_id_should_be_a_valid_scope(self, client_id, other_scopes):
        # B2C needs this https://learn.microsoft.com/en-us/azure/active-directory-b2c/access-tokens#openid-connect-scopes
        reserved_scope = ['openid', 'profile', 'offline_access']
        scopes_to_use = [client_id] + other_scopes
        self.assertEqual(
            set(ClientApplication(client_id)._decorate_scope(scopes_to_use)),
            set(scopes_to_use + reserved_scope),
            "Scope decoration should return input scopes plus reserved scopes")

    def test_client_id_should_be_a_valid_scope(self):
        self._test_client_id_should_be_a_valid_scope("client_id", [])
        self._test_client_id_should_be_a_valid_scope("client_id", ["foo"])

