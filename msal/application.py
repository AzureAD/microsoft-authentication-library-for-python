from . import oauth2
from .authority import Authority
from .client_credential import ClientCredentialRequest


class ClientApplication(object):
    DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common/"

    def __init__(
            self, client_id,
            validate_authority=True, authority=DEFAULT_AUTHORITY):
        self.client_id = client_id
        self.validate_authority = validate_authority
        self.authority = authority

    def acquire_token_silent(
            self, scope,
            user=None,  # It can be a string as user id, or a User object
            authority=None,  # See get_authorization_request_url()
            policy='',
            force_refresh=False,  # To force refresh an Access Token (not a RT)
            **kwargs):
        a = Authority(self.authority, policy=policy)  # TODO
        client = oauth2.Client(self.client_id, token_endpoint=a.token_endpoint)
        refresh_token = kwargs.get('refresh_token')  # For testing purpose
        response = client.get_token_by_refresh_token(
            refresh_token, scope=scope,
            client_secret=getattr(self, 'client_credential'))  # TODO: JWT too
        # TODO: refresh the refresh_token
        return response


class PublicClientApplication(ClientApplication):  # browser app or mobile app

    ## TBD: what if redirect_uri is not needed in the constructor at all?
    ##  Device Code flow does not need redirect_uri anyway.

    # OUT_OF_BAND = "urn:ietf:wg:oauth:2.0:oob"
    # def __init__(self, client_id, redirect_uri=None, **kwargs):
    #     super(PublicClientApplication, self).__init__(client_id, **kwargs)
    #     self.redirect_uri = redirect_uri or self.OUT_OF_BAND

    def acquire_token(
            self,
            scope,
            # additional_scope=None,  # See also get_authorization_request_url()
            login_hint=None,
            ui_options=None,
            # user=None,  # TBD: It exists in MSAL-dotnet but not in MSAL-Android
            policy='',
            authority=None,  # See get_authorization_request_url()
            extra_query_params=None,
            ):
        # It will handle the TWO round trips of Authorization Code Grant flow.
        raise NotImplemented()


class ConfidentialClientApplication(ClientApplication):  # server-side web app
    def __init__(
            self, client_id, client_credential, user_token_cache=None,
            # redirect_uri=None,  # Experimental: Removed for now.
            #   acquire_token_for_client() doesn't need it
            **kwargs):
        """
        :param client_credential: It can be a string containing client secret,
            or an X509 certificate container in this form:

                {
                    "certificate": "-----BEGIN PRIVATE KEY-----...",
                    "thumbprint": "A1B2C3D4E5F6...",
                }
        """
        super(ConfidentialClientApplication, self).__init__(client_id, **kwargs)
        self.client_credential = client_credential
        self.user_token_cache = user_token_cache
        self.app_token_cache = None  # TODO

    def acquire_token_for_client(self, scope, policy=''):
        return ClientCredentialRequest(
            client_id=self.client_id, client_credential=self.client_credential,
            scope=scope, policy=policy, authority=self.authority).run()

    def get_authorization_request_url(
            self,
            scope,
            # additional_scope=None,
                # TBD: Under the hood, we will merge additional_scope and scope
                # before sending them on the wire. So there is no practical
                # difference than removing this parameter and using scope only.
            login_hint=None,
            state=None,  # TBD: It is not in MSAL-dotnet nor MSAL-Android,
                         # but it is recommended in OAuth2 RFC. Do we want it?
            policy='',
            redirect_uri=None,
            authority=None,  # By default, it will use self.authority;
                             # Multi-tenant app can use new authority on demand
            extra_query_params=None,  # None or a dictionary
            ):
        """Constructs a URL for you to start a Authorization Code Grant.

        :param scope: Scope refers to the resource that will be used in the
            resulting token's audience.
        :param additional_scope: Additional scope is a concept only in AAD.
            It refers to other resources you might want to prompt to consent
            for in the same interaction, but for which you won't get back a
            token for in this particular operation.
            (Under the hood, we simply merge scope and additional_scope before
            sending them on the wire.)
        """
        a = Authority(self.authority, policy=policy)  # TODO
        grant = oauth2.AuthorizationCodeGrant(
            self.client_id, authorization_endpoint=a.authorization_endpoint)
        return grant.authorization_url(
            redirect_uri=redirect_uri,
            scope=scope,  # TODO: handle additional_scope
            state=state, login_hint=login_hint,
            **(extra_query_params or {}))

    def acquire_token_by_authorization_code(
            self,
            code,  # TBD:
                   # .NET 's protected method defines 2 parameters: code, scope.
                   # .NET 's public method defines 2 parameters: scope, code.
            scope,  # TBD: This could be optional. Shall it? See the document below.
            redirect_uri=None,
                # TBD: It is not in MSAL-dotnet. Do we need it? OAuth2 RFC says:
                #
                # REQUIRED, if the "redirect_uri" parameter was included in the
                # authorization request as described in Section 4.1.1, and their
                # values MUST be identical.
                #
                # So, shall we either also provide this parameter here,
                # or shall we remove the redirect_uri in the
                # get_authorization_request_url()?
            policy=''
            ):
        """The second half of the Authorization Code Grant.

        :param code: The authorization code returned from Authorization Server.
        :param scope:

            If you requested user consent for multiple resources, here you will
            typically want to provide a subset of what you required in AC.

            OAuth2 was designed mostly for singleton services,
            where tokens are always meant for the same resource and the only
            changes are in the scopes.
            In AAD, tokens can be issued for multiple 3rd parth resources.
            You can ask authorization code for multiple resources,
            but when you redeem it, the token is for only one intended
            recipient, called audience.
            So the developer need to specify a scope so that we can restrict the
            token to be issued for the corresponding audience.
        """
        #    If absent, STS will give you a token associated to ONE of the scope
        #    sent in the authorization request. So only omit this when you are
        #    working with only one scope.
        scope = scope or ["openid", "email", "profile", "offline_access"]  # TBD

        a = Authority(self.authority, policy=policy)  # TODO
        grant = oauth2.AuthorizationCodeGrant(
            self.client_id, token_endpoint=a.token_endpoint)
        return grant.get_token(
            code, scope=scope, redirect_uri=redirect_uri,
            client_secret=self.client_credential)

    def acquire_token_on_behalf_of(
            self, user_assertion, scope, authority=None, policy=''):
        pass

