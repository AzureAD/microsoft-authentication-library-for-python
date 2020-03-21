from ..oidc import AbstractClient
from . import oauth2


class Client(oauth2.Client, AbstractClient):
    """OpenID Connect is a layer on top of the OAuth2.

    See its specs at https://openid.net/connect/
    """

    async def _obtain_token(self, grant_type, *args, **kwargs):
        """The result will also contain one more key "id_token_claims",
        whose value will be a dictionary returned by :func:`~decode_id_token`.
        """
        ret = await super(Client, self)._obtain_token(grant_type, *args, **kwargs)
        if "id_token" in ret:
            ret["id_token_claims"] = self.decode_id_token(ret["id_token"])
        return ret

    async def obtain_token_by_authorization_code(self, code, nonce=None, **kwargs):
        """Get a token via auhtorization code. a.k.a. Authorization Code Grant."""
        result = await super(Client, self).obtain_token_by_authorization_code(
            code, **kwargs)
        nonce_in_id_token = result.get("id_token_claims", {}).get("nonce")
        if "id_token_claims" in result and nonce and nonce != nonce_in_id_token:
            raise ValueError(
                'The nonce in id token ("%s") should match your nonce ("%s")' %
                (nonce_in_id_token, nonce))
        return result

