import unittest
try:
    from unittest.mock import patch, ANY
except:
    from mock import patch, ANY

from tests.http_client import MinimalResponse
from tests.test_token_cache import build_response

import msal


class TestCcsRoutingInfoTestCase(unittest.TestCase):

    def test_acquire_token_by_auth_code_flow(self):
        app = msal.ClientApplication("client_id")
        state = "foo"
        flow = app.initiate_auth_code_flow(
            ["some", "scope"], login_hint="johndoe@contoso.com", state=state)
        with patch.object(app.http_client, "post", return_value=MinimalResponse(
                status_code=400, text='{"error": "mock"}')) as mocked_method:
            app.acquire_token_by_auth_code_flow(flow, {
                "state": state,
                "code": "bar",
                "client_info":  # MSAL asks for client_info, so it would be available
                    "eyJ1aWQiOiJhYTkwNTk0OS1hMmI4LTRlMGEtOGFlYS1iMzJlNTNjY2RiNDEiLCJ1dGlkIjoiNzJmOTg4YmYtODZmMS00MWFmLTkxYWItMmQ3Y2QwMTFkYjQ3In0",
                })
            self.assertEqual(
                "Oid:aa905949-a2b8-4e0a-8aea-b32e53ccdb41@72f988bf-86f1-41af-91ab-2d7cd011db47",
                mocked_method.call_args[1].get("headers", {}).get('X-AnchorMailbox'),
                "CSS routing info should be derived from client_info")

    # I've manually tested acquire_token_interactive. No need to automate it,
    # because it and acquire_token_by_auth_code_flow() share same code path.

    def test_acquire_token_silent(self):
        uid = "foo"
        utid = "bar"
        client_id = "my_client_id"
        scopes = ["some", "scope"]
        authority_url = "https://login.microsoftonline.com/common"
        token_cache = msal.TokenCache()
        token_cache.add({  # Pre-populate the cache
            "client_id": client_id,
            "scope": scopes,
            "token_endpoint": "{}/oauth2/v2.0/token".format(authority_url),
            "response": build_response(
                access_token="an expired AT to trigger refresh", expires_in=-99,
                uid=uid, utid=utid, refresh_token="this is a RT"),
            })  # The add(...) helper populates correct home_account_id for future searching
        app = msal.ClientApplication(
            client_id, authority=authority_url, token_cache=token_cache)
        with patch.object(app.http_client, "post", return_value=MinimalResponse(
                status_code=400, text='{"error": "mock"}')) as mocked_method:
            account = {"home_account_id": "{}.{}".format(uid, utid)}
            app.acquire_token_silent(["scope"], account)
            self.assertEqual(
                "Oid:{}@{}".format(  # Server accepts case-insensitive value
                    uid, utid),  # It would look like "Oid:foo@bar"
                mocked_method.call_args[1].get("headers", {}).get('X-AnchorMailbox'),
                "CSS routing info should be derived from home_account_id")

    def test_acquire_token_by_username_password(self):
        app = msal.ClientApplication("client_id")
        username = "johndoe@contoso.com"
        with patch.object(app.http_client, "post", return_value=MinimalResponse(
                status_code=400, text='{"error": "mock"}')) as mocked_method:
            app.acquire_token_by_username_password(username, "password", ["scope"])
            self.assertEqual(
                "upn:" + username,
                mocked_method.call_args[1].get("headers", {}).get('X-AnchorMailbox'),
                "CSS routing info should be derived from client_info")

