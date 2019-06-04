import json
import base64
import time

from . import oauth2


def base64decode(raw):
    """A helper can handle a padding-less raw input"""
    raw += '=' * (-len(raw) % 4)  # https://stackoverflow.com/a/32517907/728675
    return base64.b64decode(raw).decode("utf-8")


def decode_id_token(id_token, client_id=None, issuer=None, nonce=None, now=None):
    """Decodes and validates an id_token and returns its claims as a dictionary.

    ID token claims would at least contain: "iss", "sub", "aud", "exp", "iat",
    per `specs <https://openid.net/specs/openid-connect-core-1_0.html#IDToken>`_
    and it may contain other optional content such as "preferred_username",
    `maybe more <https://openid.net/specs/openid-connect-core-1_0.html#Claims>`_
    """
    decoded = json.loads(base64decode(id_token.split('.')[1]))
    err = None  # https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation
    if issuer and issuer != decoded["iss"]:
        # https://openid.net/specs/openid-connect-discovery-1_0.html#ProviderConfigurationResponse
        err = ('2. The Issuer Identifier for the OpenID Provider, "%s", '
            "(which is typically obtained during Discovery), "
            "MUST exactly match the value of the iss (issuer) Claim.") % issuer
    if client_id:
        valid_aud = client_id in decoded["aud"] if isinstance(
            decoded["aud"], list) else client_id == decoded["aud"]
        if not valid_aud:
            err = "3. The aud (audience) Claim must contain this client's client_id."
    # Per specs:
    # 6. If the ID Token is received via direct communication between
    # the Client and the Token Endpoint (which it is in this flow),
    # the TLS server validation MAY be used to validate the issuer
    # in place of checking the token signature.
    if (now or time.time()) > decoded["exp"]:
        err = "9. The current time MUST be before the time represented by the exp Claim."
    if nonce and nonce != decoded.get("nonce"):
        err = ("11. Nonce must be the same value "
            "as the one that was sent in the Authentication Request")
    if err:
        raise RuntimeError("%s id_token was: %s" % (
            err, json.dumps(decoded, indent=2)))
    return decoded


class Client(oauth2.Client):
    """OpenID Connect is a layer on top of the OAuth2.

    See its specs at https://openid.net/connect/
    """

    def decode_id_token(self, id_token, nonce=None):
        """See :func:`~decode_id_token`."""
        return decode_id_token(
            id_token, nonce=nonce,
            client_id=self.client_id, issuer=self.configuration.get("issuer"))

    def _obtain_token(self, grant_type, *args, **kwargs):
        """The result will also contain one more key "id_token_claims",
        whose value will be a dictionary returned by :func:`~decode_id_token`.
        """
        ret = super(Client, self)._obtain_token(grant_type, *args, **kwargs)
        if "id_token" in ret:
            ret["id_token_claims"] = self.decode_id_token(ret["id_token"])
        return ret

