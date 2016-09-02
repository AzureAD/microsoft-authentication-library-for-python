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
    def __init__(
            self, client_id,
            client_credential=None,  # Only needed for Confidential Client
            authorization_endpoint=None, token_endpoint=None):
        self.client_id = client_id
        self.client_credential = client_credential
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint

    def authorization_url(self, response_type, **kwargs):
        params = {'client_id': self.client_id, 'response_type': response_type}
        params.update(kwargs)
        params = {k: v for k, v in params.items() if v is not None}  # clean up
        sep = '&' if '?' in self.authorization_endpoint else '?'
        return "%s%s%s" % (self.authorization_endpoint, sep, urlencode(params))

    def get_token(self, grant_type, **kwargs):
        data = {'client_id': self.client_id, 'grant_type': grant_type}
        data.update(kwargs)
        # We don't need to clean up None values here, because requests lib will.

        # Quoted from https://tools.ietf.org/html/rfc6749#section-2.3.1
        # Clients in possession of a client password MAY use the HTTP Basic
        # authentication.
        # Alternatively, (but NOT RECOMMENDED,)
        # the authorization server MAY support including the
        # client credentials in the request-body using the following
        # parameters: client_id, client_secret.
        auth = None
        if self.client_credential and not 'client_secret' in data:
            auth = (self.client_id, self.client_credential)  # HTTP Basic Auth

        resp = requests.post(
            self.token_endpoint, headers={'Accept': 'application/json'},
            data=data, auth=auth)
        if resp.status_code>=500:
            resp.raise_for_status()  # TODO: Will probably retry here
        # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
        # even an error response will be a valid json structure,
        # so we simply return it here, without needing to invent an exception.
        return resp.json()


class AuthorizationCodeGrant(Client):

    def authorization_url(
            self, redirect_uri=None, scope=None, state=None, **kwargs):
        """Generate an authorization url to be visited by resource owner.

        :param response_type: MUST be set to "code" or "token".
        :param scope: It is a space-delimited, case-sensitive string.
            Some ID provider can accept empty string to represent default scope.
        """
        return super(AuthorizationCodeGrant, self).authorization_url(
            'code', redirect_uri=redirect_uri, scope=scope, state=state,
            **kwargs)
        # Later when you receive the response at your redirect_uri,
        # validate_authorization() may be handy to check the returned state.

    def get_token(self, code, redirect_uri=None, client_id=None, **kwargs):
        """Get an access token.

        See also https://tools.ietf.org/html/rfc6749#section-4.1.3

        :param code: The authorization code received from authorization server.
        :param redirect_uri:
            Required, if the "redirect_uri" parameter was included in the
            authorization request, and their values MUST be identical.
        :param client_id: Required, if the client is not authenticating itself.
            See https://tools.ietf.org/html/rfc6749#section-3.2.1
        """
        return super(AuthorizationCodeGrantFlow, self).get_token(
            'authorization_code', code=code,
            redirect_uri=redirect_uri, client_id=client_id, **kwargs)


def validate_authorization(params, state=None):
    """A thin helper to examine the authorization being redirected back"""
    if not isinstance(params, dict):
        params = parse_qs(params)
    if params.get('state') != state:
        raise ValueError('state mismatch')
    return params


class ImplicitGrant(Client):
    # This class is only for illustrative purpose.
    # You probably won't implement your ImplicitGrant flow in Python anyway.
    def authorization_url(self, redirect_uri=None, scope=None, state=None):
        return super(ImplicitGrant, self).authorization_url('token', **locals())

    def get_token(self):
        raise NotImplemented("Token is already issued during authorization")


class ResourceOwnerPasswordCredentialsGrant(Client):

    def authorization_url(self, **kwargs):
        raise NotImplemented(
            "You should have already obtained resource owner's password")

    def get_token(self, username, password, scope=None, **kwargs):
        return super(ResourceOwnerPasswordCredentialsGrant, self).get_token(
            "password", username=username, password=password, scope=scope,
            **kwargs)


class ClientCredentialGrant(Client):
    def authorization_url(self, **kwargs):
        # Since the client authentication is used as the authorization grant
        raise NotImplemented("No additional authorization request is needed")

    def get_token(self, scope=None, **kwargs):
        return super(ClientCredentialGrant, self).get_token(
            "client_credentials", scope=scope, **kwargs)

