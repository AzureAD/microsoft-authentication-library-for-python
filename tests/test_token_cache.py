import logging
import base64
import json
import time
import warnings

from msal.token_cache import (
    TokenCache, SerializableTokenCache, _compute_ext_cache_key,
    _parse_claims_or_raise, _merge_claims,
)
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

    def test_add_redacts_client_claims_in_debug_log(self):
        # Both fields live in event["data"] for different reasons: the
        # cache-key-only "client_claims" pseudo-param contributes to
        # ext_cache_key (it is excluded from the wire), while the merged "claims"
        # is a real OAuth wire parameter that is excluded from ext_cache_key. Both
        # may carry sensitive values, so TokenCache.add()'s DEBUG log must redact
        # them regardless of whether they affect the hash.
        secret = '{"access_token": {"nbf": {"essential": "SENSITIVE"}}}'
        with self.assertLogs("msal.token_cache", level="DEBUG") as cm:
            self.cache.add({
                "client_id": "my_client_id",
                "scope": ["s1"],
                "data": {"client_claims": secret, "claims": secret},
                "token_endpoint": "https://login.example.com/contoso/v2/token",
                "response": build_response(
                    uid="uid", utid="utid",
                    expires_in=3600, access_token="an access token"),
                }, now=1000)
        logged = "\n".join(cm.output)
        self.assertNotIn("SENSITIVE", logged)
        self.assertIn("********", logged)

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


class TestComputeExtCacheKey(unittest.TestCase):
    """Tests for the _compute_ext_cache_key hash function."""

    def test_empty_data_returns_empty_string(self):
        self.assertEqual("", _compute_ext_cache_key(None))
        self.assertEqual("", _compute_ext_cache_key({}))

    def test_excluded_fields_are_ignored(self):
        self.assertEqual("", _compute_ext_cache_key({"key_id": "k1", "token_type": "ssh-cert", "req_cnf": "nonce", "claims": "{}"}),
            "Fields in _EXT_CACHE_KEY_EXCLUDED_FIELDS should produce an empty hash")

    def test_fmi_path_produces_non_empty_hash(self):
        result = _compute_ext_cache_key({"fmi_path": "SomePath/Credential"})
        self.assertNotEqual("", result)
        self.assertIsInstance(result, str)

    def test_same_input_produces_same_hash(self):
        h1 = _compute_ext_cache_key({"fmi_path": "path/a"})
        h2 = _compute_ext_cache_key({"fmi_path": "path/a"})
        self.assertEqual(h1, h2)

    def test_different_fmi_paths_produce_different_hashes(self):
        h1 = _compute_ext_cache_key({"fmi_path": "path/a"})
        h2 = _compute_ext_cache_key({"fmi_path": "path/b"})
        self.assertNotEqual(h1, h2)

    def test_empty_fmi_path_value_is_ignored(self):
        self.assertEqual("", _compute_ext_cache_key({"fmi_path": ""}))

    def test_excluded_fields_dont_affect_hash(self):
        h1 = _compute_ext_cache_key({"fmi_path": "path/a"})
        h2 = _compute_ext_cache_key({"fmi_path": "path/a", "key_id": "k1", "req_cnf": "nonce"})
        self.assertEqual(h1, h2, "Excluded fields should not affect the hash")

    def test_non_excluded_fields_are_included_in_hash(self):
        h1 = _compute_ext_cache_key({"fmi_path": "path/a"})
        h2 = _compute_ext_cache_key({"fmi_path": "path/a", "custom_param": "val"})
        self.assertNotEqual(h1, h2, "Non-excluded fields should change the hash")


class TestClientClaimsCacheKey(unittest.TestCase):
    """client_claims must drive the extended cache key; server-issued claims must not."""

    def test_client_claims_produces_non_empty_hash(self):
        result = _compute_ext_cache_key({"client_claims": '{"a": 1}'})
        self.assertNotEqual("", result)
        self.assertIsInstance(result, str)

    def test_claims_is_excluded_but_client_claims_is_not(self):
        # Server-issued "claims" (claims_challenge) must not affect the key, because
        # it bypasses the cache. Client-originated "client_claims" must affect it.
        self.assertEqual("", _compute_ext_cache_key({"claims": '{"a": 1}'}))
        self.assertNotEqual("", _compute_ext_cache_key({"client_claims": '{"a": 1}'}))

    def test_same_client_claims_produce_same_hash(self):
        self.assertEqual(
            _compute_ext_cache_key({"client_claims": '{"a": 1}'}),
            _compute_ext_cache_key({"client_claims": '{"a": 1}'}))

    def test_different_client_claims_produce_different_hashes(self):
        self.assertNotEqual(
            _compute_ext_cache_key({"client_claims": '{"a": 1}'}),
            _compute_ext_cache_key({"client_claims": '{"a": 2}'}))

    def test_empty_client_claims_value_is_ignored(self):
        self.assertEqual("", _compute_ext_cache_key({"client_claims": ""}))


class TestClaimsHelpers(unittest.TestCase):
    """Tests for the shared _parse_claims_or_raise / _merge_claims helpers."""

    def test_parse_valid_object(self):
        self.assertEqual({"a": 1}, _parse_claims_or_raise('{"a": 1}'))

    def test_parse_rejects_non_object_and_malformed(self):
        for bad in ["not json", "[1, 2]", "null", "123", '"a string"', "true"]:
            with self.assertRaises(ValueError, msg="{!r} should raise".format(bad)):
                _parse_claims_or_raise(bad)

    def test_parse_rejects_non_string_types(self):
        # Non-str/bytes inputs make json.loads raise TypeError; the helper must
        # surface the same friendly ValueError so callers behave consistently
        # regardless of the bad input's type.
        for bad in [123, None, 1.5, True, ["a"], {"a": 1}]:
            with self.assertRaises(
                    ValueError, msg="{!r} should raise ValueError".format(bad)):
                _parse_claims_or_raise(bad)

    def test_parse_error_does_not_leak_raw_claims(self):
        # A malformed payload that contains a secret-looking value
        secret = '{"super": "secret-value-123"'  # missing closing brace
        with self.assertRaises(ValueError) as ctx:
            _parse_claims_or_raise(secret)
        self.assertNotIn("secret-value-123", str(ctx.exception),
            "Error message must never echo the raw claims content")

    def test_merge_returns_other_when_one_side_is_empty(self):
        self.assertEqual({"a": 1}, json.loads(_merge_claims(None, '{"a": 1}')))
        self.assertEqual({"a": 1}, json.loads(_merge_claims('{"a": 1}', None)))
        self.assertEqual({"a": 1}, json.loads(_merge_claims("", '{"a": 1}')))
        self.assertEqual({"a": 1}, json.loads(_merge_claims('{"a": 1}', "")))

    def test_merge_of_two_empties_is_falsy(self):
        self.assertFalse(_merge_claims(None, None))
        self.assertFalse(_merge_claims("", ""))

    def test_merge_deep_merges_objects(self):
        merged = json.loads(_merge_claims(
            '{"access_token": {"xms_cc": {"values": ["cp1"]}}}',
            '{"access_token": {"xms_az_nwperimid": {"essential": true}}}'))
        self.assertEqual(
            {"xms_cc": {"values": ["cp1"]}, "xms_az_nwperimid": {"essential": True}},
            merged["access_token"])

    def test_merge_leaf_conflict_second_arg_wins(self):
        # When both sides set the SAME leaf to different values, the second
        # argument (client-originated claims) wins. _acquire_token_for_client
        # passes server-issued claims first and forwarded_client_claims second,
        # so the caller's value takes precedence on a direct conflict.
        merged = json.loads(_merge_claims(
            '{"access_token": {"acrs": {"values": ["server"]}}}',
            '{"access_token": {"acrs": {"values": ["client"]}}}'))
        self.assertEqual({"values": ["client"]}, merged["access_token"]["acrs"])

    def test_merge_nested_conflict_preserves_disjoint_siblings(self):
        # A conflict on one nested key must not drop the other side's disjoint
        # sibling keys at the same level.
        merged = json.loads(_merge_claims(
            '{"access_token": {"a": {"values": ["server"]}, "keep_server": 1}}',
            '{"access_token": {"a": {"values": ["client"]}, "keep_client": 2}}'))
        self.assertEqual(
            {"a": {"values": ["client"]}, "keep_server": 1, "keep_client": 2},
            merged["access_token"])


class TestExtCacheKeyIsolation(unittest.TestCase):
    """Tests that ext_cache_key provides proper cache isolation in TokenCache."""

    def _build_event(self, client_id, scope, token_endpoint, access_token, data=None, **kwargs):
        return {
            "client_id": client_id,
            "scope": scope,
            "token_endpoint": token_endpoint,
            "response": build_response(access_token=access_token, expires_in=3600),
            "data": data or {},
            **kwargs,
        }

    def test_at_key_includes_ext_cache_key_when_present(self):
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        key_without = key_maker(
            home_account_id="", environment="env", client_id="cid",
            realm="realm", target="scope")
        key_with = key_maker(
            home_account_id="", environment="env", client_id="cid",
            realm="realm", target="scope", ext_cache_key="somehash")
        self.assertNotEqual(key_without, key_with,
            "Keys with and without ext_cache_key should differ")
        self.assertIn("somehash", key_with)

    def test_different_ext_cache_keys_produce_different_at_keys(self):
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        key_a = key_maker(
            home_account_id="", environment="env", client_id="cid",
            realm="realm", target="scope", ext_cache_key="hash_a")
        key_b = key_maker(
            home_account_id="", environment="env", client_id="cid",
            realm="realm", target="scope", ext_cache_key="hash_b")
        self.assertNotEqual(key_a, key_b)

    def test_fmi_tokens_are_stored_with_ext_cache_key(self):
        cache = TokenCache()
        event = self._build_event(
            "cid", ["s1"], "https://login.example.com/tenant/v2/token",
            "fmi_token", data={"fmi_path": "some/path"})
        cache.add(event)
        at_entries = list(cache.search(TokenCache.CredentialType.ACCESS_TOKEN, target=["s1"]))
        self.assertEqual(0, len(at_entries),
            "FMI tokens should NOT be found by a query without ext_cache_key")

    def test_fmi_tokens_found_with_matching_ext_cache_key_query(self):
        cache = TokenCache()
        ext_key = _compute_ext_cache_key({"fmi_path": "some/path"})
        event = self._build_event(
            "cid", ["s1"], "https://login.example.com/tenant/v2/token",
            "fmi_token", data={"fmi_path": "some/path"})
        cache.add(event)
        at_entries = list(cache.search(
            TokenCache.CredentialType.ACCESS_TOKEN, target=["s1"],
            query={"client_id": "cid", "environment": "login.example.com",
                   "realm": "tenant", "home_account_id": None,
                   "ext_cache_key": ext_key}))
        self.assertEqual(1, len(at_entries))
        self.assertEqual("fmi_token", at_entries[0]["secret"])

    def test_non_fmi_tokens_not_affected_by_fmi_cache(self):
        cache = TokenCache()
        # Add FMI token
        cache.add(self._build_event(
            "cid", ["s1"], "https://login.example.com/tenant/v2/token",
            "fmi_token", data={"fmi_path": "some/path"}))
        # Add regular token
        cache.add(self._build_event(
            "cid", ["s1"], "https://login.example.com/tenant/v2/token",
            "regular_token"))
        # Search without ext_cache_key should find only regular token
        at_entries = list(cache.search(
            TokenCache.CredentialType.ACCESS_TOKEN, target=["s1"],
            query={"client_id": "cid", "environment": "login.example.com",
                   "realm": "tenant", "home_account_id": None}))
        self.assertEqual(1, len(at_entries))
        self.assertEqual("regular_token", at_entries[0]["secret"])


class TestCrossMsalCacheKeyCompatibility(unittest.TestCase):
    """Verify that _compute_ext_cache_key produces hashes identical to the shared
    MSAL length-prefix ("netstring") encoding used by MSAL Go's
    CacheExtKeyGenerator (AzureAD/microsoft-authentication-library-for-go#629).

    The algorithm:
      1. Sort key-value pairs alphabetically by key (ordinal / case-sensitive)
      2. Length-prefix each part with its UTF-8 byte length and concatenate:
         "<len(key)>:<key><len(value)>:<value>..."
      3. SHA-256 hash
      4. Base64url encode (no padding), lowercased

    The length prefixes make the *serialization* injective: distinct component
    sets can never produce the same pre-hash string. (The final cache key is a
    SHA-256 digest, so only a cryptographically negligible hash collision
    remains possible.)

    NOTE: This is the encoding the whole MSAL SDK family is converging on
    (Go already merged it; .NET/Java/JS are landing the same change), so these
    hashes are byte-identical across SDKs. The cache *key format* (the 'atext'
    segment layout, asserted below) matches Go and .NET too. Caches are not
    shared across languages, so cross-language parity is a convenience, not a
    correctness requirement.
    """

    def test_two_params_hash_matches_shared_encoding(self):
        """Shared length-prefix expected: latlwkpewb_a0rcsmjvkecqt0_huumkw4sflzociike"""
        result = _compute_ext_cache_key({"key1": "value1", "key2": "value2"})
        self.assertEqual("latlwkpewb_a0rcsmjvkecqt0_huumkw4sflzociike", result)

    def test_two_different_params_hash_matches_shared_encoding(self):
        """Shared length-prefix expected: jjoe9jgfmdtnj0rzuetsqy7kzs2m1xfnjjxwsfxsrxq"""
        result = _compute_ext_cache_key({"key3": "value3", "key4": "value4"})
        self.assertEqual("jjoe9jgfmdtnj0rzuetsqy7kzs2m1xfnjjxwsfxsrxq", result)

    def test_five_params_hash_matches_shared_encoding(self):
        """Shared length-prefix expected (full hash): prrdp31y37ufw3lo7hly0oimjjvg_34m9ji30ocu4tw"""
        result = _compute_ext_cache_key({
            "key3": "value3", "key4": "value4",
            "key5": "value5", "key6": "value6", "key7": "value7",
        })
        self.assertEqual("prrdp31y37ufw3lo7hly0oimjjvg_34m9ji30ocu4tw", result)

    def test_order_independence_matches_shared_encoding(self):
        """Same keys in different insertion order must produce the same hash
        (mirrors TestCacheKeyComponentHashConsistency in Go)."""
        h1 = _compute_ext_cache_key({"key3": "value3", "key4": "value4",
                                      "key5": "value5", "key6": "value6", "key7": "value7"})
        h2 = _compute_ext_cache_key({"key7": "value7", "key4": "value4",
                                      "key6": "value6", "key5": "value5", "key3": "value3"})
        self.assertEqual(h1, h2)

    def test_at_cache_key_uses_atext_credential_type(self):
        """When ext_cache_key is present the credential type segment of the
        AT cache key must be 'atext' (not 'accesstoken'), matching Go/dotnet.

        Go:    {hid}-{env}-atext-{clientID}-{realm}-{scopes}-{hash}
        .NET:  {hid}-{env}-atext-{clientID}-{tenantId}-{scopes}-{hash}
        """
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        key = key_maker(
            home_account_id="hid", environment="env", client_id="cid",
            realm="realm", target="scope",
            ext_cache_key="latlwkpewb_a0rcsmjvkecqt0_huumkw4sflzociike")
        self.assertEqual(
            "hid-env-atext-cid-realm-scope-latlwkpewb_a0rcsmjvkecqt0_huumkw4sflzociike",
            key)

    def test_at_cache_key_without_ext_uses_accesstoken(self):
        """Regular ATs (no ext_cache_key) must keep 'accesstoken' credential type."""
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        key = key_maker(
            home_account_id="hid", environment="env", client_id="cid",
            realm="realm", target="scope")
        self.assertEqual("hid-env-accesstoken-cid-realm-scope", key)

    def test_full_at_cache_key(self):
        """Reproduce the full 'atext' cache key layout (mirrors MSAL .NET's
        CacheKeyExtensionTests expectedCacheKey1 shape) with the shared
        length-prefix hash for {key1:value1, key2:value2}.
        """
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        ext_hash = _compute_ext_cache_key({"key1": "value1", "key2": "value2"})
        key = key_maker(
            home_account_id="",
            environment="login.windows.net",
            client_id="d3adb33f-c0de-ed0c-c0de-deadb33fc0d3",
            realm="common",
            target="r1/scope1 r1/scope2",
            ext_cache_key=ext_hash)
        expected = "-login.windows.net-atext-d3adb33f-c0de-ed0c-c0de-deadb33fc0d3-common-r1/scope1 r1/scope2-latlwkpewb_a0rcsmjvkecqt0_huumkw4sflzociike"
        self.assertEqual(expected, key)

    def test_second_full_at_cache_key(self):
        """Reproduce the 'atext' cache key layout (mirrors expectedCacheKey2 shape)
        with the shared length-prefix hash for {key3:value3, key4:value4}."""
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        ext_hash = _compute_ext_cache_key({"key3": "value3", "key4": "value4"})
        key = key_maker(
            home_account_id="",
            environment="login.windows.net",
            client_id="d3adb33f-c0de-ed0c-c0de-deadb33fc0d3",
            realm="common",
            target="r1/scope1 r1/scope2",
            ext_cache_key=ext_hash)
        expected = "-login.windows.net-atext-d3adb33f-c0de-ed0c-c0de-deadb33fc0d3-common-r1/scope1 r1/scope2-jjoe9jgfmdtnj0rzuetsqy7kzs2m1xfnjjxwsfxsrxq"
        self.assertEqual(expected, key)

    def test_go_style_at_cache_key(self):
        """Reproduce the Go AccessToken.Key() *format* (segment layout):
        'testhid-env-atext-clientid-realm-user.read-{hash}'. The hash follows the
        shared length-prefix encoding (byte-identical to MSAL Go).
        """
        cache = TokenCache()
        key_maker = cache.key_makers[TokenCache.CredentialType.ACCESS_TOKEN]
        ext_hash = _compute_ext_cache_key({"key1": "value1", "key2": "value2"})
        key = key_maker(
            home_account_id="testhid",
            environment="env",
            client_id="clientid",
            realm="realm",
            target="user.read",
            ext_cache_key=ext_hash)
        expected = "testhid-env-atext-clientid-realm-user.read-latlwkpewb_a0rcsmjvkecqt0_huumkw4sflzociike"
        self.assertEqual(expected, key)


class TestExtCacheKeyCollisionResistance(unittest.TestCase):
    """The length-prefix ("netstring") serialization must be injective: no two
    semantically different component sets may serialize to the same pre-hash
    string. (The cache key itself is a SHA-256 digest, so the residual
    collision risk is only the cryptographically negligible hash-layer one;
    these tests pin the serialization layer that we control.)

    A plain ``key + value`` concatenation (the old scheme) is ambiguous and
    caused cache-slot collisions -- one FMI/agent-identity token entry could
    evict another, forcing redundant token re-fetches. These tests pin the
    boundary cases that the old scheme got wrong.
    """

    def test_key_value_boundary_ambiguity(self):
        # Old scheme: both -> "fmi_pathvalue".
        self.assertNotEqual(
            _compute_ext_cache_key({"fmi_path": "value"}),
            _compute_ext_cache_key({"fmi_pat": "hvalue"}))

    def test_multi_entry_boundary_ambiguity(self):
        # Old scheme: both -> "abcde".
        self.assertNotEqual(
            _compute_ext_cache_key({"a": "b", "cd": "e"}),
            _compute_ext_cache_key({"ab": "c", "d": "e"}))

    def test_value_containing_encoding_delimiters(self):
        # Values that themselves contain the "<len>:<data>" delimiters must not
        # be able to forge a different component layout.
        self.assertNotEqual(
            _compute_ext_cache_key({"a": "5:hello"}),
            _compute_ext_cache_key({"a5": "hello"}))
        self.assertNotEqual(
            _compute_ext_cache_key({"x": "1:y1:z"}),
            _compute_ext_cache_key({"x": "1:y", "1": "z"}))

    def test_injectivity_over_adversarial_alphabet(self):
        # Build many distinct component dicts from an adversarial alphabet that
        # stresses the length-prefix delimiters and UTF-8 boundaries, then assert
        # no two *distinct* dicts share a hash (and identical dicts agree).
        import itertools
        alphabet = ["", "0", "1", "9", ":", "|", "\\", "a", "ab",
                    u"\u00e9", u"\U0001F600", u"e\u0301"]
        seen = {}
        for k1, v1 in itertools.product(alphabet, repeat=2):
            for k2, v2 in itertools.product(alphabet, repeat=2):
                # Keys must be truthy to survive field-selection, and distinct so
                # the two pairs don't silently overwrite each other in a dict
                # (which would collapse intended 2-entry cases). Values must be
                # truthy too; empty values are dropped by field-selection.
                pairs = [(k, v) for k, v in ((k1, v1), (k2, v2)) if k and v]
                if len(pairs) == 2 and pairs[0][0] == pairs[1][0]:
                    continue
                comps = dict(pairs)
                if not comps:
                    continue
                # Canonicalize so logically-equal dicts compare equal.
                canonical = tuple(sorted(comps.items()))
                h = _compute_ext_cache_key(comps)
                if h in seen:
                    self.assertEqual(
                        seen[h], canonical,
                        "Hash collision between {!r} and {!r} -> {}".format(
                            dict(seen[h]), comps, h))
                else:
                    seen[h] = canonical
        # seen holds one entry per *distinct* component set, so its size proves
        # we exercised a meaningful number of unique inputs (not repeats).
        self.assertGreater(len(seen), 100)

    def test_utf8_byte_length_not_codepoint_length(self):
        # 'é' as one 2-byte codepoint (U+00E9) vs 'e' + combining acute accent
        # (U+0065 U+0301, 3 bytes) render alike but are distinct. A code-point
        # length prefix (len(str)) would risk a collision at some boundary; the
        # UTF-8 byte-length prefix keeps them apart and matches MSAL Go.
        precomposed = u"\u00e9"          # 1 code point, 2 UTF-8 bytes
        decomposed = u"e\u0301"          # 2 code points, 3 UTF-8 bytes
        self.assertNotEqual(
            _compute_ext_cache_key({"k": precomposed}),
            _compute_ext_cache_key({"k": decomposed}))
        # A concrete boundary pair the two length schemes disagree on:
        # code-point length would make these ambiguous, byte length does not.
        self.assertNotEqual(
            _compute_ext_cache_key({"k": u"\u00e9x"}),
            _compute_ext_cache_key({"k": u"e\u0301"}))

    def test_key_order_independence(self):
        self.assertEqual(
            _compute_ext_cache_key({"b": "2", "a": "1", "c": "3"}),
            _compute_ext_cache_key({"c": "3", "a": "1", "b": "2"}))

    def test_empty_and_single_entry_edges(self):
        self.assertEqual("", _compute_ext_cache_key(None))
        self.assertEqual("", _compute_ext_cache_key({}))
        self.assertEqual("", _compute_ext_cache_key({"fmi_path": ""}))
        single = _compute_ext_cache_key({"fmi_path": "p"})
        self.assertTrue(single)
        self.assertEqual(single, _compute_ext_cache_key({"fmi_path": "p"}))

    def test_golden_vectors(self):
        # Shared length-prefix golden vectors: byte-identical across the MSAL SDK
        # family (Go/.NET/Java/JS). Output is already lowercased.
        golden = {
            u"a0ry_zl4gccsdp7gnw927x8s0mrmnodv6tyilt0u07m":
                {"fmi_path": "agent-app-id"},
            u"cybgactkrvlzlen1aiwzwl3ay5krkyixommrobc-ri4":
                {"a": "b", "cd": "e"},
            u"n_lucewkadzv_nybtg-2wtorgf2nrns6ihlfa7vbuzg":
                {"fmi_path": "value"},
            u"tjtm16m-suk2_bkniblr25lyuki40qyceco7knuyu0k":
                {"fmi_pat": "hvalue"},
            u"xskzaoz4ibr3mznftyxctvg1ptuh-0fuzpty7ndbfls":
                {u"\u00e9": u"\u00e9"},
        }
        for expected, components in golden.items():
            self.assertEqual(expected, _compute_ext_cache_key(components))
