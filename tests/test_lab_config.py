import io
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.serialization import NoEncryption, pkcs12

from tests.lab_config import get_client_certificate
from tests import test_e2e


class TestRequiredCertificateConfiguration(unittest.TestCase):
    def test_missing_certificate_configuration_fails(self):
        for value in (None, "", "$(LAB_APP_CLIENT_CERT_PFX_PATH)"):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["LAB_APP_CLIENT_CERT_PFX_PATH"] = value
                with self.assertRaisesRegex(
                        EnvironmentError, "LAB_APP_CLIENT_CERT_PFX_PATH"):
                    get_client_certificate()

    def test_missing_certificate_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing.pfx")
            with patch.dict(os.environ, {"LAB_APP_CLIENT_CERT_PFX_PATH": path}):
                with self.assertRaises(FileNotFoundError):
                    get_client_certificate()

    def test_invalid_certificate_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "invalid.pfx")
            with open(path, "wb") as cert_file:
                cert_file.write(b"not a PFX")
            with patch.dict(os.environ, {"LAB_APP_CLIENT_CERT_PFX_PATH": path}):
                with self.assertRaises(ValueError):
                    get_client_certificate()

    def test_valid_certificate_configuration_is_preserved(self):
        fixture = os.path.join(
            os.path.dirname(__file__), "certificate-with-password.pfx")
        with open(fixture, "rb") as cert_file:
            key, certificate, chain = pkcs12.load_key_and_certificates(
                cert_file.read(), b"password")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "valid.pfx")
            with open(path, "wb") as cert_file:
                cert_file.write(pkcs12.serialize_key_and_certificates(
                    b"test", key, certificate, chain, NoEncryption()))
            with patch.dict(os.environ, {"LAB_APP_CLIENT_CERT_PFX_PATH": path}):
                self.assertEqual({
                    "private_key_pfx_path": path,
                    "public_certificate": True,
                }, get_client_certificate())

    def test_required_scenarios_fail_without_credentials_instead_of_skipping(self):
        with patch.dict(os.environ, {"TRAVIS_TAG": "test-release"}, clear=True), patch.object(
                test_e2e.E2eTestCase, "__unittest_skip__", True, create=True), patch(
                "tests.test_e2e.msal.ConfidentialClientApplication") as application:
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(
                test_e2e.CertificateScenariosTestCase)
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertEqual(3, result.testsRun)
        self.assertEqual(3, len(result.errors))
        self.assertEqual([], result.skipped)
        self.assertEqual([], result.failures)
        for _, traceback in result.errors:
            self.assertIn("LAB_APP_CLIENT_CERT_PFX_PATH", traceback)
        application.assert_not_called()

    def test_required_scenarios_override_legacy_release_skip(self):
        self.assertIn(
            "__unittest_skip__", test_e2e.CertificateScenariosTestCase.__dict__)
        self.assertFalse(test_e2e.CertificateScenariosTestCase.__unittest_skip__)
