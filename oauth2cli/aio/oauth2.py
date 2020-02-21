"""This is the asynchronized version of ../oauth2.py"""
import json
import time
from typing import Mapping

from ..oauth2 import AbstractBaseClient, AbstractClient


async def _get_content(http_resp):
    # We accept a coroutine function (i.e. aiohttp) or a plaintext (httpx)
    return await http_resp.text() if callable(http_resp.text) else http_resp.text


class BaseClient(AbstractBaseClient):
    # Sadly, the code between sync and async client duplicates a lot.

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
        resp = await (post or self.session.post)(
                self.configuration["token_endpoint"],
                timeout=timeout or self.timeout,
                **dict(kwargs, **self._prepare_token_request(
                    grant_type, params=params, data=data, headers=headers))
                )
        # RFC defines and some uses "status_code", aiohttp uses "status"
        status_code = getattr(resp, "status_code", None) or resp.status
        if status_code >= 500:
            resp.raise_for_status()  # TODO: Will probably retry here
        return self._parse_token_resposne(await _get_content(resp))

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

    async def initiate_device_flow(self, scope=None, timeout=None, **kwargs):
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
        _data = kwargs.pop("data", {})
        _data.update(client_id=self.client_id, scope=self._stringify(scope or []))
        resp = await self.session.post(
            self.configuration[DAE],
            data=_data,
            timeout=timeout or self.timeout,
            headers=dict(self.default_headers, **kwargs.pop("headers", {})),
            **kwargs)
        flow = json.loads(await _get_content(resp))
        flow["interval"] = int(flow.get("interval", 5))  # Some IdP returns string
        flow["expires_in"] = int(flow.get("expires_in", 1800))
        flow["expires_at"] = time.time() + flow["expires_in"]  # We invent this
        return flow

    async def _obtain_token_by_device_flow(self, flow, **kwargs):
        # type: (dict, **dict) -> dict
        # This method updates flow during each run. And it is non-blocking.
        now = time.time()
        skew = 1
        if flow.get("latest_attempt_at", 0) + flow.get("interval", 5) - skew > now:
            warnings.warn('Attempted too soon. Please do sleep(flow["interval"])')
        data = kwargs.pop("data", {})
        data.update({
            "client_id": self.client_id,
            self.DEVICE_FLOW["DEVICE_CODE"]: flow["device_code"],
            })
        result = await self._obtain_token(
            self.DEVICE_FLOW["GRANT_TYPE"], data=data, **kwargs)
        if result.get("error") == "slow_down":
            # Honor https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.5
            flow["interval"] = flow.get("interval", 5) + 5
        flow["latest_attempt_at"] = now
        return result

    async def obtain_token_by_device_flow(self,
            flow,
            exit_condition=lambda flow: flow.get("expires_at", 0) < time.time(),
            sleeper=None,
            **kwargs):
        # type: (dict, Callable) -> dict
        """Obtain token by a device flow object, with customizable polling effect.

        Args:
            flow (dict):
                An object previously generated by initiate_device_flow(...).
                Its content WILL BE CHANGED by this method during each run.
                We share this object with you, so that you could implement
                your own loop, should you choose to do so.

            sleeper (Callable):
                A callable to sleep. Supply the sleep() function of your async
                framework, such as asyncio.sleep().

            exit_condition (Callable):
                This method implements a loop to provide polling effect.
                The loop's exit condition is calculated by this callback.

                The default callback makes the loop run until the flow expires.
                Therefore, one of the ways to exit the polling early,
                is to change the flow["expires_at"] to a small number such as 0.

                In case you are doing async programming, you may want to
                completely turn off the loop. You can do so by using a callback as:

                    exit_condition = lambda flow: True

                to make the loop run only once, i.e. no polling, hence non-block.
        """
        if not sleeper:
            raise ValueError("You need to provide something like asyncio.sleep")
        while True:
            result = await self._obtain_token_by_device_flow(flow, **kwargs)
            if result.get("error") not in self.DEVICE_FLOW_RETRIABLE_ERRORS:
                return result
            for i in range(flow.get("interval", 5)):  # Wait interval seconds
                if exit_condition(flow):
                    return result
                await sleeper(1)  # Shorten each round, to make exit more responsive

    async def obtain_token_by_authorization_code(
            self, code, redirect_uri=None, scope=None, **kwargs):
        """Get a token via auhtorization code. a.k.a. Authorization Code Grant.

        This is typically used by a server-side app (Confidential Client),
        but it can also be used by a device-side native app (Public Client).
        See more detail at https://tools.ietf.org/html/rfc6749#section-4.1.3

        :param code: The authorization code received from authorization server.
        :param redirect_uri:
            Required, if the "redirect_uri" parameter was included in the
            authorization request, and their values MUST be identical.
        :param scope:
            It is both unnecessary and harmless to use scope here, per RFC 6749.
            We suggest to use the same scope already used in auth request uri,
            so that this library can link the obtained tokens with their scope.
        """
        data = self._prepare_obtain_token_by_authorization_code(
            code, redirect_uri=redirect_uri, scope=scope)
        return await self._obtain_token(
            "authorization_code",
            data=dict(data, **kwargs.pop("data", {})),
            **kwargs)

    async def obtain_token_by_username_password(
            self, username, password, scope=None, **kwargs):
        """The Resource Owner Password Credentials Grant, used by legacy app."""
        data = kwargs.pop("data", {})
        data.update(username=username, password=password, scope=scope)
        return await self._obtain_token("password", data=data, **kwargs)

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
        return await self._obtain_token("client_credentials", data=data, **kwargs)

    async def obtain_token_by_assertion(
            self, assertion, grant_type, scope=None, **kwargs):
        # type: (bytes, Union[str, None], Union[str, list, set, tuple]) -> dict
        """This method implements Assertion Framework for OAuth2 (RFC 7521).
        See details at https://tools.ietf.org/html/rfc7521#section-4.1

        :param assertion:
            The assertion bytes can be a raw SAML2 assertion, or a JWT assertion.
        :param grant_type:
            It is typically either the value of :attr:`GRANT_TYPE_SAML2`,
            or :attr:`GRANT_TYPE_JWT`, the only two profiles defined in RFC 7521.
        :param scope: Optional. It must be a subset of previously granted scopes.
        """
        encoder = self.grant_assertion_encoders.get(grant_type, lambda a: a)
        data = kwargs.pop("data", {})
        data.update(scope=scope, assertion=encoder(assertion))
        return await self._obtain_token(grant_type, data=data, **kwargs)


class Client(BaseClient, AbstractClient):

    async def _obtain_token(
            self, grant_type, params=None, data=None, *args, **kwargs):
        RT = "refresh_token"
        _data = data.copy()  # to prevent side effect
        refresh_token = _data.get(RT)
        resp = await super(Client, self)._obtain_token(
            grant_type, params, _data, *args, **kwargs)
        if "error" not in resp and self.token_saver:
            _resp = resp.copy()
            if grant_type == RT and RT in _resp and isinstance(refresh_token, dict):
                _resp.pop(RT)  # So we skip it in on_obtaining_tokens(); it will
                               # be handled in self.obtain_token_by_refresh_token()
            if "scope" in _resp:
                scope = _resp["scope"].split()  # It is conceptually a set,
                    # but we represent it as a list which can be persisted to JSON
            else:
                # Note: The scope will generally be absent in authorization grant,
                #       but our obtain_token_by_authorization_code(...) encourages
                #       app developer to still explicitly provide a scope here.
                scope = _data.get("scope")
            await self.token_saver({
                "client_id": self.client_id,
                "scope": scope,
                "token_endpoint": self.configuration["token_endpoint"],
                "grant_type": grant_type,  # can be used to know an IdToken-less
                                           # response is for an app or for a user
                "response": _resp, "params": params, "data": _data,
                })
        return resp

    async def obtain_token_by_refresh_token(self, token_item, scope=None,
            rt_getter=lambda token_item: token_item["refresh_token"],
            rt_remover=None,
            **kwargs):
        # type: (Union[str, dict], Union[str, list, set, tuple], Callable) -> dict
        """This is an overload which will trigger token storage callbacks.

        :param token_item:
            A refresh token (RT) item, in flexible format. It can be a string,
            or a whatever data structure containing RT string and its metadata,
            in such case the `rt_getter` callable must be able to
            extract the RT string out from the token item data structure.

            Either way, this token_item will be passed into other callbacks as-is.

        :param scope: If omitted, is treated as equal to the scope originally
            granted by the resource ownser,
            according to https://tools.ietf.org/html/rfc6749#section-6
        :param rt_getter: A callable to translate the token_item to a raw RT string
        :param rt_remover: If absent, fall back to the one defined in initialization
        """
        resp = super(Client, self).obtain_token_by_refresh_token(
            rt_getter(token_item)
                if not isinstance(token_item, string_types) else token_item,
            scope=scope,
            **kwargs)
        if resp.get('error') == 'invalid_grant' and (rt_remover or self.rt_remover):
            await (rt_remover or self.rt_remover)(token_item)  # Discard old RT
        if 'refresh_token' in resp and self.rt_updater:
            await self.rt_updater(token_item, resp['refresh_token'])
        return resp

