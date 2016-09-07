from . import oauth2
from .exceptions import MsalServiceError


class BaseRequest(object):
    TOKEN_ENDPOINT_PATH = 'oauth2/v2.0/token'

    def __init__(
            self, authority=None, token_cache=None, scope=None, policy="",
            client_id=None, client_credential=None, authenticator=None,
            support_adfs=False, restrict_to_single_user=False):
        if not scope:
            raise ValueError("scope cannot be empty")
        self.__dict__.update(locals())

    def run(self):
        # TODO Some cache stuff here
        raw = self.get_token()
        if 'error' in raw:
            raise MsalServiceError(**raw)
        # TODO: Deal with refresh_token
        return {  # i.e. the AuthenticationResult
            "token": raw.get('access_token'),
            "expires_on": raw.get('expires_in'),  # TODO: Change into EPOCH
            "tenant_id": None,  # TODO
            "user": None,  # TODO
            "id_token": None,  # TODO
            "scope": set([]),  # TODO
            }

    def get_token(self):
        raise NotImplemented("Use proper sub-class instead")


class ClientCredentialRequest(BaseRequest):
    def get_token(self):
        return oauth2.ClientCredentialGrant(
            self.client_id,
            token_endpoint="%s%s?policy=%s" % (
                self.authority, self.TOKEN_ENDPOINT_PATH, self.policy),
            ).get_token(scope=self.scope, client_secret=self.client_credential)

