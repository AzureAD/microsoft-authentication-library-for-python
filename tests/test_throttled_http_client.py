# Test cases for https://identitydivision.visualstudio.com/devex/_git/AuthLibrariesApiReview?version=GBdev&path=%2FService%20protection%2FIntial%20set%20of%20protection%20measures.md&_a=preview&anchor=common-test-cases
from time import sleep
from random import random
import logging
from msal.throttled_http_client import ThrottledHttpClient
from tests import unittest
from tests.http_client import MinimalResponse


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class DummyHttpClient(object):
    def __init__(self, status_code=None, response_headers=None):
        self._status_code = status_code
        self._response_headers = response_headers

    def _build_dummy_response(self):
        return MinimalResponse(
            status_code=self._status_code,
            headers=self._response_headers,
            text=random(),  # So that we'd know whether a new response is received
            )

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        return self._build_dummy_response()

    def get(self, url, params=None, headers=None, **kwargs):
        return self._build_dummy_response()

    def close(self):
        raise CloseMethodCalled("Not used by MSAL, but our customers may use it")


class CloseMethodCalled(Exception):
    pass


class TestHttpDecoration(unittest.TestCase):

    def test_throttled_http_client_should_not_alter_original_http_client(self):
        original_http_client = DummyHttpClient()
        original_get = original_http_client.get
        original_post = original_http_client.post
        throttled_http_client = ThrottledHttpClient(original_http_client)
        goal = """The implementation should wrap original http_client
            and keep it intact, instead of monkey-patching it"""
        self.assertNotEqual(throttled_http_client, original_http_client, goal)
        self.assertEqual(original_post, original_http_client.post)
        self.assertEqual(original_get, original_http_client.get)

    def _test_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(
            self, http_client, retry_after):
        http_cache = {}
        http_client = ThrottledHttpClient(http_client, http_cache=http_cache)
        resp1 = http_client.post("https://example.com")  # We implemented POST only
        resp2 = http_client.post("https://example.com")  # We implemented POST only
        logger.debug(http_cache)
        self.assertEqual(resp1.text, resp2.text, "Should return a cached response")
        sleep(retry_after + 1)
        resp3 = http_client.post("https://example.com")  # We implemented POST only
        self.assertNotEqual(resp1.text, resp3.text, "Should return a new response")

    def test_429_with_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(self):
        retry_after = 1
        self._test_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(
            DummyHttpClient(
                status_code=429, response_headers={"Retry-After": retry_after}),
            retry_after)

    def test_5xx_with_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(self):
        retry_after = 1
        self._test_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(
            DummyHttpClient(
                status_code=503, response_headers={"Retry-After": retry_after}),
            retry_after)

    def test_400_with_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(self):
        """Retry-After is supposed to only shown in http 429/5xx,
        but we choose to support Retry-After for arbitrary http response."""
        retry_after = 1
        self._test_RetryAfter_N_seconds_should_keep_entry_for_N_seconds(
            DummyHttpClient(
                status_code=400, response_headers={"Retry-After": retry_after}),
            retry_after)

    def test_one_RetryAfter_request_should_block_a_similar_request(self):
        http_cache = {}
        http_client = DummyHttpClient(
            status_code=429, response_headers={"Retry-After": 2})
        http_client = ThrottledHttpClient(http_client, http_cache=http_cache)
        resp1 = http_client.post("https://example.com", data={
            "scope": "one", "claims": "bar", "grant_type": "authorization_code"})
        resp2 = http_client.post("https://example.com", data={
            "scope": "one", "claims": "foo", "grant_type": "password"})
        logger.debug(http_cache)
        self.assertEqual(resp1.text, resp2.text, "Should return a cached response")

    def test_one_RetryAfter_request_should_not_block_a_different_request(self):
        http_cache = {}
        http_client = DummyHttpClient(
            status_code=429, response_headers={"Retry-After": 2})
        http_client = ThrottledHttpClient(http_client, http_cache=http_cache)
        resp1 = http_client.post("https://example.com", data={"scope": "one"})
        resp2 = http_client.post("https://example.com", data={"scope": "two"})
        logger.debug(http_cache)
        self.assertNotEqual(resp1.text, resp2.text, "Should return a new response")

    def test_one_invalid_grant_should_block_a_similar_request(self):
        http_cache = {}
        http_client = DummyHttpClient(
            status_code=400)  # It covers invalid_grant and interaction_required
        http_client = ThrottledHttpClient(http_client, http_cache=http_cache)
        resp1 = http_client.post("https://example.com", data={"claims": "foo"})
        logger.debug(http_cache)
        resp1_again = http_client.post("https://example.com", data={"claims": "foo"})
        self.assertEqual(resp1.text, resp1_again.text, "Should return a cached response")
        resp2 = http_client.post("https://example.com", data={"claims": "bar"})
        self.assertNotEqual(resp1.text, resp2.text, "Should return a new response")
        resp2_again = http_client.post("https://example.com", data={"claims": "bar"})
        self.assertEqual(resp2.text, resp2_again.text, "Should return a cached response")

    def test_one_foci_app_recovering_from_invalid_grant_should_also_unblock_another(self):
        """
        Need not test multiple FOCI app's acquire_token_silent() here. By design,
        one FOCI app's successful populating token cache would result in another
        FOCI app's acquire_token_silent() to hit a token without invoking http request.
        """

    def test_forcefresh_behavior(self):
        """
        The implementation let token cache and http cache operate in different
        layers. They do not couple with each other.
        Therefore, acquire_token_silent(..., force_refresh=True)
        would bypass the token cache yet technically still hit the http cache.

        But that is OK, cause the customer need no force_refresh in the first place.
        After a successful AT/RT acquisition, AT/RT will be in the token cache,
        and a normal acquire_token_silent(...) without force_refresh would just work.
        This was discussed in https://identitydivision.visualstudio.com/DevEx/_git/AuthLibrariesApiReview/pullrequest/3618?_a=files
        """

    def test_http_get_200_should_be_cached(self):
        http_cache = {}
        http_client = DummyHttpClient(
            status_code=200)  # It covers UserRealm discovery and OIDC discovery
        http_client = ThrottledHttpClient(http_client, http_cache=http_cache)
        resp1 = http_client.get("https://example.com?foo=bar")
        resp2 = http_client.get("https://example.com?foo=bar")
        logger.debug(http_cache)
        self.assertEqual(resp1.text, resp2.text, "Should return a cached response")

    def test_device_flow_retry_should_not_be_cached(self):
        DEVICE_AUTH_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
        http_cache = {}
        http_client = DummyHttpClient(status_code=400)
        http_client = ThrottledHttpClient(http_client, http_cache=http_cache)
        resp1 = http_client.post(
            "https://example.com", data={"grant_type": DEVICE_AUTH_GRANT})
        resp2 = http_client.post(
            "https://example.com", data={"grant_type": DEVICE_AUTH_GRANT})
        logger.debug(http_cache)
        self.assertNotEqual(resp1.text, resp2.text, "Should return a new response")

    def test_throttled_http_client_should_provide_close(self):
        http_client = DummyHttpClient(status_code=200)
        http_client = ThrottledHttpClient(http_client)
        with self.assertRaises(CloseMethodCalled):
            http_client.close()

