import time
try:  # Python 2
    from urlparse import urljoin
except:  # Python 3
    from urllib.parse import urljoin
import logging
import sys

from .oauth2cli import Client, JwtSigner
from .authority import Authority
from .mex import send_request as mex_send_request
from .wstrust_request import send_request as wst_send_request
from .wstrust_response import *
from .token_cache import TokenCache


# The __init__.py will import this. Not the other way around.
__version__ = "0.2.0"

logger = logging.getLogger(__name__)

def decorate_scope(
        scopes, client_id,
        reserved_scope=frozenset(['openid', 'profile', 'offline_access'])):
    if not isinstance(scopes, (list, set, tuple)):
        raise ValueError("The input scopes should be a list, tuple, or set")
    scope_set = set(scopes)  # Input scopes is typically a list. Copy it to a set.
    if scope_set & reserved_scope:
        # These scopes are reserved for the API to provide good experience.
        # We could make the developer pass these and then if they do they will
        # come back asking why they don't see refresh token or user information.
        raise ValueError(
            "API does not accept {} value as user-provided scopes".format(
                reserved_scope))
    if client_id in scope_set:
        if len(scope_set) > 1:
            # We make developers pass their client id, so that they can express
            # the intent that they want the token for themselves (their own
            # app).
            # If we do not restrict them to passing only client id then they
            # could write code where they expect an id token but end up getting
            # access_token.
            raise ValueError("Client Id can only be provided as a single scope")
        decorated = set(reserved_scope)  # Make a writable copy
    else:
        decorated = scope_set | reserved_scope
    return list(decorated)


class ClientApplication(object):

    def __init__(
            self, client_id,
            client_credential=None, authority=None, validate_authority=True,
            token_cache=None,
            verify=True, proxies=None, timeout=None):
        """Create an instance of application.

        :param client_id: Your app has a clinet_id after you register it on AAD.
        :param client_credential:
            For :class:`PublicClientApplication`, you simply use `None` here.
            For :class:`ConfidentialClientApplication`,
            it can be a string containing client secret,
            or an X509 certificate container in this form::

                {
                    "private_key": "...-----BEGIN PRIVATE KEY-----...",
                    "thumbprint": "A1B2C3D4E5F6...",
                }

        :param str authority:
            A URL that identifies a token authority. It should be of the format
            https://login.microsoftonline.com/your_tenant
            By default, we will use https://login.microsoftonline.com/common
        :param bool validate_authority: (optional) Turns authority validation
            on or off. This parameter default to true.
        :param TokenCache cache:
            Sets the token cache used by this ClientApplication instance.
            By default, an in-memory cache will be created and used.
        :param verify: (optional)
            It will be passed to the
            `verify parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#ssl-cert-verification>`_
        :param proxies: (optional)
            It will be passed to the
            `proxies parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#proxies>`_
        :param timeout: (optional)
            It will be passed to the
            `timeout parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#timeouts>`_
        """
        self.client_id = client_id
        self.client_credential = client_credential
        self.verify = verify
        self.proxies = proxies
        self.timeout = timeout
        self.authority = Authority(
                authority or "https://login.microsoftonline.com/common/",
                validate_authority, verify=verify, proxies=proxies, timeout=timeout)
            # Here the self.authority is not the same type as authority in input
        self.token_cache = token_cache or TokenCache()
        self.client = self._build_client(client_credential, self.authority)

    def _build_client(self, client_credential, authority):
        client_assertion = None
        default_body = {"client_info": 1}
        if isinstance(client_credential, dict):
            assert ("private_key" in client_credential
                    and "thumbprint" in client_credential)
            signer = JwtSigner(
                client_credential["private_key"], algorithm="RS256",
                sha1_thumbprint=client_credential.get("thumbprint"))
            client_assertion = signer.sign_assertion(
                audience=authority.token_endpoint, issuer=self.client_id)
        else:
            default_body['client_secret'] = client_credential
        server_configuration = {
            "authorization_endpoint": authority.authorization_endpoint,
            "token_endpoint": authority.token_endpoint,
            "device_authorization_endpoint":
                urljoin(authority.token_endpoint, "devicecode"),
            }
        return Client(
            server_configuration,
            self.client_id,
            default_headers={
                "x-client-sku": "MSAL.Python", "x-client-ver": __version__,
                "x-client-os": sys.platform,
                "x-client-cpu": "x64" if sys.maxsize > 2 ** 32 else "x86",
                },
            default_body=default_body,
            client_assertion=client_assertion,
            on_obtaining_tokens=self.token_cache.add,
            on_removing_rt=self.token_cache.remove_rt,
            on_updating_rt=self.token_cache.update_rt,
            verify=self.verify, proxies=self.proxies, timeout=self.timeout)

    def get_authorization_request_url(
            self,
            scopes,  # type: list[str]
            # additional_scope=None,  # type: Optional[list]
            login_hint=None,  # type: Optional[str]
            state=None,  # Recommended by OAuth2 for CSRF protection
            redirect_uri=None,
            authority=None,  # By default, it will use self.authority;
                             # Multi-tenant app can use new authority on demand
            response_type="code",  # Can be "token" if you use Implicit Grant
            **kwargs):
        """Constructs a URL for you to start a Authorization Code Grant.

        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).
        :param str state: Recommended by OAuth2 for CSRF protection.
        :param str login_hint:
            Identifier of the user. Generally a User Principal Name (UPN).
        :param str redirect_uri:
            Address to return to upon receiving a response from the authority.
        :return: The authorization url as a string.
        """
        """ # TBD: this would only be meaningful in a new acquire_token_interactive()
        :param additional_scope: Additional scope is a concept only in AAD.
            It refers to other resources you might want to prompt to consent
            for in the same interaction, but for which you won't get back a
            token for in this particular operation.
            (Under the hood, we simply merge scope and additional_scope before
            sending them on the wire.)
        """
        the_authority = Authority(
            authority,
            verify=self.verify, proxies=self.proxies, timeout=self.timeout,
            ) if authority else self.authority
        client = Client(
            {"authorization_endpoint": the_authority.authorization_endpoint},
            self.client_id)
        return client.build_auth_request_uri(
            response_type="code",  # Using Authorization Code grant
            redirect_uri=redirect_uri, state=state, login_hint=login_hint,
            scope=decorate_scope(scopes, self.client_id),
            )

    def acquire_token_by_authorization_code(
            self,
            code,
            scopes,  # Syntactically required. STS accepts empty value though.
            redirect_uri=None,
                # REQUIRED, if the "redirect_uri" parameter was included in the
                # authorization request as described in Section 4.1.1, and their
                # values MUST be identical.
            ):
        """The second half of the Authorization Code Grant.

        :param code: The authorization code returned from Authorization Server.
        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).

            If you requested user consent for multiple resources, here you will
            typically want to provide a subset of what you required in AuthCode.

            OAuth2 was designed mostly for singleton services,
            where tokens are always meant for the same resource and the only
            changes are in the scopes.
            In AAD, tokens can be issued for multiple 3rd party resources.
            You can ask authorization code for multiple resources,
            but when you redeem it, the token is for only one intended
            recipient, called audience.
            So the developer need to specify a scope so that we can restrict the
            token to be issued for the corresponding audience.

        :return: A dict representing the json response from AAD:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        # If scope is absent on the wire, STS will give you a token associated
        # to the FIRST scope sent during the authorization request.
        # So in theory, you can omit scope here when you were working with only
        # one scope. But, MSAL decorates your scope anyway, so they are never
        # really empty.
        assert isinstance(scopes, list), "Invalid parameter type"
        return self.client.obtain_token_by_authorization_code(
                code, redirect_uri=redirect_uri,
                data={"scope": decorate_scope(scopes, self.client_id)},
            )

    def get_accounts(self, username=None):
        """Get a list of accounts which previously signed in, i.e. exists in cache.

        An account can later be used in :func:`~acquire_token_silent`
        to find its tokens.

        :param username:
            Filter accounts with this username only. Case insensitive.
        :return: A list of account objects.
            Each account is a dict. For now, we only document its "username" field.
            Your app can choose to display those information to end user,
            and allow user to choose one of his/her accounts to proceed.
        """
        # The following implementation finds accounts only from saved accounts,
        # but does NOT correlate them with saved RTs. It probably won't matter,
        # because in MSAL universe, there are always Accounts and RTs together.
        accounts = self.token_cache.find(
            self.token_cache.CredentialType.ACCOUNT,
            query={"environment": self.authority.instance})
        if username:
            # Federated account["username"] from AAD could contain mixed case
            lowercase_username = username.lower()
            accounts = [a for a in accounts
                if a["username"].lower() == lowercase_username]
        return accounts

    def acquire_token_silent(
            self,
            scopes,  # type: List[str]
            account,  # type: Optional[Account]
            authority=None,  # See get_authorization_request_url()
            force_refresh=False,  # type: Optional[boolean]
            **kwargs):
        """Acquire an access token for given account, without user interaction.

        It is done either by finding a valid access token from cache,
        or by finding a valid refresh token from cache and then automatically
        use it to redeem a new access token.

        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).
        :param account:
            one of the account object returned by :func:`~get_accounts`,
            or use None when you want to find an access token for this client.
        :param force_refresh:
            If True, it will skip Access Token look-up,
            and try to find a Refresh Token to obtain a new Access Token.
        :return:
            - A dict containing "access_token" key, when cache lookup succeeds.
            - None when cache lookup does not yield anything.
        """
        assert isinstance(scopes, list), "Invalid parameter type"
        the_authority = Authority(
            authority,
            verify=self.verify, proxies=self.proxies, timeout=self.timeout,
            ) if authority else self.authority

        if not force_refresh:
            matches = self.token_cache.find(
                self.token_cache.CredentialType.ACCESS_TOKEN,
                target=scopes,
                query={
                    "client_id": self.client_id,
                    "environment": the_authority.instance,
                    "realm": the_authority.tenant,
                    "home_account_id": (account or {}).get("home_account_id"),
                    })
            now = time.time()
            for entry in matches:
                expires_in = int(entry["expires_on"]) - now
                if expires_in < 5*60:
                    continue  # Removal is not necessary, it will be overwritten
                logger.debug("Cache hit an AT")
                return {  # Mimic a real response
                    "access_token": entry["secret"],
                    "token_type": "Bearer",
                    "expires_in": int(expires_in),  # OAuth2 specs defines it as int
                    }

        matches = self.token_cache.find(
            self.token_cache.CredentialType.REFRESH_TOKEN,
            # target=scopes,  # AAD RTs are scope-independent
            query={
                "client_id": self.client_id,
                "environment": the_authority.instance,
                "home_account_id": (account or {}).get("home_account_id"),
                # "realm": the_authority.tenant,  # AAD RTs are tenant-independent
                })
        client = self._build_client(self.client_credential, the_authority)
        for entry in matches:
            logger.debug("Cache hit an RT")
            response = client.obtain_token_by_refresh_token(
                entry, rt_getter=lambda token_item: token_item["secret"],
                scope=decorate_scope(scopes, self.client_id))
            if "error" not in response:
                return response
            logger.debug(
                "Refresh failed. {error}: {error_description}".format(**response))


class PublicClientApplication(ClientApplication):  # browser app or mobile app

    def __init__(self, client_id, client_credential=None, **kwargs):
        if client_credential is not None:
            raise ValueError("Public Client should not possess credentials")
        super(PublicClientApplication, self).__init__(
            client_id, client_credential=None, **kwargs)

    def initiate_device_flow(self, scopes=None, **kwargs):
        """Initiate a Device Flow instance,
        which will be used in :func:`~acquire_token_by_device_flow`.

        :param list[str] scopes:
            Scopes requested to access a protected API (a resource).
        :return: A dict representing a newly created Device Flow object.

            - A successful response would contain "user_code" key, among others
            - an error response would contain some other readable key/value pairs.
        """
        return self.client.initiate_device_flow(
            scope=decorate_scope(scopes or [], self.client_id),
            **kwargs)

    def acquire_token_by_device_flow(self, flow, **kwargs):
        """Obtain token by a device flow object, with customizable polling effect.

        :param dict flow:
            A dict previously generated by :func:`~initiate_device_flow`.
            By default, this method's polling effect  will block current thread.
            You can abort the polling loop at any time,
            by changing the value of the flow's "expires_at" key to 0.

        :return: A dict representing the json response from AAD:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        return self.client.obtain_token_by_device_flow(
                flow,
                data={"code": flow["device_code"]},  # 2018-10-4 Hack:
                    # during transition period,
                    # service seemingly need both device_code and code parameter.
                **kwargs)

    def acquire_token_by_username_password(
            self, username, password, scopes=None, **kwargs):
        """Gets a token for a given resource via user credentails.

        :param str username: Typically a UPN in the form of an email address.
        :param str password: The password.
        :param list[str] scopes:
            Scopes requested to access a protected API (a resource).

        :return: A dict representing the json response from AAD:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        scopes = decorate_scope(scopes, self.client_id)
        if not self.authority.is_adfs:
            user_realm_result = self.authority.user_realm_discovery(username)
            if user_realm_result.get("account_type") == "Federated":
                return self._acquire_token_by_username_password_federated(
                    user_realm_result, username, password, scopes=scopes, **kwargs)
        return self.client.obtain_token_by_username_password(
                username, password, scope=scopes, **kwargs)

    def _acquire_token_by_username_password_federated(
            self, user_realm_result, username, password, scopes=None, **kwargs):
        verify = kwargs.pop("verify", self.verify)
        proxies = kwargs.pop("proxies", self.proxies)
        wstrust_endpoint = {}
        if user_realm_result.get("federation_metadata_url"):
            wstrust_endpoint = mex_send_request(
                user_realm_result["federation_metadata_url"],
                verify=verify, proxies=proxies)
        logger.debug("wstrust_endpoint = %s", wstrust_endpoint)
        wstrust_result = wst_send_request(
            username, password, user_realm_result.get("cloud_audience_urn"),
            wstrust_endpoint.get("address",
                # Fallback to an AAD supplied endpoint
                user_realm_result.get("federation_active_auth_url")),
            wstrust_endpoint.get("action"), verify=verify, proxies=proxies)
        if not ("token" in wstrust_result and "type" in wstrust_result):
            raise RuntimeError("Unsuccessful RSTR. %s" % wstrust_result)
        GRANT_TYPE_SAML1_1 = 'urn:ietf:params:oauth:grant-type:saml1_1-bearer'
        grant_type = {
            SAML_TOKEN_TYPE_V1: GRANT_TYPE_SAML1_1,
            SAML_TOKEN_TYPE_V2: self.client.GRANT_TYPE_SAML2,
            WSS_SAML_TOKEN_PROFILE_V1_1: GRANT_TYPE_SAML1_1,
            WSS_SAML_TOKEN_PROFILE_V2: self.client.GRANT_TYPE_SAML2
            }.get(wstrust_result.get("type"))
        if not grant_type:
            raise RuntimeError(
                "RSTR returned unknown token type: %s", wstrust_result.get("type"))
        self.client.grant_assertion_encoders.setdefault(  # Register a non-standard type
            grant_type, self.client.encode_saml_assertion)
        return self.client.obtain_token_by_assertion(
            wstrust_result["token"], grant_type, scope=scopes, **kwargs)


class ConfidentialClientApplication(ClientApplication):  # server-side web app

    def acquire_token_for_client(self, scopes, **kwargs):
        """Acquires token from the service for the confidential client.

        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).

        :return: A dict representing the json response from AAD:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        # TBD: force_refresh behavior
        return self.client.obtain_token_for_client(
                scope=scopes,  # This grant flow requires no scope decoration
                **kwargs)

    def acquire_token_on_behalf_of(self, user_assertion, scopes, authority=None):
        raise NotImplementedError()

