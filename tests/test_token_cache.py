import os
import json
import logging

from msal.token_cache import *
from tests import unittest


THIS_FOLDER = os.path.dirname(__file__)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class TokenCacheTestCase(unittest.TestCase):

    def assertAdd(self):
        cache = TokenCache()
        cache.add({
            "client_id": "my_client_id",
            "scope": ["s1", "s2"],
            "token_endpoint": "https://login.microsoftonline.com/contoso/v2/token",
            "response": {
		"access_token":"2YotnFZFEjr1zCsicMWpAA",
		"token_type":"example",
		"expires_in":3600,
		"refresh_token":"tGzv3JOkF0XG5Qx2TlKWIA",
		"example_parameter":"example_value"
		},
            })

