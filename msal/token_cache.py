import json
import threading
import time
import logging
import base64

from .authority import canonicalize

def _string_cmp(str1, str2):
    '''Case insensitive comparison. Return true if both are None'''
    str1 = str1 if str1 is not None else ''
    str2 = str2 if str2 is not None else ''
    return str1.lower() == str2.lower()

class TokenCacheKey(object): # pylint: disable=too-few-public-methods
    def __init__(self, authority, resource, client_id, user_id):
        self.authority = authority
        self.resource = resource
        self.client_id = client_id
        self.user_id = user_id

    def __hash__(self):
        return hash((self.authority, self.resource, self.client_id, self.user_id))

    def __eq__(self, other):
        return _string_cmp(self.authority, other.authority) and \
               _string_cmp(self.resource, other.resource) and \
               _string_cmp(self.client_id, other.client_id) and \
               _string_cmp(self.user_id, other.user_id)

# pylint: disable=protected-access

def _get_cache_key(entry):
    return TokenCacheKey(
        entry.get(TokenResponseFields._AUTHORITY),
        entry.get(TokenResponseFields.RESOURCE),
        entry.get(TokenResponseFields._CLIENT_ID),
        entry.get(TokenResponseFields.USER_ID))


def is_subdict_of(small, big):
    return dict(big, **small) == big

def base64decode(raw):  # This can handle a padding-less raw input
    raw += '=' * (-len(raw) % 4)  # https://stackoverflow.com/a/32517907/728675
    return base64.b64decode(raw).decode("utf-8")


class TokenCache(object):

    class CredentialType:
        ACCESS_TOKEN = "AccessToken"
        REFRESH_TOKEN = "RefreshToken"
        ACCOUNT = "Account"  # Not exactly a credential type, but we put it here
        ID_TOKEN = "IdToken"

    def __init__(self, state=None):
        self._cache = {}
        self._lock = threading.RLock()
        if state:
            self.deserialize(state)
        self.has_state_changed = False

    def _find(self, credential_type, target=None, query=None):
        target = target or []
        assert isinstance(target, list), "Invalid parameter type"
        with self._lock:
            return [entry
                for entry in self._cache.get(credential_type, {}).values()
                if is_subdict_of(query or {}, entry)
                and set(target) <= set(entry.get("target", []))]

    def find(self, query):
        with self._lock:

            return [entry
                for entry in self._cache.get(query["credential_type"], {}).values()
                if is_subdict_of({
                    "client_id": query["client_id"],
                    "home_account_id": query["account"].home_account_id()  # TODO
                        if query.get("account") else None,
                    }, entry)]

            return self._query_cache(
                query.get(TokenResponseFields.IS_MRRT),
                query.get(TokenResponseFields.USER_ID),
                query.get(TokenResponseFields._CLIENT_ID))

    def remove(self, entries):
        with self._lock:
            for e in entries:
                key = _get_cache_key(e)
                removed = self._cache.pop(key, None)
                if removed is not None:
                    self.has_state_changed = True

    def _add(self, **event):  # TODO: Changes to a normal dict
            # lambda client_id=None, scope=None, token_endpoint=None,
            #        response=None, params=None, data=None, **kwargs:
            #        None,
        logging.debug("event=%s", json.dumps(event, indent=4))
        response = event.get("response", {})
        access_token = response.get("access_token", {})
        refresh_token = response.get("refresh_token", {})
        id_token = response.get("id_token", {})
        client_info = {}
        home_account_id = None
        if "client_info" in response:
            client_info = json.loads(base64decode(response["client_info"]))
            home_account_id = "{uid}.{utid}".format(**client_info)
        environment = realm = None
        if "token_endpoint" in event:
            _, environment, realm = canonicalize(event["token_endpoint"])
        with self._lock:
            if refresh_token:
                self._cache.setdefault(self.CredentialType.REFRESH_TOKEN, {})["key"] = {}
            if access_token:
                key = "-".join([
                    home_account_id or "",
                    environment or "",
                    self.CredentialType.ACCESS_TOKEN,
                    event.get("client_id", ""),
                    realm or "",
                    ' '.join(sorted(event.get("scope", []))),
                    ]).lower()
                now = time.time()
                self._cache.setdefault(self.CredentialType.ACCESS_TOKEN, {})[key] = {
                    "home_account_id": home_account_id,
                    "environment": environment,
                    "credential_type": self.CredentialType.ACCESS_TOKEN,
                    "client_id": event.get("client_id"),
                    "secret": access_token,
                    "target": event.get("scope"),
                    "realm": realm,
                    "cached_at": now,
                    "expires_on": now + response.get("expires_in", 3599),
                    "extended_expires_on": now + response.get("ext_expires_in", 0),
                    }
            if client_info:
                decoded_id_token = json.loads(
                    base64decode(id_token.split('.')[1])) if id_token else {}
                key = "-".join([
                    home_account_id or "",
                    environment or "",
                    realm or "",
                    ]).lower()
                self._cache.setdefault(self.CredentialType.ACCOUNT, {})[key] = {
                    "home_account_id": home_account_id,
                    "environment": environment,
                    "realm": realm,
                    "local_account_id": decoded_id_token.get(
                        "oid", decoded_id_token.get("sub")),
                    "username": decoded_id_token.get("preferred_username"),
                    "authority_type": "AAD",  # Always AAD?
                    }
            if id_token:
                key = "-".join([
                    home_account_id or "",
                    environment or "",
                    self.CredentialType.ID_TOKEN,
                    event.get("client_id", ""),
                    realm or "",
                    ]).lower()
                self._cache.setdefault(self.CredentialType.ID_TOKEN, {})[key] = {
                    "home_account_id": home_account_id,
                    "environment": environment,
                    "realm": realm,
                    "credential_type": self.CredentialType.ID_TOKEN,
                    "client_id": event.get("client_id"),
                    "secret": id_token,
                    # "authority": "it is optional",
                    }

    def _remove_rt(self, rt_item):
        with self._lock:
            print(rt_item)

    def _update_rt(self, rt_item, new_rt):
        with self._lock:
            print(rt_item, new_rt)

    def add(self, entries):
        with self._lock:
            for e in entries:
                key = _get_cache_key(e)
                self._cache[key] = e
            self.has_state_changed = True

    def serialize(self):
        with self._lock:
            return json.dumps(list(self._cache.values()))

    def deserialize(self, state):
        with self._lock:
            self._cache.clear()
            if state:
                tokens = json.loads(state)
                for t in tokens:
                    key = _get_cache_key(t)
                    self._cache[key] = t

    def read_items(self):
        '''output list of tuples in (key, authentication-result)'''
        with self._lock:
            return self._cache.items()

    def _query_cache(self, is_mrrt, user_id, client_id):
        matches = []
        for k in self._cache:
            v = self._cache[k]
            #None value will be taken as wildcard match
            #pylint: disable=too-many-boolean-expressions
            if ((is_mrrt is None or is_mrrt == v.get(TokenResponseFields.IS_MRRT)) and
                    (user_id is None or _string_cmp(user_id, v.get(TokenResponseFields.USER_ID))) and
                    (client_id is None or _string_cmp(client_id, v.get(TokenResponseFields._CLIENT_ID)))):
                matches.append(v)
        return matches
