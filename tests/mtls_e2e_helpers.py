"""Real-transport recording and certificate fixtures for the mTLS E2E tests."""
import base64
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import msal
from msal.application import _load_mtls_cert_material
from msal.oauth2cli.oidc import decode_part
from sample.confidential_client_mtls_http_client_sample import PolicyHttpClient


JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
JWT_POP = "urn:ietf:params:oauth:client-assertion-type:jwt-pop"
GRAPH_REJECTION_MARKERS = (
    "MissingClientCertificate",
    # The stable prefix is explicitly documented by the pinned Go reference.
    "Cnf claim not supported over",
    "MtlsCnfClaimRequestDataValidationFailed",
)


def jwt_claims(token):
    __tracebackhide__ = True
    if not isinstance(token, str) or len(token.split(".")) != 3:
        raise AssertionError("Expected a compact JWT with three segments.")
    try:
        claims = json.loads(decode_part(token.split(".")[1]))
    except (ValueError, TypeError):
        raise AssertionError("The JWT payload is not valid JSON.") from None
    if not isinstance(claims, dict):
        raise AssertionError("The JWT payload must be an object.")
    return claims


def binding_from_credential(credential):
    material = _load_mtls_cert_material(credential)
    return msal.MtlsBindingContext(
        material["cert_pem"], material["private_key_pem"],
        material["x5c"], material["key_id"])


def unrelated_binding():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "msal-python-unrelated-client")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .sign(key, hashes.SHA256()))
    return binding_from_credential({
        "private_key": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii"),
        "public_certificate": certificate.public_bytes(
            serialization.Encoding.PEM).decode("ascii"),
    })


def _verify_client_assertion(assertion, binding, endpoint, client_id):
    __tracebackhide__ = True
    if binding is None:
        raise AssertionError("A certificate is required to verify the client assertion.")
    try:
        header = jwt.get_unverified_header(assertion)
        certificate = x509.load_der_x509_certificate(
            base64.b64decode(binding.x5c[0]))
        claims = jwt.decode(
            assertion, certificate.public_key(), algorithms=["PS256"],
            audience=endpoint, issuer=client_id)
    except (jwt.InvalidTokenError, ValueError, TypeError, IndexError):
        raise AssertionError(
            "The signed client assertion failed certificate/signature/audience validation."
        ) from None
    chain = header.get("x5c")
    if (not isinstance(chain, list) or not chain or chain[0] != binding.x5c[0]
            or claims.get("sub") != client_id
            or not all(claims.get(field) for field in ("exp", "iat", "jti"))):
        raise AssertionError(
            "The client assertion must identify the configured certificate and client.")


def token_request_summary(
        response, binding, adapter, expected_assertion=None, assertion_audience=None):
    """Retain only safe metadata, never tokens, assertions, or raw POST bodies."""
    __tracebackhide__ = True
    request = response.request
    endpoint = urlparse(request.url)._replace(query="", fragment="").geturl()
    if (request.method != "POST"
            or not urlparse(endpoint).path.endswith("/oauth2/v2.0/token")):
        return None
    try:
        raw = request.body.decode("utf-8") if isinstance(
            request.body, bytes) else request.body
        form = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
        payload = json.loads(response.text)
    except (ValueError, TypeError, AttributeError):
        raise AssertionError("The real token exchange did not carry a valid OAuth form/JSON response.") from None
    if not isinstance(payload, dict):
        raise AssertionError("The real token endpoint did not return an OAuth JSON object.")
    error = payload.get("error")
    if error is not None and (
            not isinstance(error, str) or not re.fullmatch(r"[a-z_]{1,64}", error)):
        raise AssertionError("The token endpoint returned an invalid OAuth error code.")
    codes = payload.get("error_codes", [])
    if not isinstance(codes, list) or any(not isinstance(code, int) for code in codes):
        raise AssertionError("OAuth error_codes must be a list of integers.")
    fields = {key: values[0] for key, values in form.items()}
    assertion = fields.get("client_assertion")
    assertion_type = fields.get("client_assertion_type")
    signed = False
    if assertion and assertion_type == JWT_BEARER:
        _verify_client_assertion(
            assertion, binding, assertion_audience or endpoint,
            fields.get("client_id"))
        signed = True
    return {
        "url": endpoint,
        "fields": frozenset(fields),
        "grant_type": fields.get("grant_type"),
        "requested_token_use": fields.get("requested_token_use"),
        "scope": frozenset(fields.get("scope", "").split()),
        "token_type": fields.get("token_type"),
        "client_assertion_type": assertion_type,
        "client_assertion_present": bool(assertion),
        "signed_client_assertion": signed,
        "assertion_matches": (
            expected_assertion is not None and assertion == expected_assertion),
        "status_code": response.status_code,
        "error": error,
        "error_codes": tuple(codes[:8]),
        "tls_adapter": type(adapter).__name__,
        "middleware": request.headers.get("X-Msal-Pipeline"),
    }


@contextmanager
def record_token_requests(binding, expected_assertion=None, assertion_audience=None):
    """Delegate Session.send unchanged, including MSAL's built-in TLS adapter."""
    __tracebackhide__ = True
    records = []
    send = requests.Session.send

    def recording_send(session, request, **kwargs):
        response = send(session, request, **kwargs)
        summary = token_request_summary(
            response, binding, session.get_adapter(request.url),
            expected_assertion=expected_assertion,
            assertion_audience=assertion_audience)
        if summary is not None:
            records.append(summary)
        return response

    with patch.object(requests.Session, "send", new=recording_send):
        yield records


class RecordingPolicyHttpClient(PolicyHttpClient):
    """Add safe recording to the existing sample's real certificate pipeline."""

    def __init__(self, binding_context=None, expected_binding=None,
                 expected_assertion=None, **kwargs):
        __tracebackhide__ = True
        self.binding_context = binding_context
        self.expected_binding = expected_binding or binding_context
        self.expected_assertion = expected_assertion
        self.token_requests = []
        super().__init__(binding_context=binding_context, **kwargs)

    def create_mtls_client(self, binding_context):
        with self._lock:
            self._check_open()
            child = RecordingPolicyHttpClient(
                binding_context=binding_context,
                expected_binding=self.expected_binding,
                expected_assertion=self.expected_assertion, **self._config)
            self._children.append(child)
            return child

    def post(self, url, **kwargs):
        __tracebackhide__ = True
        response = super().post(url, **kwargs)
        summary = token_request_summary(
            response, self.expected_binding,
            self._session.get_adapter(response.request.url),
            expected_assertion=self.expected_assertion)
        if summary is not None:
            self.token_requests.append(summary)
        return response
