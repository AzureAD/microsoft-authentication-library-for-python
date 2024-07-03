import os
try:
    from unittest.mock import patch
except:
    from mock import patch

import msal
from msal.authority import *
from tests import unittest
from tests.http_client import MinimalHttpClient


@unittest.skipIf(os.getenv("TRAVIS_TAG"), "Skip network io during tagged release")
class TestAuthority(unittest.TestCase):

    def _test_given_host_and_tenant(self, host, tenant):
        c = MinimalHttpClient()
        a = Authority('https://{}/{}'.format(host, tenant), c)
        self.assertEqual(
            a.authorization_endpoint,
            'https://{}/{}/oauth2/v2.0/authorize'.format(host, tenant))
        self.assertEqual(
            a.token_endpoint,
            'https://{}/{}/oauth2/v2.0/token'.format(host, tenant))
        c.close()

    def _test_authority_builder(self, host, tenant):
        c = MinimalHttpClient()
        a = Authority(AuthorityBuilder(host, tenant), c)
        self.assertEqual(
            a.authorization_endpoint,
            'https://{}/{}/oauth2/v2.0/authorize'.format(host, tenant))
        self.assertEqual(
            a.token_endpoint,
            'https://{}/{}/oauth2/v2.0/token'.format(host, tenant))
        c.close()

    def test_wellknown_host_and_tenant(self):
        # Assert all well known authority hosts are using their own "common" tenant
        for host in WELL_KNOWN_AUTHORITY_HOSTS:
            if host != AZURE_CHINA:  # It is prone to ConnectionError
                self._test_given_host_and_tenant(host, "common")

    def test_wellknown_host_and_tenant_using_new_authority_builder(self):
        self._test_authority_builder(AZURE_PUBLIC, "consumers")
        self._test_authority_builder(AZURE_US_GOVERNMENT, "common")
        ## AZURE_CHINA is prone to some ConnectionError. We skip it to speed up our tests.
        # self._test_authority_builder(AZURE_CHINA, "organizations")

    @unittest.skip("As of Jan 2017, the server no longer returns V1 endpoint")
    def test_lessknown_host_will_return_a_set_of_v1_endpoints(self):
        # This is an observation for current (2016-10) server-side behavior.
        # It is probably not a strict API contract. I simply mention it here.
        less_known = 'login.windows.net'  # less.known.host/
        v1_token_endpoint = 'https://{}/common/oauth2/token'.format(less_known)
        a = Authority(
            'https://{}/common'.format(less_known), MinimalHttpClient())
        self.assertEqual(a.token_endpoint, v1_token_endpoint)
        self.assertNotIn('v2.0', a.token_endpoint)

    def test_unknown_host_wont_pass_instance_discovery(self):
        _assert = (
            # Was Regexp, added alias Regex in Py 3.2, and Regexp will be gone in Py 3.12
            getattr(self, "assertRaisesRegex", None) or
            getattr(self, "assertRaisesRegexp", None))
        with _assert(ValueError, "invalid_instance"):
            Authority('https://example.com/tenant_doesnt_matter_in_this_case',
                      MinimalHttpClient())

    def test_invalid_host_skipping_validation_can_be_turned_off(self):
        try:
            Authority(
                'https://example.com/invalid',
                MinimalHttpClient(), validate_authority=False)
        except ValueError as e:
            if "invalid_instance" in str(e):  # Imprecise but good enough
                self.fail("validate_authority=False should turn off validation")
        except:  # Could be requests...RequestException, json...JSONDecodeError, etc.
            pass  # Those are expected for this unittest case


@patch("msal.authority.tenant_discovery", return_value={
    "authorization_endpoint": "https://contoso.com/placeholder",
    "token_endpoint": "https://contoso.com/placeholder",
    })
class TestCiamAuthority(unittest.TestCase):
    http_client = MinimalHttpClient()

    def test_path_less_authority_should_work(self, oidc_discovery):
        Authority('https://contoso.ciamlogin.com', self.http_client)
        oidc_discovery.assert_called_once_with(
            "https://contoso.ciamlogin.com/contoso.onmicrosoft.com/v2.0/.well-known/openid-configuration",
            self.http_client)

    def test_authority_with_path_should_be_used_as_is(self, oidc_discovery):
        Authority('https://contoso.ciamlogin.com/anything', self.http_client)
        oidc_discovery.assert_called_once_with(
            "https://contoso.ciamlogin.com/anything/v2.0/.well-known/openid-configuration",
            self.http_client)


@patch("msal.authority._instance_discovery")
@patch("msal.authority.tenant_discovery", return_value={
    "authorization_endpoint": "https://contoso.com/authorize",
    "token_endpoint": "https://contoso.com/token",
    })
class TestOidcAuthority(unittest.TestCase):
    def test_authority_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        c = MinimalHttpClient()
        a = Authority(None, c, oidc_authority_url="https://contoso.com/tenant")
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            "https://contoso.com/tenant/.well-known/openid-configuration", c)
        self.assertEqual(a.authorization_endpoint, 'https://contoso.com/authorize')
        self.assertEqual(a.token_endpoint, 'https://contoso.com/token')

    def test_application_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        app = msal.ClientApplication(
            "id",
            authority=None,
            oidc_authority="https://contoso.com/tenant",
            )
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            "https://contoso.com/tenant/.well-known/openid-configuration",
            app.http_client)
        self.assertEqual(
            app.authority.authorization_endpoint, 'https://contoso.com/authorize')
        self.assertEqual(app.authority.token_endpoint, 'https://contoso.com/token')

class TestAuthorityInternalHelperCanonicalize(unittest.TestCase):

    def test_canonicalize_tenant_followed_by_extra_paths(self):
        _, i, t = canonicalize("https://example.com/tenant/subpath?foo=bar#fragment")
        self.assertEqual("example.com", i)
        self.assertEqual("tenant", t)

    def test_canonicalize_tenant_followed_by_extra_query(self):
        _, i, t = canonicalize("https://example.com/tenant?foo=bar#fragment")
        self.assertEqual("example.com", i)
        self.assertEqual("tenant", t)

    def test_canonicalize_tenant_followed_by_extra_fragment(self):
        _, i, t = canonicalize("https://example.com/tenant#fragment")
        self.assertEqual("example.com", i)
        self.assertEqual("tenant", t)

    def test_canonicalize_rejects_non_https(self):
        with self.assertRaises(ValueError):
            canonicalize("http://non.https.example.com/tenant")

    def test_canonicalize_rejects_tenantless(self):
        with self.assertRaises(ValueError):
            canonicalize("https://no.tenant.example.com")

    def test_canonicalize_rejects_tenantless_host_with_trailing_slash(self):
        with self.assertRaises(ValueError):
            canonicalize("https://no.tenant.example.com/")


@unittest.skipIf(os.getenv("TRAVIS_TAG"), "Skip network io during tagged release")
class TestAuthorityInternalHelperUserRealmDiscovery(unittest.TestCase):
    def test_memorize(self):
        # We use a real authority so the constructor can finish tenant discovery
        authority = "https://login.microsoftonline.com/common"
        self.assertNotIn(authority, Authority._domains_without_user_realm_discovery)
        a = Authority(authority, MinimalHttpClient(), validate_authority=False)

        try:
            # We now pretend this authority supports no User Realm Discovery
            class MockResponse(object):
                status_code = 404
            a.user_realm_discovery("john.doe@example.com", response=MockResponse())
            self.assertIn(
                "login.microsoftonline.com",
                Authority._domains_without_user_realm_discovery,
                "user_realm_discovery() should memorize domains not supporting URD")
            a.user_realm_discovery("john.doe@example.com",
                response="This would cause exception if memorization did not work")
        finally:  # MUST NOT let the previous test changes affect other test cases
            Authority._domains_without_user_realm_discovery = set([])


@patch("msal.authority.tenant_discovery", return_value={
    "authorization_endpoint": "https://contoso.com/placeholder",
    "token_endpoint": "https://contoso.com/placeholder",
    })
@patch("msal.authority._instance_discovery")
@patch.object(msal.ClientApplication, "_get_instance_metadata", return_value=[])
class TestMsalBehaviorsWithoutAndWithInstanceDiscoveryBoolean(unittest.TestCase):
    """Test cases use ClientApplication, which is a base class of both PCA and CCA"""

    def test_by_default_a_known_to_microsoft_authority_should_skip_validation_but_still_use_instance_metadata(
            self, instance_metadata, known_to_microsoft_validation, _):
        app = msal.ClientApplication("id", authority="https://login.microsoftonline.com/common")
        known_to_microsoft_validation.assert_not_called()
        app.get_accounts()  # This could make an instance metadata call for authority aliases
        instance_metadata.assert_called_once_with()

    def test_validate_authority_boolean_should_skip_validation_and_instance_metadata(
            self, instance_metadata, known_to_microsoft_validation, _):
        """Pending deprecation, but kept for backward compatibility, for now"""
        app = msal.ClientApplication(
            "id", authority="https://contoso.com/common", validate_authority=False)
        known_to_microsoft_validation.assert_not_called()
        app.get_accounts()  # This could make an instance metadata call for authority aliases
        instance_metadata.assert_not_called()

    def test_by_default_adfs_should_skip_validation_and_instance_metadata(
            self, instance_metadata, known_to_microsoft_validation, _):
        """Not strictly required, but when/if we already supported it, we better keep it"""
        app = msal.ClientApplication("id", authority="https://contoso.com/adfs")
        known_to_microsoft_validation.assert_not_called()
        app.get_accounts()  # This could make an instance metadata call for authority aliases
        instance_metadata.assert_not_called()

    def test_by_default_b2c_should_skip_validation_and_instance_metadata(
            self, instance_metadata, known_to_microsoft_validation, _):
        """Not strictly required, but when/if we already supported it, we better keep it"""
        app = msal.ClientApplication(
            "id", authority="https://login.b2clogin.com/contoso/b2c_policy")
        known_to_microsoft_validation.assert_not_called()
        app.get_accounts()  # This could make an instance metadata call for authority aliases
        instance_metadata.assert_not_called()

    def test_turning_off_instance_discovery_should_work_for_all_kinds_of_clouds(
            self, instance_metadata, known_to_microsoft_validation, _):
        for authority in [
                "https://login.microsoftonline.com/common",  # Known to Microsoft
                "https://contoso.com/adfs",  # ADFS
                "https://login.b2clogin.com/contoso/b2c_policy",  # B2C
                "https://private.cloud/foo",  # Private Cloud
                ]:
            self._test_turning_off_instance_discovery_should_skip_authority_validation_and_instance_metadata(
                authority, instance_metadata, known_to_microsoft_validation)

    def _test_turning_off_instance_discovery_should_skip_authority_validation_and_instance_metadata(
            self, authority, instance_metadata, known_to_microsoft_validation):
        app = msal.ClientApplication("id", authority=authority, instance_discovery=False)
        known_to_microsoft_validation.assert_not_called()
        app.get_accounts()  # This could make an instance metadata call for authority aliases
        instance_metadata.assert_not_called()

