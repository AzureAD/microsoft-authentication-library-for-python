import unittest

import msal
from tests.http_client import MinimalHttpClient


class DummyHttpClient(object):
    def get(self, url, **kwargs):
        raise RuntimeError("just for testing purpose")


class TestAuthorityHonorsPatchedRequests(unittest.TestCase):
    """This is only a workaround for an undocumented behavior."""
    def test_authority_honors_a_patched_requests(self):
        # First, we test that the original, unmodified authority is working
        a = msal.authority.Authority(
            "https://login.microsoftonline.com/common", MinimalHttpClient())
        a.initialize()
        self.assertEqual(
            a.authorization_endpoint,
            'https://login.microsoftonline.com/common/oauth2/v2.0/authorize')

        original = msal.authority.requests
        try:
            # Now we mimic a (discouraged) practice of patching authority.requests
            msal.authority.requests = DummyHttpClient()
            # msal.authority is expected to honor that patch.
            with self.assertRaises(RuntimeError):
                a = msal.authority.Authority(
                    "https://login.microsoftonline.com/common", MinimalHttpClient())
                a.initialize()
        finally:  # Tricky:
            # Unpatch is necessary otherwise other test cases would be affected
            msal.authority.requests = original
