"""This module extends the OAuth2 Client with a builtin refresh token storage.

Design Goal(s):

1. Keep the number of user prompts as low as possible.

   This will be achieved by saving the refresh token (RT) into a storage
   and then automatically use them to acquire new access token (AT) later.
   Consequently, the RT concept will be abstracted away from the API,
   so API callers don't even need to know about and deal with RTs.

   - The token storage should be scalable,
     because a confidential client can potentially store lots of RTs in it.

   - The token storage should better be in a standard format,
     so it can potentially be shared among different languages or platforms,
     should the need arise.

2. AT cache for performance consideration is NOT implemented in this module.
"""
import time

from .oauth2 import Client


class AbstractStorage(object):  # The concrete implementation can be an RDBMS
    def find(self, query):
        raise NotImplementedError("Will return items matching given query")

    def add(self, item):
        raise NotImplementedError("Will add item into this storage")

    def remove(self, item):
        # TBD: Shall this be changed into remove(query)?
        raise NotImplementedError("Will remove item (if any) from this storage")

    def commit(self):
        """This can be useful when you use a db cursor instance as the storage.
        This default implementation does nothing, though."""


def _is_subdict_of(small, big):
    return dict(big, **small) == big


class StorageInRam(AbstractStorage):
    def __init__(self):
        self.storage = []
    def find(self, query):
        return [item for item in self.storage if _is_subdict_of(query, item)]
    def add(self, item):
        self.storage.append(item)
    def remove(self, item):
        self.storage.remove(item)


class ClientWithRefreshTokenStorage(Client):
    def __init__(
            self, client_id, refresh_token_storage=StorageInRam(), **kwargs):
        super(ClientWithRefreshTokenStorage, self).__init__(client_id, **kwargs)
        self.refresh_token_storage = refresh_token_storage

    def create_refresh_token_item(
            self,
            response,  # the raw response from authorization server
            scope  # The scope that was used to acquire the response
            ):  # Converts it into an item to be stored inside the token storage
        return {
            "client_id": self.client_id,
            "authority": self.token_endpoint,
            "user_id": "TBD %s" % response.get('id_token'),
            "refresh_token": response["refresh_token"],
            "scope": self._normalize_to_string(response.get("scope", scope)),
            "create_at": time.time(),
            }

    def _get_token(self, grant_type, *args, **kwargs):
        """Automatically maintains self.refresh_token_storage"""
        resp = super(ClientWithRefreshTokenStorage, self)._get_token(
            grant_type, *args, **kwargs)
        if 'error' not in resp and 'refresh_token' in resp:  # A RT is obtained
            self.refresh_token_storage.add(
                self.create_refresh_token_item(resp, kwargs.get('scope')))
            self.refresh_token_storage.commit()
        # If _get_token() is rejected, we could also remove old RT from storage.
        # But, this method knows only a RT but not the item containing this RT,
        # so we would have to delete by search:
        #       for item in find({'RT': kwargs['RT']}): delete(item)
        # which would typically be slow.
        # So we make a decision to only do storage cleaning in the other method,
        # acquire_token_with_refresh_token_in_storage(...), which can handily do
        #       delete(old_item)
        # The tradeoff is that, those acquire_token_with_refresh_token(...) call
        # which are NOT started by acquire_token_with_refresh_token_in_storage()
        # would then NOT trigger a RT cleanup. This is not a big issue though,
        # considering that we expect caller of this class no longer need to
        # directly invoke acquire_token_with_refresh_token(...) anyway.
        return resp

    def acquire_token_with_refresh_token_in_storage(self, query):
        """If a RT in the storage matches query,
        then use it to talk to authorization server, and acquire a fresh AT.
        Otherwise returns None.

        Caller is supposed to keep the returned AT,
        typically in a local session, which is out of the scope of this class.

        Usage:

            # Leverage a pre-existing RT (if any) to skip user interaction
            AT = client.acquire_token_with_refresh_token_in_storage(query)
            if AT is None:  # This happens when needed RT does not exist
                AT = client.acquire_token_with_one_of_actual_grants(...)
            # Now you end up with a fresh AT, so it will just work
            happily_access_resource_with(AT)
            # Of course, after some time, the AT may expire or get revoked,
            # so you will need to redo this process again.

        :param query: A query to be matched against those items in storage.
            It is typically a dict, e.g. {"client_id": "...", "scope": "..."}.
            Its exact format is intentionally left undefined by this method.
            Caller can use whatever criteria that fits its business logic,
            in whatever format that the backend storage supports.
        :returns: A dict with several keys including "access_token", or None
        """
        has_state_changed = False
        try:
            for item in self.refresh_token_storage.find(query):
                resp = self.acquire_token_with_refresh_token(
                    item["refresh_token"],
                    scope=query.get('scope', item.get('scope')))
                if resp.get('error') == 'invalid_grant' or 'error' not in resp:
                    self.refresh_token_storage.remove(item)  # Discard old RT
                    has_state_changed = True
                if 'error' not in resp:
                    return resp
        finally:
            if has_state_changed:  # commit all the changes during loop
                self.refresh_token_storage.commit()

