import unittest
from unittest.mock import Mock, patch
from msal.application import ConfidentialClientApplication


@patch('msal.application.Authority')
@patch('msal.application.JwtAssertionCreator', new_callable=lambda: Mock(
    return_value=Mock(create_regenerative_assertion=Mock(return_value="mock_jwt_assertion"))))
class TestClientCredentialWithOptionalThumbprint(unittest.TestCase):
    """Test that thumbprint is optional when public_certificate is provided"""

    # Sample test certificate and private key (PEM format)
    # These are minimal valid PEM structures for testing
    test_private_key = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj
MzEfYyjiWA4R4/M2bS1+fWIcPm15j7uo6xKvRr4PNx5bKMDFqMdW6/xfqFWX0nZK
-----END PRIVATE KEY-----"""

    test_certificate = """-----BEGIN CERTIFICATE-----
MIIC5jCCAc6gAwIBAgIJALdYQVsVsNZHMA0GCSqGSIb3DQEBCwUAMBYxFDASBgNV
BAMMC0V4YW1wbGUgQ0EwHhcNMjQwMTAxMDAwMDAwWhcNMjUwMTAxMDAwMDAwWjAW
-----END CERTIFICATE-----"""

    # Test thumbprint values
    test_sha1_thumbprint = "A1B2C3D4E5F6"
    test_sha256_thumbprint = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2"


    def _setup_mocks(self, mock_authority_class, authority="https://login.microsoftonline.com/common"):
        """Helper to setup Authority mock"""
        # Setup Authority mock
        mock_authority = Mock()
        mock_authority.is_adfs = "adfs" in authority.lower()

        # Extract instance from authority URL
        if mock_authority.is_adfs:
            # For ADFS: https://adfs.contoso.com/adfs -> adfs.contoso.com
            mock_authority.instance = authority.split("//")[1].split("/")[0]
            mock_authority.token_endpoint = f"https://{mock_authority.instance}/adfs/oauth2/token"
            mock_authority.authorization_endpoint = f"https://{mock_authority.instance}/adfs/oauth2/authorize"
        else:
            # For AAD: https://login.microsoftonline.com/common -> login.microsoftonline.com
            mock_authority.instance = authority.split("//")[1].split("/")[0]
            mock_authority.token_endpoint = f"https://{mock_authority.instance}/common/oauth2/v2.0/token"
            mock_authority.authorization_endpoint = f"https://{mock_authority.instance}/common/oauth2/v2.0/authorize"

        mock_authority.device_authorization_endpoint = None
        mock_authority_class.return_value = mock_authority

        return mock_authority

    def _setup_certificate_mocks(self, mock_extract, mock_load_cert):
        """Helper to setup certificate parsing mocks"""
        # Mock certificate loading
        mock_cert = Mock()
        mock_load_cert.return_value = mock_cert

        # Mock _extract_cert_and_thumbprints to return thumbprints
        mock_extract.return_value = (
            "mock_sha256_thumbprint",  # sha256_thumbprint
            "mock_sha1_thumbprint",     # sha1_thumbprint
            ["mock_x5c_value"]          # x5c
        )

    def _verify_assertion_params(self, mock_jwt_creator_class, expected_algorithm,
                                  expected_thumbprint_type, expected_thumbprint_value=None,
                                  has_x5c=False):
        """Helper to verify JwtAssertionCreator was called with correct params"""
        mock_jwt_creator_class.assert_called_once()
        call_args = mock_jwt_creator_class.call_args

        # Verify algorithm
        self.assertEqual(call_args[1]['algorithm'], expected_algorithm)

        # Verify thumbprint type
        if expected_thumbprint_type == 'sha256':
            self.assertIn('sha256_thumbprint', call_args[1])
            self.assertNotIn('sha1_thumbprint', call_args[1])
            if expected_thumbprint_value:
                self.assertEqual(call_args[1]['sha256_thumbprint'], expected_thumbprint_value)
        elif expected_thumbprint_type == 'sha1':
            self.assertIn('sha1_thumbprint', call_args[1])
            self.assertNotIn('sha256_thumbprint', call_args[1])
            if expected_thumbprint_value:
                self.assertEqual(call_args[1]['sha1_thumbprint'], expected_thumbprint_value)

        # Verify x5c header if expected
        if has_x5c:
            self.assertIn('headers', call_args[1])
            self.assertIn('x5c', call_args[1]['headers'])

        return call_args

    @patch('cryptography.x509.load_pem_x509_certificate')
    @patch('msal.application._extract_cert_and_thumbprints')
    def test_pem_with_certificate_only_uses_sha256(
            self, mock_extract, mock_load_cert, mock_jwt_creator_class, mock_authority_class):
        """Test that providing only public_certificate (no thumbprint) uses SHA-256"""
        authority = "https://login.microsoftonline.com/common"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_oidc = False  # AAD is not OIDC generic
        self._setup_certificate_mocks(mock_extract, mock_load_cert)

        # Create app with certificate credential WITHOUT thumbprint
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "public_certificate": self.test_certificate,
                # Note: NO thumbprint provided
            },
            authority=authority
        )

        # Verify SHA-256 with PS256 algorithm is used
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            has_x5c=True
        )

    def test_pem_with_manual_thumbprint_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that providing manual thumbprint (no certificate) uses SHA-1"""
        authority = "https://login.microsoftonline.com/common"
        self._setup_mocks(mock_authority_class, authority)

        # Create app with manual thumbprint (legacy approach)
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                # Note: NO public_certificate provided
            },
            authority=authority
        )

        # Verify SHA-1 with RS256 algorithm is used
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_both_uses_manual_thumbprint_as_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that providing both thumbprint and certificate prefers manual thumbprint (SHA-1)"""
        authority = "https://login.microsoftonline.com/common"
        self._setup_mocks(mock_authority_class, authority)

        # Create app with BOTH thumbprint and certificate
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "public_certificate": self.test_certificate,
            },
            authority=authority
        )

        # Verify manual thumbprint takes precedence (backward compatibility)
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint,
            has_x5c=True  # x5c should still be present
        )

    @patch('cryptography.x509.load_pem_x509_certificate')
    @patch('msal.application._extract_cert_and_thumbprints')
    def test_pem_with_adfs_uses_sha1(
            self, mock_extract, mock_load_cert, mock_jwt_creator_class, mock_authority_class):
        """Test that ADFS authority uses SHA-1 even with SHA-256 thumbprint"""
        authority = "https://adfs.contoso.com/adfs"
        self._setup_mocks(mock_authority_class, authority)
        self._setup_certificate_mocks(mock_extract, mock_load_cert)

        # Create app with certificate on ADFS
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "public_certificate": self.test_certificate,
            },
            authority=authority
        )

        # ADFS should force SHA-1 with RS256 even though SHA-256 would be calculated
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1'
        )

    def test_pem_with_neither_raises_error(self, mock_jwt_creator_class, mock_authority_class):
        """Test that providing neither thumbprint nor certificate raises ValueError"""
        authority = "https://login.microsoftonline.com/common"
        self._setup_mocks(mock_authority_class, authority)

        # Should raise ValueError when neither thumbprint nor certificate provided
        with self.assertRaises(ValueError) as context:
            app = ConfidentialClientApplication(
                client_id="my_client_id",
                client_credential={
                    "private_key": self.test_private_key,
                    # Note: NO thumbprint and NO public_certificate
                },
                authority=authority
            )

        self.assertIn("thumbprint", str(context.exception).lower())
        self.assertIn("public_certificate", str(context.exception).lower())

    def test_pem_with_thumbprint_sha256_only_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that providing only thumbprint_sha256 uses SHA-256"""
        authority = "https://login.microsoftonline.com/common"
        self._setup_mocks(mock_authority_class, authority)

        # Create app with only SHA256 thumbprint
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # Verify SHA-256 with PS256 algorithm is used
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_both_thumbprints_aad_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, AAD authority uses SHA-256"""
        authority = "https://login.microsoftonline.com/common"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_oidc = False  # AAD is not OIDC generic

        # Create app with BOTH thumbprints for AAD
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For AAD, should use SHA-256 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_both_thumbprints_adfs_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, ADFS authority uses SHA-1"""
        authority = "https://adfs.contoso.com/adfs"
        self._setup_mocks(mock_authority_class, authority)

        # Create app with BOTH thumbprints for ADFS
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For ADFS, should use SHA-1 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_both_thumbprints_b2c_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, B2C authority uses SHA-256"""
        authority = "https://contoso.b2clogin.com/contoso.onmicrosoft.com/B2C_1_susi"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_b2c = True  # Manually set _is_b2c to True for this B2C authority
        mock_authority._is_oidc = False  # B2C is not OIDC generic

        # Create app with BOTH thumbprints for B2C
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For B2C, should use SHA-256 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_both_thumbprints_ciam_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, CIAM authority uses SHA-256"""
        authority = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_b2c = True  # CIAM sets _is_b2c to True
        mock_authority._is_oidc = False  # CIAM is not OIDC generic

        # Create app with BOTH thumbprints for CIAM
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For CIAM, should use SHA-256 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_both_thumbprints_generic_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, OIDC generic authority uses SHA-1"""
        authority = "https://custom.oidc.authority.com/tenant"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        
        # Set up as an OIDC generic authority
        mock_authority.is_adfs = False
        mock_authority._is_b2c = True  # OIDC sets this but it's not truly B2C
        mock_authority._is_oidc = True  # This distinguishes OIDC from B2C/CIAM

        # Create app with BOTH thumbprints for OIDC generic authority
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For OIDC generic authorities, should use SHA-1 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_both_thumbprints_dsts_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, dSTS authority uses SHA-1"""
        authority = "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        
        # Set up as a dSTS authority (dSTS is treated as OIDC)
        mock_authority.is_adfs = False
        mock_authority._is_b2c = True  # OIDC sets this but it's not truly B2C
        mock_authority._is_oidc = True  # dSTS is treated as OIDC generic

        # Create app with BOTH thumbprints for dSTS authority
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For dSTS authorities, should use SHA-1 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_both_thumbprints_unknown_aad_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that with both thumbprints, unknown AAD authority (e.g., sovereign cloud) uses SHA-256"""
        authority = "https://login.microsoftonline.de/tenant"  # Example of sovereign cloud not in known list
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        
        # Set up as an AAD authority (not ADFS, not B2C, not OIDC)
        mock_authority.is_adfs = False
        mock_authority._is_b2c = False
        mock_authority._is_oidc = False

        # Create app with BOTH thumbprints for unknown AAD authority
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # For AAD authorities (even unknown ones), should use SHA-256 when both are provided
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    # Additional tests for SHA256-only with different authority types
    def test_pem_with_thumbprint_sha256_only_b2c_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that B2C with only SHA256 thumbprint uses SHA-256"""
        authority = "https://contoso.b2clogin.com/contoso.onmicrosoft.com/B2C_1_susi"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_b2c = True
        mock_authority._is_oidc = False

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_thumbprint_sha256_only_ciam_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that CIAM with only SHA256 thumbprint uses SHA-256"""
        authority = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_b2c = True
        mock_authority._is_oidc = False

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_thumbprint_sha256_only_adfs_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that ADFS with only SHA256 thumbprint uses SHA-256 (even though ADFS prefers SHA1)"""
        authority = "https://adfs.contoso.com/adfs"
        self._setup_mocks(mock_authority_class, authority)

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # When only SHA256 is provided, it should be used even for ADFS
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_thumbprint_sha256_only_oidc_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that OIDC generic with only SHA256 thumbprint uses SHA-256"""
        authority = "https://custom.oidc.authority.com/tenant"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority.is_adfs = False
        mock_authority._is_b2c = True
        mock_authority._is_oidc = True

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # When only SHA256 is provided, it should be used even for OIDC
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    def test_pem_with_thumbprint_sha256_only_dsts_uses_sha256(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that dSTS with only SHA256 thumbprint uses SHA-256"""
        authority = "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority.is_adfs = False
        mock_authority._is_b2c = True
        mock_authority._is_oidc = True

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint_sha256": self.test_sha256_thumbprint,
            },
            authority=authority
        )

        # When only SHA256 is provided, it should be used even for dSTS
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='PS256',
            expected_thumbprint_type='sha256',
            expected_thumbprint_value=self.test_sha256_thumbprint
        )

    # Tests for SHA1-only with different authority types
    def test_pem_with_thumbprint_sha1_only_b2c_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that B2C with only SHA1 thumbprint uses SHA-1"""
        authority = "https://contoso.b2clogin.com/contoso.onmicrosoft.com/B2C_1_susi"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_b2c = True
        mock_authority._is_oidc = False

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_thumbprint_sha1_only_ciam_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that CIAM with only SHA1 thumbprint uses SHA-1"""
        authority = "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority._is_b2c = True
        mock_authority._is_oidc = False

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_thumbprint_sha1_only_adfs_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that ADFS with only SHA1 thumbprint uses SHA-1"""
        authority = "https://adfs.contoso.com/adfs"
        self._setup_mocks(mock_authority_class, authority)

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_thumbprint_sha1_only_oidc_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that OIDC generic with only SHA1 thumbprint uses SHA-1"""
        authority = "https://custom.oidc.authority.com/tenant"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority.is_adfs = False
        mock_authority._is_b2c = True
        mock_authority._is_oidc = True

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )

    def test_pem_with_thumbprint_sha1_only_dsts_uses_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that dSTS with only SHA1 thumbprint uses SHA-1"""
        authority = "https://test-instance1-dsts.dsts.core.azure-test.net/dstsv2/common"
        mock_authority = self._setup_mocks(mock_authority_class, authority)
        mock_authority.is_adfs = False
        mock_authority._is_b2c = True
        mock_authority._is_oidc = True

        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": self.test_sha1_thumbprint,
            },
            authority=authority
        )

        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=self.test_sha1_thumbprint
        )


if __name__ == "__main__":
    unittest.main()
