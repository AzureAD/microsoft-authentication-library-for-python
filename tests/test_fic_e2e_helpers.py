"""Offline default-FIC guards, not evidence of live token acquisitions."""
from unittest.mock import Mock, patch

import pytest
import requests

from tests import test_e2e
from tests.mtls_e2e_helpers import (
    JWT_BEARER, JWT_POP, token_request_summary, unrelated_binding,
)
from tests.test_mtls_e2e_helpers import (
    CertificateFixture, _CREDENTIAL, _ENDPOINT, _jwt,
)


class TestDefaultFicCapture(CertificateFixture):
    def test_jwt_pop_capture_requires_the_exact_leg_one_assertion(self):
        form = dict(
            self._signed_form(), client_assertion="unit-fic-assertion",
            client_assertion_type=JWT_POP)
        for expected, matches in (("unit-fic-assertion", True), ("different", False)):
            summary = token_request_summary(
                self._response(form=form), self.binding,
                requests.adapters.HTTPAdapter(), expected_assertion=expected)
            self.assertEqual(matches, summary["assertion_matches"])
            self.assertFalse("unit-fic-assertion" in repr(summary))

    def test_default_fic_capture_failure_traceback_hides_the_expected_assertion(self):
        assertion = _jwt({"aud": "api://AzureADTokenExchange"})
        app = Mock()
        app.acquire_token_for_client.side_effect = AssertionError(
            "Unit acquisition failed before receiving a response.")
        scenario = test_e2e.FicTwoLegTestCase("test_credential_fic_output_pop")
        with pytest.raises(AssertionError) as failure:
            scenario._acquire_recorded(
                app, scenario._MTLS_POP_SCOPE, self.binding,
                pop=True, expected_assertion=assertion)
        self._assert_private_traceback(failure, assertion)


class TestFicScenarioGuards(CertificateFixture):
    def setUp(self):
        self.leg1 = Mock()
        self.leg2 = Mock()
        self.leg1.acquire_token_for_client.return_value = {
            "access_token": _jwt({
                "aud": "api://AzureADTokenExchange",
                "cnf": {"x5t#S256": self.binding.thumbprint_sha256}}),
            "token_type": "mtls_pop", "binding_certificate": self.binding,
        }
        self.leg2.acquire_token_for_client.return_value = {
            "access_token": _jwt({
                "aud": "https://graph.microsoft.com",
                "cnf": {"x5t#S256": self.binding.thumbprint_sha256}}),
            "token_type": "mtls_pop", "binding_certificate": self.binding,
        }
        self.factory = Mock(side_effect=[self.leg1, self.leg2])
        self.scenario = test_e2e.FicTwoLegTestCase("test_credential_fic_output_pop")
        self.invoke_provider = True
        self.omit_final_record = False
        self.final_record_changes = {}
        patchers = (
            patch("tests.lab_config.get_client_certificate", return_value=_CREDENTIAL),
            patch("msal.ConfidentialClientApplication", self.factory),
            patch.object(self.scenario, "_acquire_recorded",
                         side_effect=self._recorded),
            patch.object(self.scenario, "_call_graph_with_mtls_pop_token"),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.scenario.doCleanups)

    def _recorded(self, app, scope, binding, pop=False,
                  http_client=None, expected_assertion=None):
        config = self.factory.call_args.kwargs
        final = app is self.leg2
        assertion = None
        if final and self.invoke_provider:
            assertion = config["client_credential"]["client_assertion"]()
        result = app.acquire_token_for_client(scope, mtls_proof_of_possession=pop)
        region = config["azure_region"]
        record = {
            "url": _ENDPOINT.replace(
                "://mtlsauth.", "://{}.mtlsauth.".format(region)) if region else _ENDPOINT,
            "fields": frozenset(
                ("scope", "client_id", "grant_type")
                + (("client_assertion", "client_assertion_type") if final else ())
                + (("token_type",) if pop else ())),
            "grant_type": "client_credentials", "scope": frozenset(scope),
            "client_assertion_type": JWT_POP if final else None,
            "client_assertion_present": final, "signed_client_assertion": False,
            "assertion_matches": assertion == expected_assertion,
            "token_type": "mtls_pop" if pop else None,
            "status_code": 200, "tls_adapter": "_MtlsHTTPAdapter",
        }
        if final:
            record.update(self.final_record_changes)
        return result, [] if final and self.omit_final_record else [record]

    def test_global_pairing_provider_wire_type_and_resource_validation(self):
        self.scenario.test_credential_fic_output_pop()
        calls = self.factory.call_args_list
        self.assertEqual(2, len(calls))
        for app_call in calls:
            self.assertEqual(self.scenario._SNI_ALLOWLISTED_CLIENT_ID, app_call.args[0])
            self.assertEqual(
                self.scenario._SNI_ALLOWLISTED_AUTHORITY, app_call.kwargs["authority"])
            self.assertIs(False, app_call.kwargs["azure_region"])
        self.assertIs(
            calls[0].kwargs["client_credential"],
            calls[1].kwargs["client_credential"]["mtls_binding_certificate"])
        self.leg1.acquire_token_for_client.assert_called_once_with(
            ["api://AzureADTokenExchange/.default"], mtls_proof_of_possession=True)
        self.leg2.acquire_token_for_client.assert_called_once_with(
            ["https://graph.microsoft.com/.default"], mtls_proof_of_possession=True)
        self.scenario._call_graph_with_mtls_pop_token.assert_called_once()

    def test_regional_bearer_has_storage_audience_and_no_resource_mtls(self):
        self.leg2.acquire_token_for_client.return_value = {
            "access_token": _jwt({"aud": "https://storage.azure.com"}),
            "token_type": "Bearer"}
        self.scenario.test_credential_fic_output_bearer()
        self.assertTrue(all(
            call.kwargs["azure_region"] == "westus3"
            for call in self.factory.call_args_list))
        self.leg2.acquire_token_for_client.assert_called_once_with(
            ["https://storage.azure.com/.default"], mtls_proof_of_possession=False)
        self.scenario._call_graph_with_mtls_pop_token.assert_not_called()

    def test_leg_one_errors_fail_instead_of_skipping(self):
        for method in (
                self.scenario.test_credential_fic_output_pop,
                self.scenario.test_credential_fic_output_bearer):
            for error in ("token_type_mismatch", "invalid_client", "invalid_grant"):
                with self.subTest(error=error, method=method.__name__):
                    self.factory.side_effect = [self.leg1, self.leg2]
                    self.leg1.acquire_token_for_client.return_value = {"error": error}
                    with self.assertRaisesRegex(AssertionError, error):
                        method()
        self.leg2.acquire_token_for_client.assert_not_called()

    def test_leg_two_errors_fail_instead_of_skipping(self):
        for method in (
                self.scenario.test_credential_fic_output_pop,
                self.scenario.test_credential_fic_output_bearer):
            for error in ("invalid_client", "invalid_grant", "temporarily_unavailable"):
                with self.subTest(error=error, method=method.__name__):
                    self.factory.side_effect = [self.leg1, self.leg2]
                    self.leg2.acquire_token_for_client.return_value = {"error": error}
                    with self.assertRaisesRegex(AssertionError, error):
                        method()
        self.scenario._call_graph_with_mtls_pop_token.assert_not_called()

    def test_federated_assertion_is_opaque_but_still_requires_certificate_binding(self):
        self.leg1.acquire_token_for_client.return_value["access_token"] = "opaque"
        self.scenario.test_credential_fic_output_pop()
        self.leg2.acquire_token_for_client.reset_mock()
        for binding in (None, {}, unrelated_binding()):
            self.factory.side_effect = [self.leg1, self.leg2]
            self.leg1.acquire_token_for_client.return_value["binding_certificate"] = binding
            with self.assertRaises(AssertionError):
                self.scenario.test_credential_fic_output_pop()
        self.leg2.acquire_token_for_client.assert_not_called()

    def test_final_pop_requires_type_binding_cnf_and_audience(self):
        original = self.leg2.acquire_token_for_client.return_value
        for changes in (
                {"token_type": "Bearer"}, {"binding_certificate": None},
                {"binding_certificate": {}}, {"access_token": "opaque"},
                {"access_token": _jwt({
                    "aud": "https://graph.microsoft.com",
                    "cnf": {"x5t#S256": "different-certificate"}})},
                {"access_token": _jwt({
                    "aud": "https://wrong.example",
                    "cnf": {"x5t#S256": self.binding.thumbprint_sha256}})}):
            with self.subTest(fields=tuple(changes)):
                self.factory.side_effect = [self.leg1, self.leg2]
                self.leg2.acquire_token_for_client.return_value = dict(original, **changes)
                with self.assertRaises(AssertionError):
                    self.scenario.test_credential_fic_output_pop()
        self.scenario._call_graph_with_mtls_pop_token.assert_not_called()

    def test_final_bearer_rejects_bound_or_wrong_audience_results(self):
        for result in (
                {"access_token": "unit-token", "token_type": "mtls_pop"},
                {"access_token": _jwt({"aud": "https://storage.azure.com"}),
                 "token_type": "Bearer", "binding_certificate": {}},
                {"access_token": _jwt({"aud": "https://wrong.example"}),
                 "token_type": "Bearer"}):
            self.factory.side_effect = [self.leg1, self.leg2]
            self.leg2.acquire_token_for_client.return_value = result
            with self.assertRaises(AssertionError):
                self.scenario.test_credential_fic_output_bearer()
        self.scenario._call_graph_with_mtls_pop_token.assert_not_called()

    def test_fic_requires_provider_invocation_and_actual_wire_evidence(self):
        for invoke, omit, changes in (
                (False, False, {}), (True, True, {}),
                (True, False, {"client_assertion_type": JWT_BEARER}),
                (True, False, {"assertion_matches": False})):
            self.factory.side_effect = [self.leg1, self.leg2]
            self.invoke_provider, self.omit_final_record = invoke, omit
            self.final_record_changes = changes
            with self.assertRaises(AssertionError):
                self.scenario.test_credential_fic_output_pop()
        self.scenario._call_graph_with_mtls_pop_token.assert_not_called()
