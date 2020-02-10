"""This is the asynchronized version of ../oauth2.py"""
import abc
from typing import Mapping

from ..oauth2 import AbstractBaseClient


class AsyncAbstractBaseClient(abc.ABC):  # TBD

    #@abc.abstractmethod
    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class BaseClient(AbstractBaseClient):
    async def _obtain_token(  # The verb "obtain" is influenced by OAUTH2 RFC 6749
            self, grant_type,
            params: Mapping[str, str] = None,  # a dict to be sent as query string to the endpoint
            data=None,  # All relevant data, which will go into the http body
            headers=None,  # a dict to be sent as request headers
            timeout=None,
            post=None,  # A callable to replace requests.post(), for testing.
                        # Such as: lambda url, **kwargs:
                        #   Mock(status_code=200, text="{}")
            **kwargs  # Relay all extra parameters to underlying requests
            ):  # Returns the json object came from the OAUTH2 response
        async with (post or self.session.post)(
                self.configuration["token_endpoint"],
                timeout=timeout or self.timeout,
                **dict(kwargs, **self._prepare_request(
                    grant_type, params=params, data=data, headers=headers))
                ) as response:
            return self._parse_resposne(await response.json())

    async def obtain_token_by_refresh_token(
            self,
            refresh_token: str,
            scope=None,
            **kwargs):
        # type: (str, Union[str, list, set, tuple]) -> dict
        """Obtain an access token via a refresh token.

        :param refresh_token: The refresh token issued to the client
        :param scope: If omitted, is treated as equal to the scope originally
            granted by the resource ownser,
            according to https://tools.ietf.org/html/rfc6749#section-6
        """
        assert isinstance(refresh_token, string_types)
        data = kwargs.pop('data', {})
        data.update(refresh_token=refresh_token, scope=scope)
        return await self._obtain_token("refresh_token", data=data, **kwargs)


class Client(BaseClient):

    async def obtain_token_for_client(self, scope=None, **kwargs):
        """Obtain token for this client (rather than for an end user),
        a.k.a. the Client Credentials Grant, used by Backend Applications.

        We don't name it obtain_token_by_client_credentials(...) because those
        credentials are typically already provided in class constructor, not here.
        You can still explicitly provide an optional client_secret parameter,
        or you can provide such extra parameters as `default_body` during the
        class initialization.
        """
        data = kwargs.pop("data", {})
        data.update(scope=scope)
        result = await self._obtain_token("client_credentials", data=data, **kwargs)
        return result

