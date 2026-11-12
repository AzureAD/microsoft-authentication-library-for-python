import base64
import asyncio
import ctypes
import hashlib
import json
import time
import threading
import sys
import uuid
import unittest
from contextlib import contextmanager
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
from itertools import permutations

import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from cryptography.x509.oid import NameOID

from msal import (
    ManagedIdentityClient, ManagedIdentityError, SerializableTokenCache,
    SystemAssignedManagedIdentity, UserAssignedManagedIdentity,
)
from msal._managed_identity_v2 import (
    _Deadline, _ImdsClient, _MaaTokenCache, _V2Error, _imds_base,
)
from msal._native_mtls import _NativeMtlsError


CLIENT = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def response(payload, status=200, marker="IMDS/2", url=None):
    result = requests.Response()
    result.status_code, result.url = status, url
    result.headers["Server"] = marker
    result._content = json.dumps(payload).encode("utf-8")
    return result


def platform():
    return dict(clientId=CLIENT, tenantId=TENANT, cuId={"vmId": "unit-vm"},
                attestationEndpoint="https://fallback.attestation.example")


def jwt(expiry):
    payload = base64.urlsafe_b64encode(json.dumps(expiry).encode()).rstrip(b"=").decode()
    return "header." + payload + ".signature"


class SoftwareSignerFixture:
    """CSR/response unit fixture only: never evidence of protected-key support."""
    def __init__(self):
        self.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend())
        self.public_key = self.private_key.public_key()
        self.generation_id = hashlib.sha256(self.public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
        self.store_uri = "unit-software-fixture-not-a-production-cng-uri"

    def sign_sha256_pss(self, digest):
        return self.private_key.sign(
            digest, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            utils.Prehashed(hashes.SHA256()))

    def close(self):
        pass

    def certificate(self, *, hours=48, other_key=None):
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit certificate")])
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(other_key or self.public_key)
                       .serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=5))
                       .not_valid_after(now + timedelta(hours=hours))
                       .sign(self.private_key, hashes.SHA256(), default_backend()))
        return base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()


class ImdsV2ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.identity = SystemAssignedManagedIdentity()
        self.imds = _ImdsClient(self.http, self.identity, base="http://169.254.169.254")
        self.budget = _Deadline(90)

    def test_selectors_are_preserved_on_both_credential_operations(self):
        signer = SoftwareSignerFixture()
        issued = dict(client_id=CLIENT, tenant_id=TENANT, identity_type="UAMI",
                      mtls_authentication_endpoint="https://region.login.example/",
                      certificate=signer.certificate())
        for identity, field, value in (
                (UserAssignedManagedIdentity(client_id=CLIENT), "client_id", CLIENT),
                (UserAssignedManagedIdentity(object_id="object"), "object_id", "object"),
                (UserAssignedManagedIdentity(resource_id="/subscriptions/unit"), "mi_res_id", "/subscriptions/unit")):
            with self.subTest(field=field):
                self.http.reset_mock()
                self.http.get.return_value = response(platform())
                self.http.post.return_value = response(issued)
                client = _ImdsClient(self.http, identity, base=self.imds.base)
                metadata = client.platform(self.budget, "correlation")
                credential = client.issue(
                    metadata, signer, budget=self.budget, correlation_id="correlation")
                for call in (self.http.get.call_args, self.http.post.call_args):
                    self.assertEqual(call.kwargs["params"], {"cred-api-version": "2.0", field: value})
                    self.assertFalse(call.kwargs["allow_redirects"])
                    self.assertEqual(call.kwargs["headers"]["Metadata"], "true")
                    self.assertEqual(call.kwargs["headers"]["x-ms-client-request-id"], "correlation")
                self.assertEqual(credential["token_endpoint"],
                                 "https://region.login.example/" + TENANT + "/oauth2/v2.0/token")
                body = json.loads(self.http.post.call_args.kwargs["data"])
                self.assertNotIn("attestation_token", body)
                csr = x509.load_der_x509_csr(base64.b64decode(body["csr"]), default_backend())
                self.assertTrue(csr.is_signature_valid)
                self.assertEqual(csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, CLIENT)

    def test_origin_marker_and_selected_client_are_checked(self):
        self.http.get.return_value = response(platform())
        self.assertEqual(self.imds.platform(self.budget, "correlation")["tenantId"], TENANT)
        for marker in ("NOTIMDS", "IMDS.attacker", "IMDS other", ""):
            with self.subTest(marker=marker):
                self.http.get.return_value = response(platform(), marker=marker)
                with self.assertRaises(_V2Error):
                    self.imds.platform(self.budget, "correlation")
        self.http.get.return_value = response(
            platform(), url="http://elsewhere/metadata/identity/getplatformmetadata")
        with self.assertRaises(_V2Error):
            self.imds.platform(self.budget, "correlation")
        self.http.get.return_value = response(platform())
        with self.assertRaises(_V2Error):
            _ImdsClient(self.http, UserAssignedManagedIdentity(client_id="different"),
                        base=self.imds.base).platform(self.budget, "correlation")

    def test_regional_attestation_clouds_and_compute_fallback(self):
        for cloud, region, endpoint in (
                ("AzurePublicCloud", "WESTUS2", "https://westus2.attestation.microsoft.com"),
                ("AzureUSGovernmentCloud", "usgovvirginia", "https://usgovvirginia.attestation.microsoftonline.us"),
                ("AzureChinaCloud", "chinaeast2", "https://chinaeast2.attestation.partner.microsoftonline.cn"),
                ("AzureBleuCloud", "francecentral", "https://francecentral.attestation.sovcloud-identity.fr")):
            with self.subTest(cloud=cloud):
                self.http.get.return_value = response(dict(azEnvironment=cloud, location=region))
                self.assertEqual(self.imds.attestation_endpoint(platform(), self.budget, "id"), endpoint)
        for compute in (dict(azEnvironment="unknown", location="westus2"),
                        dict(azEnvironment="AzurePublicCloud", location="westus2.evil"),
                        dict(azEnvironment="AzurePublicCloud", location="-westus2"),
                        [], {}):
            with self.subTest(compute=compute):
                self.http.get.return_value = response(compute)
                self.assertEqual(self.imds.attestation_endpoint(platform(), self.budget, "id"),
                                 platform()["attestationEndpoint"])
        self.http.get.side_effect = requests.Timeout()
        self.assertEqual(self.imds.attestation_endpoint(platform(), self.budget, "id"),
                         platform()["attestationEndpoint"])

    def test_expired_compute_budget_terminates_without_fallback(self):
        clock = Mock(side_effect=[0, 0, 2])
        budget = _Deadline(1, clock=clock)
        self.http.get.return_value = response({})
        with self.assertRaises(_V2Error) as caught:
            self.imds.attestation_endpoint(platform(), budget, "id")
        self.assertEqual(caught.exception.error_code, "timeout")
        self.http.post.assert_not_called()

    def test_imds_retry_count_and_backoff_depend_on_first_status(self):
        for status, delays in ((410, [10] * 7), (404, [1, 2, 4]), (429, [1, 2, 4])):
            with self.subTest(status=status):
                clock = [0]
                waits = []

                def sleep(seconds):
                    waits.append(seconds)
                    clock[0] += seconds

                budget = _Deadline(90, clock=lambda: clock[0], sleep=sleep)
                self.http.reset_mock()
                self.http.get.return_value = response({}, status=status)
                result = self.imds.request("GET", "/probe", budget=budget, correlation_id="id")
                self.assertEqual(result.status_code, status)
                self.assertEqual(waits, delays)
                self.assertEqual(self.http.get.call_count, len(delays) + 1)
        self.http.reset_mock()
        self.http.get.return_value = response({}, status=404)
        self.imds.request("GET", "/probe", budget=_Deadline(1), correlation_id="id", probe=True)
        self.http.get.assert_called_once()

    def test_service_codes_are_bounded_and_only_trusted_imds_errors_are_used(self):
        path = "/metadata/identity/getplatformmetadata"
        for error in ("attestation_required", {"code": "policy_rejected"}):
            with self.subTest(error=error):
                result = response({"error": error, "error_description": "private description"}, status=403)
                with self.assertRaises(_V2Error) as caught:
                    self.imds._json(result, path)
                self.assertEqual(caught.exception.error_code,
                                 error if isinstance(error, str) else error["code"])
                self.assertEqual(caught.exception.status_code, 403)
                self.assertNotIn("private description", str(caught.exception))
        for error in ("private description", "x" * 129, 42, None):
            with self.subTest(error=error), self.assertRaises(_V2Error) as caught:
                self.imds._json(response({"error": error}, status=403), path)
            self.assertEqual(caught.exception.error_code, "request_failed")
        for result in (response({"error": "attestation_required"}, status=403, marker="other"),
                       response({"error": "attestation_required"}, status=403, url="http://other/")):
            with self.assertRaises(_V2Error) as caught:
                self.imds._json(result, path)
            self.assertEqual(caught.exception.error_code, "invalid_origin")
        malformed = response({}, status=403)
        malformed._content = b"private non-JSON body"
        with self.assertRaises(_V2Error) as caught:
            self.imds._json(malformed, path)
        self.assertEqual(caught.exception.error_code, "request_failed")

    def test_credential_identity_endpoint_and_key_mismatch_fail_closed(self):
        signer = SoftwareSignerFixture()
        issued = dict(client_id=CLIENT, tenant_id=TENANT, identity_type="SAMI",
                      mtls_authentication_endpoint="https://region.login.example",
                      certificate=signer.certificate(hours=1))
        self.http.post.return_value = response(issued)
        self.assertGreater(self.imds.issue(
            platform(), signer, budget=self.budget, correlation_id="id")["expires_on"], 0)
        for field, value in (
                ("client_id", "wrong"), ("tenant_id", "wrong"), ("identity_type", "UAMI"),
                ("mtls_authentication_endpoint", "http://region.login.example"),
                ("mtls_authentication_endpoint", "https://region.login.example/" + TENANT),
                ("certificate", signer.certificate(other_key=SoftwareSignerFixture().public_key))):
            with self.subTest(field=field):
                self.http.post.return_value = response(dict(issued, **{field: value}))
                with self.assertRaises(_V2Error):
                    self.imds.issue(platform(), signer, budget=self.budget, correlation_id="id")

    def test_imds_base_rejects_remote_or_credential_bearing_overrides(self):
        for host in ("http://example.com", "http://127.0.0.1/path",
                     "http://localhost:0", "http://localhost/?query"):
            with self.subTest(host=host), patch.dict("os.environ", AZURE_POD_IDENTITY_AUTHORITY_HOST=host):
                with self.assertRaises(_V2Error):
                    _imds_base()
        with patch.dict("os.environ", AZURE_POD_IDENTITY_AUTHORITY_HOST="http://localhost:8080/"):
            self.assertEqual(_imds_base(), "http://localhost:8080")


class MaaCacheTests(unittest.TestCase):
    def test_endpoint_and_actual_key_generation_determine_fresh_reuse(self):
        cache = _MaaTokenCache(clock=lambda: 1000)
        load = Mock(return_value=jwt({"exp": 1400}))
        first = cache.get_or_attest("https://MAA.example/", "generation-a", load)
        self.assertEqual(cache.get_or_attest("https://maa.example", "generation-a", load), first)
        load.assert_called_once()
        cache.get_or_attest("https://maa.example", "generation-b", load)
        self.assertEqual(load.call_count, 2)

    def test_invalid_or_ambiguous_expiry_is_never_cached(self):
        cache = _MaaTokenCache(clock=lambda: 1000)
        invalid = [jwt(payload) for payload in (
            {"exp": 1300}, {"exp": "1400"}, {"exp": True},
            {"nested": {"exp": 1400}}, {"exp": float("inf")}, [])]
        ambiguous = base64.urlsafe_b64encode(b'{"exp":1400,"\\u0065xp":1500}').rstrip(b"=").decode()
        invalid.append("header." + ambiguous + ".signature")
        for token in invalid:
            load = Mock(return_value=token)
            for attempt in range(2):
                with self.assertRaises(_V2Error):
                    cache.get_or_attest("https://maa.example", "generation", load)
            self.assertEqual(load.call_count, 2)

    def test_concurrent_misses_share_one_native_attestation(self):
        cache = _MaaTokenCache(clock=lambda: 1000)
        load = Mock(return_value=jwt({"exp": 1400}))
        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(
                lambda unused: cache.get_or_attest("https://maa.example", "generation", load), range(32)))
        self.assertTrue(all(result == results[0] for result in results))
        load.assert_called_once()


@contextmanager
def locked_fixture_chain(path, digest):
    if hashlib.sha256(path.read_bytes()).digest() != digest:
        raise _V2Error("Unit fixture chain integrity failure.",
               stage="binding", error_code="chain_integrity_failed")
    yield


class PublicProtectedFlowTests(unittest.TestCase):
    """Public SDK wiring with software/mocked native boundaries, not live MSI v2."""
    def setUp(self):
        from msal.managed_identity import DEFAULT_TO_VM
        self.signer = SoftwareSignerFixture()
        self.http = requests.Session()
        self.addCleanup(self.http.close)
        self.http.trust_env = False
        self.http.get = Mock(side_effect=self._get)
        self.http.post = Mock(side_effect=self._issue)
        self.runtime = Mock()
        self.runtime.request.side_effect = self._native_request
        self.attestation = Mock()
        self.attestation.attest.return_value = jwt({"exp": time.time() + 3600})
        self.token_requests, self.next_tokens = [], []
        self.cache = SerializableTokenCache()
        self.client = ManagedIdentityClient(SystemAssignedManagedIdentity(), http_client=self.http,
                                    token_cache=self.cache, client_capabilities=["CP1"])
        for target, arguments in (
        ("msal.managed_identity.get_managed_identity_source", dict(return_value=DEFAULT_TO_VM)),
        ("msal._managed_identity_v2._BINDINGS", dict(new=OrderedDict())),
        ("msal._managed_identity_v2._FORCE_MINT", dict(new=set())),
        ("msal._managed_identity_v2._MAA_CACHE", dict(new=_MaaTokenCache())),
        ("msal._managed_identity_v2._load_runtime", dict(return_value=self.runtime)),
        ("msal._managed_identity_v2._NativeKey", dict(return_value=self.signer)),
        ("msal._managed_identity_v2._load_attestation", dict(return_value=self.attestation)),
        ("msal._managed_identity_binding._locked_chain", dict(new=locked_fixture_chain))):
            started = patch(target, **arguments)
            started.start()
            self.addCleanup(started.stop)
        disabled = patch.dict("os.environ", MSAL_MI_DISABLE_IMDS_V2="0",
                              MSAL_MI_DISABLE_PERSISTENT_CERT_CACHE="1")
        disabled.start()
        self.addCleanup(disabled.stop)

    def _get(self, url, **kwargs):
        if url.endswith("/metadata/instance/compute"):
            return response(dict(azEnvironment="AzurePublicCloud", location="westus2"), url=url)
        return response(platform(), url=url)

    def _issue(self, url, **kwargs):
        return response(dict(client_id=CLIENT, tenant_id=TENANT, identity_type="SAMI",
                     certificate=self.signer.certificate(),
                     mtls_authentication_endpoint="https://region.login.example"), url=url)

    def _native_request(self, **kwargs):
        if kwargs["url"].endswith("/oauth2/v2.0/token"):
            self.token_requests.append(kwargs)
            form = parse_qs(kwargs["body"].decode("ascii"))
            return response(self.next_tokens.pop(0) if self.next_tokens else dict(
        access_token="opaque-unit-token", token_type=form["token_type"][0], expires_in=3600))
        return response({"value": "unit-resource"})

    def _acquire(self, **kwargs):
        return self.client.acquire_token_for_client(resource="https://vault.example", **kwargs)

    def test_public_pop_exchange_uses_canonical_fields_claims_and_live_binding_on_cache_hit(self):
        result = self._acquire(mtls_proof_of_possession=True,
                       claims_challenge='{"access_token":{"custom":{"essential":true}}}')
        call = self.token_requests[0]
        self.assertEqual(call["url"], "https://region.login.example/" + TENANT + "/oauth2/v2.0/token")
        form = parse_qs(call["body"].decode())
        self.assertEqual(form["client_id"], [CLIENT])
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["scope"], ["https://vault.example/.default"])
        self.assertEqual(form["token_type"], ["mtls_pop"])
        claims = json.loads(form["claims"][0])
        self.assertEqual(claims["access_token"]["xms_cc"]["values"], ["CP1"])
        self.assertTrue(claims["access_token"]["custom"]["essential"])
        self.assertFalse({"req_cnf", "client_assertion", "client_secret"} & form.keys())
        self.assertIn("client-request-id", call["headers"])
        self.assertNotIn("Authorization", call["headers"])
        second = self._acquire(mtls_proof_of_possession=True)
        third = self._acquire(mtls_proof_of_possession=True)
        self.assertEqual(third["token_source"], "cache")
        self.assertIs(second["binding_certificate"], third["binding_certificate"])
        self.assertNotIn("binding_certificate", self.cache.serialize())
        self.assertNotIn("store_uri", self.cache.serialize())
        with result["binding_certificate"].open_session() as session:
            answer = session.get("https://vault.example/secret", headers={
        "Authorization": "mtls_pop opaque-unit-token", "x-ms-tokenboundauth": "true"})
            self.assertEqual(answer.json(), {"value": "unit-resource"})

    def test_ordinary_bearer_over_mtls_and_pop_caches_cannot_satisfy_each_other(self):
        with patch("msal.managed_identity._obtain_token",
           return_value=dict(access_token="ordinary", token_type="Bearer", expires_in=3600)):
            self.assertEqual(self._acquire()["access_token"], "ordinary")
        pop = self._acquire(mtls_proof_of_possession=True)
        bearer = self._acquire(request_over_mtls=True)
        self.assertIsNotNone(pop["binding_certificate"])
        self.assertIsNone(bearer["binding_certificate"])
        with patch("msal.managed_identity._obtain_token") as legacy:
            self.assertEqual(self._acquire()["access_token"], "ordinary")
            legacy.assert_not_called()
        self.assertEqual(self._acquire(request_over_mtls=True)["token_source"], "cache")
        self.assertEqual(len(self.token_requests), 2)

    def test_all_mode_cache_orders_remain_isolated(self):
        options = dict(ordinary={}, pop={"mtls_proof_of_possession": True},
                       bearer={"request_over_mtls": True})
        for order in permutations(options):
            with self.subTest(order=order):
                self.cache = self.client._token_cache = SerializableTokenCache()
                before = len(self.token_requests)
                with patch("msal.managed_identity._obtain_token", return_value=dict(
                        access_token="ordinary", token_type="Bearer", expires_in=3600)):
                    for mode in order:
                        result = self._acquire(**options[mode])
                        self.assertEqual(result["token_type"].lower(), "mtls_pop" if mode == "pop" else "bearer")
                    for mode in reversed(order):
                        result = self._acquire(**options[mode])
                        self.assertEqual(result["token_source"], "cache")
                        self.assertEqual(result["token_type"].lower(), "mtls_pop" if mode == "pop" else "bearer")
                        self.assertEqual(result.get("binding_certificate") is not None, mode == "pop")
                self.assertEqual(len(self.token_requests), before + 2)

    def test_keyguard_failure_preserves_required_strength_and_native_category(self):
        from msal._native_mtls import _NativeKeyError
        denied = _NativeKeyError("Unit protected key unavailable", native_code=7, system_error=123)
        with patch("msal._managed_identity_v2._NativeKey", side_effect=denied):
            with self.assertRaises(_V2Error) as caught:
                self._acquire(mtls_proof_of_possession=True, minimum_binding_strength=3)
        self.assertEqual(caught.exception.stage, "keyguard")
        self.assertEqual(caught.exception.error_code, "minimum_binding_strength_not_met")
        self.assertEqual(caught.exception.native_code, 7)
        self.assertEqual(caught.exception.system_error, 123)
        self.http.post.assert_not_called()
        self.runtime.request.assert_not_called()

    def test_force_refresh_remints_leaf_but_reuses_fresh_attestation_generation(self):
        first = self._acquire(mtls_proof_of_possession=True, with_attestation_support=True)
        second = self._acquire(mtls_proof_of_possession=True, with_attestation_support=True, force_refresh=True)
        self.assertNotEqual(first["binding_certificate"].key_id, second["binding_certificate"].key_id)
        self.assertEqual(self.http.post.call_count, 2)
        self.attestation.attest.assert_called_once_with(
            "https://westus2.attestation.microsoft.com", self.signer, TENANT)
        self.assertEqual(first["binding_certificate"]._key.generation_id,
                 second["binding_certificate"]._key.generation_id)

    def test_claims_are_redacted_from_cache_diagnostics_without_losing_partitioning(self):
        challenge = '{"access_token":{"private":{"value":"private-challenge-sentinel"}}}'
        with self.assertLogs("msal.token_cache", level="DEBUG") as captured:
            self._acquire(mtls_proof_of_possession=True, claims_challenge=challenge)
        self.assertNotIn("private-challenge-sentinel", "\n".join(captured.output))
        before = len(self.token_requests)
        self._acquire(mtls_proof_of_possession=True)
        self.assertEqual(len(self.token_requests), before + 1)

    def test_forced_ordinary_challenge_still_identifies_the_cached_token(self):
        with patch("msal.managed_identity._obtain_token", return_value=dict(
                access_token="challenged-token", token_type="Bearer", expires_in=3600)) as legacy:
            self._acquire()
            self._acquire(force_refresh=True, claims_challenge='{"access_token":{}}')
        self.assertEqual(legacy.call_count, 2)
        self.assertEqual(legacy.call_args.kwargs["access_token_sha256_to_refresh"],
                         hashlib.sha256(b"challenged-token").hexdigest())

    def test_transient_ests_status_retries_same_binding_and_obeys_post_policy(self):
        from urllib3.util.retry import Retry
        self.http.mount("https://", requests.adapters.HTTPAdapter(max_retries=Retry(
            total=2, status_forcelist=(503,), allowed_methods=frozenset(("POST",)))))
        unavailable = response({}, status=503)
        unavailable._content = b"temporary non-JSON response"
        self.runtime.request.side_effect = [
            unavailable, response(dict(access_token="retried", token_type="mtls_pop", expires_in=3600))]
        self.assertEqual(self._acquire(mtls_proof_of_possession=True)["access_token"], "retried")
        self.assertEqual(self.runtime.request.call_count, 2)
        self.http.post.assert_called_once()
        self.http.mount("https://", requests.adapters.HTTPAdapter(max_retries=Retry(
            total=2, status_forcelist=(503,))))
        self.runtime.request.reset_mock()
        self.runtime.request.side_effect = [unavailable]
        with self.assertRaises(_V2Error) as caught:
            self._acquire(mtls_proof_of_possession=True, force_refresh=True)
        self.assertEqual(caught.exception.error_code, "request_failed")
        self.runtime.request.assert_called_once()

    def test_default_ests_retries_are_bounded_and_do_not_retry_oauth_denials(self):
        for status, expected in ((503, 4), (429, 4), (400, 1)):
            with self.subTest(status=status), patch("msal._managed_identity_v2._Deadline.wait"):
                self.runtime.request.reset_mock()
                self.runtime.request.side_effect = None
                self.runtime.request.return_value = response(dict(error="unauthorized_client"), status=status)
                before = self.http.post.call_count
                with self.assertRaises(_V2Error):
                    self._acquire(mtls_proof_of_possession=True, force_refresh=True)
                self.assertEqual(self.runtime.request.call_count, expected)
                self.assertEqual(self.http.post.call_count, before + 1)

    def test_attestation_denial_prevents_credential_issuance(self):
        self.attestation.attest.side_effect = _V2Error(
            "unit denial", stage="attestation", error_code="attestation_failed")
        with self.assertRaises(_V2Error):
            self._acquire(mtls_proof_of_possession=True, with_attestation_support=True)
        self.http.post.assert_not_called()
        self.runtime.request.assert_not_called()

    def test_invalid_client_remints_once_but_wrong_token_types_never_enter_cache(self):
        self.next_tokens = [dict(error="invalid_client"), dict(
            access_token="recovered", token_type="mtls_pop", expires_in=3600)]
        result = self._acquire(mtls_proof_of_possession=True)
        self.assertEqual(result["access_token"], "recovered")
        self.assertEqual(self.http.post.call_count, 2)
        for token_type in (None, "pop", "bearer"):
            with self.subTest(token_type=token_type):
                self.next_tokens = [dict(access_token="wrong-type", token_type=token_type, expires_in=3600)]
                with self.assertRaises(_V2Error):
                    self._acquire(mtls_proof_of_possession=True, force_refresh=True)
                self.assertNotIn("wrong-type", self.cache.serialize())

    def test_peer_trust_errors_are_not_retried_and_foreign_resource_origins_are_rejected(self):
        error = _NativeMtlsError("unit trust failure", native_code=8,
                         openssl_error=(20 << 23) | 134)
        self.runtime.request.side_effect = error
        with self.assertRaises(_NativeMtlsError):
            self._acquire(mtls_proof_of_possession=True)
        self.assertEqual(self.http.post.call_count, 1)
        self.runtime.request.side_effect = self._native_request
        result = self._acquire(mtls_proof_of_possession=True)
        with result["binding_certificate"].open_session() as session:
            count = self.runtime.request.call_count
            with self.assertRaises(_V2Error):
                session.get("https://unrelated.example/secret",
                            headers={"Authorization": "mtls_pop unit"})
            self.assertEqual(self.runtime.request.call_count, count)

    def test_disabled_or_invalid_modes_do_no_http_or_native_work(self):
        for options in (dict(mtls_proof_of_possession=True, request_over_mtls=True),
                dict(with_attestation_support=True),
                dict(mtls_proof_of_possession=True, minimum_binding_strength=2)):
            with self.subTest(options=options), self.assertRaises(ManagedIdentityError):
                self._acquire(**options)
        for value in ("1", "TRUE"):
            with patch.dict("os.environ", MSAL_MI_DISABLE_IMDS_V2=value):
                with self.assertRaises(_V2Error) as caught:
                    self._acquire(mtls_proof_of_possession=True, minimum_binding_strength=3)
                self.assertEqual(caught.exception.error_code, "minimum_binding_strength_not_met")
        self.http.get.assert_not_called()
        self.http.post.assert_not_called()
        self.runtime.request.assert_not_called()

    def test_wrapped_binding_reset_retries_once_but_unrelated_oauth_failure_does_not(self):
        reset = requests.ConnectionError("unit wrapper")
        reset.__cause__ = ConnectionResetError(10054, "unit reset")
        self.runtime.request.side_effect = [reset, response(dict(
            access_token="recovered", token_type="mtls_pop", expires_in=3600))]
        self.assertEqual(self._acquire(mtls_proof_of_possession=True)["access_token"], "recovered")
        self.assertEqual(self.http.post.call_count, 2)
        self.runtime.request.side_effect = self._native_request
        self.next_tokens = [dict(error="unauthorized_client")]
        before = self.http.post.call_count
        with self.assertRaises(_V2Error) as caught:
            self._acquire(mtls_proof_of_possession=True, force_refresh=True)
        self.assertEqual(caught.exception.error_code, "unauthorized_client")
        self.assertEqual(self.http.post.call_count, before + 1)

    def test_native_certificate_alerts_recover_once_and_nested_trust_failure_never_retries(self):
        for reason in (1042, 1043, 1044, 1045, 1046, 1048):
            with self.subTest(reason=reason):
                self.runtime.request.side_effect = [
                    _NativeMtlsError("Unit peer binding rejection", native_code=8,
                                     openssl_error=(20 << 23) | reason),
                    response(dict(access_token="recovered", token_type="mtls_pop", expires_in=3600))]
                before = self.http.post.call_count
                self.assertEqual(self._acquire(mtls_proof_of_possession=True, force_refresh=True)[
                    "access_token"], "recovered")
                self.assertEqual(self.http.post.call_count, before + 2)
        reset = _NativeMtlsError("Unit outer reset", system_error=10054)
        reset.__cause__ = _NativeMtlsError("Unit inner trust failure", openssl_error=(20 << 23) | 134)
        self.runtime.request.side_effect = reset
        before = self.http.post.call_count
        with self.assertRaises(_NativeMtlsError):
            self._acquire(mtls_proof_of_possession=True, force_refresh=True)
        self.assertEqual(self.http.post.call_count, before + 1)

    def test_async_cancellation_retains_native_slot_chain_and_key_until_worker_exits(self):
        binding = self._acquire(mtls_proof_of_possession=True)["binding_certificate"]
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("The unit native-operation gate timed out.")
            return response({"value": "unit-resource"})

        self.runtime.request.side_effect = delayed

        async def scenario():
            session = binding.open_async_session(max_concurrency=1)
            try:
                first = asyncio.create_task(session.get("https://vault.example/secret"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                second = asyncio.create_task(session.get("https://vault.example/secret"))
                await asyncio.sleep(0)
                self.assertFalse(second.done())
                self.assertEqual(len(session._inflight), 1)
                binding.close()
                self.assertTrue(binding._chain_path.exists())
                self.assertIsNotNone(binding._key)
                release.set()
                with self.assertRaises(_V2Error):
                    await second
                self.assertFalse(binding._chain_path.exists())
                self.assertIsNone(binding._key)
            finally:
                release.set()
                await session.aclose()

        asyncio.run(scenario())

    def test_restart_reattaches_persisted_leaf_before_returning_serialized_pop_token(self):
        from msal._managed_identity_v2 import _BINDINGS
        store = Mock()
        store.read.return_value = None
        with patch("msal._managed_identity_store._CertificateStore", return_value=store):
            first = self._acquire(mtls_proof_of_possession=True)
            store.read.return_value = store.write.call_args.args[2]
            _BINDINGS.clear()
            client = ManagedIdentityClient(SystemAssignedManagedIdentity(), http_client=self.http,
                                            token_cache=self.cache, client_capabilities=["CP1"])
            before = self.http.post.call_count
            second = client.acquire_token_for_client(
                resource="https://vault.example", mtls_proof_of_possession=True)
            self.assertEqual(second["token_source"], "cache")
            self.assertIsNot(first["binding_certificate"], second["binding_certificate"])
            self.assertEqual(first["binding_certificate"].key_id, second["binding_certificate"].key_id)
            self.assertEqual(self.http.post.call_count, before)

    def test_failed_forced_refresh_cannot_restore_evicted_persisted_generation(self):
        store = Mock()
        store.read.return_value = None
        with patch("msal._managed_identity_store._CertificateStore", return_value=store):
            first = self._acquire(mtls_proof_of_possession=True)
            store.read.return_value = store.write.call_args.args[2]
            self.http.post.side_effect = _V2Error("Unit issuance denial", stage="imds", error_code="request_failed")
            with self.assertRaises(_V2Error):
                self._acquire(mtls_proof_of_possession=True, force_refresh=True)
            self.http.post.side_effect = self._issue
            second = self._acquire(mtls_proof_of_possession=True)
            self.assertNotEqual(first["binding_certificate"].key_id, second["binding_certificate"].key_id)
            self.assertEqual(second["token_source"], "identity_provider")

    def test_failed_invalidation_is_shared_with_another_client(self):
        from msal._managed_identity_v2 import _BINDINGS, _FORCE_MINT
        for forced in (True, False):
            with self.subTest(forced=forced):
                _BINDINGS.clear()
                _FORCE_MINT.clear()
                self.client._v2_client = None
                self.cache = self.client._token_cache = SerializableTokenCache()
                store = Mock()
                store.read.return_value = None
                store.write.return_value = False
                with patch("msal._managed_identity_store._CertificateStore", return_value=store):
                    first = self._acquire(mtls_proof_of_possession=True)
                    store.read.return_value = store.write.call_args.args[2]
                    other_cache = SerializableTokenCache()
                    other_cache.deserialize(self.cache.serialize())
                    other = ManagedIdentityClient(SystemAssignedManagedIdentity(), http_client=self.http,
                                                  token_cache=other_cache, client_capabilities=["CP1"])
                    self.http.post.side_effect = _V2Error(
                        "Unit issuance denial", stage="imds", error_code="request_failed")
                    if not forced:
                        self.next_tokens = [dict(error="invalid_client")]
                        self.cache.deserialize("{}")
                    with self.assertRaises(_V2Error):
                        self._acquire(mtls_proof_of_possession=True, force_refresh=forced)
                    # The mocked store still returns the deleted leaf: deletion failed.
                    with self.assertRaises(_V2Error):
                        other.acquire_token_for_client(
                            resource="https://vault.example", mtls_proof_of_possession=True)
                    self.http.post.side_effect = self._issue
                    recovered = other.acquire_token_for_client(
                        resource="https://vault.example", mtls_proof_of_possession=True)
                    self.assertNotEqual(first["binding_certificate"].key_id,
                                        recovered["binding_certificate"].key_id)
                    self.assertEqual(recovered["token_source"], "identity_provider")
                    _BINDINGS.clear()
                    other._v2_client = None
                    after_eviction = other.acquire_token_for_client(
                        resource="https://vault.example", mtls_proof_of_possession=True)
                    self.assertNotEqual(first["binding_certificate"].key_id,
                                        after_eviction["binding_certificate"].key_id)
                    self.assertEqual(after_eviction["token_source"], "identity_provider")

    def test_incompatible_transport_fails_before_imds_or_protected_key_work(self):
        client = ManagedIdentityClient(SystemAssignedManagedIdentity(), http_client=Mock())
        with self.assertRaises(_V2Error) as caught:
            client.acquire_token_for_client(resource="https://vault.example", mtls_proof_of_possession=True)
        self.assertEqual(caught.exception.error_code, "unsupported_transport")
        self.http.get.assert_not_called()
        self.http.post.assert_not_called()

    def test_v1_only_explicit_request_reports_unsupported_before_loading_native(self):
        self.http.get.return_value = response({}, status=404)
        self.http.get.side_effect = None
        # A terminal probe avoids the normal credential-operation 404 retry policy.
        with patch("msal._managed_identity_v2._ImdsClient.platform", side_effect=_V2Error(
                "Unit v1-only host", stage="imds", error_code="imds_v2_not_supported")):
            with patch("msal._managed_identity_v2._load_runtime") as load:
                with self.assertRaises(_V2Error) as caught:
                    self._acquire(mtls_proof_of_possession=True)
                self.assertEqual(caught.exception.error_code, "imds_v2_not_supported")
                load.assert_not_called()


class CapabilityDiscoveryTests(unittest.TestCase):
    def setUp(self):
        from msal.managed_identity import DEFAULT_TO_VM
        self.http = Mock()
        self.key = Mock()
        self.client = ManagedIdentityClient(SystemAssignedManagedIdentity(), http_client=self.http)
        for target, arguments in (
                ("msal.managed_identity.get_managed_identity_source", dict(return_value=DEFAULT_TO_VM)),
                ("msal._managed_identity_v2._load_runtime", dict(return_value=Mock())),
                ("msal._managed_identity_v2._NativeKey", dict(return_value=self.key))):
            started = patch(target, **arguments)
            self.addCleanup(started.stop)
            started.start()
        environment = patch.dict("os.environ", MSAL_MI_DISABLE_IMDS_V2="0")
        environment.start()
        self.addCleanup(environment.stop)

    def test_v2_first_discovery_proves_keyguard_without_issuing_and_does_not_reroute_ordinary_calls(self):
        self.http.get.return_value = response({}, status=400)
        result = self.client.get_managed_identity_capabilities(timeout=2)
        self.assertEqual(result["source"], "IMDS")
        self.assertEqual(result["max_supported_binding_strength"], 3)
        self.assertTrue(result["is_mtls_pop_supported_by_host"])
        self.assertTrue(self.http.get.call_args.args[0].endswith("/getplatformmetadata"))
        self.assertNotIn("Metadata", self.http.get.call_args.kwargs["headers"])
        self.key.close.assert_called_once()
        self.http.post.assert_not_called()
        result["max_supported_binding_strength"] = 99
        self.assertEqual(self.client.get_managed_identity_capabilities()["max_supported_binding_strength"], 3)
        self.http.get.assert_called_once()
        with patch("msal.managed_identity._obtain_token", return_value=dict(
                access_token="ordinary", expires_in=3600, token_type="Bearer")) as legacy:
            self.assertEqual(self.client.acquire_token_for_client(resource="resource")["access_token"], "ordinary")
            legacy.assert_called_once()
        with self.assertRaises(ManagedIdentityError):
            self.client.get_managed_identity_capabilities(timeout=0)

    def test_v1_fallback_keeps_remaining_budget_and_kill_switch_bypasses_cached_v2(self):
        self.http.get.side_effect = [
            response({}, status=404), response({}, status=400),
            response(dict(osType="Windows", securityProfile={"securityType": "TrustedLaunch"}))]
        self.assertEqual(self.client.get_managed_identity_capabilities(timeout=2)[
            "max_supported_binding_strength"], 1)
        calls = self.http.get.call_args_list
        self.assertTrue(calls[0].args[0].endswith("/getplatformmetadata"))
        self.assertTrue(calls[1].args[0].endswith("/oauth2/token"))
        self.assertEqual(calls[1].kwargs["params"]["api-version"], "2018-02-01")
        self.assertLessEqual(calls[1].kwargs["timeout"], calls[0].kwargs["timeout"])
        self.http.get.reset_mock()
        self.http.get.side_effect = None
        self.http.get.return_value = response({}, status=400)
        with patch.dict("os.environ", MSAL_MI_DISABLE_IMDS_V2="TRUE"):
            result = self.client.get_managed_identity_capabilities(timeout=2)
            self.assertEqual(result["max_supported_binding_strength"], 0)
            self.http.get.assert_called_once()
            self.assertTrue(self.http.get.call_args.args[0].endswith("/oauth2/token"))
        self.key.close.assert_not_called()

    def test_timed_out_waiter_does_not_cancel_owner_or_poison_successful_cache(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def gated(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("The unit discovery gate timed out.")
            return response({}, status=400)

        self.http.get.side_effect = gated
        with ThreadPoolExecutor(max_workers=1) as callers:
            owner = callers.submit(self.client.get_managed_identity_capabilities, timeout=4)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(_V2Error) as caught:
                    self.client.get_managed_identity_capabilities(timeout=0.01)
                self.assertEqual(caught.exception.error_code, "timeout")
                self.assertFalse(owner.done())
            finally:
                release.set()
            self.assertEqual(owner.result(timeout=2)["max_supported_binding_strength"], 3)
        self.assertEqual(self.client.get_managed_identity_capabilities()["max_supported_binding_strength"], 3)
        self.http.get.assert_called_once()

    def test_longer_waiter_restarts_after_short_owner_deadline_expires(self):
        from msal._managed_identity_v2 import _CapabilityDiscovery
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def short_probe(discovery, base, disabled, budget):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
                raise _V2Error("Unit owner timeout", stage="discovery", error_code="timeout")
            return discovery._result("IMDS", 3)

        original = _Deadline.remaining

        def remaining(budget):
            if threading.current_thread().name.startswith("long-waiter"):
                joined.set()
            return original(budget)

        with patch.object(_CapabilityDiscovery, "_probe", short_probe), \
                patch.object(_Deadline, "remaining", remaining), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="short-owner") as owners, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="long-waiter") as waiters:
            owner = owners.submit(self.client.get_managed_identity_capabilities, timeout=0.01)
            self.assertTrue(entered.wait(2))
            waiter = waiters.submit(self.client.get_managed_identity_capabilities, timeout=2)
            self.assertTrue(joined.wait(1))
            with self.assertRaises(_V2Error):
                owner.result(timeout=1)
            release.set()
            self.assertEqual(waiter.result(timeout=1)["max_supported_binding_strength"], 3)


class NativeAttestationOwnershipTests(unittest.TestCase):
    def test_initialization_and_empty_or_invalid_outputs_preserve_native_cleanup(self):
        from msal._native_attestation import _NativeAttestation
        native = _NativeAttestation.__new__(_NativeAttestation)
        library = native._library = Mock()
        key = Mock()
        key.with_borrowed_handle.side_effect = lambda operation: operation(456)
        library.InitAttestationLib.return_value = 7
        with self.assertRaises(_V2Error):
            native.attest("https://maa.example", key, TENANT)
        library.AttestKeyGuardImportKey.assert_not_called()
        library.FreeAttestationToken.assert_not_called()
        library.UninitAttestationLib.assert_not_called()
        library.InitAttestationLib.return_value = 0
        for contents in (b"", b"\xff", b"x" * 1048577):
            with self.subTest(length=len(contents)):
                library.reset_mock()
                token = ctypes.create_string_buffer(contents)
                def attest(*arguments):
                    ctypes.cast(arguments[4], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(token)
                    return 0
                library.AttestKeyGuardImportKey.side_effect = attest
                with self.assertRaises(_V2Error):
                    native.attest("https://maa.example", key, TENANT)
                library.FreeAttestationToken.assert_called_once()
                library.UninitAttestationLib.assert_called_once()

    def test_native_call_uses_tenant_metadata_and_frees_before_uninitializing(self):
        from msal._native_attestation import _NativeAttestation
        native = _NativeAttestation.__new__(_NativeAttestation)
        library = native._library = Mock()
        library.InitAttestationLib.return_value = 0
        token = ctypes.create_string_buffer(b"header.payload.signature")
        key = Mock()
        key.with_borrowed_handle.side_effect = lambda operation: operation(456)

        def attest(endpoint, auth, payload, handle, output, client_id):
            self.assertEqual((endpoint, auth, payload, handle, client_id),
                     (b"https://maa.example", None, None, 456, TENANT.encode()))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(token)
            return 0

        library.AttestKeyGuardImportKey.side_effect = attest
        self.assertEqual(native.attest("https://maa.example", key, TENANT), token.value.decode())
        library.FreeAttestationToken.assert_called_once()
        self.assertEqual([call[0] for call in library.mock_calls][-2:],
                 ["FreeAttestationToken", "UninitAttestationLib"])
        library.reset_mock()
        library.AttestKeyGuardImportKey.side_effect = lambda *args: (
            ctypes.cast(args[4], ctypes.POINTER(ctypes.c_void_p)).__setitem__(0, ctypes.addressof(token)) or 7)
        with self.assertRaises(_V2Error):
            native.attest("https://maa.example", key, TENANT)
        library.FreeAttestationToken.assert_called_once()
        library.UninitAttestationLib.assert_called_once()

@unittest.skipUnless(sys.platform == "win32", "Actual Windows certificate-store API fixture.")
class WindowsCertificateStoreTests(unittest.TestCase):
    """Actual public-certificate store API; the signer is a software unit fixture."""
    def setUp(self):
        from msal._managed_identity_store import _CertificateStore
        environment = patch.dict("os.environ", MSAL_MI_DISABLE_PERSISTENT_CERT_CACHE="0")
        environment.start()
        self.addCleanup(environment.stop)
        self.store = _CertificateStore()
        self.alias = "python-unit-" + uuid.uuid4().hex
        self.addCleanup(self.store.remove, self.alias)
        self.key = SoftwareSignerFixture()
        self.key.store_uri = "cng://?key=microsoft-identity-native-vbs-unit-" + uuid.uuid4().hex
        certificate = x509.load_der_x509_certificate(base64.b64decode(self.key.certificate()), default_backend())
        self.credential = dict(
            leaf_der=certificate.public_bytes(serialization.Encoding.DER),
            client_id=CLIENT, token_endpoint="https://login.example/" + TENANT + "/oauth2/v2.0/token")
        self.expiry = certificate.not_valid_after_utc.timestamp()

    def test_roundtrip_exact_24_hour_cutoff_generation_mismatch_and_opt_out(self):
        self.store.write(self.alias, self.key, self.credential)
        with patch("msal._managed_identity_store.time.time", return_value=self.expiry - 86400):
            self.assertIsNone(self.store.read(self.alias, self.key))

        with patch("msal._managed_identity_store.time.time", return_value=self.expiry - 86401):
            found = self.store.read(self.alias, self.key)
            self.assertIsNotNone(found)
            self.assertEqual(found["leaf_der"], self.credential["leaf_der"])
            self.assertEqual(found["client_id"], CLIENT)
        self.assertIsNone(self.store.read(self.alias + "-unrelated", self.key))
        self.assertIsNone(self.store.read(self.alias, SoftwareSignerFixture()))
        with patch.dict("os.environ", MSAL_MI_DISABLE_PERSISTENT_CERT_CACHE="TRUE"):
            self.assertIsNone(self.store.read(self.alias, self.key))
        self.store.remove(self.alias, leaf_der=b"not-the-current-leaf")
        self.assertIsNotNone(self.store.read(self.alias, self.key))
        self.store.remove(self.alias, leaf_der=self.credential["leaf_der"])
        self.assertIsNone(self.store.read(self.alias, self.key))


    def test_pruning_removes_only_older_exact_alias_leaf_and_keeps_newer(self):
        self.store.write(self.alias, self.key, self.credential)
        newer = x509.load_der_x509_certificate(
            base64.b64decode(self.key.certificate(hours=49)), default_backend())
        newest = dict(self.credential, leaf_der=newer.public_bytes(serialization.Encoding.DER),
                      expires_on=newer.not_valid_after_utc.timestamp())
        self.store.write(self.alias, self.key, newest)
        self.assertEqual(self.store.read(self.alias, self.key)["leaf_der"], newest["leaf_der"])
        self.store.remove(self.alias, leaf_der=newest["leaf_der"])
        self.assertIsNone(self.store.read(self.alias, self.key))


class BindingNegativeControlTests(unittest.TestCase):
    def test_only_exact_401_unauthorized_can_satisfy_live_negative_control(self):
        from tests.test_mi_v2_e2e import assert_binding_rejected
        assert_binding_rejected(response({"error": {"code": "Unauthorized"}}, status=401))
        for status in (200, 302, 403, 429, 500, 503):
            with self.subTest(status=status), self.assertRaises(AssertionError):
                assert_binding_rejected(response({"error": {"code": "Unauthorized"}}, status=status))
        for body in ({}, {"error": {}}, {"error": {"code": "Forbidden"}}, []):
            with self.subTest(body=body), self.assertRaises(AssertionError):
                assert_binding_rejected(response(body, status=401))
