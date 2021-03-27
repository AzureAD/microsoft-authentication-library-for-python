import json
import base64
import time
import random
import string
import warnings
import hashlib
try:
    from functools import lru_cache
except:
    def lru_cache():
        def dummy_decorator(func):
            return func
        return dummy_decorator


from . import oauth2

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


def decode_id_token(
        id_token, audiences=None, issuer=None, nonce=None,
        keys=None, algorithms=None,
        _now=None,  # Only for unit testing purpose
        _keys_cache={},  # Mutable dict used as an internal cache for this function
        client_id=None,  # Backward compatibility. Use audiences instead.
        ):
    """Decode and validate an id_token, return claims as a dictionary.

    This method is a lower level helper. You don't normally need to use this.
    Use this method :class:`oidc.Client.decode_id_token` instead.

    This method uses keyword-parameters, to allow arbitrary parameter order.

    ID token claims would at least contain: "iss", "sub", "aud", "exp", "iat",
    per `specs <https://openid.net/specs/openid-connect-core-1_0.html#IDToken>`_
    and it may contain other optional content such as "preferred_username",
    `maybe more <https://openid.net/specs/openid-connect-core-1_0.html#Claims>`_

    :param dict keys: A {"kid": JWK, ...} mapping.
    :param list algorithms: Algorithms accepted by underlying JWT library.
    """
    # Based on https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation

    claims = json.loads(decode_part(id_token.split('.')[1]))

    if client_id and not audiences:
        warnings.warn("Use ``audiences=[client_id]`` instead", DeprecationWarning)
        audiences = [client_id]

    if audiences:  # Mismatching audience is a common error, so we check it first
        assert "aud" in claims, '"aud" is a required claim per OIDC specs'
        audiences_in_claim = set(claims["aud"]) if isinstance(
            # The aud claim can contain a list of strings or a single string
            # https://openid.net/specs/openid-connect-core-1_0.html#IDToken
            claims["aud"], list) else set([claims["aud"]])
        if not set(audiences) & audiences_in_claim:
            raise ValueError(
                "3. The aud claim ({}) and your expected audiences ({}) "
                "should have intersection, case-sensitively. "
                # Some IdP accepts wrong casing request but issues right casing IDT
                "You should not attempt to decode/validate tokens "  # It is a FAQ
                "that were not issued to your app.".format(claims["aud"], audiences))

    if issuer and issuer != claims.get("iss"):  # "iss" is a required field,
        # however this implementation allows caller to opt out of this check.
        raise ValueError(
            '2. The Issuer Identifier for the OpenID Provider, "{}", '
            '(which is typically obtained during Discovery), '
            'MUST exactly match the value of the iss (issuer) Claim, "{}".'
            .format(issuer, claims.get("iss")))

    if nonce and nonce != claims.get("nonce"):
        raise ValueError(
            "11. Nonce in token ({}) should match expected value ({})".format(
            claims.get("nonce"), nonce))

    _now = int(_now or time.time())
    skew = 120  # in seconds
    TIME_SUGGESTION = "Make sure your computer's time and time zone are both correct."
    if "nbf" in claims and _now + skew < claims["nbf"]:  # nbf is optional per JWT specs
        # This is not an ID token validation, but a JWT validation
        # https://tools.ietf.org/html/rfc7519#section-4.1.5
        raise ValueError("JWT (nbf={}) is not yet valid. {}".format(
            claims["nbf"], TIME_SUGGESTION))
    if not ("exp" in claims and _now - skew < claims["exp"]):
        raise ValueError("9. Token expired at {}, current time is {}. {}".format(
            claims.get("exp"), _now, TIME_SUGGESTION))

    # We delay the signature validation to the end, so that those more common
    # "aud" failure etc. won't be masked by a blanket "Invalid signature".
    if keys and algorithms:
        # We allow the signature validation to be optional.  Per specs:
        # 6. If the ID Token is received via direct communication between
        # the Client and the Token Endpoint (which it is during _obtain_token()),
        # the TLS server validation MAY be used to validate the issuer
        # in place of checking the token signature.
        import jwt  # Lazy import. Run "pip install pyjwt[crypto]" to install.
            # https://renzolucioni.com/verifying-jwts-with-jwks-and-pyjwt/
            # String JWK since PyJWT 1.5.2 https://github.com/jpadilla/pyjwt/pull/202
            # Dict JWK since PR 511 in PyJWT 2.0.0
            # https://github.com/jpadilla/pyjwt/releases/tag/2.0.0
            #
            # Future Alternative:
            # pip install python-jose, which supports JWK set of RFC7517
            # https://github.com/mpdavis/python-jose/blob/3.2.0/jose/jws.py#L232-L238

        # Pre-process keys into _key_cache for PyJWT. Not needed if we use python-jose
        for kid, jwk in keys.items():
            if kid not in _keys_cache:
                _keys_cache[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))

        # Choose the needed key
        kid = json.loads(decode_part(id_token.split('.')[0])).get("kid")
        if kid not in _keys_cache:
            raise ValueError("Unknown kid: {}".format(kid))
        key = _keys_cache[kid]

        try:
            jwt.decode(id_token, key, algorithms=algorithms, options={
                "verify_aud": False,  # We will have flexible logic elsewhere
                })
        except jwt.InvalidAlgorithmError:
            raise ValueError("Invalid algorithm. Run pip install pyjwt[crypto]")
        except jwt.InvalidSignatureError:
            raise ValueError("Invalid signature")

    return claims


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

    @lru_cache()
    def _get_jwks(self, ttl_hash=None):
        """Return {kid: jwk} of current IdP"""
        del ttl_hash  # Learned from https://stackoverflow.com/a/55900800/728675
        return {key["kid"]: key for key in json.loads(
            self._http_client.get(self.configuration["jwks_uri"]).text)["keys"]}

    def decode_id_token(self, id_token, audiences=None, issuer=None):
        """Decode and validate ID token, also validate its signature.

        You do *not* need to use this method
        to validate ID token obtained freshly by any other methods of this class.
        Those methods already validate ID token for you.

        You only need to use this method on ID tokens obtained from elsewhere.

        :param audiences:
            By default, we validate id token's aud claim containing client_id.
            In rare case that your IdP would issue an alias in the aud claim,
            the caller would need to either provide all the aliases here,
            or use an empty list to disable this check.
        """
        assert "jwks_uri" in self.configuration, "Required field in OIDC Discovery"
        assert "id_token_signing_alg_values_supported" in self.configuration
        assert "issuer" in self.configuration, "Required field in OIDC Discovery"
        return decode_id_token(
            id_token,
            audiences=audiences or [self.client_id],
            issuer=issuer or self.configuration["issuer"],
            keys=self._get_jwks(
                # Although the specs mentions verifier to do on-demand key loading
                # https://openid.net/specs/openid-connect-core-1_0.html#RotateEncKeys
                # We do periodic update, good enough assuming IdP allows lead time.
                ttl_hash=int(time.time() / 3600)),  # Hourly update
            algorithms=self.configuration["id_token_signing_alg_values_supported"])

    def _obtain_token(self, grant_type, *args, **kwargs):
        """The result will also contain one more key "id_token_claims",
        whose value will be a dictionary returned by :func:`~decode_id_token`.
        """
        ret = super(Client, self)._obtain_token(grant_type, *args, **kwargs)
        if "id_token" in ret:
            # Does not need to validate a fresh ID token's signature or audience
            ret["id_token_claims"] = decode_id_token(ret["id_token"])
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
            If you provided a nonce when calling :func:`build_auth_request_uri`,
            same nonce should also be provided here, so that we'll validate it.
            An exception will be raised if the nonce in id token mismatches.
        """
        warnings.warn(
            "Use obtain_token_by_auth_code_flow() instead", DeprecationWarning)
        result = super(Client, self).obtain_token_by_authorization_code(
            code, **kwargs)
        nonce_in_id_token = result.get("id_token_claims", {}).get("nonce")
        if "id_token_claims" in result and nonce and nonce != nonce_in_id_token:
            raise ValueError(
                'The nonce in id token ("%s") should match your nonce ("%s")' %
                (nonce_in_id_token, nonce))
        return result

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
        nonce = "".join(random.sample(string.ascii_letters, 16))
        flow = super(Client, self).initiate_auth_code_flow(
            scope=_scope, nonce=_nonce_hash(nonce), **kwargs)
        flow["nonce"] = nonce
        if kwargs.get("max_age") is not None:
            flow["max_age"] = kwargs["max_age"]
        return flow

    def obtain_token_by_auth_code_flow(self, auth_code_flow, auth_response, **kwargs):
        """Validate the auth_response being redirected back, and then obtain tokens,
        including ID token which can be used for user sign in.

        Internally, it implements nonce to mitigate replay attack.
        It also implements PKCE to mitigate the auth code interception attack.

        See :func:`oauth2.Client.obtain_token_by_auth_code_flow` in parent class
        for descriptions on other parameters and return value.
        """
        result = super(Client, self).obtain_token_by_auth_code_flow(
            auth_code_flow, auth_response, **kwargs)
        if "id_token_claims" in result:
            nonce_in_id_token = result.get("id_token_claims", {}).get("nonce")
            expected_hash = _nonce_hash(auth_code_flow["nonce"])
            if nonce_in_id_token != expected_hash:
                raise RuntimeError(
                    'The nonce in id token ("%s") should match our nonce ("%s")' %
                    (nonce_in_id_token, expected_hash))

            if auth_code_flow.get("max_age") is not None:
                auth_time = result.get("id_token_claims", {}).get("auth_time")
                if not auth_time:
                    raise RuntimeError(
                        "13. max_age was requested, ID token should contain auth_time")
                now = int(time.time())
                skew = 120  # 2 minutes. Hardcoded, for now
                if now - skew > auth_time + auth_code_flow["max_age"]:
                    raise RuntimeError(
                            "13. auth_time ({auth_time}) was requested, "
                            "by using max_age ({max_age}) parameter, "
                            "and now ({now}) too much time has elasped "
                            "since last end-user authentication. "
                            "The ID token was: {id_token}".format(
                        auth_time=auth_time,
                        max_age=auth_code_flow["max_age"],
                        now=now,
                        id_token=json.dumps(result["id_token_claims"], indent=2),
                        ))
        return result

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

        Internally, it implements nonce to mitigate replay attack.
        It also implements PKCE to mitigate the auth code interception attack.

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

