# Note: Since Aug 2019 we move all e2e tests into test_e2e.py,
# so this test_application file contains only unit tests without dependency.
import base64
import json
import logging
import sys
import time
import warnings
from unittest.mock import patch, Mock
import msal
from msal.application import (
    extract_certs,
    ClientApplication, PublicClientApplication, ConfidentialClientApplication,
    _str2bytes, _merge_claims_challenge_and_capabilities,
)
from tests import unittest
from tests.test_token_cache import build_id_token, build_response
from tests.http_client import MinimalHttpClient, MinimalResponse
from msal.telemetry import CLIENT_CURRENT_TELEMETRY, CLIENT_LAST_TELEMETRY


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

_OIDC_DISCOVERY = "msal.authority.tenant_discovery"
_OIDC_DISCOVERY_MOCK = Mock(return_value={
    "authorization_endpoint": "https://contoso.com/placeholder",
    "token_endpoint": "https://contoso.com/placeholder",
    "issuer": "https://contoso.com/tenant",
})


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

    @patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
    def setUp(self):
        self.authority_url = "https://login.microsoftonline.com/common"
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


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestClientApplicationAcquireTokenSilentFociBehaviors(unittest.TestCase):

    def setUp(self):
        self.authority_url = "https://login.microsoftonline.com/common"
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
            app.authority, self.scopes, self.account, post=tester)
        self.assertIsNotNone(next(app.token_cache.search(
            msal.TokenCache.CredentialType.REFRESH_TOKEN, query={"secret": self.frt}), None),
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
            app.authority, self.scopes, self.account, post=tester)

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
            app.authority, self.scopes, self.account, post=tester)
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        self.assertEqual("at", at.get("access_token"), "New app should get a new AT")
        app_metadata = next(app.token_cache.search(
            msal.TokenCache.CredentialType.APP_METADATA,
            query={"client_id": app.client_id}), None)
        self.assertIsNotNone(app_metadata, "Should record new app's metadata")
        self.assertEqual("1", app_metadata.get("family_id"),
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
            app.authority, self.scopes, self.account, post=tester)
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        self.assertEqual(json.loads(error_response), resp, "Error raised will be returned")

    def test_family_app_remove_account(self):
        logger.debug("%s.cache = %s", self.id(), self.cache.serialize())
        app = ClientApplication(
            self.preexisting_family_app_id,
            authority=self.authority_url, token_cache=self.cache)
        account = app.get_accounts()[0]
        mine = {"home_account_id": account["home_account_id"]}

        self.assertIsNotNone(next(self.cache.search(
            self.cache.CredentialType.ACCESS_TOKEN, query=mine), None))
        self.assertIsNotNone(next(self.cache.search(
            self.cache.CredentialType.REFRESH_TOKEN, query=mine), None))
        self.assertIsNotNone(next(self.cache.search(
            self.cache.CredentialType.ID_TOKEN, query=mine), None))
        self.assertIsNotNone(next(self.cache.search(
            self.cache.CredentialType.ACCOUNT, query=mine), None))

        app.remove_account(account)

        self.assertIsNone(next(self.cache.search(
            self.cache.CredentialType.ACCESS_TOKEN, query=mine), None))
        self.assertIsNone(next(self.cache.search(
            self.cache.CredentialType.REFRESH_TOKEN, query=mine), None))
        self.assertIsNone(next(self.cache.search(
            self.cache.CredentialType.ID_TOKEN, query=mine), None))
        self.assertIsNone(next(self.cache.search(
            self.cache.CredentialType.ACCOUNT, query=mine), None))


class TestClientApplicationForAuthorityMigration(unittest.TestCase):

    # Chose to not mock oidc discovery, because AuthorityMigration might rely on real data
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
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_CACHE)
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
    soon = 60  # application.py considers tokens within 5 minutes as expired

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

    def assertRefreshOn(self, result, refresh_in):
        refresh_on = int(time.time() + refresh_in)
        self.assertTrue(
            refresh_on - 1 < result.get("refresh_on", 0) < refresh_on + 1,
            "refresh_on should be set properly")

    def test_fresh_token_should_be_returned_from_cache(self):
        # a.k.a. Return unexpired token that is not above token refresh expiration threshold
        refresh_in = 450
        access_token = "An access token prepopulated into cache"
        self.populate_cache(
            access_token=access_token, expires_in=900, refresh_in=refresh_in)
        result = self.app.acquire_token_silent(
            ['s1'], self.account,
            post=lambda url, *args, **kwargs:  # Utilize the undocumented test feature
                self.fail("I/O shouldn't happen in cache hit AT scenario")
            )
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_CACHE)
        self.assertEqual(access_token, result.get("access_token"))
        self.assertNotIn("refresh_in", result, "Customers need not know refresh_in")
        self.assertRefreshOn(result, refresh_in)

    def test_aging_token_and_available_aad_should_return_new_token(self):
        # a.k.a. Attempt to refresh unexpired token when AAD available
        self.populate_cache(access_token="old AT", expires_in=3599, refresh_in=-1)
        new_access_token = "new AT"
        new_refresh_in = 123
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,4|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": new_access_token,
                "refresh_in": new_refresh_in,
                }))
        result = self.app.acquire_token_silent(['s1'], self.account, post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(new_access_token, result.get("access_token"))
        self.assertNotIn("refresh_in", result, "Customers need not know refresh_in")
        self.assertRefreshOn(result, new_refresh_in)

    def test_aging_token_and_unavailable_aad_should_return_old_token(self):
        # a.k.a. Attempt refresh unexpired token when AAD unavailable
        refresh_in = -1
        old_at = "old AT"
        self.populate_cache(
            access_token=old_at, expires_in=3599, refresh_in=refresh_in)
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,4|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=400, text=json.dumps({"error": "foo"}))
        result = self.app.acquire_token_silent(['s1'], self.account, post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_CACHE)
        self.assertEqual(old_at, result.get("access_token"))
        self.assertRefreshOn(result, refresh_in)

    def test_expired_token_and_unavailable_aad_should_return_error(self):
        # a.k.a. Attempt refresh expired token when AAD unavailable
        self.populate_cache(
            access_token="expired at", expires_in=self.soon, refresh_in=-900)
        error = "something went wrong"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,3|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=400, text=json.dumps({"error": error}))
        result = self.app.acquire_token_silent_with_error(
            ['s1'], self.account, post=mock_post)
        self.assertEqual(error, result.get("error"), "Error should be returned")

    def test_expired_token_and_available_aad_should_return_new_token(self):
        # a.k.a. Attempt refresh expired token when AAD available
        self.populate_cache(
            access_token="expired at", expires_in=self.soon, refresh_in=-900)
        new_access_token = "new AT"
        new_refresh_in = 123
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|84,3|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": new_access_token,
                "refresh_in": new_refresh_in,
                }))
        result = self.app.acquire_token_silent(['s1'], self.account, post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(new_access_token, result.get("access_token"))
        self.assertNotIn("refresh_in", result, "Customers need not know refresh_in")
        self.assertRefreshOn(result, new_refresh_in)


# TODO Patching oidc discovery ends up failing. But we plan to remove offline telemetry anyway.
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
        self.assertEqual(result[app._TOKEN_SOURCE], app._TOKEN_SOURCE_CACHE)
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
        self.assertEqual(result[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))

        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|622,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            self.assertEqual("4|0|||", (headers or {}).get(CLIENT_LAST_TELEMETRY),
                "The previous success should reset all offline telemetry counters")
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = app.acquire_token_by_device_flow({"device_code": "123"}, post=mock_post)
        self.assertEqual(result[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))


class TestTelemetryOnClientApplication(unittest.TestCase):
    @classmethod
    @patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
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
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))

    def test_acquire_token_by_refresh_token(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|85,1|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_by_refresh_token("rt", ["s"], post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))


class TestTelemetryOnPublicClientApplication(unittest.TestCase):
    @classmethod
    @patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
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
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))

    def test_acquire_token_by_username_password(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|301,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_by_username_password(
            "username", "password", ["scope"], post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))


class TestTelemetryOnConfidentialClientApplication(unittest.TestCase):
    @classmethod
    @patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
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
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual("AT 1", result.get("access_token"), "Shall get a new token")

        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|730,3|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "AT 2",
                "expires_in": 3600,
                "refresh_in": -100,  # A hack to make sure it will attempt refresh
                }))
        result = self.app.acquire_token_for_client(["scope"], post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual("AT 2", result.get("access_token"), "Shall get a new token")

        def mock_post(url, headers=None, *args, **kwargs):
            # 1/0  # TODO: Make sure this was called
            self.assertEqual("4|730,4|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=400, text=json.dumps({"error": "foo"}))
        result = self.app.acquire_token_for_client(["scope"], post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_CACHE)
        self.assertEqual("AT 2", result.get("access_token"), "Shall get aging token")

    def test_acquire_token_on_behalf_of(self):
        at = "this is an access token"
        def mock_post(url, headers=None, *args, **kwargs):
            self.assertEqual("4|523,0|", (headers or {}).get(CLIENT_CURRENT_TELEMETRY))
            return MinimalResponse(status_code=200, text=json.dumps({"access_token": at}))
        result = self.app.acquire_token_on_behalf_of("assertion", ["s"], post=mock_post)
        self.assertEqual(result[self.app._TOKEN_SOURCE], self.app._TOKEN_SOURCE_IDP)
        self.assertEqual(at, result.get("access_token"))


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
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
    def _test_certain_authority_should_emit_warning(self, authority):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret", authority=authority)
        def mock_post(url, headers=None, *args, **kwargs):
            return MinimalResponse(
                status_code=200, text=json.dumps({"access_token": "an AT"}))
        with self.assertWarns(DeprecationWarning):
            app.acquire_token_for_client(["scope"], post=mock_post)

    @patch(_OIDC_DISCOVERY, new=Mock(return_value={
        "authorization_endpoint": "https://contoso.com/common",
        "token_endpoint": "https://contoso.com/common",
        "issuer": "https://contoso.com/common",
        }))
    def test_common_authority_should_emit_warning(self):
        self._test_certain_authority_should_emit_warning(
            authority="https://login.microsoftonline.com/common")

    @patch(_OIDC_DISCOVERY, new=Mock(return_value={
        "authorization_endpoint": "https://contoso.com/organizations",
        "token_endpoint": "https://contoso.com/organizations",
        "issuer": "https://contoso.com/organizations",
        }))
    def test_organizations_authority_should_emit_warning(self):
        self._test_certain_authority_should_emit_warning(
            authority="https://login.microsoftonline.com/organizations")


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestClientAssertionCallback(unittest.TestCase):
    """Issue #746: client_credential={'client_assertion': callable} support."""

    _AUTHORITY = "https://login.microsoftonline.com/my_tenant"

    def _mock_post_capturing(self, captured):
        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured.append(dict(data or {}))
            return MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an AT", "expires_in": 3600}))
        return mock_post

    def test_callable_client_assertion_is_invoked_per_request(self):
        calls = {"n": 0}
        def assertion_cb():
            calls["n"] += 1
            return "assertion-{}".format(calls["n"])
        app = ConfidentialClientApplication(
            "client_id",
            client_credential={"client_assertion": assertion_cb},
            authority=self._AUTHORITY)
        captured = []
        app.acquire_token_for_client(
            ["s1"], post=self._mock_post_capturing(captured))
        app.acquire_token_for_client(
            ["s2"], post=self._mock_post_capturing(captured))
        self.assertEqual(2, calls["n"], "Callable should be called per request")
        self.assertEqual("assertion-1", captured[0]["client_assertion"])
        self.assertEqual("assertion-2", captured[1]["client_assertion"])
        self.assertEqual(
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            captured[0]["client_assertion_type"])

    def test_autorefresher_caches_assertion(self):
        from msal import AutoRefresher
        calls = {"n": 0}
        def assertion_cb():
            calls["n"] += 1
            return "static-assertion"
        app = ConfidentialClientApplication(
            "client_id",
            client_credential={
                "client_assertion": AutoRefresher(assertion_cb, expires_in=3600)},
            authority=self._AUTHORITY)
        captured = []
        app.acquire_token_for_client(
            ["s1"], post=self._mock_post_capturing(captured))
        app.acquire_token_for_client(
            ["s2"], post=self._mock_post_capturing(captured))
        self.assertEqual(
            1, calls["n"],
            "AutoRefresher should reuse the assertion within its lifetime")
        self.assertEqual("static-assertion", captured[0]["client_assertion"])
        self.assertEqual("static-assertion", captured[1]["client_assertion"])

    def test_string_client_assertion_still_works_for_backward_compat(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            app = ConfidentialClientApplication(
                "client_id",
                client_credential={"client_assertion": "static-jwt"},
                authority=self._AUTHORITY)
        captured = []
        result = app.acquire_token_for_client(
            ["s"], post=self._mock_post_capturing(captured))
        self.assertEqual("an AT", result.get("access_token"))
        self.assertEqual("static-jwt", captured[0]["client_assertion"])

    def test_string_client_assertion_emits_deprecation_warning(self):
        with self.assertWarns(DeprecationWarning):
            ConfidentialClientApplication(
                "client_id",
                client_credential={"client_assertion": "static-jwt"},
                authority=self._AUTHORITY)

    def test_callable_client_assertion_does_not_emit_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ConfidentialClientApplication(
                "client_id",
                client_credential={"client_assertion": lambda: "x"},
                authority=self._AUTHORITY)
        offending = [
            w for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "client_assertion" in str(w.message)]
        self.assertEqual(
            [], offending,
            "Callable client_assertion must not emit a deprecation warning")


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestAcquireTokenForClientWithFmiPath(unittest.TestCase):
    """Test that acquire_token_for_client(fmi_path=...) attaches fmi_path to HTTP body."""

    def test_fmi_path_rejects_non_string_types(self):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")
        for bad_value in [123, True, ["path"], {"path": "value"}, b"bytes"]:
            with self.assertRaises(ValueError, msg="fmi_path={!r} should raise".format(bad_value)):
                app.acquire_token_for_client(["scope"], fmi_path=bad_value)

    def test_fmi_path_is_included_in_request_body(self):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")
        fmi_path = "SomeFmiPath/FmiCredentialPath"
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an AT",
                    "expires_in": 3600,
                }))

        result = app.acquire_token_for_client(
            ["scope"], fmi_path=fmi_path, post=mock_post)
        self.assertIn("access_token", result)
        self.assertIn("fmi_path", captured_data,
            "fmi_path should be present in the HTTP request body")
        self.assertEqual(fmi_path, captured_data["fmi_path"],
            "fmi_path value should match the input")

    def test_fmi_path_coexists_with_other_data(self):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")
        fmi_path = "another/fmi/path"
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an AT",
                    "expires_in": 3600,
                }))

        result = app.acquire_token_for_client(
            ["scope"], fmi_path=fmi_path, post=mock_post)
        self.assertIn("access_token", result)
        self.assertEqual(fmi_path, captured_data["fmi_path"])
        self.assertEqual("client_credentials", captured_data.get("grant_type"))

    def test_fmi_path_preserves_existing_data_params(self):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")
        fmi_path = "my/fmi/path"
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an AT",
                    "expires_in": 3600,
                }))

        result = app.acquire_token_for_client(
            ["scope"], fmi_path=fmi_path,
            data={"extra_key": "extra_value"},
            post=mock_post)
        self.assertIn("access_token", result)
        self.assertEqual(fmi_path, captured_data["fmi_path"])
        self.assertEqual("extra_value", captured_data.get("extra_key"),
            "Pre-existing data params should be preserved")

    def test_cached_token_is_returned_on_second_call(self):
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")
        fmi_path = "SomeFmiPath/FmiCredentialPath"
        call_count = [0]

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            call_count[0] += 1
            return MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an AT",
                    "expires_in": 3600,
                }))

        result1 = app.acquire_token_for_client(
            ["scope"], fmi_path=fmi_path, post=mock_post)
        self.assertIn("access_token", result1)
        self.assertEqual(result1[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP)

        result2 = app.acquire_token_for_client(
            ["scope"], fmi_path=fmi_path, post=mock_post)
        self.assertIn("access_token", result2)
        self.assertEqual(result2[app._TOKEN_SOURCE], app._TOKEN_SOURCE_CACHE,
            "Second call should return token from cache")

    def test_different_fmi_paths_are_cached_separately(self):
        """Tokens acquired with different fmi_path values must NOT share cache entries."""
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")

        def mock_post_factory(token_value):
            def mock_post(url, headers=None, data=None, *args, **kwargs):
                return MinimalResponse(
                    status_code=200, text=json.dumps({
                        "access_token": token_value,
                        "expires_in": 3600,
                    }))
            return mock_post

        # Acquire token with path A
        result_a = app.acquire_token_for_client(
            ["scope"], fmi_path="PathA/credential", post=mock_post_factory("AT_for_path_A"))
        self.assertEqual("AT_for_path_A", result_a["access_token"])

        # Acquire token with path B (should NOT get path A's cached token)
        result_b = app.acquire_token_for_client(
            ["scope"], fmi_path="PathB/credential", post=mock_post_factory("AT_for_path_B"))
        self.assertEqual("AT_for_path_B", result_b["access_token"])
        self.assertEqual(result_b[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP,
            "Different FMI path should NOT return a cached token from another path")

        # Verify path A still returns its own cached token
        result_a2 = app.acquire_token_for_client(
            ["scope"], fmi_path="PathA/credential", post=mock_post_factory("should_not_be_used"))
        self.assertEqual("AT_for_path_A", result_a2["access_token"])
        self.assertEqual(result_a2[app._TOKEN_SOURCE], app._TOKEN_SOURCE_CACHE,
            "Same FMI path should return cached token")

    def test_fmi_token_does_not_interfere_with_non_fmi_token(self):
        """FMI-cached tokens must not be returned for non-FMI acquire_token_for_client."""
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")

        # First, cache a token via FMI path
        app.acquire_token_for_client(
            ["scope"], fmi_path="some/fmi/path",
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "FMI_AT", "expires_in": 3600})))

        # Now call regular acquire_token_for_client — should NOT get FMI token
        result = app.acquire_token_for_client(
            ["scope"],
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "regular_AT", "expires_in": 3600})))
        self.assertEqual("regular_AT", result["access_token"])
        self.assertEqual(result[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP,
            "Non-FMI call should not return FMI-cached token")


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestRemoveTokensForClient(unittest.TestCase):
    def test_remove_tokens_for_client_should_remove_client_tokens_only(self):
        at_for_user = "AT for user"
        cca = msal.ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/microsoft.onmicrosoft.com")
        self.assertIsNone(next(cca.token_cache.search(
            msal.TokenCache.CredentialType.ACCESS_TOKEN), None))
        cca.acquire_token_for_client(
            ["scope"],
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps({"access_token": "AT for client"})))
        self.assertEqual(1, len(list(cca.token_cache.search(
            msal.TokenCache.CredentialType.ACCESS_TOKEN))))
        cca.acquire_token_by_username_password(
            "johndoe", "password", ["scope"],
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps(build_response(
                    access_token=at_for_user, expires_in=3600,
                    uid="uid", utid="utid",  # This populates home_account_id
                    ))))
        self.assertEqual(2, len(list(cca.token_cache.search(
            msal.TokenCache.CredentialType.ACCESS_TOKEN))))
        cca.remove_tokens_for_client()
        remaining_tokens = list(cca.token_cache.search(
            msal.TokenCache.CredentialType.ACCESS_TOKEN))
        self.assertEqual(1, len(remaining_tokens))
        self.assertEqual(at_for_user, remaining_tokens[0].get("secret"))


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
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


@patch("sys.platform", new="darwin")  # Pretend running on Mac.
@patch("msal.authority.tenant_discovery", new=Mock(return_value={
    "authorization_endpoint": "https://contoso.com/placeholder",
    "token_endpoint": "https://contoso.com/placeholder",
    "issuer": "https://contoso.com/placeholder",
    }))
class TestMsalBehaviorWithoutPyMsalRuntimeOrBroker(unittest.TestCase):

    @patch("msal.application._init_broker", new=Mock(side_effect=ImportError(
        "PyMsalRuntime not installed"
    )))
    def test_broker_should_be_disabled_by_default(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://login.microsoftonline.com/common",
            )
        self.assertFalse(app._enable_broker)

    @patch("msal.application._init_broker", new=Mock(side_effect=ImportError(
        "PyMsalRuntime not installed"
    )))
    def test_opt_in_should_error_out_when_pymsalruntime_not_installed(self):
        """Because it is actionable to app developer to add dependency declaration"""
        with self.assertRaises(ImportError):
            app = msal.PublicClientApplication(
                "client_id",
                authority="https://login.microsoftonline.com/common",
                enable_broker_on_mac=True,
                )

    @patch("msal.application._init_broker", new=Mock(side_effect=RuntimeError(
        "PyMsalRuntime raises RuntimeError when broker initialization failed"
    )))
    def test_should_fallback_when_pymsalruntime_failed_to_initialize_broker(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://login.microsoftonline.com/common",
            enable_broker_on_mac=True,
            )
        self.assertFalse(app._enable_broker)


@patch("sys.platform", new="darwin")  # Pretend running on Mac.
@patch("msal.authority.tenant_discovery", new=Mock(return_value={
    "authorization_endpoint": "https://contoso.com/placeholder",
    "token_endpoint": "https://contoso.com/placeholder",
    "issuer": "https://contoso.com/placeholder",
    }))
@patch("msal.application._init_broker", new=Mock())  # Pretend pymsalruntime installed and working
class TestBrokerFallbackWithDifferentAuthorities(unittest.TestCase):

    def test_broker_should_be_disabled_by_default(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://login.microsoftonline.com/common",
            )
        self.assertFalse(app._enable_broker)

    def test_broker_should_be_enabled_when_opted_in(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://login.microsoftonline.com/common",
            enable_broker_on_mac=True,
            )
        self.assertTrue(app._enable_broker)

    def test_should_fallback_to_non_broker_when_using_adfs(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://contoso.com/adfs",
            #instance_discovery=False,  # Automatically skipped when detected ADFS
            enable_broker_on_mac=True,
            )
        self.assertFalse(app._enable_broker)

    def test_should_fallback_to_non_broker_when_using_b2c(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://contoso.b2clogin.com/contoso/policy",
            #instance_discovery=False,  # Automatically skipped when detected B2C
            enable_broker_on_mac=True,
            )
        self.assertFalse(app._enable_broker)

    def test_should_use_broker_when_disabling_instance_discovery(self):
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://contoso.com/path",
            instance_discovery=False,  # Need this for a generic authority url
            enable_broker_on_mac=True,
            )
        # TODO: Shall we bypass broker when opted out of instance discovery?
        self.assertTrue(app._enable_broker)  # Current implementation enables broker

    def test_should_fallback_to_non_broker_when_using_oidc_authority(self):
        app = msal.PublicClientApplication(
            "client_id",
            oidc_authority="https://contoso.com/path",
            enable_broker_on_mac=True,
            )
        self.assertFalse(app._enable_broker)

    def test_app_did_not_register_redirect_uri_should_error_out(self):
        """Because it is actionable to app developer to add redirect URI"""
        app = msal.PublicClientApplication(
            "client_id",
            authority="https://login.microsoftonline.com/common",
            enable_broker_on_mac=True,
            )
        self.assertTrue(app._enable_broker)
        with patch.object(
            # Note: We tried @patch("msal.broker.foo", ...) but it ended up with
            # "module msal does not have attribute broker"
            app, "_acquire_token_interactive_via_broker", return_value={
                "error": "broker_error",
                "error_description":
                    "(pii).  "  # pymsalruntime no longer surfaces AADSTS error,
                                # So MSAL Python can't raise RedirectUriError.
                    "Status: Response_Status.Status_ApiContractViolation, "
                    "Error code: 3399614473, Tag 557973642",
            }):
            result = app.acquire_token_interactive(
                ["scope"],
                parent_window_handle=app.CONSOLE_WINDOW_HANDLE,
                )
            self.assertEqual(result.get("error"), "broker_error")


class MismatchingScopeTestCase(unittest.TestCase):
    """Test cache behavior when HTTP response scope differs from requested scope"""

    def test_token_should_be_cached_with_response_scope(self):
        """Based on https://datatracker.ietf.org/doc/html/rfc6749#section-3.3
        authorization server may issue an access token with different scope.
        For example, eSTS normalizes scopes by adding or removing trailing slash.
        Calling app is supposed to use the normalized scope for subsequent calls.
        """

        # Create a fresh app instance
        app = ConfidentialClientApplication(
            "client_id", client_credential="secret",
            authority="https://login.microsoftonline.com/common")

        # Mocked request: ask for "invalid_scope" scope but receive "valid_scope1 valid_scope2" scope in response
        def mock_post(url, headers=None, *args, **kwargs):
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "AT_with_valid_scope1_valid_scope2_scopes",
                "expires_in": 3600,
                "scope": "valid_scope1 valid_scope2",  # Response scope differs from requested scope
                "token_type": "Bearer"
            }))

        result1 = app.acquire_token_for_client(["invalid_scope"], post=mock_post)
        self.assertEqual(result1[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP)
        self.assertEqual("AT_with_valid_scope1_valid_scope2_scopes", result1.get("access_token"))
        self.assertEqual(["valid_scope1", "valid_scope2"], result1.get("scope").split())  # Scope from response

        # Second request: ask for same "invalid_scope" scope again
        # Since cached token has "valid_scope1 valid_scope2" scopes, it shouldn't match the "invalid_scope" request
        # This should go to IDP again and receive the same response
        result2 = app.acquire_token_for_client(["invalid_scope"], post=mock_post)
        # Should get a new token from IDP, not from cache
        self.assertEqual(result2[app._TOKEN_SOURCE], app._TOKEN_SOURCE_IDP)
        self.assertEqual("AT_with_valid_scope1_valid_scope2_scopes", result2.get("access_token"))
        self.assertEqual(["valid_scope1", "valid_scope2"], result2.get("scope").split())

        # Third and fourth requests: ask for individual valid scopes
        # Should hit cache for the token that has "valid_scope1 valid_scope2" scopes
        for scope in ["valid_scope1", "valid_scope2"]:
            result = app.acquire_token_for_client([scope])
            self.assertEqual(result[app._TOKEN_SOURCE], app._TOKEN_SOURCE_CACHE)
            self.assertEqual("AT_with_valid_scope1_valid_scope2_scopes", result.get("access_token"))
            self.assertIsNone(result.get("scope"), "scope field is not returned when token comes from cache")


def _build_user_fic_response(uid="user_oid", utid="tenant_id", access_token="user_at"):
    """Build a mock user_fic response with client_info and id_token."""
    client_info = base64.b64encode(json.dumps({
        "uid": uid, "utid": utid,
    }).encode()).decode("utf-8")
    id_token_claims = {
        "iss": "https://login.microsoftonline.com/tenant_id/v2.0",
        "sub": "subject",
        "aud": "agent_app_id",
        "exp": time.time() + 3600,
        "iat": time.time(),
        "oid": uid,
        "preferred_username": "user@contoso.com",
        "tid": utid,
    }
    id_token = "header.%s.signature" % base64.b64encode(
        json.dumps(id_token_claims).encode()).decode("utf-8")
    return json.dumps({
        "access_token": access_token,
        "expires_in": 3600,
        "token_type": "Bearer",
        "client_info": client_info,
        "id_token": id_token,
        "refresh_token": "a_refresh_token",
    })


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestUserFicProtocol(unittest.TestCase):
    """Tests that acquire_token_by_user_federated_identity_credential sends correct POST body."""

    def _make_app(self):
        return ConfidentialClientApplication(
            "agent_app_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")

    def test_sends_correct_grant_type_and_params(self):
        app = self._make_app()
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        result = app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="instance_token_t2",
            username="user@contoso.com",
            post=mock_post)
        self.assertIn("access_token", result)
        self.assertEqual("user_fic", captured_data.get("grant_type"))
        self.assertEqual("instance_token_t2",
            captured_data.get("user_federated_identity_credential"))
        self.assertEqual("1", captured_data.get("client_info"))
        self.assertEqual("agent_app_id", captured_data.get("client_id"))

    def test_scope_includes_oidc_scopes(self):
        app = self._make_app()
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="t2", username="user@contoso.com", post=mock_post)
        scope_str = captured_data.get("scope", "")
        for oidc_scope in ("openid", "offline_access", "profile"):
            self.assertIn(oidc_scope, scope_str,
                "OIDC scope '{}' should be present".format(oidc_scope))

    def test_with_username_sends_username_not_user_id(self):
        app = self._make_app()
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        app.acquire_token_by_user_federated_identity_credential(
            ["scope"], assertion="t2", username="user@contoso.com", post=mock_post)
        self.assertEqual("user@contoso.com", captured_data.get("username"))
        self.assertNotIn("user_id", captured_data,
            "user_id should NOT be in body when username is provided")

    def test_with_oid_sends_user_id_not_username(self):
        app = self._make_app()
        captured_data = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        app.acquire_token_by_user_federated_identity_credential(
            ["scope"], assertion="t2",
            user_object_id="00000000-0000-0000-0000-000000000001",
            post=mock_post)
        self.assertEqual("00000000-0000-0000-0000-000000000001",
            captured_data.get("user_id"))
        self.assertNotIn("username", captured_data,
            "username should NOT be in body when user_object_id is provided")

    def test_ccs_routing_header_with_username(self):
        app = self._make_app()
        captured_headers = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_headers.update(headers or {})
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        app.acquire_token_by_user_federated_identity_credential(
            ["scope"], assertion="t2", username="user@contoso.com", post=mock_post)
        self.assertEqual("upn:user@contoso.com",
            captured_headers.get("X-AnchorMailbox"),
            "CCS routing header should use UPN format for username path")

    def test_ccs_routing_header_with_oid(self):
        app = self._make_app()
        captured_headers = {}

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_headers.update(headers or {})
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        app.acquire_token_by_user_federated_identity_credential(
            ["scope"], assertion="t2",
            user_object_id="user_oid_123", post=mock_post)
        self.assertIn("X-AnchorMailbox", captured_headers,
            "CCS routing header should be present for OID path")
        self.assertTrue(
            captured_headers["X-AnchorMailbox"].startswith("Oid:"),
            "CCS routing header should use Oid format for user_object_id path")


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestUserFicCacheBehavior(unittest.TestCase):
    """Tests that user_fic tokens are stored in user cache with account info."""

    def _make_app(self):
        return ConfidentialClientApplication(
            "agent_app_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")

    def test_token_stored_in_user_cache_with_account(self):
        app = self._make_app()

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            return MinimalResponse(status_code=200, text=_build_user_fic_response(
                uid="user_oid", utid="tenant_id", access_token="fic_at"))

        result = app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="t2", username="user@contoso.com", post=mock_post)
        self.assertIn("access_token", result)

        # Verify the account was created
        accounts = app.get_accounts()
        self.assertTrue(len(accounts) > 0, "Account should be created from user_fic response")
        account = accounts[0]
        self.assertEqual("user_oid.tenant_id", account["home_account_id"])

    def test_token_not_stored_as_atext(self):
        """user_fic tokens should use standard AccessToken type, not atext."""
        app = self._make_app()

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            return MinimalResponse(status_code=200, text=_build_user_fic_response())

        app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="t2", username="user@contoso.com", post=mock_post)

        # Check the raw cache for credential type
        at_entries = list(app.token_cache.search(
            msal.TokenCache.CredentialType.ACCESS_TOKEN, query={}))
        self.assertTrue(len(at_entries) > 0, "AT should be cached")
        self.assertNotIn("ext_cache_key", at_entries[0],
            "user_fic tokens should NOT have ext_cache_key")

    def test_acquire_token_silent_returns_cached_fic_token(self):
        app = self._make_app()

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            return MinimalResponse(status_code=200, text=_build_user_fic_response(
                uid="user_oid", utid="tenant_id", access_token="cached_fic_at"))

        app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="t2", username="user@contoso.com", post=mock_post)

        accounts = app.get_accounts()
        self.assertTrue(len(accounts) > 0)

        # Silent call should return cached token without hitting network
        silent_result = app.acquire_token_silent(
            ["https://graph.microsoft.com/.default"], account=accounts[0])
        self.assertIn("access_token", silent_result)
        self.assertEqual("cached_fic_at", silent_result["access_token"])

    def test_oid_path_token_stored_and_retrievable_via_silent(self):
        """user_fic with user_object_id should cache and retrieve like username."""
        app = self._make_app()

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            return MinimalResponse(status_code=200, text=_build_user_fic_response(
                uid="user_oid", utid="tenant_id", access_token="oid_fic_at"))

        result = app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="t2", user_object_id="user_oid", post=mock_post)
        self.assertIn("access_token", result)

        # Verify no ext_cache_key on cached token
        at_entries = list(app.token_cache.search(
            msal.TokenCache.CredentialType.ACCESS_TOKEN, query={}))
        self.assertTrue(len(at_entries) > 0, "AT should be cached")
        self.assertNotIn("ext_cache_key", at_entries[0],
            "OID-path user_fic tokens should NOT have ext_cache_key")

        # Verify account and silent retrieval
        accounts = app.get_accounts()
        self.assertTrue(len(accounts) > 0)
        silent_result = app.acquire_token_silent(
            ["https://graph.microsoft.com/.default"], account=accounts[0])
        self.assertIn("access_token", silent_result)
        self.assertEqual("oid_fic_at", silent_result["access_token"])

    def test_account_source_is_set_to_user_fic(self):
        """Accounts created by user_fic should have account_source set."""
        app = self._make_app()

        def mock_post(url, headers=None, data=None, *args, **kwargs):
            return MinimalResponse(status_code=200, text=_build_user_fic_response(
                uid="user_oid", utid="tenant_id"))

        app.acquire_token_by_user_federated_identity_credential(
            ["https://graph.microsoft.com/.default"],
            assertion="t2", username="user@contoso.com", post=mock_post)

        accounts = app.get_accounts()
        self.assertTrue(len(accounts) > 0)
        self.assertEqual("user_fic", accounts[0].get("account_source"),
            "FIC accounts should have account_source='user_fic' to avoid "
            "broker path misrouting")


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestUserFicInputValidation(unittest.TestCase):
    """Tests that input validation rejects invalid parameters."""

    def _make_app(self):
        return ConfidentialClientApplication(
            "agent_app_id", client_credential="secret",
            authority="https://login.microsoftonline.com/my_tenant")

    def test_empty_assertion_raises(self):
        app = self._make_app()
        with self.assertRaises(ValueError):
            app.acquire_token_by_user_federated_identity_credential(
                ["scope"], assertion="", username="user@contoso.com")

    def test_none_assertion_raises(self):
        app = self._make_app()
        with self.assertRaises(ValueError):
            app.acquire_token_by_user_federated_identity_credential(
                ["scope"], assertion=None, username="user@contoso.com")

    def test_no_user_identifier_raises(self):
        app = self._make_app()
        with self.assertRaises(ValueError):
            app.acquire_token_by_user_federated_identity_credential(
                ["scope"], assertion="t2")

    def test_both_user_identifiers_raises(self):
        app = self._make_app()
        with self.assertRaises(ValueError):
            app.acquire_token_by_user_federated_identity_credential(
                ["scope"], assertion="t2",
                username="user@contoso.com",
                user_object_id="oid-123")

    def test_reserved_scopes_rejected(self):
        app = self._make_app()
        with self.assertRaises(ValueError):
            app.acquire_token_by_user_federated_identity_credential(
                ["openid"], assertion="t2", username="user@contoso.com")


@patch(_OIDC_DISCOVERY, new=_OIDC_DISCOVERY_MOCK)
class TestAssertionCallbackContext(unittest.TestCase):
    """Tests that assertion callbacks receive context when they accept arguments."""

    def test_context_aware_callback_receives_fmi_path(self):
        received_context = {}

        def assertion_with_context(context):
            received_context.update(context)
            return "assertion_value"

        app = ConfidentialClientApplication(
            "client_id",
            client_credential={"client_assertion": assertion_with_context},
            authority="https://login.microsoftonline.com/my_tenant")

        app.acquire_token_for_client(
            ["scope"], fmi_path="agent_app_123",
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an_at", "expires_in": 3600})))

        self.assertEqual("client_id", received_context.get("client_id"))
        self.assertIn("token_endpoint", received_context)
        self.assertEqual("agent_app_123", received_context.get("fmi_path"))

    def test_context_aware_callback_omits_fmi_path_when_not_set(self):
        received_context = {}

        def assertion_with_context(context):
            received_context.update(context)
            return "assertion_value"

        app = ConfidentialClientApplication(
            "client_id",
            client_credential={"client_assertion": assertion_with_context},
            authority="https://login.microsoftonline.com/my_tenant")

        app.acquire_token_for_client(
            ["scope"],
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an_at", "expires_in": 3600})))

        self.assertEqual("client_id", received_context.get("client_id"))
        self.assertNotIn("fmi_path", received_context)

    def test_legacy_zero_arg_callback_still_works(self):
        call_count = [0]

        def legacy_callback():
            call_count[0] += 1
            return "legacy_assertion"

        app = ConfidentialClientApplication(
            "client_id",
            client_credential={"client_assertion": legacy_callback},
            authority="https://login.microsoftonline.com/my_tenant")

        result = app.acquire_token_for_client(
            ["scope"],
            post=lambda url, **kwargs: MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an_at", "expires_in": 3600})))

        self.assertIn("access_token", result)
        self.assertEqual(1, call_count[0], "Legacy callback should be invoked once")

    def test_context_callback_type_error_not_swallowed(self):
        """If a one-arg callback raises TypeError internally, it should propagate."""
        def buggy_callback(context):
            raise TypeError("Bug inside callback")

        app = ConfidentialClientApplication(
            "client_id",
            client_credential={"client_assertion": buggy_callback},
            authority="https://login.microsoftonline.com/my_tenant")

        with self.assertRaises(TypeError, msg="Internal TypeError should propagate"):
            app.acquire_token_for_client(
                ["scope"],
                post=lambda url, **kwargs: MinimalResponse(
                    status_code=200, text=json.dumps({
                        "access_token": "an_at", "expires_in": 3600})))

    def test_lambda_with_defaulted_param_treated_as_zero_arg(self):
        """A lambda like ``lambda token=token: token`` should be treated as
        zero-arg because all its positional params have defaults."""
        captured_value = "my_assertion_value"
        assertion_callable = lambda token=captured_value: token  # noqa: E731

        app = ConfidentialClientApplication(
            "client_id",
            client_credential={"client_assertion": assertion_callable},
            authority="https://login.microsoftonline.com/my_tenant")

        captured_data = {}
        def mock_post(url, headers=None, data=None, *args, **kwargs):
            captured_data.update(data or {})
            return MinimalResponse(
                status_code=200, text=json.dumps({
                    "access_token": "an_at", "expires_in": 3600}))

        result = app.acquire_token_for_client(["scope"], post=mock_post)
        self.assertIn("access_token", result)
        # The assertion should be the string value, not a dict context object
        self.assertEqual(
            captured_value, captured_data.get("client_assertion"),
            "Lambda with defaulted params should return its default value, "
            "not receive a context dict")