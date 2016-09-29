from msal.authority import *
from msal.exceptions import MsalServiceError
from tests import unittest


class TestAuthority(unittest.TestCase):

    def test_wellknown_authority(self):
        # Test one specific sample in straightforward way, for readability
        a = Authority('https://login.windows.net/tenant')
        self.assertEqual(
            a.authorization_endpoint,
            'https://login.windows.net/tenant/oauth2/v2.0/authorize')
        self.assertEqual(
            a.token_endpoint,
            'https://login.windows.net/tenant/oauth2/v2.0/token')

        # Test all well known authority hosts, using predefined constants
        for host in WELL_KNOWN_AUTHORITY_HOSTS:
            url = 'https://{}/tenant'.format(host)
            a = Authority(url)
            self.assertEqual(
                a.authorization_endpoint, url + AUTHORIZATION_ENDPOINT)
            self.assertEqual(a.token_endpoint, url + TOKEN_ENDPOINT)

    def test_unknown_authority(self):
        url = 'https://login.microsoftonline.in/unknown-tenant.onmicrosoft.com'
        with self.assertRaises(MsalServiceError):
            a = Authority(url)  # Expects tenant discovery failure
        a = Authority(url, validate_authority=False)
        self.assertEqual(
            a.authorization_endpoint,
            'https://login.microsoftonline.in/unknown-tenant.onmicrosoft.com/oauth2/v2.0/authorize')
        self.assertEqual(
            a.token_endpoint,
            'https://login.microsoftonline.in/unknown-tenant.onmicrosoft.com/oauth2/v2.0/token')


class TestAuthorityInternalHelpers(unittest.TestCase):  # They aren't public API

    def test_canonicalize_tenant_followed_by_extra_paths(self):
        self.assertEqual(
            canonicalize("https://example.com/tenant/subpath?foo=bar#fragment"),
            ("https://example.com/tenant", "example.com", "tenant"))

    def test_canonicalize_tenant_followed_by_extra_query(self):
        self.assertEqual(
            canonicalize("https://example.com/tenant?foo=bar#fragment"),
            ("https://example.com/tenant", "example.com", "tenant"))

    def test_canonicalize_tenant_followed_by_extra_fragment(self):
        self.assertEqual(
            canonicalize("https://example.com/tenant#fragment"),
            ("https://example.com/tenant", "example.com", "tenant"))

    def test_canonicalize_rejects_non_https(self):
        with self.assertRaises(ValueError):
            canonicalize("http://non.https.example.com/tenant")

    def test_canonicalize_rejects_tenantless(self):
        with self.assertRaises(ValueError):
            canonicalize("https://no.tenant.example.com")

    def test_canonicalize_rejects_tenantless_host_with_trailing_slash(self):
        with self.assertRaises(ValueError):
            canonicalize("https://no.tenant.example.com/")

    def test_instance_discovery_with_unknown_tenant(self):
        with self.assertRaises(MsalServiceError):
            instance_discovery("https://login.microsoftonline.in/nonexist.com")

    def test_instance_discovery(self):
        mock_response = {'tenant_discovery_endpoint': 'http://a.com'}
        endpoint = instance_discovery(
            "https://login.microsoftonline.in/tenant.com", mock_response)
        self.assertEqual(endpoint, mock_response['tenant_discovery_endpoint'])

