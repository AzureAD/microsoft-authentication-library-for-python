from unittest import TestCase

from msal.crypto import _generate_rsa_key, _convert_rsa_keys


class CryptoTestCase(TestCase):
    def test_key_generation(self):
        key = _generate_rsa_key()
        _, jwk = _convert_rsa_keys(key)
        self.assertEqual(jwk.get("kty"), "RSA")
        self.assertIsNotNone(jwk.get("n") and jwk.get("e"))

