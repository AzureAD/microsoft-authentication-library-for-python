"""This OAuth2 client implementation aims to be spec-compliant, and generic."""
# OAuth2 spec https://tools.ietf.org/html/rfc6749

try:
    from urllib.parse import urlencode, parse_qs
except ImportError:
    from urlparse import parse_qs
    from urllib import urlencode

import requests


class Client(object):
    # This low-level interface works. Yet you'll find those *Grant sub-classes
    # more friendly to remind you what parameters are needed in each scenario.
    # More on Client Types at https://tools.ietf.org/html/rfc6749#section-2.1
    def __init__(
            self, client_id,
            client_credential=None,  # Only needed for Confidential Client
            authorization_endpoint=None, token_endpoint=None):
        self.client_id = client_id
        self.client_credential = client_credential
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint

    def _authorization_url(self, response_type, **kwargs):
        # response_type can be set to "code" or "token".
        params = {'client_id': self.client_id, 'response_type': response_type}
        params.update(kwargs)  # Note: None values will override params
        params = {k: v for k, v in params.items() if v is not None}  # clean up
        sep = '&' if '?' in self.authorization_endpoint else '?'
        return "%s%s%s" % (self.authorization_endpoint, sep, urlencode(params))

    def _get_token(self, grant_type, **kwargs):
        data = {'client_id': self.client_id, 'grant_type': grant_type}
        data.update(kwargs)  # Note: None values will override data
        # We don't need to clean up None values here, because requests lib will.

        # Quoted from https://tools.ietf.org/html/rfc6749#section-2.3.1
        # Clients in possession of a client password MAY use the HTTP Basic
        # authentication.
        # Alternatively, (but NOT RECOMMENDED,)
        # the authorization server MAY support including the
        # client credentials in the request-body using the following
        # parameters: client_id, client_secret.
        auth = None
        if (self.client_credential and data.get('client_id')
                and 'client_secret' not in data):
            auth = (data['client_id'], self.client_credential) # HTTP Basic Auth

        assert self.token_endpoint, "You need to provide token_endpoint"
        resp = requests.post(
            self.token_endpoint, headers={'Accept': 'application/json'},
            data=data, auth=auth)
        if resp.status_code>=500:
            resp.raise_for_status()  # TODO: Will probably retry here
        # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
        # even an error response will be a valid json structure,
        # so we simply return it here, without needing to invent an exception.
        return resp.json()

    def get_token_by_refresh_token(self, refresh_token, scope=None, **kwargs):
        return self._get_token(
            "refresh_token", refresh_token=refresh_token, scope=scope, **kwargs)


class AuthorizationCodeGrant(Client):
    # Can be used by Confidential Client or Public Client.
    # See https://tools.ietf.org/html/rfc6749#section-4.1.3

    def authorization_url(
            self, redirect_uri=None, scope=None, state=None, **kwargs):
        """Generate an authorization url to be visited by resource owner.

        :param redirect_uri: Optional. Server will use the pre-registered one.
        :param scope: It is a space-delimited, case-sensitive string.
            Some ID provider can accept empty string to represent default scope.
        """
        return super(AuthorizationCodeGrant, self)._authorization_url(
            'code', redirect_uri=redirect_uri, scope=scope, state=state,
            **kwargs)
        # Later when you receive the response at your redirect_uri,
        # validate_authorization() may be handy to check the returned state.

    def get_token(self, code, redirect_uri=None, **kwargs):
        """Get an access token.

        See also https://tools.ietf.org/html/rfc6749#section-4.1.3

        :param code: The authorization code received from authorization server.
        :param redirect_uri:
            Required, if the "redirect_uri" parameter was included in the
            authorization request, and their values MUST be identical.
        :param client_id: Required, if the client is not authenticating itself.
            See https://tools.ietf.org/html/rfc6749#section-3.2.1
        """
        return super(AuthorizationCodeGrant, self)._get_token(
            'authorization_code', code=code,
            redirect_uri=redirect_uri, **kwargs)


def validate_authorization(params, state=None):
    """A thin helper to examine the authorization being redirected back"""
    if not isinstance(params, dict):
        params = parse_qs(params)
    if params.get('state') != state:
        raise ValueError('state mismatch')
    return params


class ImplicitGrant(Client):
    """Implicit Grant is used to obtain access tokens (but not refresh token).

    It is optimized for public clients known to operate a particular
    redirection URI.  These clients are typically implemented in a browser
    using a scripting language such as JavaScript.
    Quoted from https://tools.ietf.org/html/rfc6749#section-4.2
    """
    def authorization_url(self, redirect_uri=None, scope=None, state=None):
        return super(ImplicitGrant, self)._authorization_url(
            'token', **locals())


class ResourceOwnerPasswordCredentialsGrant(Client):  # Legacy Application flow
    def get_token(self, username, password, scope=None, **kwargs):
        return super(ResourceOwnerPasswordCredentialsGrant, self)._get_token(
            "password", username=username, password=password, scope=scope,
            **kwargs)


class ClientCredentialGrant(Client):  # a.k.a. Backend Application flow
    def get_token(self, client_secret=None, scope=None, **kwargs):
        '''Get token by client credential.

        :param client_secret:
            You may explicitly provide it, so that it will show up in http body;
            Or you may skip it, the base class will use self.client_credentials;
            Or you may skip it and provide other parameters required by your AS.
        '''
        return super(ClientCredentialGrant, self)._get_token(
            "client_credentials", client_secret=client_secret, scope=scope,
            **kwargs)

