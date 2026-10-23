import logging
import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import warnings
from unittest.mock import Mock, patch

from msal.token_cache import (
    TokenCache, SerializableTokenCache, _compute_ext_cache_key, _key_id_to_str,
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
        access_token=None, expires_in=3600, token_type="Bearer",
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


class _CountingAccessTokenEntries(dict):
    visits = 0

    def __iter__(self):
        for key in super().__iter__():
            self.visits += 1
            yield key

    def keys(self):
        return iter(self)

    def items(self):
        for item in super().items():
            self.visits += 1
            yield item

    def values(self):
        for value in super().values():
            self.visits += 1
            yield value


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

    def test_add_redacts_authentication_data_in_debug_log(self):
        sensitive_data = {
            "code": "SENSITIVE_AUTHORIZATION_CODE",
            "code_verifier": "SENSITIVE_PKCE_VERIFIER",
            "device_code": "SENSITIVE_DEVICE_CODE",
            "username": "SENSITIVE_REQUEST_USERNAME",
            "user_id": "SENSITIVE_USER_ID",
            "password": "SENSITIVE_PASSWORD",
            "client_secret": "SENSITIVE_CLIENT_SECRET",
            "client_assertion": "SENSITIVE_CLIENT_ASSERTION",
            "refresh_token": "SENSITIVE_REQUEST_REFRESH_TOKEN",
            "assertion": b"SENSITIVE_ASSERTION",
            "user_federated_identity_credential": "SENSITIVE_FEDERATED_CREDENTIAL",
            "client_claims": '{"custom_claim": "SENSITIVE_CLIENT_CLAIM"}',
            "claims": '{"access_token": {"custom_claim": "SENSITIVE_CLAIM"}}',
        }
        event = {
            "client_id": "my_client_id",
            "scope": ["s1"],
            "grant_type": "authorization_code",
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "username": "SENSITIVE_EVENT_USERNAME",
            "data": dict(sensitive_data, redirect_uri="http://localhost"),
            "response": build_response(
                uid="SENSITIVE_UID", utid="SENSITIVE_UTID",
                access_token="SENSITIVE_ACCESS_TOKEN",
                refresh_token="SENSITIVE_RESPONSE_REFRESH_TOKEN",
                id_token=build_id_token(sub="SENSITIVE_SUBJECT"),
                id_token_claims={"sub": "SENSITIVE_SUBJECT"},
                username="SENSITIVE_RESPONSE_USERNAME"),
        }
        original_event = copy.deepcopy(event)
        expected_log = copy.deepcopy(event)
        expected_log["username"] = "********"
        expected_log["data"].update({k: "********" for k in sensitive_data})
        expected_log["response"].update({k: "********" for k in (
            "access_token", "refresh_token", "id_token", "id_token_claims",
            "username", "client_info",
        )})

        with self.assertLogs("msal.token_cache", level="DEBUG") as cm:
            self.cache.add(event, now=1000)

        self.assertEqual(event, original_event)
        for credential_type, field in (
                (TokenCache.CredentialType.ACCESS_TOKEN, "access_token"),
                (TokenCache.CredentialType.REFRESH_TOKEN, "refresh_token"),
                (TokenCache.CredentialType.ID_TOKEN, "id_token")):
            entries = list(self.cache._cache[credential_type].values())
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["secret"], event["response"][field])
            self.assertEqual(
                entries[0]["home_account_id"], "SENSITIVE_UID.SENSITIVE_UTID")
        access_token = next(iter(
            self.cache._cache[TokenCache.CredentialType.ACCESS_TOKEN].values()))
        self.assertEqual(
            access_token["ext_cache_key"],
            _compute_ext_cache_key(original_event["data"]))
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(
            json.loads(cm.records[0].getMessage().removeprefix("event=")),
            expected_log)
        self.assertNotIn("SENSITIVE", "\n".join(cm.output))

    def test_add_redacts_username_without_changing_account(self):
        for username_location in ("data", "event"):
            with self.subTest(username_location=username_location):
                event = {
                    "client_id": "my_client_id",
                    "token_endpoint": "https://login.example.com/contoso/v2/token",
                    "data": {},
                    "response": build_response(uid="uid", utid="utid"),
                }
                source = event["data"] if username_location == "data" else event
                source["username"] = "SENSITIVE_USERNAME"
                original_event = copy.deepcopy(event)
                with self.assertLogs("msal.token_cache", level="DEBUG") as cm:
                    self.cache.add(event, now=1000)
                self.assertEqual(event, original_event)
                account = self.cache._cache["Account"][
                    "uid.utid-login.example.com-contoso"]
                self.assertEqual(account["username"], "SENSITIVE_USERNAME")
                self.assertNotIn("SENSITIVE_USERNAME", "\n".join(cm.output))

    def test_add_redacts_broker_account_id_without_changing_account(self):
        event = {
            "client_id": "my_client_id",
            "grant_type": "broker",
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "_account_id": "SENSITIVE_BROKER_ACCOUNT_ID",
            "response": build_response(
                uid="uid", utid="utid", access_token="at",
                id_token_claims={"sub": "subject"},
                _account_id="SENSITIVE_BROKER_ACCOUNT_ID"),
        }
        original_event = copy.deepcopy(event)
        with self.assertLogs("msal.token_cache", level="DEBUG") as cm:
            self.cache.add(event, now=1000)

        self.assertEqual(event, original_event)
        account = self.cache._cache["Account"][
            "uid.utid-login.example.com-contoso"]
        self.assertEqual(
            account["local_account_id"], "SENSITIVE_BROKER_ACCOUNT_ID")
        self.assertEqual(account["account_source"], "broker")
        self.assertEqual(len(cm.records), 1)
        logged_event = json.loads(
            cm.records[0].getMessage().removeprefix("event="))
        self.assertEqual(logged_event["_account_id"], "********")
        self.assertEqual(logged_event["response"]["_account_id"], "********")
        self.assertNotIn("SENSITIVE_BROKER_ACCOUNT_ID", "\n".join(cm.output))

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
                'token_type': 'Bearer',
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
                'token_type': 'Bearer',
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

    def _test_data_should_be_saved_and_searchable_in_access_token(
            self, data, token_type="some type"):
        scopes = ["s2", "s1", "s3"]  # Not in particular order
        now = 1000
        self.cache.add({
            "data": data,
            "client_id": "my_client_id",
            "scope": scopes,
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "response": build_response(
                uid="uid", utid="utid",  # client_info
                expires_in=3600, access_token="an access token", token_type=token_type,
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
        self._test_data_should_be_saved_and_searchable_in_access_token(
            {"key_id": "1"}, token_type="mtls_pop")
        self._test_data_should_be_saved_and_searchable_in_access_token(
            {"key_id": "2"}, token_type="mtls_pop")
        self.assertEqual(
            len(list(self.cache.search("AccessToken", now=1000))),
            2, """Access tokens are now isolated by key_id, so that a key-bound
mTLS token never overwrites another one bound to a different
key, nor a plain Bearer token for the same scope. We therefore keep 2 tokens.""")
        # Each key-bound token remains independently searchable by its own key_id
        for key_id in ("1", "2"):
            self.assertFoundAccessToken(
                scopes=["s1", "s2", "s3"], now=1000, query=dict(
                    key_id=key_id,
                    client_id="my_client_id",
                    environment="login.example.com",
                    realm="contoso",
                    home_account_id="uid.utid",
                ))

    def test_key_id_is_coerced_to_str_for_cache_key(self):
        # A bytes (or otherwise non-str) key_id must not raise a TypeError while
        # building the AT cache key (the "-" + key_id concatenation once assumed
        # str). This is defensive: current callers already pass an ASCII str.
        self.assertEqual("THUMB", _key_id_to_str(b"THUMB"))
        self.assertEqual("THUMB", _key_id_to_str("THUMB"))
        self.assertEqual("123", _key_id_to_str(123))  # Any other type -> str()
        # Bytes IDs are normalized before persistence and during lookup.
        self.cache = SerializableTokenCache()
        self.cache.add({
            "client_id": "my_client_id", "scope": ["s1"],
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "data": {"key_id": b"THUMB"},
            "response": build_response(access_token="bound_at"),
            }, now=1000)
        self.cache.deserialize(self.cache.serialize())
        for key_id in (b"THUMB", "THUMB"):
            entry = self.assertFoundAccessToken(
                scopes=["s1"], now=1000, query=dict(
                    client_id="my_client_id", environment="login.example.com",
                    realm="contoso", home_account_id=None, key_id=key_id))
            self.assertEqual("THUMB", entry["key_id"])
        self.assertEqual(1, len(self.cache._cache["AccessToken"]))

    def test_non_ascii_key_id_bytes_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "ASCII"):
            self.cache.add({
                "data": {"key_id": b"\xff"},
                "response": build_response(access_token="bound_at"),
                })

    def test_bound_scope_suffix_cannot_collide_with_unbound_scope(self):
        common = dict(
            client_id="my_client_id",
            token_endpoint="https://login.example.com/contoso/v2/token")
        events = [
            dict(common, scope=["scope"], data={"key_id": "abc"},
                 response=build_response(access_token="bound_at", token_type="mtls_pop")),
            dict(common, scope=["scope-abc"],
                 response=build_response(access_token="bearer_at", token_type="Bearer")),
            ]
        query = dict(
            client_id="my_client_id", environment="login.example.com",
            realm="contoso", home_account_id=None)
        for insertion_order in (events, list(reversed(events))):
            with self.subTest(order=[e["scope"] for e in insertion_order]):
                self.cache = TokenCache()
                for event in insertion_order:
                    self.cache.add(event, now=1000)
                self.assertEqual(2, len(list(self.cache.search("AccessToken", now=1000))))
                self.assertEqual("bearer_at", self.assertFoundAccessToken(
                    scopes=["scope-abc"], query=query, now=1000)["secret"])
                self.assertEqual("bound_at", self.assertFoundAccessToken(
                    scopes=["scope"], query=dict(query, key_id="abc"),
                    now=1000)["secret"])

    def test_legacy_bound_entries_are_isolated_and_removable(self):
        self.cache.add({
            "client_id": "my_client_id", "scope": ["s1"],
            "token_endpoint": "https://login.example.com/contoso/v2/token",
            "data": {"key_id": "THUMB"},
            "response": build_response(access_token="legacy_at", token_type="ssh-cert"),
            }, now=1000)
        entry = next(iter(self.cache._cache["AccessToken"].values()))
        legacy_key = self.at_key_maker(**dict(entry, key_id=None))
        for stored_key in (legacy_key, legacy_key + "-THUMB"):
            for expired in (False, True):
                with self.subTest(key=stored_key, expired=expired):
                    self.cache = SerializableTokenCache()
                    self.cache.deserialize(json.dumps({
                        "AccessToken": {stored_key: entry}}))
                    query = dict(
                        client_id="my_client_id", environment="login.example.com",
                        realm="contoso", home_account_id=None)
                    self.assertEqual([], list(self.cache.search(
                        "AccessToken", target=["s1"],
                        query=dict(query, token_type="Bearer"), now=1000)))
                    if expired:
                        list(self.cache.search("AccessToken", now=5000))
                    else:
                        self.assertFoundAccessToken(
                            scopes=["s1"], query=dict(query, key_id="THUMB"),
                            now=1000)
                        self.cache.remove_at(entry)
                    self.assertEqual({}, self.cache._cache["AccessToken"])
                    self.assertTrue(self.cache.has_state_changed)

    def test_fast_lookup_checks_stored_scope(self):
        entry = {
            "credential_type": "AccessToken", "home_account_id": None,
            "environment": "env", "client_id": "cid", "realm": "tenant",
            "target": "different", "secret": "wrong_at", "expires_on": "4600",
            }
        query = dict(
            home_account_id=None, environment="env", client_id="cid", realm="tenant")
        key = self.at_key_maker(**dict(query, target="requested"))
        self.cache._cache["AccessToken"] = {key: entry}
        self.assertEqual([], list(self.cache.search(
            "AccessToken", target=["requested"], query=query, now=1000)))

    def test_case_distinct_key_ids_remain_distinct_after_serialization(self):
        self.cache = SerializableTokenCache()
        for key_id in ("Thumb", "thumb"):
            self.cache.add({
                "scope": ["s1"], "data": {"key_id": key_id},
                "response": build_response(access_token=key_id, token_type="mtls_pop"),
                }, now=1000)
        self.cache.deserialize(self.cache.serialize())
        for key_id in ("Thumb", "thumb"):
            entries = list(self.cache.search(
                "AccessToken", target=["s1"], query={"key_id": key_id}, now=1000))
            self.assertEqual([key_id], [entry["secret"] for entry in entries])

    def test_bearer_and_mtls_pop_tokens_coexist_and_isolate(self):
        # The crux of mTLS PoP cache isolation (plan C6): a Bearer token and an
        # mtls_pop token for the SAME app/scope/tenant must coexist, and an
        # unkeyed (Bearer) lookup must never return the key-bound mtls_pop token.
        scopes = ["s2", "s1", "s3"]
        now = 1000
        common = dict(
            client_id="my_client_id", scope=scopes,
            token_endpoint="https://login.example.com/contoso/v2/token")
        # 1) Store a plain Bearer AT (no key_id)
        self.cache.add(dict(common, data={}, response=build_response(
            uid="uid", utid="utid", expires_in=3600,
            access_token="bearer_at", token_type="Bearer")), now=now)
        # 2) Store an mtls_pop AT bound to a cert key_id, same scope
        self.cache.add(dict(common, data={"key_id": "THUMB"}, response=build_response(
            uid="uid", utid="utid", expires_in=3600,
            access_token="mtls_at", token_type="mtls_pop")), now=now)
        self.assertEqual(
            2, len(list(self.cache.search("AccessToken", now=now))),
            "Bearer and mtls_pop ATs for the same scope must be separate entries")
        base_query = dict(
            client_id="my_client_id", environment="login.example.com",
            realm="contoso", home_account_id="uid.utid")
        # A Bearer lookup (no key_id) must return ONLY the Bearer token
        bearer = self.assertFoundAccessToken(
            scopes=scopes, now=now, query=dict(base_query, token_type="Bearer"))
        self.assertEqual("bearer_at", bearer["secret"])
        self.assertEqual("Bearer", bearer["token_type"])
        # A keyed lookup must return the mtls_pop token
        pop = self.assertFoundAccessToken(
            scopes=scopes, now=now,
            query=dict(base_query, key_id="THUMB", token_type="mtls_pop"))
        self.assertEqual("mtls_at", pop["secret"])
        self.assertEqual("mtls_pop", pop["token_type"])

    def test_broad_search_without_target_enumerates_key_bound_tokens(self):
        # Removal paths enumerate ATs by account with no target and no key_id.
        # Such a removal search must still surface key-bound (mtls_pop) ATs;
        # otherwise sign-out / remove_account would leave them behind. Regression
        # guard for the key_id isolation gate.
        scopes = ["s2", "s1", "s3"]
        now = 1000
        common = dict(
            client_id="my_client_id", scope=scopes,
            token_endpoint="https://login.example.com/contoso/v2/token")
        self.cache.add(dict(common, data={}, response=build_response(
            uid="uid", utid="utid", expires_in=3600,
            access_token="bearer_at", token_type="Bearer")), now=now)
        self.cache.add(dict(common, data={"key_id": "THUMB"}, response=build_response(
            uid="uid", utid="utid", expires_in=3600,
            access_token="mtls_at", token_type="mtls_pop")), now=now)
        owned_by_account = dict(
            client_id="my_client_id", environment="login.example.com",
            realm="contoso", home_account_id="uid.utid")
        # Public enumeration requires no opt-in to include both token types.
        found = list(self.cache.search(
            TokenCache.CredentialType.ACCESS_TOKEN, query=owned_by_account, now=now))
        secrets = {at["secret"] for at in found}
        self.assertIn("mtls_at", secrets,
            "A public search must enumerate key-bound ATs so they can be removed")
        self.assertIn("bearer_at", secrets)

    def test_empty_scopes_for_use_lookup_does_not_leak_key_bound_token(self):
        # A for-use lookup with EMPTY scopes is also target-less, but unlike a
        # removal enumeration (for_removal=True) it must NOT receive a key-bound
        # (mtls_pop) token it did not ask for. Isolation is unconditional for
        # for-use lookups, mirroring MSAL .NET (token-type isolation is never
        # gated on scope state). Regression for the empty-scopes bypass.
        scopes = ["s2", "s1", "s3"]
        now = 1000
        common = dict(
            client_id="my_client_id", scope=scopes,
            token_endpoint="https://login.example.com/contoso/v2/token")
        self.cache.add(dict(common, data={}, response=build_response(
            uid="uid", utid="utid", expires_in=3600,
            access_token="bearer_at", token_type="Bearer")), now=now)
        self.cache.add(dict(common, data={"key_id": "THUMB"}, response=build_response(
            uid="uid", utid="utid", expires_in=3600,
            access_token="mtls_at", token_type="mtls_pop")), now=now)
        base_query = dict(
            client_id="my_client_id", environment="login.example.com",
            realm="contoso", home_account_id="uid.utid", token_type="Bearer")
        # Empty scopes (target=[]) + no key_id + no for_removal -> only the unkeyed AT
        found = list(self.cache.search(
            TokenCache.CredentialType.ACCESS_TOKEN, target=[],
            query=base_query, now=now))
        secrets = {at["secret"] for at in found}
        self.assertNotIn("mtls_at", secrets,
            "Empty-scope for-use lookup must not leak the key-bound mtls_pop token")
        self.assertIn("bearer_at", secrets)

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


class ReleasedCacheCompatibilityTestCase(unittest.TestCase):

    def released_cache(self):
        cls = type(self)
        if hasattr(cls, "ReleasedCache"):
            return cls.ReleasedCache()
        try:
            source = subprocess.check_output(
                ["git", "show", "1.39.0:msal/token_cache.py"],
                cwd=str(Path(__file__).resolve().parents[1]),
                stderr=subprocess.DEVNULL).decode("utf-8-sig")
            application_source = subprocess.check_output(
                ["git", "show", "1.39.0:msal/application.py"],
                cwd=str(Path(__file__).resolve().parents[1]),
                stderr=subprocess.DEVNULL).decode("utf-8-sig")
        except (OSError, subprocess.CalledProcessError):
            raise unittest.SkipTest("Compatibility tests require the local 1.39.0 tag")
        namespace = {"__name__": "msal._released_token_cache", "__package__": "msal"}
        exec(compile(source, "1.39.0:msal/token_cache.py", "exec"), namespace)
        cls.ReleasedCache = namespace["SerializableTokenCache"]
        application = {"__name__": "msal._released_application", "__package__": "msal"}
        exec(compile(application_source, "1.39.0:msal/application.py", "exec"), application)
        application["TokenCache"] = namespace["TokenCache"]
        cls.ReleasedApplication = application["ConfidentialClientApplication"]
        return cls.ReleasedCache()

    def setUp(self):
        self.cache = SerializableTokenCache()
        self.query = dict(
            home_account_id=None, environment="login.example.com",
            client_id="client", realm="tenant")

    def event(self, scheme, secret, key_id="Thumb", scopes=None):
        return {
            "client_id": "client",
            "token_endpoint": "https://login.example.com/tenant/v2/token",
            "scope": scopes or ["s1", "s2"],
            "data": {"key_id": key_id} if key_id is not None else {},
            "response": build_response(access_token=secret, token_type=scheme),
        }

    def search(self, scheme=None, key_id=None, target=None, **kwargs):
        query = dict(self.query)
        if scheme is not None:
            query["token_type"] = scheme
        if key_id is not None:
            query["key_id"] = key_id
        return list(self.cache.search(
            "AccessToken", target=target, query=query, now=1000, **kwargs))

    def canonical_cache(self, count):
        cache = TokenCache()
        buckets = {
            name: _CountingAccessTokenEntries()
            for name in ("AccessToken", "AccessToken_mTLS")}
        for i in range(count):
            scheme = ("Bearer", "ssh-cert", "mtls_pop")[i % 3]
            entry = dict(
                credential_type="AccessToken", token_type=scheme,
                home_account_id=None, environment="login.example.com",
                client_id="fixture{}".format(i), realm="tenant", target="s1 s2",
                secret="fixture", cached_at="1000", expires_on="4600")
            if scheme != "Bearer":
                entry["key_id"] = "Thumb"
            bucket = "AccessToken_mTLS" if scheme == "mtls_pop" else "AccessToken"
            buckets[bucket][cache.key_makers["AccessToken"](**entry)] = entry
        cache._cache.update(buckets)
        return cache, buckets

    def released_application(self, cache):
        app = self.ReleasedApplication.__new__(self.ReleasedApplication)
        app.client_id = "client"
        app.token_cache = cache
        app.authority = Mock(instance="login.example.com", tenant="tenant")
        app._enable_broker = False
        app._build_telemetry_context = Mock()
        app._get_authority_aliases = Mock(return_value=[])
        app._acquire_token_for_client = Mock(return_value={"access_token": "from-network"})
        return app

    def test_released_writer_and_reader_keep_legacy_keys_and_cleanup(self):
        for scheme, key_id in (("Bearer", None), ("ssh-cert", "Thumb")):
            with self.subTest(scheme=scheme):
                released = self.released_cache()
                event = self.event(scheme, "old", key_id=key_id)
                released.add(event, now=1000)
                released_keys = set(json.loads(released.serialize())["AccessToken"])
                self.cache.deserialize(released.serialize())
                self.assertFalse(self.cache.has_state_changed)
                self.cache.add(self.event(scheme, "renewed", key_id=key_id), now=1000)
                self.cache.add(self.event("mtls_pop", "mtls"), now=1000)
                self.assertEqual(
                    released_keys, set(json.loads(self.cache.serialize())["AccessToken"]))
                released.deserialize(self.cache.serialize())
                found = list(released.search(
                    "AccessToken", target=["s1"], query=self.query, now=1000))
                self.assertEqual(["renewed"], [entry["secret"] for entry in found])
                with patch("time.time", return_value=1000):
                    self.released_application(released).remove_tokens_for_client()
                self.assertEqual([], list(released.search(
                    "AccessToken", query=self.query, now=1000)))
                self.cache.deserialize(released.serialize())
                self.assertEqual(["mtls"], [
                    entry["secret"] for entry in self.search("mtls_pop", "Thumb")])

    def test_released_ordinary_lookups_cannot_return_mtls(self):
        self.cache.add(self.event("mTlS_pOp", "mtls"), now=1000)
        released = self.released_cache()
        for with_bearer in (False, True):
            if with_bearer:
                self.cache.add(self.event("Bearer", "bearer", key_id=None), now=1000)
            released.deserialize(self.cache.serialize())
            for target in (["s1", "s2"], ["s1"], []):
                for query in (self.query, {"client_id": "client"}):
                    with self.subTest(bearer=with_bearer, target=target, query=query):
                        self.assertEqual(
                            ["bearer"] if with_bearer else [],
                            [entry["secret"] for entry in released.search(
                                "AccessToken", target=target, query=query, now=1000)])

    def test_actual_released_ordinary_acquisition_cannot_use_mtls(self):
        self.cache.add(self.event("mtls_pop", "mtls"), now=1000)
        for with_bearer in (False, True):
            if with_bearer:
                self.cache.add(self.event("Bearer", "bearer", key_id=None), now=1000)
            released = self.released_cache()
            released.deserialize(self.cache.serialize())
            for target in (["s1", "s2"], ["s1"], []):
                app = self.released_application(released)
                with patch("time.time", return_value=1000):
                    result = app._acquire_token_silent_from_cache_and_possibly_refresh_it(
                        target, None, app.authority)
                self.assertEqual(
                    "bearer" if with_bearer else "from-network", result["access_token"])
                self.assertEqual(not with_bearer, app._acquire_token_for_client.called)

    def test_released_ssh_renewal_and_subset_lookup_have_no_stale_duplicate(self):
        released = self.released_cache()
        released.add(self.event("ssh-cert", "old"), now=1000)
        self.cache.deserialize(released.serialize())
        self.cache.add(self.event("SSH-CERT", "renewed"), now=1001)
        for target in (["s1", "s2"], ["s1"], []):
            self.assertEqual(["renewed"], [
                entry["secret"] for entry in self.search(
                    "sSh-CeRt", "Thumb", target=target)])
        self.assertEqual(1, len(json.loads(self.cache.serialize())["AccessToken"]))

    def test_public_ssh_enumeration_and_cleanup_need_no_key_id_or_flag(self):
        self.cache.add(self.event("SSH-CERT", "ssh"), now=1000)
        self.cache.add(self.event("mtls_pop", "mtls"), now=1000)
        for target in (["s1", "s2"], ["s1"], []):
            self.assertEqual(["ssh"], [
                entry["secret"] for entry in self.search("ssh-cert", target=target)])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.assertEqual(["ssh"], [entry["secret"] for entry in self.cache.find(
                    "AccessToken", target=target,
                    query=dict(self.query, token_type="ssh-cert"), now=1000)])
        found = self.search()
        self.assertEqual({"ssh", "mtls"}, {entry["secret"] for entry in found})
        for entry in found:
            self.cache.remove_at(entry)
        self.assertEqual([], self.search())
        self.assertEqual([], self.search(for_removal=True))

    def test_same_key_id_isolated_by_scheme_in_both_insertion_orders(self):
        events = [self.event("sSh-CeRt", "ssh"), self.event("MtLs_PoP", "mtls")]
        for ordered in (events, list(reversed(events))):
            self.cache = SerializableTokenCache()
            for event in ordered:
                self.cache.add(event, now=1000)
            self.cache.deserialize(self.cache.serialize())
            for target in (["s1", "s2"], ["s1"], []):
                for scheme, secret in (("SSH-CERT", "ssh"), ("MTLS_POP", "mtls")):
                    self.assertEqual([secret], [
                        entry["secret"] for entry in self.search(
                            scheme, "Thumb", target=target)])
                self.assertEqual([], self.search("Bearer", target=target))
                self.assertEqual(["mtls"], [
                    entry["secret"] for entry in self.search("mtls_pop", target=target)])
            self.cache.remove_at(self.search("ssh-cert")[0])
            self.assertEqual(["mtls"], [
                entry["secret"] for entry in self.search("mtls_pop", "Thumb")])

    def test_missing_legacy_token_type_matches_bearer_case_insensitively(self):
        released = self.released_cache()
        released.add(self.event("Bearer", "bearer", key_id=None), now=1000)
        state = json.loads(released.serialize())
        for entry in state["AccessToken"].values():
            entry.pop("token_type")
        self.cache.deserialize(json.dumps(state))
        for target in (["s1", "s2"], ["s1"], []):
            self.assertEqual(["bearer"], [
                entry["secret"] for entry in self.search("bEaReR", target=target)])
            self.assertEqual([], self.search("ssh-cert", target=target))

    def test_pre_fix_serialized_state_migrates_and_renews_without_losing_certs(self):
        released = self.released_cache()
        released.add(self.event("ssh-cert", "ssh-old"), now=1000)
        ssh = next(iter(json.loads(released.serialize())["AccessToken"].values()))
        mtls = dict(ssh, token_type="mtls_pop", secret="mtls-old")
        other_cert = dict(mtls, key_id="thumb", secret="other-cert")
        newer_mtls = dict(mtls, cached_at="1002", secret="mtls-newer")
        self.cache.deserialize(json.dumps({
            "AccessToken": {
                "atkey:ssh": ssh, "atkey:mtls": mtls, "atkey:other": other_cert},
            "AccessToken_mTLS": {"atkey:newer": newer_mtls},
        }))
        state = json.loads(self.cache.serialize())
        self.assertEqual(
            {released.key_makers["AccessToken"](**ssh)}, set(state["AccessToken"]))
        reader = self.released_cache()
        reader.deserialize(self.cache.serialize())
        self.assertEqual(["ssh-old"], [entry["secret"] for entry in reader.search(
            "AccessToken", target=["s1"], query=self.query, now=1000)])
        self.assertEqual(["mtls-newer"], [
            entry["secret"] for entry in self.search("mtls_pop", "Thumb")])
        self.cache.add(self.event("SSH-CERT", "ssh-renewed"), now=1003)
        self.cache.add(self.event("MTLS_POP", "mtls-renewed"), now=1003)
        for target in (["s1", "s2"], ["s1"], []):
            self.assertEqual(["ssh-renewed"], [
                entry["secret"] for entry in self.search(
                    "ssh-cert", "Thumb", target=target)])
            self.assertEqual(["mtls-renewed"], [
                entry["secret"] for entry in self.search(
                    "mtls_pop", "Thumb", target=target)])
            self.assertEqual(["other-cert"], [
                entry["secret"] for entry in self.search(
                    "mtls_pop", "thumb", target=target)])
        self.assertEqual(3, len(self.search()))
        self.assertEqual([], list(self.cache.search("AccessToken", now=5000)))
        self.assertEqual([], self.search())

    def test_native_cache_renewal_replaces_noncanonical_bound_keys(self):
        for scheme in ("ssh-cert", "mtls_pop"):
            with self.subTest(scheme=scheme):
                self.cache = TokenCache()
                self.cache.add(self.event(scheme, "old"), now=1000)
                old = self.search(scheme, "Thumb")[0]
                self.cache._cache["AccessToken"] = {"atkey:old": old}
                self.cache.add(self.event("mtls_pop", "other", key_id="thumb"), now=1000)
                self.cache.add(self.event(scheme, "renewed"), now=1001)
                self.assertEqual(["renewed"], [
                    entry["secret"] for entry in self.search(
                        scheme, "Thumb", target=["s1"])])
                self.assertEqual(["other"], [
                    entry["secret"] for entry in self.search(
                        "mtls_pop", "thumb", target=["s1"])])
                self.assertEqual(2, len(self.search()))

    def test_deserialize_marks_migration_dirty_until_persisted(self):
        for scheme in ("ssh-cert", "mtls_pop"):
            with self.subTest(scheme=scheme):
                self.cache = SerializableTokenCache()
                self.cache.add(self.event(scheme, "cached"), now=1000)
                entry = self.search(scheme, "Thumb")[0]
                self.cache.deserialize(json.dumps({
                    "AccessToken": {"atkey:pre-fix": entry}}))
                self.assertTrue(self.cache.has_state_changed)
                self.assertEqual(["cached"], [
                    item["secret"] for item in self.search(
                        scheme, "Thumb", target=["s1", "s2"])])
                self.assertTrue(self.cache.has_state_changed)
                persisted = self.cache.serialize()
                self.assertFalse(self.cache.has_state_changed)
                self.cache.deserialize(persisted)
                self.assertFalse(self.cache.has_state_changed)
                self.cache.add(self.event(scheme, "renewed"), now=1001)
                self.assertEqual(["renewed"], [
                    entry["secret"] for entry in self.search(
                        scheme, "Thumb", target=["s1"])])

    def test_deserialize_does_not_invoke_persistence_hooks(self):
        class PersistenceHookCache(SerializableTokenCache):
            def modify(self, *args, **kwargs):
                raise AssertionError("deserialize reentered a persistence hook")

        for scheme in ("ssh-cert", "mtls_pop"):
            self.cache = SerializableTokenCache()
            self.cache.add(self.event(scheme, "cached"), now=1000)
            canonical = self.cache.serialize()
            entry = self.search(scheme, "Thumb")[0]
            for state in (canonical, json.dumps({
                    "AccessToken": {"atkey:old": entry}})):
                with self.subTest(scheme=scheme, state=state):
                    cache = PersistenceHookCache()
                    cache.deserialize(state)
                    self.assertEqual(["cached"], [
                        item["secret"] for item in cache.search(
                            "AccessToken", query={"token_type": scheme}, now=1000)])

    def test_persisted_cache_reloads_canonical_and_legacy_bound_tokens(self):
        try:
            from msal_extensions import FilePersistence, PersistedTokenCache
        except ImportError:
            self.skipTest("MSAL Extensions is not installed")

        for scheme in ("ssh-cert", "mtls_pop"):
            self.cache = SerializableTokenCache()
            self.cache.add(self.event(scheme, "cached"), now=1000)
            canonical = self.cache.serialize()
            entry = self.search(scheme, "Thumb")[0]
            for state in (canonical, json.dumps({
                    "AccessToken": {"atkey:old": entry}})):
                with self.subTest(scheme=scheme, state=state):
                    with tempfile.TemporaryDirectory() as directory:
                        persistence = FilePersistence(str(Path(directory) / "cache"))
                        persistence.save(state)
                        cache = PersistedTokenCache(persistence)
                        with patch.object(cache, "modify", side_effect=AssertionError(
                                "reload must not acquire a persistence lock")):
                            found = list(cache.search(
                                "AccessToken", query={"token_type": scheme}, now=1000))
                        self.assertEqual(["cached"], [item["secret"] for item in found])
                        cache.add(self.event(scheme, "renewed"), now=1001)
                        found = list(cache.search(
                            "AccessToken", query={"token_type": scheme}, now=1001))
                        self.assertEqual(["renewed"], [item["secret"] for item in found])
                        cache.remove_at(found[0])
                        reloaded = PersistedTokenCache(persistence)
                        self.assertEqual([], list(reloaded.search(
                            "AccessToken", now=1000)))

    def test_mtls_reload_identity_work_is_linear(self):
        from msal import token_cache

        self.cache.add(self.event("mtls_pop", "cached"), now=1000)
        sample = self.search("mtls_pop", "Thumb")[0]
        for legacy in (False, True):
            for count in (50, 100):
                entries = {}
                for i in range(count):
                    entry = dict(sample, target="scope{}".format(i))
                    key = ("atkey:old{}".format(i) if legacy else
                           self.cache.key_makers["AccessToken"](**entry))
                    entries[key] = entry
                state = json.dumps({
                    "AccessToken" if legacy else "AccessToken_mTLS": entries})
                with self.subTest(legacy=legacy, count=count), patch.object(
                        token_cache, "_access_token_identity",
                        wraps=token_cache._access_token_identity) as identity:
                    cache = SerializableTokenCache()
                    cache.deserialize(state)
                    self.assertLessEqual(identity.call_count, 4 * count)
                    self.assertEqual(legacy, cache.has_state_changed)
                self.assertEqual(count, len(list(cache.search(
                    "AccessToken", now=1000))))

    def test_canonical_bound_writes_do_not_scan_either_bucket(self):
        from msal import token_cache

        original_identity = token_cache._access_token_identity
        for count in (0, 100, 1000, 10000):
            for mtls_only in (False, True):
                with self.subTest(count=count, mtls_only=mtls_only):
                    self.cache, buckets = self.canonical_cache(count)
                    if mtls_only:
                        self.cache._cache.pop("AccessToken")
                    self.cache.add(self.event(
                        "mtls_pop", "warmup", scopes=["warmup"]), now=1000)
                    for entries in buckets.values():
                        entries.visits = 0
                    identity_calls = 0

                    def counted_identity(entry):
                        nonlocal identity_calls
                        identity_calls += 1
                        return original_identity(entry)

                    with patch.object(token_cache, "_access_token_identity",
                                      new=counted_identity):
                        for scheme, key_id in (
                                ("Bearer", None), ("ssh-cert", "Thumb"),
                                ("mtls_pop", "Thumb")):
                            for i in range(10):
                                scopes = ["{}-{}".format(scheme, i)]
                                for secret in ("inserted", "renewed"):
                                    self.cache.add(self.event(
                                        scheme, secret, key_id=key_id,
                                        scopes=scopes), now=1001)
                    self.assertEqual(
                        0, sum(entries.visits for entries in buckets.values()))
                    self.assertLessEqual(identity_calls, 120)
                    for scheme in ("Bearer", "ssh-cert", "mtls_pop"):
                        self.assertEqual(["renewed"], [
                            entry["secret"] for entry in self.search(
                                scheme, target=["{}-9".format(scheme)])])

    def test_bound_token_population_work_is_linear(self):
        from msal import token_cache

        original_identity = token_cache._access_token_identity
        for count in (100, 200, 400):
            with self.subTest(count=count):
                self.cache, buckets = self.canonical_cache(0)
                identity_calls = 0

                def counted_identity(entry):
                    nonlocal identity_calls
                    identity_calls += 1
                    return original_identity(entry)

                with patch.object(token_cache, "_access_token_identity",
                                  new=counted_identity):
                    for i in range(count):
                        scheme = ("Bearer", "ssh-cert", "mtls_pop")[i % 3]
                        self.cache.add(self.event(
                            scheme, "inserted", scopes=["scope{}".format(i)],
                            key_id=None if scheme == "Bearer" else "Thumb"),
                            now=1000)
                self.assertEqual(
                    0, sum(entries.visits for entries in buckets.values()))
                self.assertLessEqual(identity_calls, 2 * count)
                self.assertEqual(count, sum(len(entries) for entries in buckets.values()))

    def test_bound_writes_reconcile_replaced_native_cache_or_bucket(self):
        for scheme in ("ssh-cert", "mtls_pop"):
            for replacement in ("cache", "AccessToken", "AccessToken_mTLS"):
                with self.subTest(scheme=scheme, replacement=replacement):
                    self.cache = TokenCache()
                    self.cache.add(self.event(scheme, "old"), now=1000)
                    old = self.search(scheme, "Thumb")[0]
                    self.cache.add(self.event(
                        "mtls_pop", "other-certificate", key_id="thumb"), now=1000)
                    if replacement == "cache":
                        cache = dict(self.cache._cache)
                        cache.setdefault("AccessToken", {})["legacy:old"] = old
                        self.cache._cache = cache
                    else:
                        entries = dict(self.cache._cache.get(replacement, {}))
                        entries["legacy:old"] = old
                        self.cache._cache[replacement] = entries
                    self.cache.add(self.event(scheme, "renewed"), now=1001)
                    self.cache.remove_at(old)
                    self.assertEqual(["renewed"], [
                        entry["secret"] for entry in self.search(
                            scheme, "Thumb", target=["s1"])])
                    current = self.search(scheme, "Thumb")[0]
                    self.cache.remove_at(current)
                    self.assertEqual([], self.search(scheme, "Thumb"))
                    self.cache.add(self.event(scheme, "after-removal"), now=1002)
                    self.assertEqual(["after-removal"], [
                        entry["secret"] for entry in self.search(scheme, "Thumb")])
                    self.assertEqual(["other-certificate"], [
                        entry["secret"] for entry in self.search("mtls_pop", "thumb")])

    def test_bound_write_tracking_handles_shared_ssh_slots_and_stale_removal(self):
        self.cache = TokenCache()
        self.cache.add(self.event("ssh-cert", "old"), now=1000)
        old = self.search("ssh-cert", "Thumb")[0]
        for scheme, key_id in (
                ("Bearer", None), ("ssh-cert", "thumb"), ("ssh-cert", "Thumb")):
            self.cache.add(self.event(scheme, "replacement", key_id=key_id), now=1001)
            self.cache.remove_at(old)
            self.assertEqual(["replacement"], [
                entry["secret"] for entry in self.search(scheme, key_id)])
        self.cache.add(self.event("mtls_pop", "separate"), now=1001)
        self.cache.add(self.event("Bearer", "ordinary", key_id=None), now=1002)
        self.cache.add(self.event("SSH-CERT", "renewed"), now=1003)
        self.assertEqual(["renewed"], [
            entry["secret"] for entry in self.search("ssh-cert", "Thumb")])
        self.assertEqual(["separate"], [
            entry["secret"] for entry in self.search("mtls_pop", "Thumb")])

    def test_canonical_expiration_and_bulk_removal_do_not_scan_per_token(self):
        class CountingEntries(dict):
            scanned = 0

            def items(self):
                for item in super().items():
                    type(self).scanned += 1
                    yield item

        for scheme in ("Bearer", "mtls_pop"):
            self.cache = SerializableTokenCache()
            self.cache.add(self.event(scheme, "cached"), now=1000)
            sample = self.search(scheme, "Thumb")[0]
            for expired in (False, True):
                cache = TokenCache()
                entries = CountingEntries()
                for i in range(100):
                    entry = dict(sample, target="scope{}".format(i))
                    entries[cache.key_makers["AccessToken"](**entry)] = entry
                bucket = "AccessToken_mTLS" if scheme == "mtls_pop" else "AccessToken"
                cache._cache[bucket] = entries
                CountingEntries.scanned = 0
                with self.subTest(scheme=scheme, expired=expired):
                    found = list(cache.search(
                        "AccessToken", now=5000 if expired else 1000))
                    for entry in found:
                        cache.remove_at(entry)
                    self.assertEqual({}, entries)
                    self.assertLessEqual(CountingEntries.scanned, 300)

    def test_removal_preserves_replacements_and_cleans_legacy_duplicates(self):
        for scheme in ("ssh-cert", "mtls_pop"):
            self.cache = SerializableTokenCache()
            self.cache.add(self.event(scheme, "old"), now=1000)
            old = self.search(scheme, "Thumb")[0]
            canonical = json.loads(self.cache.serialize())
            canonical.setdefault("AccessToken", {})["atkey:duplicate"] = old
            self.cache.deserialize(json.dumps(canonical))
            self.assertEqual(1, len(self.search(scheme, "Thumb")))
            self.cache.remove_at(old)
            self.assertEqual([], self.search())

            cache = TokenCache()
            cache._cache["AccessToken"] = {"old1": old, "old2": dict(old)}
            cache.remove_at(old)
            self.assertEqual([], list(cache.search("AccessToken", now=1000)))

            self.cache.add(self.event(scheme, "renewed"), now=1001)
            self.cache.remove_at(old)
            self.assertEqual(["renewed"], [
                entry["secret"] for entry in self.search(scheme, "Thumb")])


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

    def test_empty_scopes_lookup_does_not_leak_ext_bound_token(self):
        # A for-use lookup with EMPTY scopes is target-less, but must NOT receive
        # an ext_cache_key-bound (FMI) token it did not ask for. Isolation is
        # unconditional for for-use lookups (for_removal defaults False).
        cache = TokenCache()
        cache.add(self._build_event(
            "cid", ["s1"], "https://login.example.com/tenant/v2/token",
            "fmi_token", data={"fmi_path": "some/path"}))
        cache.add(self._build_event(
            "cid", ["s1"], "https://login.example.com/tenant/v2/token",
            "regular_token"))
        # Empty scopes + no ext_cache_key + no for_removal -> only the regular token
        at_entries = list(cache.search(
            TokenCache.CredentialType.ACCESS_TOKEN, target=[],
            query={"client_id": "cid", "environment": "login.example.com",
                   "realm": "tenant", "home_account_id": None}))
        secrets = {at["secret"] for at in at_entries}
        self.assertNotIn("fmi_token", secrets,
            "Empty-scope for-use lookup must not leak the ext-bound FMI token")
        self.assertIn("regular_token", secrets)


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
