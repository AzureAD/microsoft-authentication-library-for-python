"""Simulator(s) that can be used to create MSAL instance
whose token cache is in a certain state, and remains unchanged.
This generic simulator(s) becomes the test subject for different benchmark tools.

For example, you can install pyperf and then run:

    pyperf timeit -s "from tests.simulator import ClientCredentialGrantSimulator as T; t=T(tokens_per_tenant=1, cache_hit=True)" "t.run()"
"""
import json
import logging
import random
from unittest.mock import patch

import msal
from tests.http_client import MinimalResponse


logger = logging.getLogger(__name__)


def _count_access_tokens(app):
    return len(app.token_cache._cache[app.token_cache.CredentialType.ACCESS_TOKEN])


class ClientCredentialGrantSimulator(object):

    def __init__(self, number_of_tenants=1, tokens_per_tenant=1, cache_hit=False):
        logger.info(
            "number_of_tenants=%d, tokens_per_tenant=%d, cache_hit=%s",
            number_of_tenants, tokens_per_tenant, cache_hit)
        with patch.object(msal.authority, "tenant_discovery", return_value={
            "authorization_endpoint": "https://contoso.com/placeholder",
            "token_endpoint": "https://contoso.com/placeholder",
        }) as _:  # Otherwise it would fail on OIDC discovery
            self.apps = [  # In MSAL Python, each CCA binds to one tenant only
                msal.ConfidentialClientApplication(
                    "client_id", client_credential="foo",
                    authority="https://login.microsoftonline.com/tenant_%s" % t,
                ) for t in range(number_of_tenants)
            ]
        for app in self.apps:
            for i in range(tokens_per_tenant):  # Populate token cache
                self.run(app=app, scope="scope_{}".format(i))
            assert tokens_per_tenant == _count_access_tokens(app), (
                "Token cache did not populate correctly: {}".format(json.dumps(
                app.token_cache._cache, indent=4)))

            if cache_hit:
                self.run(app=app)["access_token"]  # Populate 1 token to be hit
                expected_tokens = tokens_per_tenant + 1
            else:
                expected_tokens = tokens_per_tenant
            app.token_cache.modify = lambda *args, **kwargs: None  # Cache becomes read-only
            self.run(app=app)["access_token"]
            assert expected_tokens == _count_access_tokens(app), "Cache shall not grow"

    def run(self, app=None, scope=None):
        # This implementation shall be as concise as possible
        app = app or random.choice(self.apps)
        #return app.acquire_token_for_client([scope or "scope"], post=_fake)
        return app.acquire_token_for_client(
            [scope or "scope"],
            post=lambda url, **kwargs: MinimalResponse(  # Using an inline lambda is as fast as a standalone function
                status_code=200, text='''{
                "access_token": "AT for %s",
                "token_type": "bearer"
                }''' % kwargs["data"]["scope"],
            ))

