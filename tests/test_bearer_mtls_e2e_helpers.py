"""Offline rejection guards for the real Bearer-over-mTLS E2E captures."""
from contextlib import nullcontext
from unittest.mock import Mock, patch

import requests

from tests import test_e2e
from tests.mtls_e2e_helpers import token_request_summary
from tests.test_mtls_e2e_helpers import CertificateFixture, _CREDENTIAL


class TestBearerMtlsE2eHelpers(CertificateFixture):
    def test_user_capture_requires_exact_rejection_not_success_or_credential_failure(self):
        scenario = test_e2e.CertificateScenariosTestCase(
            "test_send_certificate_over_mtls_on_behalf_of_request_capture")
        self.addCleanup(scenario.doCleanups)
        form = self._signed_form()
        form.update(
            grant_type="urn:ietf:params:oauth:grant-type:jwt-bearer",
            assertion="unit-user-assertion", requested_token_use="on_behalf_of")
        del form["refresh_token"]
        app = Mock()
        for status, payload, accepted in (
                (200, {"access_token": "must-not-be-accepted"}, False),
                (400, {"error": "invalid_client", "error_codes": [700027]}, False),
                (503, {"error": "invalid_grant"}, False),
                (400, {"error": "invalid_grant", "error_codes": [50027]}, False),
                (400, {"error": "invalid_request", "error_codes": [700027]}, False),
                (400, {"error": "invalid_request", "error_codes": [50027]}, True)):
            with self.subTest(status=status, accepted=accepted):
                response = self._response(form=form, status=status, payload=payload)
                summary = token_request_summary(
                    response, self.binding, requests.adapters.HTTPAdapter())
                summary["tls_adapter"] = "_MtlsHTTPAdapter"
                app.acquire_token_on_behalf_of.return_value = payload
                with patch(
                        "tests.lab_config.get_client_certificate",
                        return_value=_CREDENTIAL), patch(
                        "msal.ConfidentialClientApplication", return_value=app), patch(
                        "tests.test_e2e.record_token_requests",
                        return_value=nullcontext([summary])):
                    with (nullcontext() if accepted
                            else self.assertRaises(AssertionError)):
                        scenario.test_send_certificate_over_mtls_on_behalf_of_request_capture()
