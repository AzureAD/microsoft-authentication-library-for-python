"""This OAuth2 client implementation aims to be spec-compliant, and generic."""
# OAuth2 spec https://tools.ietf.org/html/rfc6749

try:
    from urllib.parse import urlencode, parse_qs
except ImportError:
    from urlparse import parse_qs
    from urllib import urlencode
import logging
import warnings
import time

import requests



class BaseClient(object):
    # This low-level interface works. Yet you'll find its sub-class
    # more friendly to remind you what parameters are needed in each scenario.
    # More on Client Types at https://tools.ietf.org/html/rfc6749#section-2.1
    def __init__(
            self,
            client_id,
            client_secret=None,  # Triggers HTTP AUTH for Confidential Client
            default_body=None,  # a dict to be sent in each token request,
                # usually contains Confidential Client authentication parameters
                # such as {'client_id': 'your_id', 'client_secret': 'secret'}
                # if you choose to not use HTTP AUTH
            configuration=None,  # configuration dict of the authorization server
            ):
        """Initialize a client object to talk all the OAuth2 grants to the server.

        Args:
            configuration (dict):
                It contains the configuration (i.e. metadata) of the auth server.
                The actual content typically contains keys like
                "authorization_endpoint", "token_endpoint", etc..
                Based on RFC 8414 (https://tools.ietf.org/html/rfc8414),
                you can probably fetch it online from either
                https://example.com/.../.well-known/oauth-authorization-server
                or
                https://example.com/.../.well-known/openid-configuration
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.default_body = default_body or {}
        self.configuration = configuration or {}

    def _build_auth_request_params(self, response_type, **kwargs):
        # response_type is a string defined in
        #   https://tools.ietf.org/html/rfc6749#section-3.1.1
        # or it can be a space-delimited string as defined in
        #   https://tools.ietf.org/html/rfc6749#section-8.4
        response_type = self._stringify(response_type)

        params = {'client_id': self.client_id, 'response_type': response_type}
        params.update(kwargs)  # Note: None values will override params
        params = {k: v for k, v in params.items() if v is not None}  # clean up
        if params.get('scope'):
            params['scope'] = self._stringify(params['scope'])
        return params  # A dict suitable to be used in http request

    def _obtain_token(  # The verb "obtain" is influenced by OAUTH2 RFC 6749
            self, grant_type,
            params=None,  # a dict to be send as query string to the endpoint
            data=None,  # All relevant data, which will go into the http body
            timeout=None,  # A timeout value which will be used by requests lib
            ):  # Returns the json object came from the OAUTH2 response
        _data = {'client_id': self.client_id, 'grant_type': grant_type}
        _data.update(self.default_body)  # It may contain authen parameters
        _data.update(data or {})  # So the content in data param prevails
        # We don't have to clean up None values here, because requests lib will.

        if _data.get('scope'):
            _data['scope'] = self._stringify(_data['scope'])

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

        if "token_endpoint" not in self.configuration:
            raise ValueError("token_endpoint not found in configuration")
        resp = requests.post(
            self.configuration["token_endpoint"],
            headers={'Accept': 'application/json'},
            params=params, data=_data, auth=auth, timeout=timeout)
        if resp.status_code >= 500:
            resp.raise_for_status()  # TODO: Will probably retry here
        try:
            # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
            # even an error response will be a valid json structure,
            # so we simply return it here, without needing to invent an exception.
            return resp.json()
        except ValueError:
            logging.exception("Token response is not in json format")
            raise

    def obtain_token_with_refresh_token(self, refresh_token, scope=None, **kwargs):
        """Obtain an access token via a refresh token.

        :param refresh_token: The refresh token issued to the client
        :param scope: If omitted, is treated as equal to the scope originally
            granted by the resource ownser,
            according to https://tools.ietf.org/html/rfc6749#section-6
        """
        data = kwargs.pop('data', {})
        data.update(refresh_token=refresh_token, scope=scope)
        return self._obtain_token("refresh_token", data=data, **kwargs)

    def _stringify(self, sequence):
        if isinstance(sequence, (list, set, tuple)):
            return ' '.join(sorted(sequence))  # normalizing it, ascendingly
        return sequence  # as-is


class Client(BaseClient):  # We choose to implement all 4 grants in 1 class
    """This is the main API for oauth2 client.

    Its methods define and document parameters mentioned in OAUTH2 RFC 6749.
    """
    DEVICE_FLOW = {  # consts for device flow, that can be customized by sub-class
        "GRANT_TYPE": "urn:ietf:params:oauth:grant-type:device_code",
        "DEVICE_CODE": "device_code",
        }
    DEVICE_FLOW_RETRIABLE_ERRORS = ("authorization_pending", "slow_down")

    def initiate_device_flow(self, scope=None, **kwargs):
        # type: (list, **dict) -> dict
        # The naming of this method is following the wording of this specs
        # https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.1
        """Initiate a device flow.

        Returns the data defined in Device Flow specs.
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.2

        You should then orchestrate the User Interaction as defined in here
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.3

        And possibly here
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.3.1
        """
        DAE = "device_authorization_endpoint"
        if not self.configuration.get(DAE):
            raise ValueError("You need to provide device authorization endpoint")
        flow = requests.post(self.configuration[DAE], data={
            "client_id": self.client_id, "scope": self._stringify(scope or []),
            }, **kwargs).json()
        flow["interval"] = int(flow.get("interval", 5))  # Some IdP returns string
        flow["expires_in"] = int(flow.get("expires_in", 1800))
        return flow

    def _obtain_token_by_device_flow(self, flow, **kwargs):
        # type: (dict, **dict) -> dict
        # This method updates flow during each run. And it is non-blocking.
        now = time.time()
        skew = 1
        if flow.get("latest_attempt_at", 0) + flow.get("interval", 5) - skew > now:
            warnings.warn('Attempted too soon. Please do time.sleep(flow["interval"])')
        data = kwargs.pop("data", {})
        data.update({
            "client_id": self.client_id,
            self.DEVICE_FLOW["DEVICE_CODE"]: flow["device_code"],
            })
        result = self._obtain_token(
            self.DEVICE_FLOW["GRANT_TYPE"], data=data, **kwargs)
        if result.get("error") == "slow_down":
            # Respecting https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.5
            flow["interval"] = flow.get("interval", 5) + 5
        flow["latest_attempt_at"] = now
        return result

    def obtain_token_by_device_flow(self, flow, exit_condition=lambda: True, **kwargs):
        # type: (dict, Callable) -> dict
        """Obtain token by a device flow object, with optional polling effect.

        Args:
            flow (dict):
                An object previously generated by initiate_device_flow(...).
            exit_condition (Callable):
                This method implements a loop to provide polling effect.
                The loop's exit condition is calculated by this callback.
                The default callback makes the loop run only once, i.e. no polling.
        """
        _flow = flow.copy()
        while True:
            result = self._obtain_token_by_device_flow(_flow, **kwargs)
            if result.get("error") not in self.DEVICE_FLOW_RETRIABLE_ERRORS:
                return result
            for i in range(_flow.get("interval", 5)):  # Wait interval seconds
                if exit_condition():
                    return result
                time.sleep(1)  # Shorten each round, to make exit more responsive

    def build_auth_request_uri(
            self,
            response_type, redirect_uri=None, scope=None, state=None, **kwargs):
        """Generate an authorization uri to be visited by resource owner.

        Later when the response reaches your redirect_uri,
        you can use parse_auth_response() to check the returned state.

        This method could be named build_authorization_request_uri() instead,
        but then there would be a build_authentication_request_uri() in the OIDC
        subclass doing almost the same thing. So we use a loose term "auth" here.

        :param response_type:
            Must be "code" when you are using Authorization Code Grant,
            "token" when you are using Implicit Grant, or other
            (possibly space-delimited) strings as registered extension value.
            See https://tools.ietf.org/html/rfc6749#section-3.1.1
        :param redirect_uri: Optional. Server will use the pre-registered one.
        :param scope: It is a space-delimited, case-sensitive string.
            Some ID provider can accept empty string to represent default scope.
        :param state: Recommended. An opaque value used by the client to
            maintain state between the request and callback.
        :param kwargs: Other parameters, typically defined in OpenID Connect.
        """
        if "authorization_endpoint" not in self.configuration:
            raise ValueError("authorization_endpoint not found in configuration")
        authorization_endpoint = self.configuration["authorization_endpoint"]
        params = self._build_auth_request_params(
            response_type, redirect_uri=redirect_uri, scope=scope, state=state,
            **kwargs)
        sep = '&' if '?' in authorization_endpoint else '?'
        return "%s%s%s" % (authorization_endpoint, sep, urlencode(params))

    @staticmethod
    def parse_auth_response(params, state=None):
        """Parse the authorization response being redirected back.

        :param params: A string or dict of the query string
        :param state: REQUIRED if the state parameter was present in the client
            authorization request. This function will compare it with response.
        """
        if not isinstance(params, dict):
            params = parse_qs(params)
        if params.get('state') != state:
            raise ValueError('state mismatch')
        return params

    def obtain_token_with_authorization_code(
            self, code, redirect_uri=None, **kwargs):
        """Get a token via auhtorization code. a.k.a. Authorization Code Grant.

        This is typically used by a server-side app (Confidential Client),
        but it can also be used by a device-side native app (Public Client).
        See more detail at https://tools.ietf.org/html/rfc6749#section-4.1.3

        :param code: The authorization code received from authorization server.
        :param redirect_uri:
            Required, if the "redirect_uri" parameter was included in the
            authorization request, and their values MUST be identical.
        """
        data = kwargs.pop("data", {})
        data.update(code=code, redirect_uri=redirect_uri)
        if not self.client_secret:
            # client_id is required, if the client is not authenticating itself.
            # See https://tools.ietf.org/html/rfc6749#section-4.1.3
            data["client_id"] = self.client_id
        return self._obtain_token("authorization_code", data=data, **kwargs)

    def obtain_token_with_username_password(
            self, username, password, scope=None, **kwargs):
        """The Resource Owner Password Credentials Grant, used by legacy app."""
        data = kwargs.pop("data", {})
        data.update(username=username, password=password, scope=scope)
        return self._obtain_token("password", data=data, **kwargs)

    def obtain_token_with_client_credentials(self, scope=None, **kwargs):
        """Get token by client credentials. a.k.a. Client Credentials Grant,
        used by Backend Applications.

        You may want to explicitly provide an optional client_secret parameter,
        or you can provide such extra parameters as `default_body` during the
        class initialization.
        """
        data = kwargs.pop("data", {})
        data.update(scope=scope)
        return self._obtain_token("client_credentials", data=data, **kwargs)

