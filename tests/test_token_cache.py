import logging
import base64
import json
import time
import warnings

from msal.token_cache import TokenCache, SerializableTokenCache
from tests import unittest


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


# NOTE: These helpers were once implemented as static methods in TokenCacheTestCase.
#       That would cause other test files' "from ... import TokenCacheTestCase"
#       to re-run all test cases in this file.
#       Now we avoid that, by defining these helpers in module level.
def build_id_token(
        iss="issuer", sub="subject", aud="my_client_id", exp=None, iat=None,
        **claims):  # AAD issues "preferred_username", ADFS issues "upn"
    return "header.%s.signature" % base64.b64encode(json.dumps(dict({
        "iss": iss,
        "sub": sub,
        "aud": aud,
        "exp": exp or (time.time() + 100),
        "iat": iat or time.time(),
        }, **claims)).encode()).decode('utf-8')


def build_response(  # simulate a response from AAD
        uid=None, utid=None,  # If present, they will form client_info
        access_token=None, expires_in=3600, token_type="some type",
        **kwargs  # Pass-through: refresh_token, foci, id_token, error, refresh_in, ...
        ):
    response = {}
    if uid and utid:  # Mimic the AAD behavior for "client_info=1" request
        response["client_info"] = base64.b64encode(json.dumps({
            "uid": uid, "utid": utid,
            }).encode()).decode('utf-8')
    if access_token:
        response.update({
            "access_token": access_token,
            "expires_in": expires_in,
            "token_type": token_type,
            })
    response.update(kwargs)  # Pass-through key-value pairs as top-level fields
    return response


class TokenCacheTestCase(unittest.TestCase):

    def setUp(self):
        self.cache = TokenCache()
        self.at_key_maker = self.cache.key_makers[
            TokenCache.CredentialType.ACCESS_TOKEN]

    def testAddByAad(self):
        client_id = "my_client_id"
        id_token = build_id_token(
            oid="object1234", preferred_username="John Doe", aud=client_id)
        now = 1000
        self.cache.add({
            "client_id": client_id,
            "scope": ["s2", "s1", "s3"],  # Not in particular order
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "response": build_response(
                uid="uid", utid="utid",  # client_info
                expires_in=3600, access_token="an access token",
                id_token=id_token, refresh_token="a refresh token"),
            }, now=now)
        access_token_entry = {
                'cached_at': "1000",
                'client_id': 'my_client_id',
                'credential_type': 'AccessToken',
                'environment': 'login.example.com',
                'expires_on': "4600",
                'extended_expires_on': "4600",
                'home_account_id': "uid.utid",
                'realm': 'contoso',
                'secret': 'an access token',
                'target': 's1 s2 s3',  # Sorted
                'token_type': 'some type',
            }
        self.assertEqual(access_token_entry, self.cache._cache["AccessToken"].get(
            self.at_key_maker(**access_token_entry)))
        with warnings.catch_warnings():
            self.assertIn(
                access_token_entry,
                self.cache.find(self.cache.CredentialType.ACCESS_TOKEN, now=now),
                "find(..., query=None) should not crash, even though MSAL does not use it")
        self.assertEqual(
            {
                'client_id': 'my_client_id',
                'credential_type': 'RefreshToken',
                'environment': 'login.example.com',
                'home_account_id': "uid.utid",
                'last_modification_time': '1000',
                'secret': 'a refresh token',
                'target': 's1 s2 s3',  # Sorted
            },
            self.cache._cache["RefreshToken"].get(
                'uid.utid-login.example.com-refreshtoken-my_client_id--s1 s2 s3')
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
            },
            self.cache._cache.get("AppMetadata", {}).get(
                "appmetadata-login.example.com-my_client_id")
            )

    def testAddByAdfs(self):
        client_id = "my_client_id"
        id_token = build_id_token(aud=client_id, upn="JaneDoe@example.com")
        self.cache.add({
            "client_id": client_id,
            "scope": ["s2", "s1", "s3"],  # Not in particular order
            "token_endpoint": "https://fs.msidlab8.com/adfs/oauth2/token",
            "response": build_response(
                uid=None, utid=None,  # ADFS will provide no client_info
                expires_in=3600, access_token="an access token",
                id_token=id_token, refresh_token="a refresh token"),
            }, now=1000)
        access_token_entry = {
                'cached_at': "1000",
                'client_id': 'my_client_id',
                'credential_type': 'AccessToken',
                'environment': 'fs.msidlab8.com',
                'expires_on': "4600",
                'extended_expires_on': "4600",
                'home_account_id': "subject",
                'realm': 'adfs',
                'secret': 'an access token',
                'target': 's1 s2 s3',  # Sorted
                'token_type': 'some type',
            }
        self.assertEqual(access_token_entry, self.cache._cache["AccessToken"].get(
            self.at_key_maker(**access_token_entry)))
        self.assertEqual(
            {
                'client_id': 'my_client_id',
                'credential_type': 'RefreshToken',
                'environment': 'fs.msidlab8.com',
                'home_account_id': "subject",
                'last_modification_time': "1000",
                'secret': 'a refresh token',
                'target': 's1 s2 s3',  # Sorted
            },
            self.cache._cache["RefreshToken"].get(
                'subject-fs.msidlab8.com-refreshtoken-my_client_id--s1 s2 s3')
            )
        self.assertEqual(
            {
                'home_account_id': "subject",
                'environment': 'fs.msidlab8.com',
                'realm': 'adfs',
                'local_account_id': "subject",
                'username': "JaneDoe@example.com",
                'authority_type': "ADFS",
            },
            self.cache._cache["Account"].get('subject-fs.msidlab8.com-adfs')
            )
        self.assertEqual(
            {
                'credential_type': 'IdToken',
                'secret': id_token,
                'home_account_id': "subject",
                'environment': 'fs.msidlab8.com',
                'realm': 'adfs',
                'client_id': 'my_client_id',
            },
            self.cache._cache["IdToken"].get(
                'subject-fs.msidlab8.com-idtoken-my_client_id-adfs-')
            )
        self.assertEqual(
            {
                "client_id": "my_client_id",
                'environment': 'fs.msidlab8.com',
            },
            self.cache._cache.get("AppMetadata", {}).get(
                "appmetadata-fs.msidlab8.com-my_client_id")
            )

    def assertFoundAccessToken(self, *, scopes, query, data=None, now=None):
        cached_at = None
        for cached_at in self.cache.search(
                TokenCache.CredentialType.ACCESS_TOKEN,
                target=scopes, query=query, now=now,
        ):
            for k, v in (data or {}).items():  # The extra data, if any
                self.assertEqual(cached_at.get(k), v, f"AT should contain {k}={v}")
        self.assertTrue(cached_at, "AT should be cached and searchable")
        return cached_at

    def _test_data_should_be_saved_and_searchable_in_access_token(self, data):
        scopes = ["s2", "s1", "s3"]  # Not in particular order
        now = 1000
        self.cache.add({
            "data": data,
            "client_id": "my_client_id",
            "scope": scopes,
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "response": build_response(
                uid="uid", utid="utid",  # client_info
                expires_in=3600, access_token="an access token",
                refresh_token="a refresh token"),
            }, now=now)
        self.assertFoundAccessToken(scopes=scopes, data=data, now=now, query=dict(
            data,  # Also use the extra data as a query criteria
            client_id="my_client_id",
            environment="login.example.com",
            realm="contoso",
            home_account_id="uid.utid",
        ))

    def test_extra_data_should_also_be_recorded_and_searchable_in_access_token(self):
        self._test_data_should_be_saved_and_searchable_in_access_token({"key_id": "1"})

    def test_access_tokens_with_different_key_id(self):
        self._test_data_should_be_saved_and_searchable_in_access_token({"key_id": "1"})
        self._test_data_should_be_saved_and_searchable_in_access_token({"key_id": "2"})
        self.assertEqual(
            len(self.cache._cache["AccessToken"]),
            1, """Historically, tokens are not keyed by key_id,
so a new token overwrites the old one, and we would end up with 1 token in cache""")

    def test_refresh_in_should_be_recorded_as_refresh_on(self):  # Sounds weird. Yep.
        scopes = ["s2", "s1", "s3"]  # Not in particular order
        self.cache.add({
            "client_id": "my_client_id",
            "scope": scopes,
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "response": build_response(
                uid="uid", utid="utid",  # client_info
                expires_in=3600, refresh_in=1800, access_token="an access token",
                ),  #refresh_token="a refresh token"),
            }, now=1000)
        at = self.assertFoundAccessToken(scopes=scopes, query=dict(
            client_id="my_client_id",
            environment="login.example.com",
            realm="contoso",
            home_account_id="uid.utid",
        ))
        self.assertEqual("2800", at.get("refresh_on"), "Should save refresh_on")

    def test_old_rt_data_with_wrong_key_should_still_be_salvaged_into_new_rt(self):
        sample = {
            'client_id': 'my_client_id',
            'credential_type': 'RefreshToken',
            'environment': 'login.example.com',
            'home_account_id': "uid.utid",
            'secret': 'a refresh token',
            'target': 's2 s1 s3',
            }
        new_rt = "this is a new RT"
        self.cache._cache["RefreshToken"] = {"wrong-key": sample}
        self.cache.modify(
            self.cache.CredentialType.REFRESH_TOKEN, sample, {"secret": new_rt})
        self.assertEqual(
            dict(sample, secret=new_rt),
            self.cache._cache["RefreshToken"].get(
                'uid.utid-login.example.com-refreshtoken-my_client_id--s2 s1 s3')
            )


class SerializableTokenCacheTestCase(unittest.TestCase):
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

