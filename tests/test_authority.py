import os

from msal.authority import _validate_issuer
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
    "issuer": "https://contoso.com/tenant",
    "authorization_endpoint": "https://contoso.com/authorize",
    "token_endpoint": "https://contoso.com/token",
    })
class OidcAuthorityTestCase(unittest.TestCase):
    authority = "https://contoso.com/tenant"
    authorization_endpoint = "https://contoso.com/authorize"
    token_endpoint = "https://contoso.com/token"

    def setUp(self):
        # setUp() gives subclass a dynamic setup based on their authority
        self.oidc_discovery_endpoint = (
            # MSAL Python always does OIDC Discovery,
            # not to be confused with Instance Discovery
            # Here the test is to confirm the OIDC endpoint contains no "/v2.0"
            self.authority + "/.well-known/openid-configuration")

    def test_authority_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        c = MinimalHttpClient()
        a = Authority(None, c, oidc_authority_url=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(self.oidc_discovery_endpoint, c)
        self.assertEqual(a.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(a.token_endpoint, self.token_endpoint)

    def test_application_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        app = msal.ClientApplication(
            "id", authority=None, oidc_authority=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            self.oidc_discovery_endpoint, app.http_client)
        self.assertEqual(
            app.authority.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(app.authority.token_endpoint, self.token_endpoint)


class DstsAuthorityTestCase(unittest.TestCase):
    # Test cases for dSTS authority (no longer inherits from OidcAuthorityTestCase to avoid patch conflicts)
    authority = 'https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common'
    authorization_endpoint = "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/authorize"
    token_endpoint = "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/token"

    def setUp(self):
        self.oidc_discovery_endpoint = self.authority + "/.well-known/openid-configuration"

    @patch("msal.authority._instance_discovery")
    @patch("msal.authority.tenant_discovery", return_value={
        "issuer": "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common",
        "authorization_endpoint": "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/authorize",
        "token_endpoint": "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/token",
    })
    def test_authority_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        c = MinimalHttpClient()
        a = Authority(None, c, oidc_authority_url=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(self.oidc_discovery_endpoint, c)
        self.assertEqual(a.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(a.token_endpoint, self.token_endpoint)

    @patch("msal.authority._instance_discovery")
    @patch("msal.authority.tenant_discovery", return_value={
        "issuer": "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common",
        "authorization_endpoint": "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/authorize",
        "token_endpoint": "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/token",
    })
    def test_application_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        app = msal.ClientApplication(
            "id", authority=None, oidc_authority=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            self.oidc_discovery_endpoint, app.http_client)
        self.assertEqual(
            app.authority.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(app.authority.token_endpoint, self.token_endpoint)

    @patch("msal.authority._instance_discovery")
    @patch("msal.authority.tenant_discovery", return_value={
        "issuer": "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common",
        "authorization_endpoint": "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/authorize",
        "token_endpoint": "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/token",
    })
    def test_application_obj_should_accept_dsts_url_as_an_authority(
            self, oidc_discovery, instance_discovery):
        app = msal.ClientApplication("id", authority=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            self.oidc_discovery_endpoint, app.http_client)
        self.assertEqual(
            app.authority.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(app.authority.token_endpoint, self.token_endpoint)


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

    def test_canonicalize_rejects_empty_host(self):
        with self.assertRaises(ValueError):
            canonicalize("https:///tenant")


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


class TestOidcIssuerValidation(unittest.TestCase):
    """Tests for OIDC issuer validation against authority and trusted sources."""

    # ===== Exact Match Tests =====

    def test_issuer_exact_match(self):
        _validate_issuer("https://example.com/tenant", "https://example.com/tenant")

    def test_issuer_exact_match_with_issuer_trailing_slash(self):
        _validate_issuer("https://example.com/tenant/", "https://example.com/tenant")

    def test_issuer_exact_match_with_authority_trailing_slash(self):
        _validate_issuer("https://example.com/tenant", "https://example.com/tenant/")

    def test_issuer_exact_match_both_trailing_slashes(self):
        _validate_issuer("https://example.com/tenant/", "https://example.com/tenant/")

    def test_issuer_exact_match_with_path_segments(self):
        _validate_issuer(
            "https://example.com/tenant/v2.0",
            "https://example.com/tenant/v2.0")

    def test_issuer_exact_match_guid_tenant(self):
        _validate_issuer(
            "https://example.com/12345678-1234-1234-1234-123456789012",
            "https://example.com/12345678-1234-1234-1234-123456789012")

    # ===== Trusted Hosts Tests =====

    def test_issuer_from_all_trusted_hosts(self):
        for host in TRUSTED_ISSUER_HOSTS:
            with self.subTest(host=host):
                _validate_issuer(
                    "https://{}/tenant-id".format(host),
                    "https://custom.example.com/tenant-id")

    def test_issuer_login_microsoftonline_com(self):
        _validate_issuer(
            "https://login.microsoftonline.com/contoso.com",
            "https://custom-domain.com/contoso.com")

    def test_issuer_login_microsoft_com(self):
        _validate_issuer(
            "https://login.microsoft.com/tenant-guid",
            "https://my-app.contoso.com/tenant-guid")

    def test_issuer_login_windows_net(self):
        _validate_issuer(
            "https://login.windows.net/tenant",
            "https://example.com/tenant")

    def test_issuer_sts_windows_net(self):
        _validate_issuer(
            "https://sts.windows.net/tenant-id/",
            "https://example.com/tenant-id")

    def test_issuer_login_chinacloudapi_cn(self):
        _validate_issuer(
            "https://login.chinacloudapi.cn/tenant",
            "https://myapp.cn/tenant")

    def test_issuer_login_partner_microsoftonline_cn(self):
        _validate_issuer(
            "https://login.partner.microsoftonline.cn/tenant",
            "https://myapp.cn/tenant")

    def test_issuer_login_microsoftonline_de(self):
        _validate_issuer(
            "https://login.microsoftonline.de/tenant",
            "https://myapp.de/tenant")

    def test_issuer_login_microsoftonline_us(self):
        _validate_issuer(
            "https://login.microsoftonline.us/tenant",
            "https://myapp.gov/tenant")

    def test_issuer_login_us_microsoftonline_com(self):
        _validate_issuer(
            "https://login-us.microsoftonline.com/tenant",
            "https://myapp.gov/tenant")

    def test_issuer_login_usgovcloudapi_net(self):
        _validate_issuer(
            "https://login.usgovcloudapi.net/tenant",
            "https://myapp.gov/tenant")

    # ===== Regional Host Tests =====

    def test_issuer_regional_westus2_login_microsoft_com(self):
        _validate_issuer(
            "https://westus2.login.microsoft.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_eastus_login_microsoft_com(self):
        _validate_issuer(
            "https://eastus.login.microsoft.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_westeurope_login_microsoftonline_com(self):
        _validate_issuer(
            "https://westeurope.login.microsoftonline.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_southeastasia_login_microsoftonline_com(self):
        _validate_issuer(
            "https://southeastasia.login.microsoftonline.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_uksouth_login_microsoft_com(self):
        _validate_issuer(
            "https://uksouth.login.microsoft.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_japaneast_login_microsoftonline_com(self):
        _validate_issuer(
            "https://japaneast.login.microsoftonline.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_australiaeast_login_microsoft_com(self):
        _validate_issuer(
            "https://australiaeast.login.microsoft.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_brazilsouth_login_microsoftonline_com(self):
        _validate_issuer(
            "https://brazilsouth.login.microsoftonline.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_canadacentral_login_microsoft_com(self):
        _validate_issuer(
            "https://canadacentral.login.microsoft.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_issuer_regional_centralindia_login_microsoftonline_com(self):
        _validate_issuer(
            "https://centralindia.login.microsoftonline.com/tenant-id",
            "https://custom.example.com/tenant-id")

    def test_all_regional_bases_support_region_prefix(self):
        regions = ["westus2", "eastus", "westeurope", "northeurope", "uksouth"]
        for base in TRUSTED_ISSUER_HOSTS:
            for region in regions:
                with self.subTest(region=region, base=base):
                    _validate_issuer(
                        "https://{}.{}/tenant".format(region, base),
                        "https://example.com/tenant")

    # ===== Failure Cases - Missing/Empty Issuer =====

    def test_missing_issuer_none(self):
        with self.assertRaises(ValueError) as cm:
            _validate_issuer(None, "https://example.com/tenant")
        self.assertIn("missing", str(cm.exception).lower())
        self.assertIn("issuer", str(cm.exception).lower())

    def test_missing_issuer_empty_string(self):
        with self.assertRaises(ValueError) as cm:
            _validate_issuer("", "https://example.com/tenant")
        self.assertIn("missing", str(cm.exception).lower())

    # ===== Failure Cases - Untrusted Issuers =====

    def test_untrusted_issuer_random_domain(self):
        with self.assertRaises(ValueError) as cm:
            _validate_issuer("https://evil.com/tenant", "https://example.com/tenant")
        self.assertIn("not from a trusted source", str(cm.exception))

    def test_untrusted_issuer_similar_looking_domain(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://login.microsoftonline.com.evil.com/tenant",
                "https://example.com/tenant")

    def test_untrusted_issuer_subdomain_of_untrusted(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://login.microsoft.com.attacker.com/tenant",
                "https://example.com/tenant")

    def test_untrusted_issuer_typosquat_microsoftonline(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://login.micros0ftonline.com/tenant",
                "https://example.com/tenant")

    def test_untrusted_issuer_typosquat_microsoft(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://login.micr0soft.com/tenant",
                "https://example.com/tenant")

    def test_untrusted_issuer_different_tenant(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://example.com/different-tenant",
                "https://example.com/my-tenant")

    def test_untrusted_issuer_localhost(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://localhost/tenant",
                "https://example.com/tenant")

    def test_untrusted_issuer_ip_address(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://192.168.1.1/tenant",
                "https://example.com/tenant")

    def test_untrusted_issuer_private_domain(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://internal.corp.local/tenant",
                "https://example.com/tenant")

    # ===== Failure Cases - Regional Pattern Abuse =====

    def test_untrusted_regional_pattern_non_trusted_base(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://westus2.evil.com/tenant",
                "https://example.com/tenant")

    def test_untrusted_double_region_prefix(self):
        with self.assertRaises(ValueError):
            _validate_issuer(
                "https://westus2.eastus.login.microsoft.com/tenant",
                "https://example.com/tenant")

    # ===== Edge Cases - URL Variations =====

    def test_issuer_with_port_number_exact_match(self):
        _validate_issuer(
            "https://example.com:443/tenant",
            "https://example.com:443/tenant")

    def test_issuer_with_port_trusted_host(self):
        _validate_issuer(
            "https://login.microsoftonline.com:443/tenant",
            "https://example.com/tenant")

    def test_issuer_case_sensitivity_host_uppercase(self):
        _validate_issuer(
            "https://LOGIN.MICROSOFTONLINE.COM/tenant",
            "https://example.com/tenant")

    def test_issuer_case_sensitivity_host_mixed(self):
        _validate_issuer(
            "https://Login.MicrosoftOnline.Com/tenant",
            "https://example.com/tenant")

    def test_issuer_with_query_string_exact_match(self):
        _validate_issuer(
            "https://example.com/tenant?foo=bar",
            "https://example.com/tenant?foo=bar")

    # ===== Edge Cases - B2C and CIAM Patterns =====

    def test_issuer_b2c_pattern_trusted_host(self):
        _validate_issuer(
            "https://login.microsoftonline.com/te/contoso.onmicrosoft.com/b2c_1_signin/v2.0",
            "https://contoso.b2clogin.com/contoso.onmicrosoft.com/b2c_1_signin/v2.0")

    def test_issuer_ciam_pattern_trusted_host(self):
        _validate_issuer(
            "https://login.microsoftonline.com/12345678-1234-1234-1234-123456789012/v2.0",
            "https://contoso.ciamlogin.com/12345678-1234-1234-1234-123456789012")

    # ===== Edge Cases - Version Paths =====

    def test_issuer_v1_endpoint(self):
        _validate_issuer(
            "https://sts.windows.net/tenant-id/",
            "https://example.com/tenant-id")

    def test_issuer_v2_endpoint(self):
        _validate_issuer(
            "https://login.microsoftonline.com/tenant-id/v2.0",
            "https://example.com/tenant-id")

    # ===== Boundary Tests =====

    def test_issuer_very_long_tenant_id(self):
        long_tenant = "a" * 500
        _validate_issuer(
            "https://login.microsoftonline.com/{}".format(long_tenant),
            "https://example.com/other")

    def test_issuer_minimal_path(self):
        _validate_issuer(
            "https://login.microsoftonline.com/",
            "https://example.com/tenant")

    def test_issuer_special_characters_in_tenant(self):
        _validate_issuer(
            "https://login.microsoftonline.com/tenant-with-dash_and_underscore.onmicrosoft.com",
            "https://example.com/tenant")

