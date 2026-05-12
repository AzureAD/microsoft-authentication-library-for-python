import os
try:
    from unittest.mock import patch
except:
    from mock import patch

import msal
from msal.authority import *
from msal.authority import _CIAM_DOMAIN_SUFFIX
from tests import unittest
from tests.http_client import MinimalHttpClient, RecordingHttpClient


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
    def test_ciam_issuer_with_tenant_mismatch_should_be_rejected(self, tenant_discovery_mock):
        """CIAM-shaped issuer whose subdomain tenant does not match the
        authority's first path segment must be rejected (Rule 3 tenant match)."""
        authority_url = "https://custom-domain.com/tenant"
        issuer = "https://mytenant.ciamlogin.com/tenant"
        tenant_discovery_mock.return_value = {
            "authorization_endpoint": "https://example.com/oauth2/authorize",
            "token_endpoint": "https://example.com/oauth2/token",
            "issuer": issuer,
        }
        with self.assertRaises(ValueError) as context:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertIn("issuer", str(context.exception).lower())

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



# -----------------------------------------------------------------------------
# Cross-cloud OIDC discovery hardening (mirrors MSAL.NET 4.84.0 fix #5927).
# Categories: B same-cloud aliases, C #5927 federation, D regional same-cloud,
# E CIAM tenant pattern, F cross-cloud rejection (THE FIX), H helper,
# I endpoint check end-to-end, J behavior matrix.
# -----------------------------------------------------------------------------

from msal.authority import (
    _are_in_same_cloud,
    _resolve_known_cloud,
    _ensure_endpoint_same_cloud_as_authority,
)

# Stable wording in the cross-cloud error; we assert on this rather than an
# error-code constant, matching the rest of MSAL Python.
_XCLOUD_ERROR_SUBSTRING = "sovereign cloud"


_PUBLIC = "login.microsoftonline.com"
_PUBLIC_ALIASES = (
    "login.microsoftonline.com",
    "login.windows.net",
    "login.microsoft.com",
    "sts.windows.net",
)
_CHINA = "login.partner.microsoftonline.cn"
_CHINA_ALIASES = (
    "login.partner.microsoftonline.cn",
    "login.chinacloudapi.cn",
)
_US_GOV = "login.microsoftonline.us"
_US_GOV_ALIASES = (
    "login.microsoftonline.us",
    "login.usgovcloudapi.net",
)


def _mock_oidc(tenant_discovery_mock, *, issuer, authorization_endpoint=None,
               token_endpoint=None):
    tenant_discovery_mock.return_value = {
        "authorization_endpoint": authorization_endpoint
            or "https://example.com/oauth2/authorize",
        "token_endpoint": token_endpoint
            or "https://example.com/oauth2/token",
        "issuer": issuer,
    }


# --- (B) Rule 2 - same-cloud combinations across every alias -----------------

class TestIssuerValidationSameCloudAliases(unittest.TestCase):
    """Authority is a known Microsoft host; issuer is a different alias of the
    SAME sovereign cloud. Must accept (Rule 2b)."""

    def setUp(self):
        self.http_client = MinimalHttpClient()

    def _check_pass(self, authority_host, issuer_host, tenant_discovery_mock):
        authority_url = "https://{}/tenant".format(authority_host)
        issuer = "https://{}/tenant".format(issuer_host)
        # Same-cloud endpoints so the cross-cloud endpoint check is a no-op.
        _mock_oidc(
            tenant_discovery_mock,
            issuer=issuer,
            authorization_endpoint="https://{}/tenant/oauth2/v2.0/authorize".format(authority_host),
            token_endpoint="https://{}/tenant/oauth2/v2.0/token".format(authority_host),
        )
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer(),
            "Issuer '{}' should be accepted under same-cloud authority '{}'".format(
                issuer, authority_url))

    @patch("msal.authority.tenant_discovery")
    def test_public_aliases_pairwise(self, tenant_discovery_mock):
        for issuer_host in _PUBLIC_ALIASES:
            if issuer_host == _PUBLIC:
                continue
            self._check_pass(_PUBLIC, issuer_host, tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_china_aliases_pairwise(self, tenant_discovery_mock):
        for issuer_host in _CHINA_ALIASES:
            if issuer_host == _CHINA:
                continue
            self._check_pass(_CHINA, issuer_host, tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_us_gov_aliases_pairwise(self, tenant_discovery_mock):
        for issuer_host in _US_GOV_ALIASES:
            if issuer_host == _US_GOV:
                continue
            self._check_pass(_US_GOV, issuer_host, tenant_discovery_mock)


# --- (C) Rule 2a - custom-domain federation (#5927 regression) ---------------

class TestIssuerValidationCustomDomainFederation(unittest.TestCase):
    """Custom-domain authority + ANY known-MS issuer in any cloud must pass.
    Regression coverage for issue 5927 (parentpay.com-style federations)."""

    def setUp(self):
        self.http_client = MinimalHttpClient()

    @patch("msal.authority.tenant_discovery")
    def test_parentpay_style_public_cloud(self, tenant_discovery_mock):
        authority_url = "https://clientlogin.test.parentpay.com/tid/v2.0"
        issuer = "https://login.microsoftonline.com/tid/v2.0"
        _mock_oidc(tenant_discovery_mock, issuer=issuer)
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())

    @patch("msal.authority.tenant_discovery")
    def test_custom_domain_with_us_gov_issuer(self, tenant_discovery_mock):
        authority_url = "https://customlogin.example.com/tenant"
        issuer = "https://login.microsoftonline.us/tenant"
        _mock_oidc(tenant_discovery_mock, issuer=issuer)
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())

    @patch("msal.authority.tenant_discovery")
    def test_custom_domain_with_china_issuer(self, tenant_discovery_mock):
        authority_url = "https://idp.contoso.com/tenant"
        issuer = "https://login.partner.microsoftonline.cn/tenant"
        _mock_oidc(tenant_discovery_mock, issuer=issuer)
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())


# --- (D) Rule 3 - regional same-cloud ----------------------------------------

class TestIssuerValidationRegionalSameCloud(unittest.TestCase):
    """Authority is a known Microsoft host; issuer is a regional sub-host of
    the SAME cloud. Must accept."""

    def setUp(self):
        self.http_client = MinimalHttpClient()

    @patch("msal.authority.tenant_discovery")
    def test_regional_public(self, tenant_discovery_mock):
        authority_url = "https://login.microsoftonline.com/tenant"
        issuer = "https://westus2.login.microsoft.com/tenant"
        _mock_oidc(
            tenant_discovery_mock, issuer=issuer,
            authorization_endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            token_endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        )
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())

    @patch("msal.authority.tenant_discovery")
    def test_regional_china(self, tenant_discovery_mock):
        authority_url = "https://login.partner.microsoftonline.cn/tenant"
        issuer = "https://chinaeast2.login.chinacloudapi.cn/tenant"
        _mock_oidc(
            tenant_discovery_mock, issuer=issuer,
            authorization_endpoint="https://login.partner.microsoftonline.cn/tenant/oauth2/v2.0/authorize",
            token_endpoint="https://login.partner.microsoftonline.cn/tenant/oauth2/v2.0/token",
        )
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())

    @patch("msal.authority.tenant_discovery")
    def test_regional_sovereign_de(self, tenant_discovery_mock):
        authority_url = "https://login.sovcloud-identity.de/tenant"
        issuer = "https://germanywestcentral.login.sovcloud-identity.de/tenant"
        _mock_oidc(
            tenant_discovery_mock, issuer=issuer,
            authorization_endpoint="https://login.sovcloud-identity.de/tenant/oauth2/v2.0/authorize",
            token_endpoint="https://login.sovcloud-identity.de/tenant/oauth2/v2.0/token",
        )
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())


# --- (E) Rule 3 - CIAM tenant pattern (additional) ---------------------------

class TestIssuerValidationCiamTenantPattern(unittest.TestCase):

    def setUp(self):
        self.http_client = MinimalHttpClient()

    @patch("msal.authority.tenant_discovery")
    def test_ciam_with_v2_path_segment(self, tenant_discovery_mock):
        """https://<tenant>.ciamlogin.com/<tenant>/v2.0 should pass when the
        authority's first path segment matches <tenant>."""
        authority_url = "https://customdomain.com/contoso"
        issuer = "https://contoso.ciamlogin.com/contoso/v2.0"
        _mock_oidc(tenant_discovery_mock, issuer=issuer)
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())

    @patch("msal.authority.tenant_discovery")
    def test_ciam_pathless_authority_uses_first_label_as_tenant(
            self, tenant_discovery_mock):
        # Authority has no path -> tenant is the first hostname label
        authority_url = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
        # Path-less authority is canonicalized differently; this test uses a
        # CIAM authority where the tenant comes from path segment 1.
        issuer = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com/v2.0"
        # CIAM is Public; mock endpoints on the CIAM authority host so the
        # issuer-rule branch is what we exercise (not the endpoint guard).
        _mock_oidc(
            tenant_discovery_mock,
            issuer=issuer,
            authorization_endpoint="https://contoso.ciamlogin.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize",
            token_endpoint="https://contoso.ciamlogin.com/contoso.onmicrosoft.com/oauth2/v2.0/token",
        )
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertTrue(a.has_valid_issuer())


# --- (F) Cross-cloud (THE FIX) - must throw ---------------------------------

class TestIssuerValidationCrossCloudRejection(unittest.TestCase):
    """Authority is in cloud A, issuer is in cloud B (A != B).
    Must reject. This is the security fix."""

    def setUp(self):
        self.http_client = MinimalHttpClient()

    def _expect_reject(self, authority_url, issuer, tenant_discovery_mock):
        # Match endpoints to authority cloud so the issuer mismatch is the
        # one that fires (we test endpoint-cloud mismatch separately).
        authority_host = urlparse(authority_url).hostname
        _mock_oidc(
            tenant_discovery_mock, issuer=issuer,
            authorization_endpoint="https://{}/tenant/oauth2/v2.0/authorize".format(authority_host),
            token_endpoint="https://{}/tenant/oauth2/v2.0/token".format(authority_host),
        )
        with self.assertRaises(ValueError) as ctx:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertIn("issuer", str(ctx.exception).lower())

    @patch("msal.authority.tenant_discovery")
    def test_public_authority_china_issuer(self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.microsoftonline.com/tenant",
            "https://login.partner.microsoftonline.cn/tenant",
            tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_public_authority_us_gov_issuer(self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.microsoftonline.com/tenant",
            "https://login.microsoftonline.us/tenant",
            tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_us_gov_authority_public_issuer(self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.microsoftonline.us/tenant",
            "https://login.microsoftonline.com/tenant",
            tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_china_authority_public_issuer(self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.partner.microsoftonline.cn/tenant",
            "https://login.microsoftonline.com/tenant",
            tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_public_authority_regional_china_issuer(self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.microsoftonline.com/tenant",
            "https://chinaeast2.login.chinacloudapi.cn/tenant",
            tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_us_gov_authority_regional_public_issuer(self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.microsoftonline.us/tenant",
            "https://westus2.login.microsoft.com/tenant",
            tenant_discovery_mock)

    @patch("msal.authority.tenant_discovery")
    def test_bleu_authority_delos_issuer_sovereign_to_sovereign(
            self, tenant_discovery_mock):
        self._expect_reject(
            "https://login.sovcloud-identity.fr/tenant",
            "https://login.sovcloud-identity.de/tenant",
            tenant_discovery_mock)


# --- (G) Other rejections (multi-label region prefix, http scheme, etc.) -----

class TestIssuerValidationOtherRejections(unittest.TestCase):

    def setUp(self):
        self.http_client = MinimalHttpClient()

    @patch("msal.authority.tenant_discovery")
    def test_http_scheme_issuer_under_known_authority_is_rejected(
            self, tenant_discovery_mock):
        # http:// under a known-MS authority must be rejected: Rule 2
        # requires HTTPS so an MITM cannot mint a known-MS-looking issuer.
        authority_url = "https://login.microsoftonline.com/tenant"
        issuer = "http://login.microsoftonline.com/tenant"
        _mock_oidc(
            tenant_discovery_mock, issuer=issuer,
            authorization_endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            token_endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        )
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)

    @patch("msal.authority.tenant_discovery")
    def test_multi_label_regional_prefix_is_rejected(self, tenant_discovery_mock):
        # attacker.evil.login.microsoft.com has TWO labels before the alias
        # "login.microsoft.com", so it is not a valid regional sub-host.
        authority_url = "https://customlogin.example.com/tenant"
        issuer = "https://attacker.evil.login.microsoft.com/tenant"
        _mock_oidc(tenant_discovery_mock, issuer=issuer)
        with self.assertRaises(ValueError):
            Authority(None, self.http_client, oidc_authority_url=authority_url)


# --- (H) _are_in_same_cloud helper - direct ----------------------------------

class TestAreInSameCloudHelper(unittest.TestCase):
    """Direct test of the cloud-resolution helper."""

    # PASS: same cloud
    def test_public_aliases_are_same_cloud(self):
        self.assertTrue(_are_in_same_cloud(
            "login.microsoftonline.com", "login.windows.net"))

    def test_public_with_regional_public(self):
        self.assertTrue(_are_in_same_cloud(
            "login.microsoftonline.com", "westus2.login.microsoft.com"))

    def test_us_gov_aliases_are_same_cloud(self):
        self.assertTrue(_are_in_same_cloud(
            "login.microsoftonline.us", "login.usgovcloudapi.net"))

    def test_china_with_regional_china(self):
        self.assertTrue(_are_in_same_cloud(
            "login.partner.microsoftonline.cn",
            "chinaeast2.login.chinacloudapi.cn"))

    def test_bleu_with_regional_bleu(self):
        self.assertTrue(_are_in_same_cloud(
            "login.sovcloud-identity.fr",
            "francecentral.login.sovcloud-identity.fr"))

    # REJECT: different / unknown
    def test_public_vs_china_different_cloud(self):
        self.assertFalse(_are_in_same_cloud(
            "login.microsoftonline.com", "login.partner.microsoftonline.cn"))

    def test_public_vs_us_gov_different_cloud(self):
        self.assertFalse(_are_in_same_cloud(
            "login.microsoftonline.com", "login.microsoftonline.us"))

    def test_us_gov_vs_china(self):
        self.assertFalse(_are_in_same_cloud(
            "login.microsoftonline.us", "login.chinacloudapi.cn"))

    def test_public_vs_germany_legacy(self):
        self.assertFalse(_are_in_same_cloud(
            "login.microsoftonline.com", "login.microsoftonline.de"))

    def test_bleu_vs_delos(self):
        self.assertFalse(_are_in_same_cloud(
            "login.sovcloud-identity.fr", "login.sovcloud-identity.de"))

    def test_ppe_vs_public(self):
        self.assertFalse(_are_in_same_cloud(
            "login.windows-ppe.net", "login.microsoftonline.com"))

    def test_known_vs_custom(self):
        self.assertFalse(_are_in_same_cloud(
            "login.microsoftonline.com", "custom.example.com"))

    def test_two_custom_hosts(self):
        self.assertFalse(_are_in_same_cloud(
            "custom.example.com", "another.example.org"))

    def test_custom_vs_none(self):
        self.assertFalse(_are_in_same_cloud("custom.example.com", None))

    def test_none_vs_known(self):
        self.assertFalse(_are_in_same_cloud(None, "login.microsoftonline.com"))

    # REJECT: regional-shape failures (base is Public; prefix should NOT match)
    def test_multi_label_prefix_rejected(self):
        self.assertFalse(_are_in_same_cloud(
            "attacker.evil.login.microsoft.com", "login.microsoftonline.com"))

    def test_underscore_prefix_rejected(self):
        self.assertFalse(_are_in_same_cloud(
            "weird_prefix.login.microsoft.com", "login.microsoftonline.com"))

    def test_uppercase_prefix_normalized(self):
        # _resolve_known_cloud lowercases the host, so PREFIX -> prefix
        # matches the regex and login.microsoft.com resolves to Public.
        # Live SDK Uri.Host is already lowercased; this is synthetic.
        self.assertTrue(_are_in_same_cloud(
            "PREFIX.login.microsoft.com", "login.microsoftonline.com"))

    def test_leading_hyphen_prefix_rejected(self):
        self.assertFalse(_are_in_same_cloud(
            "-leading.login.microsoft.com", "login.microsoftonline.com"))

    def test_trailing_hyphen_prefix_rejected(self):
        self.assertFalse(_are_in_same_cloud(
            "trailing-.login.microsoft.com", "login.microsoftonline.com"))

    def test_64_char_prefix_rejected(self):
        long_prefix = "a" * 64  # one over the 63-char DNS-label cap
        self.assertFalse(_are_in_same_cloud(
            "{}.login.microsoft.com".format(long_prefix),
            "login.microsoftonline.com"))

    # ACCEPT: real Azure region prefixes - must continue to work
    def test_real_region_westus2(self):
        self.assertTrue(_are_in_same_cloud(
            "westus2.login.microsoft.com", "login.microsoftonline.com"))

    def test_real_region_eastus2euap(self):
        self.assertTrue(_are_in_same_cloud(
            "eastus2euap.login.microsoft.com", "login.microsoftonline.com"))

    def test_real_region_chinaeast2(self):
        self.assertTrue(_are_in_same_cloud(
            "chinaeast2.login.chinacloudapi.cn",
            "login.partner.microsoftonline.cn"))

    def test_real_region_usgovvirginia(self):
        self.assertTrue(_are_in_same_cloud(
            "usgovvirginia.login.microsoftonline.us",
            "login.usgovcloudapi.net"))

    def test_real_region_francecentral(self):
        self.assertTrue(_are_in_same_cloud(
            "francecentral.login.sovcloud-identity.fr",
            "login.sovcloud-identity.fr"))

    def test_real_region_southafricanorth(self):
        self.assertTrue(_are_in_same_cloud(
            "southafricanorth.login.microsoft.com",
            "login.microsoftonline.com"))

    # CIAM is registered as Public via _EXTRA_HOSTS_BY_CLOUD so the
    # cross-cloud endpoint guard covers it. Make that explicit here.
    def test_ciam_subdomain_resolves_to_public(self):
        self.assertEqual(
            _resolve_known_cloud("contoso.ciamlogin.com"), "PUBLIC")
        self.assertTrue(_are_in_same_cloud(
            "contoso.ciamlogin.com", "login.microsoftonline.com"))

    def test_ciam_subdomain_vs_china_is_different_cloud(self):
        self.assertFalse(_are_in_same_cloud(
            "contoso.ciamlogin.com", "login.partner.microsoftonline.cn"))

    def test_well_known_authority_hosts_excludes_ciam(self):
        # CIAM lives in _EXTRA_HOSTS_BY_CLOUD specifically so it does NOT
        # leak into WELL_KNOWN_AUTHORITY_HOSTS, which gates
        # _get_instance_discovery_host. Regression guard.
        from msal.authority import WELL_KNOWN_AUTHORITY_HOSTS
        self.assertNotIn("ciamlogin.com", WELL_KNOWN_AUTHORITY_HOSTS)
        self.assertNotIn("contoso.ciamlogin.com", WELL_KNOWN_AUTHORITY_HOSTS)


# --- (I) End-to-end: endpoint same-cloud check (mocked HTTP) -----------------

class TestEndpointCrossCloudCheck(unittest.TestCase):
    """End-to-end coverage: cross-cloud endpoints are refused before any
    token POST; custom OIDC IdPs remain unconstrained."""

    def setUp(self):
        self.http_client = MinimalHttpClient()

    @patch("msal.authority.tenant_discovery")
    def test_cross_cloud_token_endpoint_is_refused(self, tenant_discovery_mock):
        """Public-cloud authority + China-cloud token_endpoint must throw
        the cross-cloud error before any token POST happens."""
        authority_url = "https://login.microsoftonline.com/contoso"
        # Issuer matches authority (Rule 1) so issuer validation passes;
        # the failure must come from the endpoint check.
        tenant_discovery_mock.return_value = {
            "issuer": authority_url,
            "authorization_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://login.partner.microsoftonline.cn/contoso/oauth2/v2.0/token",
        }
        # Use a recording client so we can assert no POST occurred.
        client = RecordingHttpClient()
        with self.assertRaises(ValueError) as ctx:
            Authority(None, client, oidc_authority_url=authority_url)
        message = str(ctx.exception)
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, message)
        self.assertIn(
            "https://login.partner.microsoftonline.cn/contoso/oauth2/v2.0/token",
            message)
        # Authority construction does not POST. The credentials POST that the
        # token-acquisition path would have issued must NOT have happened.
        self.assertEqual(client.post_calls, [])

    @patch("msal.authority.tenant_discovery")
    def test_custom_endpoint_under_known_authority_is_refused(
            self, tenant_discovery_mock):
        """Public-cloud authority + attacker-domain token_endpoint must throw."""
        authority_url = "https://login.microsoftonline.com/contoso"
        tenant_discovery_mock.return_value = {
            "issuer": authority_url,
            "authorization_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://attacker.example.com/oauth2/v2.0/token",
        }
        with self.assertRaises(ValueError) as ctx:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        message = str(ctx.exception)
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, message)
        self.assertIn("https://attacker.example.com/oauth2/v2.0/token", message)

    @patch("msal.authority.tenant_discovery")
    def test_cross_cloud_authorization_endpoint_is_refused(
            self, tenant_discovery_mock):
        """authorization_endpoint also enforced (token_endpoint is OK here)."""
        authority_url = "https://login.microsoftonline.com/contoso"
        tenant_discovery_mock.return_value = {
            "issuer": authority_url,
            "authorization_endpoint":
                "https://login.partner.microsoftonline.cn/contoso/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/token",
        }
        with self.assertRaises(ValueError) as ctx:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, str(ctx.exception))
        self.assertIn("authorization_endpoint", str(ctx.exception))

    @patch("msal.authority.tenant_discovery")
    def test_custom_idp_endpoint_is_allowed(self, tenant_discovery_mock):
        """Custom OIDC IdP authority: endpoint check is a no-op even on an
        unrelated host. Per-test unique host to isolate from any future
        OIDC-discovery caching."""
        authority_url = "https://idp.allowed-custom-{}.example.com/tenant".format(
            id(self))
        tenant_discovery_mock.return_value = {
            "issuer": authority_url,
            "authorization_endpoint":
                "https://idp.allowed-custom-{}.example.com/connect/authorize".format(id(self)),
            "token_endpoint":
                "https://idp.allowed-custom-{}.example.com/connect/token".format(id(self)),
        }
        # Should construct without raising the cross-cloud error.
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertEqual(
            a.token_endpoint,
            "https://idp.allowed-custom-{}.example.com/connect/token".format(id(self)))

    @patch("msal.authority.tenant_discovery")
    def test_cross_cloud_device_authorization_endpoint_is_refused(
            self, tenant_discovery_mock):
        """device_authorization_endpoint is enforced too: a cross-cloud
        device endpoint must be rejected before any device-code POST."""
        authority_url = "https://login.microsoftonline.com/contoso"
        tenant_discovery_mock.return_value = {
            "issuer": authority_url,
            "authorization_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/token",
            "device_authorization_endpoint":
                "https://login.partner.microsoftonline.cn/contoso/oauth2/v2.0/devicecode",
        }
        with self.assertRaises(ValueError) as ctx:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        message = str(ctx.exception)
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, message)
        self.assertIn("device_authorization_endpoint", message)
        self.assertIn(
            "https://login.partner.microsoftonline.cn/contoso/oauth2/v2.0/devicecode",
            message)

    @patch("msal.authority.tenant_discovery")
    def test_missing_device_authorization_endpoint_is_ok(
            self, tenant_discovery_mock):
        """device_authorization_endpoint is optional in the OIDC discovery
        document. Absence must not trip the new guard."""
        authority_url = "https://login.microsoftonline.com/contoso"
        tenant_discovery_mock.return_value = {
            "issuer": authority_url,
            "authorization_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/token",
            # No device_authorization_endpoint key at all.
        }
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertIsNone(a.device_authorization_endpoint)

    @patch("msal.authority.tenant_discovery")
    def test_ciam_authority_with_cross_cloud_token_endpoint_is_refused(
            self, tenant_discovery_mock):
        """CIAM resolves to Public, so a China token_endpoint under a CIAM
        authority must be rejected."""
        authority_url = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
        tenant_discovery_mock.return_value = {
            # Same-host issuer so issuer validation passes via Case 4 and the
            # failure comes from the endpoint check.
            "issuer": authority_url + "/v2.0",
            "authorization_endpoint":
                "https://contoso.ciamlogin.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://login.partner.microsoftonline.cn/contoso/oauth2/v2.0/token",
        }
        with self.assertRaises(ValueError) as ctx:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        message = str(ctx.exception)
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, message)
        self.assertIn("token_endpoint", message)

    @patch("msal.authority.tenant_discovery")
    def test_ciam_authority_with_same_cloud_public_endpoint_is_allowed(
            self, tenant_discovery_mock):
        """CIAM resolves to Public, so a Public-cloud endpoint under a CIAM
        authority must NOT trip the new guard."""
        authority_url = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
        tenant_discovery_mock.return_value = {
            "issuer": authority_url + "/v2.0",
            "authorization_endpoint":
                "https://contoso.ciamlogin.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize",
            # Public token endpoint under a CIAM authority is unusual but
            # in-cloud and therefore allowed.
            "token_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/token",
        }
        a = Authority(None, self.http_client, oidc_authority_url=authority_url)
        self.assertEqual(
            a.token_endpoint,
            "https://login.microsoftonline.com/contoso/oauth2/v2.0/token")

    @patch("msal.authority.tenant_discovery")
    def test_attacker_ciam_subdomain_under_public_authority_is_refused(
            self, tenant_discovery_mock):
        """Regression guard for the Rule 2b CIAM bypass: an attacker
        '<x>.ciamlogin.com' issuer under a Public authority must be rejected
        by Rule 3 before Rule 2b can short-circuit on same-cloud."""
        authority_url = "https://login.microsoftonline.com/contoso"
        # Issuer subdomain ('attacker') does NOT match the authority's first
        # path segment ('contoso'). Rule 3 must reject.
        issuer = "https://attacker.ciamlogin.com/attacker/v2.0"
        tenant_discovery_mock.return_value = {
            "issuer": issuer,
            "authorization_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/authorize",
            "token_endpoint":
                "https://login.microsoftonline.com/contoso/oauth2/v2.0/token",
        }
        with self.assertRaises(ValueError) as ctx:
            Authority(None, self.http_client, oidc_authority_url=authority_url)
        # The failure must come from issuer validation, not the endpoint
        # check (endpoints are same-cloud here).
        self.assertIn("issuer", str(ctx.exception).lower())


class TestEnsureEndpointSameCloudAsAuthorityHelper(unittest.TestCase):
    """Direct unit coverage for _ensure_endpoint_same_cloud_as_authority."""

    def test_no_op_for_custom_authority(self):
        # Should NOT raise for any endpoint when authority is custom-domain.
        _ensure_endpoint_same_cloud_as_authority(
            "https://idp.example.com/tenant",
            "https://login.partner.microsoftonline.cn/tenant/oauth2/token",
            "token_endpoint")

    def test_no_op_for_empty_endpoint(self):
        _ensure_endpoint_same_cloud_as_authority(
            "https://login.microsoftonline.com/tenant", "", "token_endpoint")
        _ensure_endpoint_same_cloud_as_authority(
            "https://login.microsoftonline.com/tenant", None, "token_endpoint")

    def test_raises_for_cross_cloud_endpoint(self):
        with self.assertRaises(ValueError) as ctx:
            _ensure_endpoint_same_cloud_as_authority(
                "https://login.microsoftonline.com/tenant",
                "https://login.partner.microsoftonline.cn/tenant/oauth2/token",
                "token_endpoint")
        message = str(ctx.exception)
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, message)
        self.assertIn("token_endpoint", message)

    def test_raises_for_unknown_endpoint_under_known_authority(self):
        with self.assertRaises(ValueError) as ctx:
            _ensure_endpoint_same_cloud_as_authority(
                "https://login.microsoftonline.com/tenant",
                "https://attacker.example.com/oauth2/token",
                "token_endpoint")
        self.assertIn(_XCLOUD_ERROR_SUBSTRING, str(ctx.exception))

    def test_passes_for_same_cloud_regional_endpoint(self):
        _ensure_endpoint_same_cloud_as_authority(
            "https://login.microsoftonline.com/tenant",
            "https://westus2.login.microsoft.com/tenant/oauth2/token",
            "token_endpoint")

    def test_passes_for_alias_endpoint(self):
        _ensure_endpoint_same_cloud_as_authority(
            "https://login.microsoftonline.us/tenant",
            "https://login.usgovcloudapi.net/tenant/oauth2/token",
            "token_endpoint")

    def test_ciam_authority_is_treated_as_public(self):
        # CIAM authorities resolve to Public, so a Public-cloud endpoint
        # passes and a cross-cloud one fails.
        _ensure_endpoint_same_cloud_as_authority(
            "https://contoso.ciamlogin.com/contoso.onmicrosoft.com",
            "https://login.microsoftonline.com/contoso/oauth2/token",
            "token_endpoint")
        with self.assertRaises(ValueError):
            _ensure_endpoint_same_cloud_as_authority(
                "https://contoso.ciamlogin.com/contoso.onmicrosoft.com",
                "https://login.partner.microsoftonline.cn/contoso/oauth2/token",
                "token_endpoint")

    def test_endpoint_name_appears_in_error_message(self):
        # Any endpoint_name string the caller supplies is surfaced verbatim,
        # so callers can attribute a failure to the specific OIDC field.
        for name in (
                "token_endpoint",
                "authorization_endpoint",
                "device_authorization_endpoint"):
            with self.assertRaises(ValueError) as ctx:
                _ensure_endpoint_same_cloud_as_authority(
                    "https://login.microsoftonline.com/tenant",
                    "https://login.partner.microsoftonline.cn/tenant/x",
                    name)
            self.assertIn(name, str(ctx.exception))

    def test_error_message_does_not_leak_authority_url_format(self):
        """The error message lists exactly: authority URL, endpoint name,
        offending endpoint URL. No tokens or secrets."""
        try:
            _ensure_endpoint_same_cloud_as_authority(
                "https://login.microsoftonline.com/tenant",
                "https://login.microsoftonline.us/tenant/oauth2/token",
                "token_endpoint")
        except ValueError as e:
            msg = str(e)
            self.assertIn(_XCLOUD_ERROR_SUBSTRING, msg)
            self.assertIn("https://login.microsoftonline.com/tenant", msg)
            self.assertIn("https://login.microsoftonline.us/tenant/oauth2/token", msg)
            self.assertIn("token_endpoint", msg)


# --- (J) Behavior matrix: BEFORE vs AFTER ------------------------------------

class TestCrossCloudBehaviorMatrix(unittest.TestCase):
    """Table-driven BEFORE-vs-AFTER matrix; a future regression is visible
    at a glance. Categories: pass-pass (unchanged accept), throw-throw
    (unchanged reject), pass-throw (the cross-cloud fix)."""

    # Each row: (label, authority, issuer, expected, category)
    _MATRIX = [
        # --- pass-pass: unchanged accepts ---
        ("rule1_exact",
         "https://login.microsoftonline.com/tenant/v2.0",
         "https://login.microsoftonline.com/tenant/v2.0",
         True, "pass-pass"),
        ("rule2_same_cloud_alias",
         "https://login.microsoftonline.com/tenant",
         "https://login.windows.net/tenant",
         True, "pass-pass"),
        ("rule3_regional_same_cloud",
         "https://login.microsoftonline.com/tenant",
         "https://westus2.login.microsoft.com/tenant",
         True, "pass-pass"),
        ("rule2a_5927_federation_public",
         "https://clientlogin.test.parentpay.com/tid/v2.0",
         "https://login.microsoftonline.com/tid/v2.0",
         True, "pass-pass"),
        ("rule2a_5927_federation_us_gov",
         "https://customlogin.example.com/tenant",
         "https://login.microsoftonline.us/tenant",
         True, "pass-pass"),
        ("ciam_path_segment_match",
         "https://customdomain.com/contoso",
         "https://contoso.ciamlogin.com/contoso/v2.0",
         True, "pass-pass"),
        # --- pass-throw: the cross-cloud fix ---
        ("xcloud_public_authority_china_issuer",
         "https://login.microsoftonline.com/tenant",
         "https://login.partner.microsoftonline.cn/tenant",
         False, "pass-throw"),
        ("xcloud_public_authority_us_gov_issuer",
         "https://login.microsoftonline.com/tenant",
         "https://login.microsoftonline.us/tenant",
         False, "pass-throw"),
        ("xcloud_us_gov_authority_public_issuer",
         "https://login.microsoftonline.us/tenant",
         "https://login.microsoftonline.com/tenant",
         False, "pass-throw"),
        ("xcloud_china_authority_public_issuer",
         "https://login.partner.microsoftonline.cn/tenant",
         "https://login.microsoftonline.com/tenant",
         False, "pass-throw"),
        ("xcloud_public_authority_regional_china_issuer",
         "https://login.microsoftonline.com/tenant",
         "https://chinaeast2.login.chinacloudapi.cn/tenant",
         False, "pass-throw"),
        ("xcloud_us_gov_authority_regional_public_issuer",
         "https://login.microsoftonline.us/tenant",
         "https://westus2.login.microsoft.com/tenant",
         False, "pass-throw"),
        ("xcloud_bleu_authority_delos_issuer",
         "https://login.sovcloud-identity.fr/tenant",
         "https://login.sovcloud-identity.de/tenant",
         False, "pass-throw"),
        ("xcloud_china_authority_us_gov_issuer",
         "https://login.partner.microsoftonline.cn/tenant",
         "https://login.microsoftonline.us/tenant",
         False, "pass-throw"),
        ("ciam_tenant_mismatch",
         "https://customdomain.com/tenantA",
         "https://tenantB.ciamlogin.com/tenantB/v2.0",
         False, "pass-throw"),
        # --- throw-throw: unchanged rejects ---
        ("http_scheme_under_known_authority",
         "https://login.microsoftonline.com/tenant",
         "http://login.microsoftonline.com/tenant",
         False, "throw-throw"),
        ("custom_authority_unknown_issuer",
         "https://customdomain.com/tenant",
         "https://malicious-site.com/tenant",
         False, "throw-throw"),
        ("multi_label_regional_lookalike",
         "https://customlogin.example.com/tenant",
         "https://attacker.evil.login.microsoft.com/tenant",
         False, "throw-throw"),
    ]

    @patch("msal.authority.tenant_discovery")
    def test_matrix(self, tenant_discovery_mock):
        for label, authority_url, issuer, expected_pass, category in self._MATRIX:
            with self.subTest(label=label, category=category):
                # Use endpoints that match the authority host so the
                # cross-cloud endpoint check is not what trips the matrix.
                authority_host = urlparse(authority_url).hostname
                tenant_discovery_mock.return_value = {
                    "issuer": issuer,
                    "authorization_endpoint":
                        "https://{}/oauth2/v2.0/authorize".format(authority_host),
                    "token_endpoint":
                        "https://{}/oauth2/v2.0/token".format(authority_host),
                }
                if expected_pass:
                    a = Authority(None, MinimalHttpClient(),
                                  oidc_authority_url=authority_url)
                    self.assertTrue(
                        a.has_valid_issuer(),
                        "{}: expected accept".format(label))
                else:
                    with self.assertRaises(ValueError, msg=label):
                        Authority(None, MinimalHttpClient(),
                                  oidc_authority_url=authority_url)
