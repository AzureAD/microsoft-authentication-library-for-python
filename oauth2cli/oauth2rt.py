"""This module extends the OAuth2 Client with a builtin refresh token storage.

Design Goal(s):

1. Keep the number of user prompts as low as possible.

   This will be achieved by saving the refresh token (RT) into a storage
   and then automatically use them to acquire new access token (AT) later.
   Consequently, the RT concept will largely be abstracted away from the API,
   so callers don't even need to know about and deal with RTs.

   - The token storage should be scalable,
     because a confidential client can potentially store lots of RTs in it.

   - The token storage should be in a generic format,
     so it would potentially be shared among different languages or platforms.

2. AT cache is NOT implemented in this module.
"""
import time

from .oauth2 import Client


class AbstractStorage(object):  # The concrete implementation can be an RDBMS
    """Define the storage behaviors that will be used by this module."""

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


def build_item(client_id, token_endpoint, scope_in_request, response):
    # response is defined in https://tools.ietf.org/html/rfc6749#section-5.1
    # The scope is usually optional in both request and response, so likely None
    return {
        "scope": Client._stringify(response.get("scope", scope_in_request)),
        "client_id": client_id,
        "authority": token_endpoint,
        "refresh_token": response.get("refresh_token"),
        "access_token": response.get("access_token"),
        }


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
            self, client_id,
            refresh_token_storage=StorageInRam(),
            item_builder=build_item,  # We use this callback rather than a method
                # so that it can be customized without subclass this class
            **kwargs):
        super(ClientWithRefreshTokenStorage, self).__init__(client_id, **kwargs)
        self.refresh_token_storage = refresh_token_storage
        self.item_builder = item_builder

    def _obtain_token(self, grant_type, params=None, data=None, *args, **kwargs):
        """Automatically maintains self.refresh_token_storage"""
        resp = super(ClientWithRefreshTokenStorage, self)._obtain_token(
            grant_type, params, data, *args, **kwargs)
        if 'error' not in resp and 'refresh_token' in resp:  # A RT is obtained
            self.refresh_token_storage.add(self.item_builder(
                self.client_id, self.token_endpoint, (data or {}).get('scope'),
                resp))
            self.refresh_token_storage.commit()
        # if grant_type == "refresh_token" and "error" in resp,
        # then the old RT is rejected.
        # In such case, we could want to also remove old RT from storage.
        # But, this method knows only a RT but not the item containing this RT,
        # so we would have to delete by search:
        #       for item in find({'RT': kwargs['RT']}): delete(item)
        # which would typically be slpw.
        # So we make a decision to only do storage cleaning in the other method,
        # obtain_token_silent(...), which can handily do
        #       delete(old_item)
        # The tradeoff is that, those obtain_token_with_refresh_token(...) call
        # which are NOT initialized by obtain_token_silent()
        # would then NOT trigger a RT cleanup. This is not a big issue though,
        # considering that we expect caller of this class no longer need to
        # directly invoke obtain_token_with_refresh_token(...) anyway.
        return resp

    def obtain_token_silent(self, query):
        """If a RT in the storage matches query,
        then use it to talk to authorization server, and acquire a fresh AT.
        Otherwise returns None.

        Caller is supposed to keep the returned AT,
        typically in a local session, which is out of the scope of this class.

        Usage:

            # Leverage a pre-existing RT (if any) to skip user interaction
            AT = client.obtain_token_silent(query)
            if AT is None:  # This happens when a matching RT does not exist
                AT = client.acquire_token_with_one_of_actual_grants(...)

            # Now you end up with a fresh AT, so it will just work
            happily_access_resource_with(AT)

            # Of course, after some time, the AT may expire or get revoked,
            # so you will need to redo this process again.

        There can be cases that, even this method returns an AT for you,
        a resource server might still reject an AT with inadequate claims
        (such as missing MFA, or other condtional access policy).
        In those cases, repeating this method call will get you nowhere.
        App developer is expected to call other grants instead.

        :param query: A query to be matched against those items in storage.
            It is conceptually a dict, e.g. {"client_id": "...", "scope": "..."}.
            Its exact format, or even type, is decided by the token storage's
            find() method. So you may be able to use a lambda here.
        :returns: The json object from authorization server, or None
        """
        has_state_changed = False
        try:
            for item in self.refresh_token_storage.find(query):
                assert 'refresh_token' in item
                if isinstance(query, dict) and 'scope' in query:
                    scope = query['scope']
                else:
                    scope = item.get('scope')
                resp = self.obtain_token_with_refresh_token(
                    item["refresh_token"], scope)
                if resp.get('error') == 'invalid_grant' or 'refresh_token' in resp:
                    self.refresh_token_storage.remove(item)  # Discard old RT
                    has_state_changed = True
                if 'error' not in resp:
                    return resp
        finally:
            if has_state_changed:  # commit all the changes during loop
                self.refresh_token_storage.commit()

