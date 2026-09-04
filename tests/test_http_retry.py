import requests

from msal.application import _build_retry

from tests import unittest


class TestRetryPolicy(unittest.TestCase):
    """The retry mounted by ClientApplication has to cover the token endpoint's POST,
    without taking over the 429/5xx handling that MSAL's ThrottledHttpClient does."""

    def setUp(self):
        self.retry = _build_retry(requests)

    def test_one_retry(self):
        self.assertEqual(1, self.retry.total)

    def test_errors_are_retried_on_post(self):
        # urllib3 gates read-error retries on whether it considers the method idempotent
        self.assertTrue(self.retry._is_method_retryable("POST"))
        self.assertTrue(self.retry._is_method_retryable("GET"))

    def test_a_throttled_post_is_left_to_msal(self):
        # A 429 or 5xx with Retry-After on the token endpoint reaches ThrottledHttpClient,
        # instead of urllib3 sleeping for Retry-After seconds and retrying in place
        self.assertFalse(self.retry.is_retry("POST", 429, has_retry_after=True))
        self.assertFalse(self.retry.is_retry("POST", 503, has_retry_after=True))

    def test_a_throttled_get_still_retries(self):
        # Unchanged from the previous HTTPAdapter(max_retries=1): the discovery GETs
        # are not throttled by MSAL itself
        self.assertTrue(self.retry.is_retry("GET", 429, has_retry_after=True))
