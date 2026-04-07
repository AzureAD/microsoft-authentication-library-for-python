import os
try:
    from unittest.mock import patch
except:
    from mock import patch

import msal
from msal.authority import *
from msal.authority import _CIAM_DOMAIN_SUFFIX
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
        # This test makes real HTTP calls to authority endpoints.
        # It is intentionally network-based to validate reachable hosts end-to-end.
        excluded_hosts = {
            DEPRECATED_AZURE_CHINA,
            "login.microsoftonline.de",  # deprecated
            "login.microsoft.com",  # issuer-only in this test context
            "login.windows.net",  # issuer-only in this test context
            "sts.windows.net",  # issuer-only in this test context
            "login.partner.microsoftonline.cn",  # issuer-only in this test context
            "login.usgovcloudapi.net",  # issuer-only in this test context
            AZURE_GOV_FR,  # currently unreachable in this environment
            AZURE_GOV_DE,  # currently unreachable in this environment
            AZURE_GOV_SG,  # currently unreachable in this environment
        }
        for host in WELL_KNOWN_AUTHORITY_HOSTS:
            if host in excluded_hosts:
                continue
            self._test_given_host_and_tenant(host, "common")

    @patch("msal.authority._instance_discovery")
    @patch("msal.authority.tenant_discovery")
    def test_new_sovereign_hosts_should_build_authority_endpoints(
            self, tenant_discovery_mock, instance_discovery_mock):
        for host in WELL_KNOWN_AUTHORITY_HOSTS:
            tenant_discovery_mock.return_value = {
                "authorization_endpoint": "https://{}/common/oauth2/v2.0/authorize".format(host),
                "token_endpoint": "https://{}/common/oauth2/v2.0/token".format(host),
                "issuer": "https://{}/common/v2.0".format(host),
            }
            instance_discovery_mock.return_value = {
                "tenant_discovery_endpoint": (
                    "https://{}/common/v2.0/.well-known/openid-configuration".format(host)
                ),
            }
            c = MinimalHttpClient()
            a = Authority(AuthorityBuilder(host, "common"), c)
            self.assertEqual(
                a.authorization_endpoint,
                "https://{}/common/oauth2/v2.0/authorize".format(host))
            self.assertEqual(
                a.token_endpoint,
                "https://{}/common/oauth2/v2.0/token".format(host))
            c.close()

    @patch("msal.authority._instance_discovery")
    @patch("msal.authority.tenant_discovery")
    def test_known_authority_should_use_same_host_and_skip_instance_discovery(
            self, tenant_discovery_mock, instance_discovery_mock):
        for host in WELL_KNOWN_AUTHORITY_HOSTS:
            tenant_discovery_mock.return_value = {
                "authorization_endpoint": "https://{}/common/oauth2/v2.0/authorize".format(host),
                "token_endpoint": "https://{}/common/oauth2/v2.0/token".format(host),
                "issuer": "https://{}/common/v2.0".format(host),
            }
            c = MinimalHttpClient()
            Authority("https://{}/common".format(host), c)
            c.close()

            instance_discovery_mock.assert_not_called()
            tenant_discovery_endpoint = tenant_discovery_mock.call_args[0][0]
            self.assertTrue(
                tenant_discovery_endpoint.startswith(
                    "https://{}/common/v2.0/.well-known/openid-configuration".format(host)))

    @patch("msal.authority._instance_discovery")
    @patch("msal.authority.tenant_discovery")
    def test_unknown_authority_should_use_world_wide_instance_discovery_endpoint(
            self, tenant_discovery_mock, instance_discovery_mock):
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/tenant/oauth2/v2.0/authorize",
            "token_endpoint": "https://example.com/tenant/oauth2/v2.0/token",
            "issuer": "https://example.com/tenant/v2.0",
        }
        instance_discovery_mock.return_value = {
            "tenant_discovery_endpoint": "https://example.com/tenant/v2.0/.well-known/openid-configuration",
        }

        c = MinimalHttpClient()
        Authority("https://example.com/tenant", c)
        c.close()

        self.assertEqual(
            "https://{}/common/discovery/instance".format(WORLD_WIDE),
            instance_discovery_mock.call_args[0][2])

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
    "issuer": "https://contoso.com/tenant",
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
@patch("msal.authority.tenant_discovery")  # Moved return_value out of the decorator
class OidcAuthorityTestCase(unittest.TestCase):
    authority = "https://contoso.com/tenant"
    authorization_endpoint = "https://contoso.com/authorize"
    token_endpoint = "https://contoso.com/token"
    issuer = "https://contoso.com/tenant"  # Added as class variable for inheritance

    def setUp(self):
        # setUp() gives subclass a dynamic setup based on their authority
        self.oidc_discovery_endpoint = (
            # MSAL Python always does OIDC Discovery,
            # not to be confused with Instance Discovery
            # Here the test is to confirm the OIDC endpoint contains no "/v2.0"
            self.authority + "/.well-known/openid-configuration")

    def setup_tenant_discovery(self, tenant_discovery):
        """Configure the tenant_discovery mock with class-specific values"""
        tenant_discovery.return_value = {
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "issuer": self.issuer,
        }

    def test_authority_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        self.setup_tenant_discovery(oidc_discovery)

        c = MinimalHttpClient()
        a = Authority(None, c, oidc_authority_url=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(self.oidc_discovery_endpoint, c)
        self.assertEqual(a.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(a.token_endpoint, self.token_endpoint)

    def test_application_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        self.setup_tenant_discovery(oidc_discovery)

        app = msal.ClientApplication(
            "id", authority=None, oidc_authority=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            self.oidc_discovery_endpoint, app.http_client)
        self.assertEqual(
            app.authority.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(app.authority.token_endpoint, self.token_endpoint)


@patch("msal.authority._instance_discovery")
@patch("msal.authority.tenant_discovery")
class DstsAuthorityTestCase(unittest.TestCase):
    # Standalone test class for dSTS authority (not inheriting to avoid decorator stacking)
    authority = 'https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common'
    authorization_endpoint = "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/authorize"
    token_endpoint = "https://some.url.dsts.core.azure-test.net/dstsv2/common/oauth2/token"
    issuer = "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common"

    def setUp(self):
        self.oidc_discovery_endpoint = self.authority + "/.well-known/openid-configuration"

    def setup_tenant_discovery(self, tenant_discovery):
        """Configure the tenant_discovery mock with class-specific values"""
        tenant_discovery.return_value = {
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "issuer": self.issuer,
        }

    def test_authority_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        self.setup_tenant_discovery(oidc_discovery)
        c = MinimalHttpClient()
        a = Authority(None, c, oidc_authority_url=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(self.oidc_discovery_endpoint, c)
        self.assertEqual(a.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(a.token_endpoint, self.token_endpoint)

    def test_application_obj_should_do_oidc_discovery_and_skip_instance_discovery(
            self, oidc_discovery, instance_discovery):
        self.setup_tenant_discovery(oidc_discovery)
        app = msal.ClientApplication(
            "id", authority=None, oidc_authority=self.authority)
        instance_discovery.assert_not_called()
        oidc_discovery.assert_called_once_with(
            self.oidc_discovery_endpoint, app.http_client)
        self.assertEqual(
            app.authority.authorization_endpoint, self.authorization_endpoint)
        self.assertEqual(app.authority.token_endpoint, self.token_endpoint)

    def test_application_obj_should_accept_dsts_url_as_an_authority(
            self, oidc_discovery, instance_discovery):
        self.setup_tenant_discovery(oidc_discovery)
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
    "issuer": "https://contoso.com/tenant",
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
        instance_metadata.assert_called_once_with("login.microsoftonline.com")

    def test_by_default_a_sovereign_known_authority_should_use_cloud_local_instance_metadata(
            self, instance_metadata, known_to_microsoft_validation, _):
        app = msal.ClientApplication("id", authority="https://login.microsoftonline.us/common")
        known_to_microsoft_validation.assert_not_called()
        app.get_accounts()  # This could make an instance metadata call for authority aliases
        instance_metadata.assert_called_once_with("login.microsoftonline.us")

    def test_fr_known_authority_should_still_work_when_instance_metadata_has_no_alias_entry(
            self, instance_metadata, known_to_microsoft_validation, _):
        app = msal.ClientApplication("id", authority="https://{}/common".format(AZURE_GOV_FR))
        known_to_microsoft_validation.assert_not_called()

        accounts = app.get_accounts()

        self.assertEqual([], accounts)
        instance_metadata.assert_called_once_with(AZURE_GOV_FR)

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


class TestAuthorityIssuerValidation(unittest.TestCase):
    """Test cases for authority.has_valid_issuer method """
    
    def setUp(self):
        self.http_client = MinimalHttpClient()
    
    def _create_authority_with_issuer(self, oidc_authority_url, issuer, tenant_discovery_mock):
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        authority = Authority(
            None, 
            self.http_client, 
            oidc_authority_url=oidc_authority_url
        )
        return authority
    
    @patch("msal.authority.tenant_discovery")
    def test_exact_match_issuer(self, tenant_discovery_mock):
        """Test when issuer exactly matches the OIDC authority URL"""
        authority_url = "https://example.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, authority_url, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(), "Issuer should be valid when it exactly matches the authority URL")
    
    @patch("msal.authority.tenant_discovery")
    def test_no_issuer(self, tenant_discovery_mock):
        """Test when no issuer is returned from OIDC discovery"""
        authority_url = "https://example.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            # No issuer key
        }
        # Since initialization now checks for valid issuer, we expect it to raise ValueError
        with self.assertRaises(ValueError) as context:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertIn("issuer", str(context.exception).lower())

    @patch("msal.authority.tenant_discovery")
    def test_same_scheme_and_host_different_path(self, tenant_discovery_mock):
        """Test when issuer has same scheme and host but different path"""
        authority_url = "https://example.com/tenant"
        issuer = f"https://{WORLD_WIDE}/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(), "Issuer should be valid when it has the same scheme and host")
    
    @patch("msal.authority.tenant_discovery")
    def test_ciam_authority_with_matching_tenant(self, tenant_discovery_mock):
        """Test CIAM authority with matching tenant in path"""
        authority_url = "https://custom-domain.com/tenant_name"
        issuer = f"https://tenant_name{_CIAM_DOMAIN_SUFFIX}"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(), "Issuer should be valid for CIAM pattern with matching tenant")
    
    @patch("msal.authority.tenant_discovery")
    def test_ciam_authority_with_host_tenant(self, tenant_discovery_mock):
        """Test CIAM authority with tenant in hostname"""
        tenant_name = "tenant_name"
        authority_url = f"https://{tenant_name}{_CIAM_DOMAIN_SUFFIX}/custom/path"
        issuer = f"https://{tenant_name}{_CIAM_DOMAIN_SUFFIX}"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(), "Issuer should be valid for CIAM pattern with tenant in hostname")
    
    @patch("msal.authority.tenant_discovery")
    def test_invalid_issuer(self, tenant_discovery_mock):
        """Test when issuer is completely different from authority"""
        authority_url = "https://example.com/tenant"
        issuer = "https://malicious-site.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        # Since initialization now checks for valid issuer, we expect it to raise ValueError
        with self.assertRaises(ValueError) as context:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertIn("issuer", str(context.exception).lower())
        self.assertIn(issuer, str(context.exception))
        self.assertIn(authority_url, str(context.exception))

    @patch("msal.authority.tenant_discovery")
    def test_custom_authority_with_microsoft_issuer(self, tenant_discovery_mock):
        """Test when custom authority is used with a known Microsoft issuer (should succeed)"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = f"https://{WORLD_WIDE}/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer from trusted Microsoft host should be valid even with custom authority")

    @patch("msal.authority.tenant_discovery")
    def test_known_authority_with_non_matching_issuer(self, tenant_discovery_mock):
        """Test when known authority is used with an issuer that doesn't match (should fail)"""
        # Known Microsoft authority URLs
        authority_url = f"https://{WORLD_WIDE}/tenant"
        issuer = "https://custom-domain.com/tenant"

        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }

        # We expect it to raise ValueError because the paths don't match
        # and we're now checking for exact matches
        with self.assertRaises(ValueError) as context:
            Authority(None, self.http_client, oidc_authority_url=authority_url)

        self.assertIn("issuer", str(context.exception).lower())
        self.assertIn(issuer, str(context.exception))
        self.assertIn(authority_url, str(context.exception))

    # Regional pattern tests
    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_westus2_login_microsoft(self, tenant_discovery_mock):
        """Test regional variant: westus2.login.microsoft.com"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://westus2.login.microsoftonline.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(), 
            "Regional issuer westus2.login.microsoftonline.com should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_eastus_login_microsoftonline(self, tenant_discovery_mock):
        """Test regional variant: eastus.login.microsoftonline.com"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://eastus.login.microsoftonline.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Regional issuer eastus.login.microsoftonline.com should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_for_china_cloud(self, tenant_discovery_mock):
        """Test regional variant for China cloud: region.login.chinacloudapi.cn"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://chinanorth.login.chinacloudapi.cn/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Regional issuer for China cloud should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_for_us_government(self, tenant_discovery_mock):
        """Test regional variant for US Government: region.login.microsoftonline.us"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://usgovvirginia.login.microsoftonline.us/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Regional issuer for US Government should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_invalid_regional_pattern_with_dots_in_region(self, tenant_discovery_mock):
        """Test that region with dots is rejected: west.us.2.login.microsoftonline.com"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://west.us.2.login.microsoftonline.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_invalid_regional_pattern_untrusted_base(self, tenant_discovery_mock):
        """Test that regional pattern with untrusted base is rejected"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://westus2.login.evil.com/tenant"  # evil.com is not trusted
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_well_known_host_issuer_directly(self, tenant_discovery_mock):
        """Test issuer from well-known Microsoft host directly (not regional)"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = f"https://{WORLD_WIDE}/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer from well-known Microsoft host should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_issuer_with_trailing_slash_match(self, tenant_discovery_mock):
        """Test issuer validation handles trailing slashes"""
        authority_url = "https://example.com/tenant/"
        issuer = "https://example.com/tenant"  # No trailing slash
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Trailing slash difference should not affect exact match")

    @patch("msal.authority.tenant_discovery")
    def test_issuer_case_sensitivity_host(self, tenant_discovery_mock):
        """Test that host comparison is case-insensitive for regional check"""
        authority_url = "https://custom-authority.com/tenant"
        issuer = "https://WESTUS2.LOGIN.MICROSOFTONLINE.COM/tenant"  # Uppercase
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Host comparison should be case-insensitive")

    # Case 3b: Regional prefix on authority host tests
    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_matching_authority_host(self, tenant_discovery_mock):
        """Test issuer with region prefix on the authority host: us.someweb.com -> someweb.com"""
        authority_url = "https://someweb.com/tenant"
        issuer = "https://us.someweb.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer with region prefix on authority host should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_westus2_on_custom_authority(self, tenant_discovery_mock):
        """Test issuer westus2.myidp.example.com with authority myidp.example.com"""
        authority_url = "https://myidp.example.com/tenant"
        issuer = "https://westus2.myidp.example.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Regional prefix westus2 on custom authority host should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_does_not_match_different_authority(self, tenant_discovery_mock):
        """Test issuer us.someweb.com should NOT match authority otherdomain.com"""
        authority_url = "https://otherdomain.com/tenant"
        issuer = "https://us.someweb.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_regional_issuer_on_authority_with_different_path(self, tenant_discovery_mock):
        """Test issuer eastus.someweb.com/v2 with authority someweb.com/tenant"""
        authority_url = "https://someweb.com/tenant"
        issuer = "https://eastus.someweb.com/v2"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Regional issuer with different path should still match by host")

    # Case 5: B2C host suffix tests
    @patch("msal.authority.tenant_discovery")
    def test_b2c_issuer_host(self, tenant_discovery_mock):
        """Test issuer from a well-known B2C host: tenant.b2clogin.com"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://tenant.b2clogin.com/tenant/v2.0"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer ending with b2clogin.com should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_b2c_china_issuer_host(self, tenant_discovery_mock):
        """Test issuer from B2C China host: tenant.b2clogin.cn"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://tenant.b2clogin.cn/tenant/v2.0"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer ending with b2clogin.cn should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_b2c_us_gov_issuer_host(self, tenant_discovery_mock):
        """Test issuer from B2C US Government host: tenant.b2clogin.us"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://tenant.b2clogin.us/tenant/v2.0"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer ending with b2clogin.us should be valid")

    @patch("msal.authority.tenant_discovery")
    def test_ciam_issuer_host_via_b2c_check(self, tenant_discovery_mock):
        """Test issuer from ciamlogin.com host is accepted via B2C check"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://mytenant.ciamlogin.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Issuer ending with ciamlogin.com should be valid")

    # Domain spoofing prevention tests
    @patch("msal.authority.tenant_discovery")
    def test_spoofed_b2c_host_should_be_rejected(self, tenant_discovery_mock):
        """fakeb2clogin.com must NOT match b2clogin.com"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://fakeb2clogin.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_spoofed_b2c_host_with_prefix_should_be_rejected(self, tenant_discovery_mock):
        """evilb2clogin.com must NOT match b2clogin.com"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://evilb2clogin.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_b2c_domain_used_as_subdomain_of_evil_site_should_be_rejected(self, tenant_discovery_mock):
        """b2clogin.com.evil.com must NOT match b2clogin.com"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://b2clogin.com.evil.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_spoofed_ciamlogin_host_should_be_rejected(self, tenant_discovery_mock):
        """fakeciamlogin.com must NOT match ciamlogin.com"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://fakeciamlogin.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_valid_b2c_subdomain_should_be_accepted(self, tenant_discovery_mock):
        """login.b2clogin.com should match .b2clogin.com"""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://login.b2clogin.com/tenant"
        authority = self._create_authority_with_issuer(authority_url, issuer, tenant_discovery_mock)
        self.assertTrue(authority.has_valid_issuer(),
            "Legitimate subdomain of b2clogin.com should be valid")

