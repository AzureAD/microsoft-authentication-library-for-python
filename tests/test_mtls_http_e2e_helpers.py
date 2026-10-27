"""Offline custom-recorder guards, not evidence of live E2E acquisitions."""

import unittest
from unittest.mock import Mock, patch

import pytest
import requests

from tests import test_e2e
from tests.mtls_http_e2e_helpers import RecordingPolicyHttpClient
from tests.test_mtls_e2e_helpers import CertificateFixture, _ENDPOINT, _jwt


class TestCustomMtlsE2eHelpers(CertificateFixture):
    def setUp(self):
        self.scenario = test_e2e.CertificateScenariosTestCase(
            "test_credential_x509_custom_http_output_pop")

    def _assert_private_traceback(self, failure, *secrets):
        rendered = str(failure.getrepr(
            style="long", showlocals=False, funcargs=True))
        self.assertIn(type(failure.value).__name__, rendered)
        self.assertFalse(
            "PRIVATE KEY" in rendered,
            "Rendered tracebacks must not expose private key material.")
        for secret in secrets:
            fragments = (secret,) + (
                (secret[:32], secret[-32:]) if len(secret) > 64 else ())
            for fragment in fragments:
                self.assertFalse(
                    fragment in rendered,
                    "Sensitive arguments must not appear in rendered tracebacks.")

    def test_custom_capture_failure_traceback_hides_the_expected_assertion(self):
        assertion = _jwt({"aud": "api://AzureADTokenExchange"})
        app = Mock()
        with RecordingPolicyHttpClient(
                expected_binding=self.binding, expected_assertion=assertion) as client:
            app.acquire_token_for_client.return_value = {"access_token": assertion}
            with pytest.raises(AssertionError) as failure:
                self.scenario._acquire_recorded(
                    app, self.scenario._MTLS_POP_SCOPE, self.binding,
                    pop=True, http_client=client, expected_assertion=assertion)
            self._assert_private_traceback(failure, assertion)

    def test_custom_recorder_failure_traceback_hides_posted_credentials(self):
        form = self._signed_form()

        def send(adapter, request, **kwargs):
            response = self._response(payload=[])
            response.request = request
            return response

        with RecordingPolicyHttpClient(expected_binding=self.binding) as owner:
            child = owner.create_mtls_client(self.binding)
            with patch.object(requests.adapters.HTTPAdapter, "send",
                              autospec=True, side_effect=send):
                with pytest.raises(AssertionError) as failure:
                    child.post(_ENDPOINT, data=form)
            self._assert_private_traceback(
                failure, form["client_assertion"], form["refresh_token"])

    def test_recorder_setup_failure_traceback_hides_the_expected_assertion(self):
        assertion = _jwt({"aud": "api://AzureADTokenExchange"})
        with pytest.raises(TypeError) as failure:
            RecordingPolicyHttpClient(
                expected_assertion=assertion, unexpected_option=True)
        self._assert_private_traceback(failure, assertion)

    def test_custom_recorder_retains_sample_pipeline_and_owner_cleanup(self):
        def send(adapter, request, **kwargs):
            response = self._response()
            response.request = request
            return response

        with RecordingPolicyHttpClient(expected_binding=self.binding) as owner:
            child = owner.create_mtls_client(self.binding)
            with patch.object(requests.adapters.HTTPAdapter, "send",
                              autospec=True, side_effect=send):
                child.post(_ENDPOINT, data=self._signed_form())
            self.assertEqual(1, child.response_count)
            self.assertEqual(1, len(child.token_requests))
            self.assertEqual(
                "custom-policy", child.token_requests[0]["middleware"])
            self.assertEqual(
                "_MtlsHTTPAdapter", child.token_requests[0]["tls_adapter"])
            self.assertEqual([], owner.token_requests)
            self.assertFalse(owner._closed or child._closed)
        self.assertTrue(owner._closed and child._closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            child.post(_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
