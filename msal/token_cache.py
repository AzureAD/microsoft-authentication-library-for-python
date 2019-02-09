import json
import threading
import time
import logging
import base64

from .authority import canonicalize


logger = logging.getLogger(__name__)

def is_subdict_of(small, big):
    return dict(big, **small) == big

def base64decode(raw):  # This can handle a padding-less raw input
    raw += '=' * (-len(raw) % 4)  # https://stackoverflow.com/a/32517907/728675
    return base64.b64decode(raw).decode("utf-8")


class TokenCache(object):
    """This is considered as a base class containing minimal cache behavior.

    Although it maintains tokens using unified schema across all MSAL libraries,
    this class does not serialize/persist them.
    See subclass :class:`SerializableTokenCache` for details on serialization.
    """

    class CredentialType:
        ACCESS_TOKEN = "AccessToken"
        REFRESH_TOKEN = "RefreshToken"
        ACCOUNT = "Account"  # Not exactly a credential type, but we put it here
        ID_TOKEN = "IdToken"

    def __init__(self):
        self._lock = threading.RLock()
        self._cache = {}

    def find(self, credential_type, target=None, query=None):
        target = target or []
        assert isinstance(target, list), "Invalid parameter type"
        target_set = set(target)
        with self._lock:
            # Since the target inside token cache key is (per schema) unsorted,
            # there is no point to attempt an O(1) key-value search here.
            # So we always do an O(n) in-memory search.
            return [entry
                for entry in self._cache.get(credential_type, {}).values()
                if is_subdict_of(query or {}, entry)
                and (target_set <= set(entry.get("target", "").split())
		    if target else True)
                ]

    def add(self, event, now=None):
        # type: (dict) -> None
        # event typically contains: client_id, scope, token_endpoint,
        # resposne, params, data, grant_type
        for sensitive in ("password", "client_secret"):
            if sensitive in event.get("data", {}):
                # Hide them from accidental exposure in logging
                event["data"][sensitive] = "********"
        logger.debug("event=%s", json.dumps(event, indent=4, sort_keys=True,
            default=str,  # A workaround when assertion is in bytes in Python 3
            ))
        response = event.get("response", {})
        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token")
        id_token = response.get("id_token")
        client_info = {}
        home_account_id = None
        if "client_info" in response:
            client_info = json.loads(base64decode(response["client_info"]))
            home_account_id = "{uid}.{utid}".format(**client_info)
        environment = realm = None
        if "token_endpoint" in event:
            _, environment, realm = canonicalize(event["token_endpoint"])
        target = ' '.join(event.get("scope", []))  # Per schema, we don't sort it

        with self._lock:

            if access_token:
                key = "-".join([
                    home_account_id or "",
                    environment or "",
                    self.CredentialType.ACCESS_TOKEN,
                    event.get("client_id", ""),
                    realm or "",
		    target,
                    ]).lower()
                now = time.time() if now is None else now
                self._cache.setdefault(self.CredentialType.ACCESS_TOKEN, {})[key] = {
                    "credential_type": self.CredentialType.ACCESS_TOKEN,
                    "secret": access_token,
                    "home_account_id": home_account_id,
                    "environment": environment,
                    "client_id": event.get("client_id"),
                    "target": target,
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
                    # "client_info": response.get("client_info"),  # Optional
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
                    "credential_type": self.CredentialType.ID_TOKEN,
                    "secret": id_token,
                    "home_account_id": home_account_id,
                    "environment": environment,
                    "realm": realm,
                    "client_id": event.get("client_id"),
                    # "authority": "it is optional",
                    }

            if refresh_token:
                key = self._build_rt_key(
                    home_account_id, environment,
                    event.get("client_id", ""), target)
                rt = {
                    "credential_type": self.CredentialType.REFRESH_TOKEN,
                    "secret": refresh_token,
                    "home_account_id": home_account_id,
                    "environment": environment,
                    "client_id": event.get("client_id"),
                    "target": target,  # Optional per schema though
                    }
                if "foci" in response:
                    rt["family_id"] = response["foci"]
                self._cache.setdefault(self.CredentialType.REFRESH_TOKEN, {})[key] = rt

    @classmethod
    def _build_rt_key(
            cls,
            home_account_id=None, environment=None, client_id=None, target=None,
            **ignored):
        return "-".join([
            home_account_id or "",
            environment or "",
            cls.CredentialType.REFRESH_TOKEN,
            client_id or "",
            "",  # RT is cross-tenant in AAD
	    target,
            ]).lower()

    def remove_rt(self, rt_item):
        key = self._build_rt_key(**rt_item)
        with self._lock:
            self._cache.setdefault(self.CredentialType.REFRESH_TOKEN, {}).pop(key, None)

    def update_rt(self, rt_item, new_rt):
        key = self._build_rt_key(**rt_item)
        with self._lock:
            RTs = self._cache.setdefault(self.CredentialType.REFRESH_TOKEN, {})
            rt = RTs.get(key, {})  # key usually exists, but we'll survive its absence
            rt["secret"] = new_rt


class SerializableTokenCache(TokenCache):
    """This serialization can be a starting point to implement your own persistence.

    This class does NOT actually persist the cache on disk/db/etc..
    Depending on your need,
    the following simple recipe for file-based persistence may be sufficient::

        import atexit
        cache = SerializableTokenCache()
        cache.deserialize(open("my_cache.bin", "rb").read())
        atexit.register(lambda:
            open("my_cache.bin", "wb").write(cache.serialize())
            # Hint: The following optional line persists only when state changed
            if cache.has_state_changed else None
            )
        app = ClientApplication(..., token_cache=cache)
        ...

    :var bool has_state_changed:
        Indicates whether the cache state has changed since last
        :func:`~serialize` or :func:`~deserialize` call.
    """
    def add(self, event, **kwargs):
        super(SerializableTokenCache, self).add(event, **kwargs)
        self.has_state_changed = True

    def remove_rt(self, rt_item):
        super(SerializableTokenCache, self).remove_rt(rt_item)
        self.has_state_changed = True

    def update_rt(self, rt_item, new_rt):
        super(SerializableTokenCache, self).update_rt(rt_item, new_rt)
        self.has_state_changed = True

    def deserialize(self, state):
        # type: (Optional[str]) -> None
        """Deserialize the cache from a state previously obtained by serialize()"""
        with self._lock:
            self._cache = json.loads(state) if state else {}
            self.has_state_changed = False  # reset

    def serialize(self):
        # type: () -> str
        """Serialize the current cache state into a string."""
        with self._lock:
            self.has_state_changed = False
            return json.dumps(self._cache)

