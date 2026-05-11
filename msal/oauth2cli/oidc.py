import json
import base64
import time
import secrets
import warnings
import hashlib
import logging

from . import oauth2


logger = logging.getLogger(__name__)

def decode_part(raw, encoding="utf-8"):
    """Decode a part of the JWT.

    JWT is encoded by padding-less base64url,
    based on `JWS specs <https://tools.ietf.org/html/rfc7515#appendix-C>`_.

    :param encoding:
        If you are going to decode the first 2 parts of a JWT, i.e. the header
        or the payload, the default value "utf-8" would work fine.
        If you are going to decode the last part i.e. the signature part,
        it is a binary string so you should use `None` as encoding here.
    """
    raw += '=' * (-len(raw) % 4)  # https://stackoverflow.com/a/32517907/728675
    raw = str(
        # On Python 2.7, argument of urlsafe_b64decode must be str, not unicode.
        # This is not required on Python 3.
        raw)
    output = base64.urlsafe_b64decode(raw)
    if encoding:
        output = output.decode(encoding)
    return output

base64decode = decode_part  # Obsolete. For backward compatibility only.

def _epoch_to_local(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))

class IdTokenError(RuntimeError):  # We waised RuntimeError before, so keep it
    """In unlikely event of an ID token is malformed, this exception will be raised."""
    def __init__(self, reason, now, claims):
        super(IdTokenError, self).__init__(
            "%s Current epoch = %s.  The id_token was approximately: %s" % (
            reason, _epoch_to_local(now), json.dumps(dict(
                claims,
                iat=_epoch_to_local(claims["iat"]) if claims.get("iat") else None,
                exp=_epoch_to_local(claims["exp"]) if claims.get("exp") else None,
            ), indent=2)))

class _IdTokenTimeError(IdTokenError):  # This is not intended to be raised and caught
    _SUGGESTION = "Make sure your computer's time and time zone are both correct."
    def __init__(self, reason, now, claims):
        super(_IdTokenTimeError, self).__init__(reason+ " " + self._SUGGESTION, now, claims)
    def log(self):
        # Influenced by JWT specs https://tools.ietf.org/html/rfc7519#section-4.1.5
        # and OIDC specs https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation
        # We used to raise this error, but now we just log it as warning, because:
        # 1. If it is caused by incorrect local machine time,
        # then the token(s) are still correct and probably functioning,
        # so, there is no point to error out.
        # 2. If it is caused by incorrect IdP time, then it is IdP's fault,
        # There is not much a client can do, so, we might as well return the token(s)
        # and let downstream components to decide what to do.
        logger.warning(str(self))

class IdTokenIssuerError(IdTokenError):
    pass

class IdTokenAudienceError(IdTokenError):
    pass

class IdTokenNonceError(IdTokenError):
    pass

def decode_id_token(id_token, client_id=None, issuer=None, nonce=None, now=None):
    """Decodes an id_token and returns its claims as a dictionary.

    ID token claims would at least contain: "iss", "sub", "aud", "exp", "iat",
    per `specs <https://openid.net/specs/openid-connect-core-1_0.html#IDToken>`_
    and it may contain other optional content such as "preferred_username",
    `maybe more <https://openid.net/specs/openid-connect-core-1_0.html#Claims>`_

    The optional parameters ``client_id``, ``issuer``, ``nonce``, and ``now``
    are ignored and only kept for backward compatibility.
    """
    return json.loads(decode_part(id_token.split('.')[1]))


def _nonce_hash(nonce):
    # https://openid.net/specs/openid-connect-core-1_0.html#NonceNotes
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


class Prompt(object):
    """This class defines the constant strings for prompt parameter.

    The values are based on
    https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest
    """
    NONE = "none"
    LOGIN = "login"
    CONSENT = "consent"
    SELECT_ACCOUNT = "select_account"
    CREATE = "create"  # Defined in https://openid.net/specs/openid-connect-prompt-create-1_0.html#PromptParameter


class Client(oauth2.Client):
    """OpenID Connect is a layer on top of the OAuth2.

    See its specs at https://openid.net/connect/
    """

    def decode_id_token(self, id_token, nonce=None):
        """See :func:`~decode_id_token`."""
        return decode_id_token(id_token)

    def _obtain_token(self, grant_type, *args, **kwargs):
        """The result will also contain one more key "id_token_claims",
        whose value will be a dictionary returned by :func:`~decode_id_token`.
        """
        ret = super(Client, self)._obtain_token(grant_type, *args, **kwargs)
        if "id_token" in ret:
            ret["id_token_claims"] = self.decode_id_token(ret["id_token"])
        return ret

    def build_auth_request_uri(self, response_type, nonce=None, **kwargs):
        """Generate an authorization uri to be visited by resource owner.

        Return value and all other parameters are the same as
        :func:`oauth2.Client.build_auth_request_uri`, plus new parameter(s):

        :param nonce:
            A hard-to-guess string used to mitigate replay attacks. See also
            `OIDC specs <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        """
        warnings.warn("Use initiate_auth_code_flow() instead", DeprecationWarning)
        return super(Client, self).build_auth_request_uri(
            response_type, nonce=nonce, **kwargs)

    def obtain_token_by_authorization_code(self, code, nonce=None, **kwargs):
        """Get a token via authorization code. a.k.a. Authorization Code Grant.

        Return value and all other parameters are the same as
        :func:`oauth2.Client.obtain_token_by_authorization_code`,
        plus new parameter(s):

        :param nonce:
            Optional. Ignored and only kept for backward compatibility.
        """
        warnings.warn(
            "Use obtain_token_by_auth_code_flow() instead", DeprecationWarning)
        return super(Client, self).obtain_token_by_authorization_code(
            code, **kwargs)

    def initiate_auth_code_flow(
            self,
            scope=None,
            **kwargs):
        """Initiate an auth code flow.

        It provides nonce protection automatically.

        :param list scope:
            A list of strings, e.g. ["profile", "email", ...].
            This method will automatically send ["openid"] to the wire,
            although it won't modify your input list.

        See :func:`oauth2.Client.initiate_auth_code_flow` in parent class
        for descriptions on other parameters and return value.
        """
        if "id_token" in kwargs.get("response_type", ""):
            # Implicit grant would cause auth response coming back in #fragment,
            # but fragment won't reach a web service.
            raise ValueError('response_type="id_token ..." is not allowed')
        _scope = list(scope) if scope else []  # We won't modify input parameter
        if "openid" not in _scope:
            # "If no openid scope value is present,
            # the request may still be a valid OAuth 2.0 request,
            # but is not an OpenID Connect request." -- OIDC Core Specs, 3.1.2.2
            # https://openid.net/specs/openid-connect-core-1_0.html#AuthRequestValidation
            # Here we just automatically add it. If the caller do not want id_token,
            # they should simply go with oauth2.Client.
            _scope.append("openid")
        nonce = secrets.token_urlsafe(16)
        flow = super(Client, self).initiate_auth_code_flow(
            scope=_scope, nonce=_nonce_hash(nonce), **kwargs)
        flow["nonce"] = nonce
        if kwargs.get("max_age") is not None:
            flow["max_age"] = kwargs["max_age"]
        return flow

    def obtain_token_by_auth_code_flow(self, auth_code_flow, auth_response, **kwargs):
        """Validate the auth_response being redirected back, and then obtain tokens,
        including ID token which can be used for user sign in.

        This method still uses the nonce generated during flow initiation,
        but the SDK no longer validates that nonce against the ID token.

        It implements PKCE to mitigate the auth code interception attack.

        See :func:`oauth2.Client.obtain_token_by_auth_code_flow` in parent class
        for descriptions on other parameters and return value.
        """
        return super(Client, self).obtain_token_by_auth_code_flow(
            auth_code_flow, auth_response, **kwargs)

    def obtain_token_by_browser(
            self,
            display=None,
            prompt=None,
            max_age=None,
            ui_locales=None,
            id_token_hint=None,  # It is relevant,
                # because this library exposes raw ID token
            login_hint=None,
            acr_values=None,
            **kwargs):
        """A native app can use this method to obtain token via a local browser.

        It implements PKCE to mitigate the auth code interception attack.

        :param string display: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        :param string prompt: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
            You can find the valid string values defined in :class:`oidc.Prompt`.

        :param int max_age: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        :param string ui_locales: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        :param string id_token_hint: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        :param string login_hint: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        :param string acr_values: Defined in
            `OIDC <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.

        See :func:`oauth2.Client.obtain_token_by_browser` in parent class
        for descriptions on other parameters and return value.
        """
        filtered_params = {k:v for k, v in dict(
            prompt=" ".join(prompt) if isinstance(prompt, (list, tuple)) else prompt,
            display=display,
            max_age=max_age,
            ui_locales=ui_locales,
            id_token_hint=id_token_hint,
            login_hint=login_hint,
            acr_values=acr_values,
            ).items() if v is not None}  # Filter out None values
        return super(Client, self).obtain_token_by_browser(
            auth_params=dict(kwargs.pop("auth_params", {}), **filtered_params),
            **kwargs)
