import logging
import base64
import json

from msal.token_cache import *
from tests import unittest


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class TokenCacheTestCase(unittest.TestCase):

    def setUp(self):
        self.cache = TokenCache()

    def testAdd(self):
        client_info = base64.b64encode(b'''
            {"uid": "uid", "utid": "utid"}
            ''').decode('utf-8')
        id_token = "header.%s.signature" % base64.b64encode(b'''{
            "sub": "subject",
            "oid": "object1234",
            "preferred_username": "John Doe"
            }''').decode('utf-8')
        self.cache.add({
            "client_id": "my_client_id",
            "scope": ["s2", "s1", "s3"],  # Not in particular order
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "response": {
		"access_token": "an access token",
		"token_type": "some type",
		"expires_in": 3600,
		"refresh_token": "a refresh token",
                "client_info": client_info,
                "id_token": id_token,
		},
            }, now=1000)
        self.assertEqual(
            {
                'cached_at': "1000",
                'client_id': 'my_client_id',
                'credential_type': 'AccessToken',
                'environment': 'login.example.com',
                'expires_on': "4600",
                'extended_expires_on': "4600",
                'home_account_id': "uid.utid",
                'realm': 'contoso',
                'secret': 'an access token',
                'target': 's2 s1 s3',
            },
            self.cache._cache["AccessToken"].get(
                'uid.utid-login.example.com-accesstoken-my_client_id-contoso-s2 s1 s3')
            )
        self.assertEqual(
            {
                'client_id': 'my_client_id',
                'credential_type': 'RefreshToken',
                'environment': 'login.example.com',
                'home_account_id': "uid.utid",
                'secret': 'a refresh token',
                'target': 's2 s1 s3',
            },
            self.cache._cache["RefreshToken"].get(
                'uid.utid-login.example.com-refreshtoken-my_client_id--s2 s1 s3')
            )
        self.assertEqual(
            {
                'home_account_id': "uid.utid",
                'environment': 'login.example.com',
                'realm': 'contoso',
                'local_account_id': "object1234",
                'username': "John Doe",
                'authority_type': "MSSTS",
            },
            self.cache._cache["Account"].get('uid.utid-login.example.com-contoso')
            )
        self.assertEqual(
            {
                'credential_type': 'IdToken',
                'secret': id_token,
                'home_account_id': "uid.utid",
                'environment': 'login.example.com',
                'realm': 'contoso',
                'client_id': 'my_client_id',
            },
            self.cache._cache["IdToken"].get(
                'uid.utid-login.example.com-idtoken-my_client_id-contoso-')
            )
        self.assertEqual(
            {
                "client_id": "my_client_id",
                'environment': 'login.example.com',
                "family_id": None,
            },
            self.cache._cache.get("AppMetadata", {}).get(
                "appmetadata-login.example.com-my_client_id")
            )


class SerializableTokenCacheTestCase(TokenCacheTestCase):
    # Run all inherited test methods, and have extra check in tearDown()

    def setUp(self):
        self.cache = SerializableTokenCache()
        self.cache.deserialize("""
            {
                "AccessToken": {
                    "an-entry": {
                        "foo": "bar"
                        }
                    },
                "customized": "whatever"
            }
            """)

    def test_has_state_changed(self):
        cache = SerializableTokenCache()
        self.assertFalse(cache.has_state_changed)
        cache.add({})  # An NO-OP add() still counts as a state change. Good enough.
        self.assertTrue(cache.has_state_changed)

    def tearDown(self):
        state = self.cache.serialize()
        logger.debug("serialize() = %s", state)
        # Now assert all extended content are kept intact
        output = json.loads(state)
        self.assertEqual(output.get("customized"), "whatever",
            "Undefined cache keys and their values should be intact")
        self.assertEqual(
            output.get("AccessToken", {}).get("an-entry"), {"foo": "bar"},
            "Undefined token keys and their values should be intact")

