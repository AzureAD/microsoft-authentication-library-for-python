"""Offline custom-recorder guards, not evidence of live E2E acquisitions."""

import socket
import unittest
from unittest.mock import Mock, patch

import pytest
import requests
import urllib3

from tests import test_e2e
from tests.mtls_http_e2e_helpers import RecordingPolicyHttpClient
from tests.test_mtls_e2e_helpers import CertificateFixture, _ENDPOINT, _jwt


class TestCustomMtlsE2eHelpers(CertificateFixture):
    def setUp(self):
        self.scenario = test_e2e.CertificateScenariosTestCase(
            "test_credential_x509_custom_http_output_pop")

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

    def test_custom_dns_failure_traceback_hides_posted_credentials(self):
        form = self._signed_form()
        form["assertion"] = _jwt({"sub": "unit-user-assertion-secret"})
        form["refresh_token"] = "unit-refresh-secret-" * 8
        secrets = tuple(form[key] for key in (
            "client_assertion", "assertion", "refresh_token"))
        for mtls in (False, True):
            with self.subTest(mtls=mtls), RecordingPolicyHttpClient(
                    expected_binding=self.binding, trust_env=False) as owner:
                client = owner.create_mtls_client(self.binding) if mtls else owner
                with patch("socket.getaddrinfo", side_effect=socket.gaierror(
                        socket.EAI_NONAME, "offline DNS resolution failure")) as resolve:
                    with pytest.raises((
                            AssertionError, requests.exceptions.RequestException)) as failure:
                        client.post(_ENDPOINT, data=form)
                self._assert_private_traceback(failure, *secrets)
                self.assertIsInstance(failure.value, AssertionError)
                self.assertEqual(
                    "The custom token request transport failed before receiving a response.",
                    str(failure.value))
                original = failure.value.__context__
                self.assertIsInstance(original, requests.exceptions.ConnectionError)
                self.assertIsInstance(original.args[0], urllib3.exceptions.MaxRetryError)
                self.assertTrue(failure.value.__suppress_context__)
                self.assertTrue(resolve.called)
                self.assertEqual([], client.token_requests)

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
