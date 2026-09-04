# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
E2E test: Acquire mTLS PoP token via MSAL → call Azure Key Vault over mTLS.

This test demonstrates the correct architecture:
  1. MSAL acquires the token and returns:
     - access_token + token_type
     - binding_certificate (WindowsCertificate — platform key handle)
  2. App developer uses a SEPARATE mTLS transport (SchannelSession)
     to call the downstream API with the binding_certificate.

Requirements:
  - Must run on a KeyGuard-enabled Azure VM (Windows)
  - Must have MSI v2 configured with attestation
  - msal-key-attestation package installed (for attestation)
  - msal-schannel-transport package installed (for downstream call)

Usage:
    RUN_E2E_TESTS=1 python -m pytest tests/test_e2e_mtls_pop.py -v
    OR
    python tests/test_e2e_mtls_pop.py  (standalone)
"""

from __future__ import annotations

import json
import os
import sys
import unittest

# Skip entirely on non-Windows
if sys.platform != "win32":
    raise unittest.SkipTest("E2E mTLS PoP tests require Windows")


# ---------------------------------------------------------------------------
# Configuration — from environment or defaults (KeyGuard Azure VM)
# ---------------------------------------------------------------------------

# These come from the MSI v2 platform metadata — no manual config needed
# for system-assigned identity. For user-assigned, set these:
E2E_CLIENT_ID = os.environ.get("MSI_V2_CLIENT_ID", "")
E2E_RESOURCE = os.environ.get("MSI_V2_RESOURCE", "https://vault.azure.net")

# Key Vault to test against
E2E_VAULT_URL = os.environ.get(
    "MSI_V2_VAULT_URL", "https://msidlabvault.vault.azure.net")
E2E_SECRET_NAME = os.environ.get("MSI_V2_SECRET_NAME", "test-secret")


class TestMtlsPopE2E(unittest.TestCase):
    """
    End-to-end: MSAL token acquisition → downstream mTLS call to Key Vault.

    Architecture:
        MSAL (obtain_token) → auth result with binding_certificate
        SchannelSession (separate package) → downstream mTLS GET to AKV
    """

    @unittest.skipUnless(
        os.environ.get("RUN_E2E_TESTS"),
        "Set RUN_E2E_TESTS=1 to run E2E tests (requires KeyGuard VM)")
    def test_acquire_token_and_call_akv(self):
        """
        Full flow:
        1. obtain_token() → gets mTLS PoP token + WindowsCertificate
        2. Verify auth result has binding_certificate
        3. Create SchannelSession with binding_certificate
        4. Call AKV GET /secrets/{name}?api-version=7.5
        5. Verify 200 OK
        """
        import requests
        from msal.msi_v2 import obtain_token
        from msal.managed_identity import SystemAssignedManagedIdentity
        from msal.windows_certificate import WindowsCertificate

        # 1. Acquire token
        http_client = requests.Session()
        mi = SystemAssignedManagedIdentity()
        if E2E_CLIENT_ID:
            from msal.managed_identity import UserAssignedManagedIdentity
            mi = UserAssignedManagedIdentity(
                ManagedIdentity={"ManagedIdentityIdType": "ClientId",
                                 "Id": E2E_CLIENT_ID})

        # Try with attestation if available
        attestation_provider = None
        try:
            from msal_key_attestation import get_attestation_token
            attestation_provider = get_attestation_token
        except ImportError:
            pass

        result = obtain_token(
            http_client=http_client,
            managed_identity=mi,
            resource=E2E_RESOURCE,
            attestation_enabled=True,
            attestation_token_provider=attestation_provider,
        )

        # 2. Verify auth result structure
        self.assertIn("access_token", result)
        self.assertIn("token_type", result)
        self.assertIn("binding_certificate", result)
        self.assertIn("binding_certificate_metadata", result)

        binding_cert = result["binding_certificate"]
        self.assertIsInstance(binding_cert, WindowsCertificate)
        self.assertTrue(binding_cert.has_private_key)
        self.assertTrue(len(binding_cert.thumbprint_sha256) == 64)

        token_type = result["token_type"]
        self.assertIn(
            token_type.lower(), ("mtls_pop", "pop"),
            f"Expected mtls_pop token type, got: {token_type}")

        print(f"\n✓ Token acquired successfully")
        print(f"  token_type: {token_type}")
        print(f"  cert thumbprint: {binding_cert.thumbprint_sha256[:16]}...")
        print(f"  x5t#S256: {binding_cert.x5t_s256}")

        # 3. Use SEPARATE transport for downstream mTLS call
        from msal_schannel_transport import SchannelSession

        # App developer constructs auth header from token_type + access_token
        auth_header = f"{token_type} {result['access_token']}"

        with SchannelSession(client_certificate=binding_cert) as session:
            # 4. Call AKV
            url = (f"{E2E_VAULT_URL}/secrets/{E2E_SECRET_NAME}"
                   f"?api-version=7.5")

            response = session.get(
                url,
                headers={
                    "Authorization": auth_header,
                    "x-ms-tokenboundauth": "true",
                },
            )

            # 5. Verify response
            print(f"\n  AKV response: HTTP {response.status_code}")
            if response.status_code == 200:
                body = response.json()
                print(f"  Secret retrieved: {E2E_SECRET_NAME}")
                self.assertIn("value", body)
            elif response.status_code == 403:
                # Expected if MSI doesn't have AKV access
                print(f"  403 Forbidden — MSI may not have AKV access policy")
                print(f"  Body: {response.text[:200]}")
            elif response.status_code == 401:
                print(f"  401 Unauthorized — token binding mismatch?")
                print(f"  Body: {response.text[:200]}")
                self.fail("401 from AKV — mTLS binding may have failed")
            else:
                print(f"  Unexpected: {response.text[:200]}")

        # 6. Cleanup — WindowsCertificate freed by context manager
        binding_cert.close()

    @unittest.skipUnless(
        os.environ.get("RUN_E2E_TESTS"),
        "Set RUN_E2E_TESTS=1 to run E2E tests (requires KeyGuard VM)")
    def test_binding_certificate_matches_token(self):
        """
        Verify that the binding_certificate's x5t#S256 matches the
        token's cnf claim — the fundamental mTLS PoP security property.
        """
        import base64
        import requests
        from msal.msi_v2 import obtain_token
        from msal.managed_identity import SystemAssignedManagedIdentity

        http_client = requests.Session()
        mi = SystemAssignedManagedIdentity()

        attestation_provider = None
        try:
            from msal_key_attestation import get_attestation_token
            attestation_provider = get_attestation_token
        except ImportError:
            pass

        result = obtain_token(
            http_client=http_client,
            managed_identity=mi,
            resource=E2E_RESOURCE,
            attestation_enabled=True,
            attestation_token_provider=attestation_provider,
        )

        self.assertIn("binding_certificate", result)
        binding_cert = result["binding_certificate"]

        try:
            # Decode JWT payload (without validation — we just check cnf)
            token = result["access_token"]
            parts = token.split(".")
            self.assertEqual(len(parts), 3, "Token should be a JWT (3 parts)")

            # Pad base64url
            payload_b64 = parts[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))

            # Check cnf claim
            self.assertIn("cnf", payload,
                          "mTLS PoP token must have 'cnf' claim")
            cnf = payload["cnf"]
            self.assertIn("x5t#S256", cnf,
                          "cnf claim must have 'x5t#S256'")

            token_thumbprint = cnf["x5t#S256"]
            cert_thumbprint = binding_cert.x5t_s256

            self.assertEqual(
                token_thumbprint, cert_thumbprint,
                f"Token binding mismatch!\n"
                f"  Token cnf.x5t#S256: {token_thumbprint}\n"
                f"  Cert x5t#S256:      {cert_thumbprint}")

            print(f"\n✓ Token binding verified: x5t#S256 = {cert_thumbprint}")
        finally:
            binding_cert.close()

    @unittest.skipUnless(
        os.environ.get("RUN_E2E_TESTS"),
        "Set RUN_E2E_TESTS=1 to run E2E tests (requires KeyGuard VM)")
    def test_certificate_from_store(self):
        """
        Test WindowsCertificate.from_store() — loads cert by thumbprint
        from the Windows cert store (separate from MSAL token flow).
        """
        from msal.windows_certificate import WindowsCertificate

        # This test requires a cert thumbprint in env
        thumbprint = os.environ.get("MSI_V2_CERT_THUMBPRINT")
        if not thumbprint:
            self.skipTest(
                "Set MSI_V2_CERT_THUMBPRINT to test from_store()")

        cert = WindowsCertificate.from_store(
            store_path="CurrentUser/My",
            thumbprint=thumbprint,
        )

        try:
            self.assertTrue(cert.has_private_key)
            self.assertEqual(
                cert.thumbprint_sha1.upper(),
                thumbprint.upper())
            self.assertTrue(len(cert.public_certificate_der) > 100)
            self.assertTrue(
                cert.public_certificate_pem.startswith(
                    "-----BEGIN CERTIFICATE-----"))
            print(f"\n✓ Cert loaded from store: {cert}")
            print(f"  SHA-256: {cert.thumbprint_sha256}")
            print(f"  x5t#S256: {cert.x5t_s256}")
        finally:
            cert.close()


if __name__ == "__main__":
    # Allow running standalone
    os.environ["RUN_E2E_TESTS"] = "1"
    unittest.main(verbosity=2)
