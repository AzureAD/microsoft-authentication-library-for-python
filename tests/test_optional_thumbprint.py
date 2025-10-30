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
        self._setup_mocks(mock_authority_class, authority)
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
        manual_thumbprint = "A1B2C3D4E5F6"
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": manual_thumbprint,
                # Note: NO public_certificate provided
            },
            authority=authority
        )

        # Verify SHA-1 with RS256 algorithm is used
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=manual_thumbprint
        )

    def test_pem_with_both_uses_manual_thumbprint_as_sha1(
            self, mock_jwt_creator_class, mock_authority_class):
        """Test that providing both thumbprint and certificate prefers manual thumbprint (SHA-1)"""
        authority = "https://login.microsoftonline.com/common"
        self._setup_mocks(mock_authority_class, authority)

        # Create app with BOTH thumbprint and certificate
        manual_thumbprint = "A1B2C3D4E5F6"
        app = ConfidentialClientApplication(
            client_id="my_client_id",
            client_credential={
                "private_key": self.test_private_key,
                "thumbprint": manual_thumbprint,
                "public_certificate": self.test_certificate,
            },
            authority=authority
        )

        # Verify manual thumbprint takes precedence (backward compatibility)
        self._verify_assertion_params(
            mock_jwt_creator_class,
            expected_algorithm='RS256',
            expected_thumbprint_type='sha1',
            expected_thumbprint_value=manual_thumbprint,
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


if __name__ == "__main__":
    unittest.main()
