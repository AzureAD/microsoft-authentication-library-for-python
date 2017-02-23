"""This module extends token_storage module with an access token cache.

Motivation: By reusing an AT to its longest possible length, your app skips
unnecessary authentication API calls on the wire, hence runs faster.

AT cache is particularly useful in command-line interface (CLI) programs because,
unlike web apps, CLI doesn't have a session across multiple CLI invocation.
"""
import time
import logging

from .token_storage import StorageInRam, ClientWithRefreshTokenStorage


class Cache(object):
    def __init__(self, storage=StorageInRam()):
        self.storage = storage  # The underlying storage for this cache
        self.logger = logging.getLogger(__name__)

    def lookup(
            self,
            query,  # A query consumed by the underlying storage.find(...)
            expires_in,
                # It can be an integer, or a callable to calculate an integer
                # based on the item returned by query. Example: lambda item: 123
            compute,  # It is a closure to compute the value during cache miss.
                      # It can also be None, meaning do not compute a new item.
            remove_expired_item=False,
            create_at=lambda item: item['create_at']  # the item creation time
            ):  # Returns either an item from cache, or return compute result
        has_state_changed = False
        try:
            for item in self.storage.find(query):
                expires_in_seconds = (
                    expires_in(item) if callable(expires_in) else expires_in)
                if create_at(item) + expires_in_seconds > time.time():  # valid
                    self.logger.warn('Cache hit')
                    return item
                if compute:
                    fresh = compute()
                    if fresh:  # Successfully acquire a new AT
                        self.logger.warn('Cache hit but expired')
                        self.storage.remove(item)
                        self.storage.add(fresh)
                        has_state_changed = True
                        return fresh
                    else:  # It means the compute() generates nothing
                        compute = None  # Then we disable further retry
                if remove_expired_item:
                    self.storage.remove(item)
                    has_state_changed = True
            else:  # In Python, this part is run when the for-loop loops nothing
                self.logger.warn('Cache missed')
                if compute:
                    fresh = compute()
                    if fresh:
                        self.storage.add(fresh)
                        has_state_changed = True
                    return fresh
        finally:
            if has_state_changed:  # commit all the changes during loop
                self.storage.commit()


class ClientWithAccessTokenCache(ClientWithRefreshTokenStorage):

    def __init__(self, client_id, access_token_cache=Cache(), **kwargs):
        super(ClientWithAccessTokenCache, self).__init__(client_id, **kwargs)
        self.access_token_cache = access_token_cache

    def normal_expiration(self, item):
        return item.get('expires_in', 3600)

    def extended_expiration(self, item):
        return item.get('ext_expires_in', item.get('expires_in', 3600) * 2)

    def create_access_token_item(self, response, scope):
        return {
            "client_id": self.client_id,
            "authority": self.token_endpoint,
            "user_id": "TBD %s" % response.get('id_token'),
            "access_token": response["access_token"],
            "id_token": response.get("id_token"),
            "scope": self._normalize_to_string(response.get("scope", scope)),
            "expires_in": response["expires_in"],
            "ext_expires_in": response.get("ext_expires_in", 0),
            "create_at": time.time(),
            }

    def acquire_token_from_cache(self, query, force_refresh=False):
        """This method makes best effort to return either a valid AT from cache,
        or a newly obtained AT if a suitable RT exists. Returns None otherwise.

        Note: when a cached AT is returned, it could already be revoked by user,
        and this class won't have a chance to know about it. So, in the scenario
        when the first AT returned with the default force_fresh=False flag fails,
        you are expected to call this method the second time with
        force_refresh=True. See the sample pattern below.

            # Try to acquire an AT, either from cache or by a preexisting RT
            AT = client.acquire_token_from_cache(query, force_refresh=False)
            if AT is None:  # This could happen if there is no valid AT nor RT
                # So you have to fall back to one of the actual methods
                AT = client.acquire_token_with_one_of_actual_grants(...)
            try:
                # Now the AT could be an old one from cache, or a fresh one
                access_resource_with(AT)
            except AuthenticationError:
                # This could happen when the AT was from cache and had been
                # revoked, so we would try to force a fresh AT based on RT
                AT = client.acquire_token_from_cache(query, force_refresh=True)
                if not AT:  # This could happen if there is no valid RT at all
                    # So you will have to fall back to the actual methods
                    AT = client.acquire_token_with_one_of_actual_grants(...)
                # Finally, you get a fresh AT, so it will just work
                access_resource_with(AT)
        """
        def real_job():
            resp = self.acquire_token_with_refresh_token_in_storage(query)
            if resp:
                return self.create_access_token_item(resp, query.get('scope'))
        if not force_refresh:  # Normal use case
            try:  # Search from cache first
                return self.access_token_cache.lookup(
                    query, expires_in=self.normal_expiration, compute=real_job)
            except IOError:  # Resillency: an older AT is the best we can have
                return self.access_token_cache.lookup(
                    query, expires_in=self.extended_expiration, compute=None)
        else:  # Ignore existing AT (if any), go get a new one AND THEN cache it
            return self.access_token_cache.lookup(
                query, expires_in=0, compute=real_job)

    def acquire_token_with_client_credentials(
            self, scope=None, force_refresh=False, **kwargs):
        """Acquires an AT for the client, with the help of builtin cache.

        Usage:

            # Acquire an AT, which probably came from cache
            AT = self.acquire_token_with_client_credentials(scope="scope1")
            try:
                access_the_resource_with(AT)
            except AuthenticationError:
                # Acquire a fresh AT with actual Client Credential Grant
                AT = self.acquire_token_with_client_credentials(
                    scope="scope1", force_refresh=True)
                access_the_resource_with(AT)

        :param scope: The scope specified by caller.
        :param force_refresh: Force to return a fresh AT.
            Note: Not every other acquire_token_with_blah() method provides such
            force_refresh behavior, even if they technically accepts such param.
        """
        token = self.acquire_token_from_cache({
            "client_id": self.client_id,
            "user_id": None,
            "authority": self.token_endpoint,
            }, force_refresh=force_refresh)
                # There is no RT returned for earlier Client Credential grant
                # so a force_refresh=True call will always return None
        if token is None:
            token = super(ClientWithAccessTokenCache, self) \
                .acquire_token_with_client_credentials(scope, **kwargs)
        return token

