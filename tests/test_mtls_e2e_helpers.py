"""Offline guard/recorder checks, not evidence of live E2E acquisitions."""
import base64
from contextlib import nullcontext
from contextlib import redirect_stdout
import io
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jwt
import pytest
import requests

import msal
from msal.application import _load_mtls_cert_material
from tests import test_e2e
from tests.mtls_e2e_helpers import (
    GRAPH_REJECTION_MARKERS, JWT_BEARER,
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


class TestMtlsE2eHelpers(CertificateFixture):
    def setUp(self):
        self.scenario = test_e2e.CertificateScenariosTestCase(
            "test_credential_x509_output_pop")

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


class TestMtlsSampleLogging(unittest.TestCase):
    def test_pop_summary_never_prints_dynamic_result_values(self):
        from sample.confidential_client_mtls_pop_sample import _print_result_summary

        sensitive = "sensitive-unit-result-value"
        for result in (
                {"access_token": sensitive, "token_type": sensitive},
                {"error": sensitive, "error_description": sensitive}):
            with self.subTest(fields=tuple(result)), redirect_stdout(io.StringIO()) as output:
                _print_result_summary(result, "Acquisition result")
            self.assertNotIn(sensitive, output.getvalue())
            self.assertIn("acquired:", output.getvalue())


class TestDefaultHttpClientCleanup(unittest.TestCase):
    def test_base_application_closes_ordinary_global_and_regional_clients(self):
        authority = test_e2e.MtlsPopTestCase._SNI_ALLOWLISTED_AUTHORITY
        metadata = {
            "authorization_endpoint": authority + "/oauth2/v2.0/authorize",
            "token_endpoint": authority + "/oauth2/v2.0/token",
            "issuer": authority + "/v2.0",
        }
        with patch("msal.authority.tenant_discovery", return_value=metadata), patch(
                "msal.application._detect_region", return_value=None):
            app = msal.ConfidentialClientApplication(
                "client", authority=authority, client_credential=_CREDENTIAL,
                azure_region=False, instance_discovery=False)
            self.addCleanup(test_e2e.MtlsPopTestCase._close_default_http_clients, app)
            global_client = app._get_mtls_client(app.authority)
            app._region_configured = "westus3"
            regional_client = app._get_mtls_client(app.authority)
        shared = getattr(app, "_mtls_http_client", None)
        with patch.object(app.http_client, "close") as ordinary_close, patch.object(
                global_client._http_client, "close") as global_close, patch.object(
                regional_client._http_client, "close") as regional_close, (
                patch.object(shared, "close") if shared is not None
                else nullcontext()) as shared_close:
            test_e2e.MtlsPopTestCase._close_default_http_clients(app)
        ordinary_close.assert_called_once_with()
        if shared is None:
            global_close.assert_called_once_with()
            regional_close.assert_called_once_with()
        else:
            shared_close.assert_called_once_with()
            global_close.assert_not_called()
            regional_close.assert_not_called()

    def test_default_bearer_clients_are_closed_once_without_shared_transport(self):
        ordinary, pop, bearer = Mock(), Mock(), Mock()
        app = SimpleNamespace(
            http_client=ordinary, _mtls_client=SimpleNamespace(_http_client=pop),
            _mtls_regional_client=None,
            _mtls_bearer_clients={
                "global": SimpleNamespace(_http_client=bearer),
                "regional": SimpleNamespace(_http_client=bearer)})
        test_e2e.MtlsPopTestCase._close_default_http_clients(app)
        for client in (ordinary, pop, bearer):
            client.close.assert_called_once_with()

    def test_shared_transport_cleanup_does_not_close_endpoint_wrappers(self):
        ordinary, shared, endpoint = Mock(), Mock(), Mock()
        app = SimpleNamespace(
            http_client=ordinary, _mtls_http_client=shared,
            _mtls_client=SimpleNamespace(_http_client=endpoint),
            _mtls_regional_client=SimpleNamespace(_http_client=endpoint),
            _mtls_bearer_clients={"global": SimpleNamespace(_http_client=endpoint)})
        test_e2e.MtlsPopTestCase._close_default_http_clients(app)
        ordinary.close.assert_called_once_with()
        shared.close.assert_called_once_with()
        endpoint.close.assert_not_called()

    def test_ordinary_application_without_mtls_transport_closes_normally(self):
        ordinary = Mock()
        app = SimpleNamespace(
            http_client=ordinary, _mtls_client=None, _mtls_regional_client=None)
        test_e2e.MtlsPopTestCase._close_default_http_clients(app)
        ordinary.close.assert_called_once_with()
