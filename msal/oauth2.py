try:
    from urllib.parse import urlencode, parse_qs
except ImportError:
    from urlparse import parse_qs
    from urllib import urlencode

import requests


def validate_authorization(params, state=None):
    """A thin helper to examine the authorization being redirected back"""
    if not isinstance(params, dict):
        params = parse_qs(params)
    if params.get('state') != state:
        raise ValueError('state mismatch')
    return params


class Client(object):
    """This OAuth2 client implementation aims to be spec-compliant, and generic.

    https://tools.ietf.org/html/rfc6749
    """
    def __init__(
            self, client_id,
            client_credential=None,  # Only needed for Confidential Client
            authorization_endpoint=None, token_endpoint=None):
        self.client_id = client_id
        self.client_credential = client_credential
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint

    def authorization_url(self,
            response_type,  # MUST be set to "code" or "token"
            redirect_uri=None,
            scope=None,
            state=None,  # Recommended by the spec
            **kwargs):
        """To generate an authorization url, to be visited by resource owner.

        :param scope: It is a space-delimited, case-sensitive string.
            Some ID provider can accept empty string to represent default scope.
        """
        assert response_type and self.client_id
        sep = '&' if '?' in self.authorization_endpoint else '?'
        params = {
            'client_id': self.client_id,
            'response_type': response_type,
            'redirect_uri': redirect_uri,
            'scope': scope,
            'state': state,
            }
        params.update(kwargs)
        params = {k: v for k, v in params.items() if v is not None}  # clean up
        return "%s%s%s" % (self.authorization_endpoint, sep, urlencode(params))

    def get_token(
            self, grant_type,
            redirect_uri=None,
            scope=None,  # Not needed in Authorization Code Grant flow
            **kwargs):
        # Depending on your chosen grant flow, you may need 'code',
        # or 'username' & 'password' pairs, or none of them in the parameters
        data = {
            'client_id': self.client_id, 'grant_type': grant_type,
            'scope': scope}
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
            resp.raise_for_status()  # TODO: Will probably try to retry here
        # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
        # even an error response will be a valid json structure,
        # so we simply return it here, without needing to invent an exception.
        return resp.json()


class AuthorizationCodeGrant(Client):

    def authorization_url(self, **kwargs):
        return super(AuthorizationCodeGrant, self).authorization_url(
            'code', **kwargs)
        # Later when you receive the redirected feedback,
        # validate_authorization() may be handy to check the returned state.

    def get_token(self, code, **kwargs):
        return super(AuthorizationCodeGrantFlow, self).get_token(
            'authorization_code', code=code, **kwargs)


class ImplicitGrant(Client):
    """This class is only for illustrative purpose.

    You probably won't implement your ImplicitGrant flow in Python.
    """

    def authorization_url(self, **kwargs):
        return super(ImplicitGrant, self).authorization_url(
            'token', **kwargs)

    def get_token(self):
        raise NotImplemented("Token is already issued during authorization")


class ResourceOwnerPasswordCredentialsGrant(Client):

    def authorization_url(self, **kwargs):
        raise NotImplemented(
            "You should have obtained resource owner's password, somehow.")

    def get_token(self, username, password, **kwargs):
        return super(ResourceOwnerPasswordCredentialsGrant, self).get_token(
            "password", username=username, password=password, **kwargs)


class ClientCredentialGrant(Client):
    def authorization_url(self, **kwargs):
        raise NotImplemented(
            # Since the client authentication is used as the authorization grant
            "No additional authorization request is needed")

    def get_token(self, **kwargs):
        return super(ClientCredentialGrant, self).get_token(
            "client_credentials", **kwargs)

