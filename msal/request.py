import time

from .exceptions import MsalServiceError


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

        # TODO: Temporary solution here
        self.token_endpoint = authority
        if authority.startswith('https://login.microsoftonline.com/common/'):
            self.token_endpoint += 'oauth2/v2.0/token'
        elif authority.startswith('https://login.windows.net/'):  # AAD?
            self.token_endpoint += 'oauth2/token'
        if policy:
            self.token_endpoint += '?policy={}'.format(policy)

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
        raw = self.get_token()
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

