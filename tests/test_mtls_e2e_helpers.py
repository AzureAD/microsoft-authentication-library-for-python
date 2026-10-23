"""Offline guard/recorder checks, not evidence of live E2E acquisitions."""
import base64
from contextlib import nullcontext
import json
import os
import time
import unittest
from unittest.mock import Mock, patch

import jwt
import pytest
import requests

from msal.application import _load_mtls_cert_material
from tests import test_e2e
from tests.mtls_e2e_helpers import (
    GRAPH_REJECTION_MARKERS, JWT_BEARER, JWT_POP, RecordingPolicyHttpClient,
    binding_from_credential, record_token_requests, token_request_summary,
    unrelated_binding,
)


_CREDENTIAL = {
    "private_key_pfx_path": os.path.join(
        os.path.dirname(__file__), "certificate-with-password.pfx"),
    "passphrase": "password", "public_certificate": True,
}
_ENDPOINT = (
    test_e2e.MtlsPopTestCase._SNI_ALLOWLISTED_AUTHORITY.replace(
        "login.microsoftonline.com", "mtlsauth.microsoft.com")
    + "/oauth2/v2.0/token")


def _jwt(claims):
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value).encode("utf-8")).rstrip(b"=").decode("ascii")

    return ".".join((encode({"alg": "none"}), encode(claims), "unit-signature"))


class CertificateFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binding = binding_from_credential(_CREDENTIAL)
        cls.material = _load_mtls_cert_material(_CREDENTIAL)

    def _signed_form(self, audience=_ENDPOINT):
        now = int(time.time())
        assertion = jwt.encode({
            "aud": audience, "iss": "client", "sub": "client",
            "iat": now, "exp": now + 600, "jti": "unit-assertion",
        }, self.material["private_key_pem"], algorithm="PS256",
            headers={"x5c": self.binding.x5c})
        if isinstance(assertion, bytes):
            assertion = assertion.decode("ascii")
        return {
            "client_id": "client", "client_assertion": assertion,
            "client_assertion_type": JWT_BEARER,
            "grant_type": "refresh_token", "refresh_token": "unit-refresh-secret",
            "scope": "https://vault.azure.net/.default",
        }

    def _response(self, form=None, status=400, payload=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(
            {"error": "invalid_grant", "error_codes": [50027]}
            if payload is None else payload).encode("utf-8")
        response.request = requests.Request(
            "POST", _ENDPOINT,
            data=self._signed_form() if form is None else form).prepare()
        return response


class TestMtlsE2eHelpers(CertificateFixture):
    def setUp(self):
        self.scenario = test_e2e.CertificateScenariosTestCase(
            "test_credential_x509_output_pop")

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

    def test_cnf_check_rejects_opaque_malformed_and_unbound_tokens(self):
        for token in (
                "opaque", "a.invalid-json.c", _jwt([]), _jwt({}),
                _jwt({"cnf": {}}), _jwt({"cnf": {"x5t#S256": ""}})):
            with self.subTest(token_shape=token.count(".")):
                with self.assertRaises(AssertionError):
                    self.scenario._cnf_x5t_s256(token)

    def test_pop_checks_the_jwt_and_returned_certificate_binding(self):
        result = {
            "token_type": "mtls_pop", "binding_certificate": self.binding,
            "access_token": _jwt({
                "aud": "00000003-0000-0000-c000-000000000000",
                "cnf": {"x5t#S256": self.binding.thumbprint_sha256}}),
        }
        self.scenario._assert_token_result(
            result, self.scenario._MTLS_POP_SCOPE, "mtls_pop", self.binding)
        wrong = dict(result, access_token=_jwt({
            "aud": "https://graph.microsoft.com",
            "cnf": {"x5t#S256": "different-certificate"}}))
        with self.assertRaises(AssertionError):
            self.scenario._assert_token_result(
                wrong, self.scenario._MTLS_POP_SCOPE, "mtls_pop", self.binding)

    def test_key_vault_accepts_its_verified_application_id_not_another_resource(self):
        for audience, accepted in (
                ("https://vault.azure.net", True),
                ("cfa8b339-82a2-471a-a3c9-0fc0be7a4093", True),
                ("00000003-0000-0000-c000-000000000000", False)):
            result = {
                "token_type": "mtls_pop", "binding_certificate": self.binding,
                "access_token": _jwt({
                    "aud": audience,
                    "cnf": {"x5t#S256": self.binding.thumbprint_sha256}}),
            }
            with self.subTest(audience=audience):
                with (nullcontext() if accepted else self.assertRaises(AssertionError)):
                    self.scenario._assert_token_result(
                        result, self.scenario._KEY_VAULT_SCOPE, "mtls_pop", self.binding)

    def test_failure_tracebacks_do_not_expose_token_arguments(self):
        for token in (
                "opaque-" + self.binding.thumbprint_sha256,
                _jwt({"aud": "https://wrong.example", "cnf": {}}),
                _jwt({"aud": "https://wrong.example", "cnf": {
                    "x5t#S256": self.binding.thumbprint_sha256}})):
            result = {
                "access_token": token, "token_type": "mtls_pop",
                "binding_certificate": self.binding,
            }
            with pytest.raises(AssertionError) as failure:
                self.scenario._assert_token_result(
                    result, self.scenario._MTLS_POP_SCOPE, "mtls_pop", self.binding)
            self._assert_private_traceback(failure, token)

    def test_cached_result_failure_traceback_hides_original_token(self):
        claims = {
            "aud": "https://graph.microsoft.com",
            "cnf": {"x5t#S256": self.binding.thumbprint_sha256},
        }
        token = _jwt(dict(claims, nonce="original"))
        cached_token = _jwt(dict(claims, nonce="cached"))
        original = {
            "access_token": token, "token_type": "mtls_pop",
            "binding_certificate": self.binding,
        }
        app = Mock(_TOKEN_SOURCE_CACHE="cache", _TOKEN_SOURCE="token_source")
        app.acquire_token_for_client.return_value = dict(
            original, access_token=cached_token, token_source="cache")
        with pytest.raises(AssertionError) as failure:
            self.scenario._assert_cached_result(
                app, self.scenario._MTLS_POP_SCOPE, original, pop=True)
        self._assert_private_traceback(failure, token, cached_token)

    def test_fic_capture_failure_tracebacks_hide_the_expected_assertion(self):
        assertion = _jwt({"aud": "api://AzureADTokenExchange"})
        for custom in (False, True):
            with self.subTest(custom=custom):
                app = Mock()
                client = RecordingPolicyHttpClient(
                    expected_binding=self.binding,
                    expected_assertion=assertion) if custom else None
                if custom:
                    self.addCleanup(client.close)
                    app.acquire_token_for_client.return_value = {"access_token": assertion}
                else:
                    app.acquire_token_for_client.side_effect = AssertionError(
                        "Unit acquisition failed before receiving a response.")
                with pytest.raises(AssertionError) as failure:
                    self.scenario._acquire_recorded(
                        app, self.scenario._MTLS_POP_SCOPE, self.binding,
                        pop=True, http_client=client, expected_assertion=assertion)
                self._assert_private_traceback(failure, assertion)

    def test_graph_success_failure_traceback_hides_the_access_token(self):
        token = _jwt({"aud": "https://graph.microsoft.com"})
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.return_value = Mock(status_code=403)
        binding = Mock(spec=["create_requests_session"])
        binding.create_requests_session.return_value = session
        with pytest.raises(AssertionError) as failure:
            self.scenario._call_graph_with_mtls_pop_token(token, binding)
        self._assert_private_traceback(failure, token)

    def test_graph_rejection_failure_traceback_hides_the_access_token(self):
        token = _jwt({"aud": "https://graph.microsoft.com"})
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.return_value = Mock(status_code=401, text="Unexpected marker")
        with patch("tests.test_e2e.requests.Session", return_value=session):
            with pytest.raises(AssertionError) as failure:
                self.scenario._assert_graph_rejects(
                    token, "mtls_pop", None, GRAPH_REJECTION_MARKERS[0])
        self._assert_private_traceback(failure, token)

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

    def test_unrelated_certificate_is_usable_and_not_the_fixture_certificate(self):
        binding = unrelated_binding()
        self.assertNotEqual(
            self.binding.thumbprint_sha256, binding.thumbprint_sha256)
        context = binding.create_ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertFalse("PRIVATE KEY" in repr(binding))

    def test_graph_controls_require_the_specific_marker_and_rejection_status(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        with patch("tests.test_e2e.requests.Session", return_value=session):
            for marker in GRAPH_REJECTION_MARKERS:
                for status in (401, 403):
                    session.get.return_value = Mock(status_code=status, text=marker)
                    self.scenario._assert_graph_rejects(
                        "unit-token", "mtls_pop", None, marker)
                for status, body in (
                        (200, marker), (400, marker), (503, marker),
                        (401, "InvalidAuthenticationToken"),
                        (401, " ".join(GRAPH_REJECTION_MARKERS))):
                    with self.subTest(marker=marker, status=status):
                        session.get.return_value = Mock(status_code=status, text=body)
                        with self.assertRaises(AssertionError):
                            self.scenario._assert_graph_rejects(
                                "unit-token", "mtls_pop", None, marker)

    def test_recorded_metadata_does_not_retain_secrets(self):
        form = self._signed_form()
        response = self._response(form=form)
        summary = token_request_summary(
            response, self.binding, requests.adapters.HTTPAdapter())
        self.assertTrue(summary["signed_client_assertion"])
        self.assertEqual(400, summary["status_code"])
        self.assertEqual((50027,), summary["error_codes"])
        for secret in (form["client_assertion"], form["refresh_token"]):
            self.assertFalse(secret in repr(summary), "Recording must not retain secrets.")
        self.assertNotIn("body", summary)
        self.assertNotIn("client_assertion", summary)

    def test_unstructured_or_unbounded_error_metadata_is_not_recorded(self):
        assertion = self._signed_form()["client_assertion"]
        for error in (assertion, "x" * 65, {"private": assertion}):
            with self.assertRaises(AssertionError) as raised:
                token_request_summary(
                    self._response(payload={"error": error}),
                    self.binding, requests.adapters.HTTPAdapter())
            self.assertFalse(assertion in str(raised.exception))
        summary = self.scenario._result_summary({
            "error": {"private": assertion},
            "error_codes": [assertion, 50027] * 10})
        self.assertFalse(assertion in summary)
        self.assertLess(len(summary), 128)

    def test_client_assertion_signature_audience_and_client_are_checked(self):
        forms = [
            self._signed_form(audience="https://wrong.example/token"),
            dict(self._signed_form(), client_id="other-client"),
            dict(self._signed_form(), client_assertion="invalid.jwt.signature"),
        ]
        for form in forms:
            with self.subTest(client_id=form["client_id"]):
                with self.assertRaises(AssertionError) as raised:
                    token_request_summary(
                        self._response(form=form), self.binding,
                        requests.adapters.HTTPAdapter())
                self.assertFalse(form["client_assertion"] in str(raised.exception))

    def test_regional_ordinary_assertion_uses_the_central_authority_audience(self):
        central = _ENDPOINT.replace("mtlsauth.microsoft.com", "login.microsoftonline.com")
        regional = _ENDPOINT.replace("mtlsauth.microsoft.com", "westus3.login.microsoft.com")
        response = self._response(form=self._signed_form(audience=central))
        response.request.url = regional
        with self.assertRaises(AssertionError):
            token_request_summary(
                response, self.binding, requests.adapters.HTTPAdapter())
        summary = token_request_summary(
            response, self.binding, requests.adapters.HTTPAdapter(),
            assertion_audience=central)
        self.assertTrue(summary["signed_client_assertion"])
        self.assertEqual(regional, summary["url"])

    def test_user_capture_requires_exact_rejection_not_success_or_credential_failure(self):
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
            response = self._response(form=form, status=status, payload=payload)
            summary = token_request_summary(
                response, self.binding, requests.adapters.HTTPAdapter())
            summary["tls_adapter"] = "_MtlsHTTPAdapter"
            app.acquire_token_on_behalf_of.return_value = payload
            with patch("tests.lab_config.get_client_certificate",
                       return_value=_CREDENTIAL), patch(
                    "msal.ConfidentialClientApplication", return_value=app), patch(
                    "tests.test_e2e.record_token_requests",
                    return_value=nullcontext([summary])):
                with (nullcontext() if accepted else self.assertRaises(AssertionError)):
                    self.scenario.test_send_certificate_over_mtls_on_behalf_of_request_capture()
        self.scenario.doCleanups()

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

    def test_default_recorder_delegates_and_does_not_accept_transport_failures(self):
        session = requests.Session()
        self.addCleanup(session.close)
        adapter = Mock(spec=requests.adapters.HTTPAdapter)

        def send(request, **kwargs):
            response = self._response()
            response.request = request
            return response

        adapter.send.side_effect = send
        session.mount("https://", adapter)
        with record_token_requests(self.binding) as records:
            response = session.post(_ENDPOINT, data=self._signed_form())
        self.assertEqual(400, response.status_code)
        self.assertEqual(1, len(records))
        adapter.send.assert_called_once()
        for error in (
                requests.exceptions.ConnectionError("offline connection failure"),
                requests.exceptions.SSLError("offline TLS failure"),
                requests.exceptions.Timeout("offline timeout")):
            adapter.send.side_effect = error
            with record_token_requests(self.binding) as records:
                with self.assertRaises(type(error)):
                    session.post(_ENDPOINT, data=self._signed_form())
            self.assertEqual([], records)

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
