import json

from tests import unittest
from tests.http_client import RecordingHttpClient, MinimalResponse
from msal.application import ConfidentialClientApplication


class TestSovereignAuthorityForClientCredentialWithRecordingHttpClient(unittest.TestCase):
    def test_acquire_token_for_client_on_gov_fr_should_keep_calls_on_same_host(self):
        host = "login.sovcloud-identity.fr"
        expected_instance_discovery_url = "https://{}/common/discovery/instance".format(host)
        expected_instance_discovery_params = {
            "api-version": "1.1",
            "authorization_endpoint": (
                "https://{}/common/oauth2/authorize".format(host)
            ),
        }

        http_client = RecordingHttpClient()

        def is_oidc_discovery(call):
            return call["url"].startswith(
                "https://{}/common/v2.0/.well-known/openid-configuration".format(host))

        def oidc_discovery_response(_call):
            return MinimalResponse(status_code=200, text=json.dumps({
                "authorization_endpoint": "https://{}/common/oauth2/v2.0/authorize".format(host),
                "token_endpoint": "https://{}/common/oauth2/v2.0/token".format(host),
                "issuer": "https://{}/common/v2.0".format(host),
            }))

        def is_instance_discovery(call):
            return (
                call["url"] == expected_instance_discovery_url
                and call["params"] == expected_instance_discovery_params
            )

        def instance_discovery_response(_call):
            return MinimalResponse(status_code=200, text=json.dumps({
                "tenant_discovery_endpoint": (
                    "https://login.microsoftonline.us/"
                    "cab8a31a-1906-4287-a0d8-4eef66b95f6e/"
                    "v2.0/.well-known/openid-configuration"
                ),
                "api-version": "1.1",
                "metadata": [
                    {
                        "preferred_network": "login.microsoftonline.com",
                        "preferred_cache": "login.windows.net",
                        "aliases": [
                            "login.microsoftonline.com",
                            "login.windows.net",
                            "login.microsoft.com",
                            "sts.windows.net",
                        ],
                    },
                    {
                        "preferred_network": "login.partner.microsoftonline.cn",
                        "preferred_cache": "login.partner.microsoftonline.cn",
                        "aliases": [
                            "login.partner.microsoftonline.cn",
                            "login.chinacloudapi.cn",
                        ],
                    },
                    {
                        "preferred_network": "login.microsoftonline.de",
                        "preferred_cache": "login.microsoftonline.de",
                        "aliases": ["login.microsoftonline.de"],
                    },
                    {
                        "preferred_network": "login.microsoftonline.us",
                        "preferred_cache": "login.microsoftonline.us",
                        "aliases": [
                            "login.microsoftonline.us",
                            "login.usgovcloudapi.net",
                        ],
                    },
                    {
                        "preferred_network": "login-us.microsoftonline.com",
                        "preferred_cache": "login-us.microsoftonline.com",
                        "aliases": ["login-us.microsoftonline.com"],
                    },
                ],
            }))

        token_counter = {"value": 0}

        def is_token_call(call):
            return call["url"].startswith("https://{}/common/oauth2/v2.0/token".format(host))

        def token_response(_call):
            token_counter["value"] += 1
            return MinimalResponse(status_code=200, text=json.dumps({
                "access_token": "AT_{}".format(token_counter["value"]),
                "expires_in": 3600,
            }))

        http_client.add_get_route(is_oidc_discovery, oidc_discovery_response)
        http_client.add_get_route(is_instance_discovery, instance_discovery_response)
        http_client.add_post_route(is_token_call, token_response)

        app = ConfidentialClientApplication(
            "client_id",
            client_credential="secret",
            authority="https://{}/common".format(host),
            http_client=http_client,
        )

        result1 = app.acquire_token_for_client(["scope1"])
        self.assertEqual("AT_1", result1.get("access_token"))

        get_calls_after_first = list(http_client.get_calls)
        post_calls_after_first = list(http_client.post_calls)

        result2 = app.acquire_token_for_client(["scope2"])
        self.assertEqual("AT_2", result2.get("access_token"))

        post_count_after_scope2 = len(http_client.post_calls)
        get_count_after_scope2 = len(http_client.get_calls)

        cached_result1 = app.acquire_token_for_client(["scope1"])
        self.assertEqual("AT_1", cached_result1.get("access_token"))

        cached_result2 = app.acquire_token_for_client(["scope2"])
        self.assertEqual("AT_2", cached_result2.get("access_token"))

        cached_result3 = app.acquire_token_for_client(["scope1"])
        self.assertEqual("AT_1", cached_result3.get("access_token"))

        self.assertEqual(
            post_count_after_scope2,
            len(http_client.post_calls),
            "Subsequent same-scope calls should be served from cache without token POST")
        self.assertEqual(
            get_count_after_scope2,
            len(http_client.get_calls),
            "Subsequent same-authority calls should not trigger additional discovery GET")

        self.assertEqual(1, len(get_calls_after_first), "First acquire should trigger one discovery GET")
        self.assertTrue(
            get_calls_after_first[0]["url"].startswith(
                "https://{}/common/v2.0/.well-known/openid-configuration".format(host)))

        self.assertEqual(1, len(post_calls_after_first), "First acquire should trigger one token POST")
        self.assertTrue(
            post_calls_after_first[0]["url"].startswith("https://{}/common/oauth2/v2.0/token".format(host)))

        self.assertEqual(1, len(http_client.get_calls), "Second acquire on same authority should not re-discover")
        self.assertEqual(2, len(http_client.post_calls), "Second acquire with a different scope should request another token")
        self.assertTrue(
            http_client.post_calls[1]["url"].startswith("https://{}/common/oauth2/v2.0/token".format(host)))

        all_urls = [c["url"] for c in http_client.get_calls + http_client.post_calls]
        self.assertTrue(all("login.microsoftonline.com" not in url for url in all_urls))
        self.assertTrue(all("https://{}/".format(host) in url for url in all_urls))
