"""This OAuth2 client implementation aims to be spec-compliant, and generic."""
# OAuth2 spec https://tools.ietf.org/html/rfc6749

try:
    from urllib.parse import urlencode, parse_qs
except ImportError:
    from urlparse import parse_qs
    from urllib import urlencode

import requests


class BaseClient(object):
    # This low-level interface works. Yet you'll find its sub-class
    # more friendly to remind you what parameters are needed in each scenario.
    # More on Client Types at https://tools.ietf.org/html/rfc6749#section-2.1
    def __init__(
            self, client_id,
            client_secret=None,  # Triggers HTTP AUTH for Confidential Client
            default_body=None,  # a dict to be sent in each token request,
                # usually contains Confidential Client authentication parameters
                # such as {'client_id': 'your_id', 'client_secret': 'secret'}
                # if you choose to not use HTTP AUTH
            authorization_endpoint=None, token_endpoint=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.default_body = default_body or {}
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint

    def _authorization_url(self, response_type, **kwargs):
        # response_type can be set to "code" or "token".
        params = {'client_id': self.client_id, 'response_type': response_type}
        params.update(kwargs)  # Note: None values will override params
        params = {k: v for k, v in params.items() if v is not None}  # clean up
        if params.get('scope'):
            params['scope'] = self._normalize_to_string(params['scope'])
        sep = '&' if '?' in self.authorization_endpoint else '?'
        return "%s%s%s" % (self.authorization_endpoint, sep, urlencode(params))

    def _get_token(
            self, grant_type,
            query=None,  # a dict to be send as query string to the endpoint
            **kwargs  # All relevant parameters, which will go into the body
            ):
        data = {'client_id': self.client_id, 'grant_type': grant_type}
        data.update(self.default_body)  # It may contain authen parameters
        data.update(  # Here we use None to mean "use default value instead"
            {k: v for k, v in kwargs.items() if v is not None})
        # We don't have to clean up None values here, because requests lib will.

        if data.get('scope'):
            data['scope'] = self._normalize_to_string(data['scope'])

        # Quoted from https://tools.ietf.org/html/rfc6749#section-2.3.1
        # Clients in possession of a client password MAY use the HTTP Basic
        # authentication.
        # Alternatively, (but NOT RECOMMENDED,)
        # the authorization server MAY support including the
        # client credentials in the request-body using the following
        # parameters: client_id, client_secret.
        auth = None
        if self.client_secret and self.client_id:
            auth = (self.client_id, self.client_secret)  # for HTTP Basic Auth

        assert self.token_endpoint, "You need to provide token_endpoint"
        resp = requests.post(
            self.token_endpoint, headers={'Accept': 'application/json'},
            params=query, data=data, auth=auth)
        if resp.status_code>=500:
            resp.raise_for_status()  # TODO: Will probably retry here
        # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
        # even an error response will be a valid json structure,
        # so we simply return it here, without needing to invent an exception.
        return resp.json()

    def acquire_token_with_refresh_token(
            self, refresh_token, scope=None, **kwargs):
        return self._get_token(
            "refresh_token", refresh_token=refresh_token, scope=scope, **kwargs)

    def _normalize_to_string(self, scope):
        if isinstance(scope, (list, set, tuple)):
            return ' '.join(scope)
        return scope  # as-is


class Client(BaseClient):
    def authorization_url(
            self,
            response_type, redirect_uri=None, scope=None, state=None, **kwargs):
        """Generate an authorization url to be visited by resource owner.

        :param response_type:
            Must be "code" when you are using Authorization Code Grant.
            Must be "token" when you are using Implicit Grant
        :param redirect_uri: Optional. Server will use the pre-registered one.
        :param scope: It is a space-delimited, case-sensitive string.
            Some ID provider can accept empty string to represent default scope.
        """
        assert response_type in ["code", "token"]
        return self._authorization_url(
            response_type, redirect_uri=redirect_uri, scope=scope, state=state,
            **kwargs)
        # Later when you receive the response at your redirect_uri,
        # validate_authorization() may be handy to check the returned state.

    def acquire_token_with_authorization_code(
            self, code, redirect_uri=None, **kwargs):
        """Get a token via auhtorization code. a.k.a. Authorization Code Grant.

        This is typically used by a server-side app (Confidential Client),
        but it can also be used by a device-side native app (Public Client).
        See more detail at https://tools.ietf.org/html/rfc6749#section-4.1.3

        :param code: The authorization code received from authorization server.
        :param redirect_uri:
            Required, if the "redirect_uri" parameter was included in the
            authorization request, and their values MUST be identical.
        :param client_id: Required, if the client is not authenticating itself.
            See https://tools.ietf.org/html/rfc6749#section-3.2.1
        """
        return self._get_token(
            'authorization_code', code=code,
            redirect_uri=redirect_uri, **kwargs)

    def acquire_token_with_username_password(
            self, username, password, scope=None, **kwargs):
        """The Resource Owner Password Credentials Grant, used by legacy app."""
        return self._get_token(
            "password", username=username, password=password, scope=scope,
            **kwargs)

    def acquire_token_with_client_credentials(self, scope=None, **kwargs):
        '''Get token by client credentials. a.k.a. Client Credentials Grant,
        used by Backend Applications.

        You may want to explicitly provide an optional client_secret parameter,
        or you can provide such extra parameters as `default_body` during the
        class initialization.
        '''
        return self._get_token("client_credentials", scope=scope, **kwargs)


def validate_authorization(params, state=None):
    """A thin helper to examine the authorization being redirected back"""
    if not isinstance(params, dict):
        params = parse_qs(params)
    if params.get('state') != state:
        raise ValueError('state mismatch')
    return params

