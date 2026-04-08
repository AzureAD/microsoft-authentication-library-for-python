# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""Tests for msal-key-attestation package."""

import json
import time
import unittest
from unittest.mock import patch, MagicMock

from msal_key_attestation.attestation import (
    _cache_clear,
    _cache_lookup,
    _cache_store,
    _CacheKey,
    _try_extract_exp_iat,
    create_attestation_provider,
)


class TestMaaTokenCache(unittest.TestCase):
    def setUp(self):
        _cache_clear()

    def tearDown(self):
        _cache_clear()

    def _make_jwt(self, exp=None, iat=None) -> str:
        """Create a minimal JWT with exp/iat claims."""
        import base64
        header = base64.urlsafe_b64encode(
            b'{"alg":"none"}').rstrip(b"=").decode("ascii")
        payload_obj = {}
        if exp is not None:
            payload_obj["exp"] = exp
        if iat is not None:
            payload_obj["iat"] = iat
        payload = base64.urlsafe_b64encode(
            json.dumps(payload_obj).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        return f"{header}.{payload}.sig"

    def test_extract_exp_iat(self):
        now = int(time.time())
        jwt = self._make_jwt(exp=now + 3600, iat=now)
        exp, iat = _try_extract_exp_iat(jwt)
        self.assertEqual(exp, now + 3600)
        self.assertEqual(iat, now)

    def test_extract_no_exp(self):
        jwt = self._make_jwt()
        exp, iat = _try_extract_exp_iat(jwt)
        self.assertIsNone(exp)
        self.assertIsNone(iat)

    def test_extract_malformed_jwt(self):
        exp, iat = _try_extract_exp_iat("not.a.jwt.at.all")
        self.assertIsNone(exp)

    def test_cache_stores_and_retrieves(self):
        now = int(time.time())
        jwt = self._make_jwt(exp=now + 3600, iat=now)
        key = _CacheKey("ep", "cid", "ck", "", "{}")
        _cache_store(key, jwt)
        self.assertEqual(_cache_lookup(key), jwt)

    def test_cache_miss(self):
        key = _CacheKey("ep", "cid", "ck", "", "{}")
        self.assertIsNone(_cache_lookup(key))

    def test_cache_expired_not_returned(self):
        now = int(time.time())
        jwt = self._make_jwt(exp=now - 100, iat=now - 200)
        key = _CacheKey("ep", "cid", "ck", "", "{}")
        _cache_store(key, jwt)
        # Token already expired, should not be cached
        self.assertIsNone(_cache_lookup(key))

    def test_cache_no_exp_not_cached(self):
        jwt = self._make_jwt()
        key = _CacheKey("ep", "cid", "ck", "", "{}")
        _cache_store(key, jwt)
        self.assertIsNone(_cache_lookup(key))

    @patch.dict("os.environ", {"MSAL_MSI_V2_ATTESTATION_CACHE": "0"})
    def test_cache_disabled_by_env(self):
        now = int(time.time())
        jwt = self._make_jwt(exp=now + 3600, iat=now)
        key = _CacheKey("ep", "cid", "ck", "", "{}")
        _cache_store(key, jwt)
        self.assertIsNone(_cache_lookup(key))

    def test_cache_clear(self):
        now = int(time.time())
        jwt = self._make_jwt(exp=now + 3600, iat=now)
        key = _CacheKey("ep", "cid", "ck", "", "{}")
        _cache_store(key, jwt)
        _cache_clear()
        self.assertIsNone(_cache_lookup(key))


class TestCreateAttestationProvider(unittest.TestCase):
    def test_returns_callable(self):
        provider = create_attestation_provider()
        self.assertTrue(callable(provider))

    @patch("msal_key_attestation.attestation.get_attestation_jwt")
    def test_provider_calls_get_attestation_jwt(self, mock_get):
        mock_get.return_value = "fake.attestation.jwt"
        provider = create_attestation_provider()
        result = provider(
            "https://attest.example.com", 12345, "client-id", "my-key-name")
        self.assertEqual(result, "fake.attestation.jwt")
        mock_get.assert_called_once_with(
            attestation_endpoint="https://attest.example.com",
            client_id="client-id",
            key_handle=12345,
            cache_key="my-key-name",
        )

    @patch("msal_key_attestation.attestation.get_attestation_jwt")
    def test_provider_forwards_empty_cache_key_as_none(self, mock_get):
        mock_get.return_value = "fake.jwt"
        provider = create_attestation_provider()
        provider("https://ep", 1, "cid", "")
        mock_get.assert_called_once_with(
            attestation_endpoint="https://ep",
            client_id="cid",
            key_handle=1,
            cache_key=None,
        )


class TestGetAttestationJwtValidation(unittest.TestCase):
    def test_empty_endpoint_raises(self):
        from msal_key_attestation.attestation import get_attestation_jwt
        with self.assertRaises(ValueError):
            get_attestation_jwt(
                attestation_endpoint="",
                client_id="c",
                key_handle=1)

    def test_empty_client_id_raises(self):
        from msal_key_attestation.attestation import get_attestation_jwt
        with self.assertRaises(ValueError):
            get_attestation_jwt(
                attestation_endpoint="https://ep",
                client_id="",
                key_handle=1)

    def test_zero_key_handle_raises(self):
        from msal_key_attestation.attestation import get_attestation_jwt
        with self.assertRaises(ValueError):
            get_attestation_jwt(
                attestation_endpoint="https://ep",
                client_id="c",
                key_handle=0)


if __name__ == "__main__":
    unittest.main()
