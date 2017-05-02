import time

from .exceptions import MsalServiceError


def decorate_scope(
        scope, client_id, policy,
        reserved_scope=frozenset(['openid', 'profile', 'offline_access'])):
    scope_set = set(scope)  # Input scope is typically a list. Copy it to a set.
    if scope_set & reserved_scope:
        # These scopes are reserved for the API to provide good experience.
        # We could make the developer pass these and then if they do they will
        # come back asking why they don't see refresh token or user information.
        raise ValueError(
            "MSAL always sends the scopes {}. They cannot be suppressed "
            "as they are required for the library to function. "
            "Do not include any of these scopes in the scope parameter.".format(
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
    if policy:
        # special case b2c scenarios to not send email and profile as scopes
        decorated.discard("email")
        decorated.discard("profile")
    return decorated


class BaseRequest(object):

    def __init__(
            self, authority=None, token_cache=None,
	    scope=None, policy="",  # TBD: If scope and policy are paramters
		# of both high level ClientApplication.acquire_token()
                # and low level oauth2.*Grant.get_token(),
		# shouldn't they be the parameters for run()?
            client_id=None, client_credential=None, authenticator=None,
            support_adfs=False, restrict_to_single_user=False):
        if not scope:
            raise ValueError("scope cannot be empty")
        self.__dict__.update(locals())

    def run(self):
        """Returns a dictionary, which typically contains following keys:

        * token: A string containing an access token (or id token)
        * expires_on: A timestamp, in seconds. So compare it with time.time().
        * user: TBD
        * and some other keys from the wire, such as "scope", "id_token", etc.,
          which may or may not appear in every different grant flow.
          So you should NOT assume their existence,
          instead you would need to access them safely by dict.get('...').
        """
        # TODO Some cache stuff here
        raw = self.get_token()  # TODO: Support policy
        if 'error' in raw:
            raise MsalServiceError(**raw)
        # TODO: Deal with refresh_token

        # Keep (most) contents in raw token response, extend it, and return it
        raw['token'] = raw.get('access_token') or raw.get('id_token')
        raw['expires_on'] = self.__timestamp(
            # A timestamp is chosen because it is more lighweight than Datetime,
            # and then the entire return value can be serialized as JSON string,
            # should the developers choose to do so.
            # This is the same timestamp format used in JWT's "iat", by the way.
            raw.get('expires_in') or raw.get('id_token_expires_in'))
        if 'scope' in raw:
            raw['scope'] = set(raw['scope'].split())  # Using SPACE as delimiter
        raw['user'] = {  # Contents derived from raw['id_token']
            # TODO: Follow https://github.com/AzureAD/microsoft-authentication-library-for-android/blob/dev/msal/src/internal/java/com/microsoft/identity/client/IdToken.java
            # https://github.com/AzureAD/microsoft-authentication-library-for-android/blob/dev/msal/src/internal/java/com/microsoft/identity/client/User.java
            }
        return raw  # equivalent to AuthenticationResult in other MSAL SDKs

    def __timestamp(self, seconds_from_now=None):  # Returns timestamp IN SECOND
        return time.time() + (
            seconds_from_now if seconds_from_now is not None else 3600)

    def get_token(self):
        raise NotImplemented("Use proper sub-class instead")

