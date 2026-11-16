import base64
import asyncio
import ctypes
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote, urlencode
from types import SimpleNamespace
import uuid

from msal._native_mtls import (
    _ARTIFACTS, _Error, _https_origin, _MAX_BODY_BYTES, _NativeMtlsError,
    _NativeKey, _NativeRuntime, _PublicKey, _Request, _Response, _timeouts, _verified_manifest,
)
from msal.oauth2cli.oauth2 import Client
from msal._managed_identity_v2 import _create_csr
from msal.managed_identity import ManagedIdentityError


class NativeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _NativeRuntime.__new__(_NativeRuntime)
        self.runtime.directory = Path(r"C:\verified-native")
        self.library = self.runtime._library = Mock()
        self.library.microsoft_identity_native_request.return_value = 1
        self.buffers = []

    def _respond(self, request, response, error):
        output = ctypes.cast(response, ctypes.POINTER(_Response)).contents
        headers = ctypes.create_string_buffer(
            b"Content-Type: application/json\r\nContent-Length: 2\r\n")
        body = ctypes.create_string_buffer(b"{}")
        self.buffers.extend((headers, body))
        output.http_status = 200
        output.headers = ctypes.addressof(headers)
        output.headers_length = len(headers.value)
        output.body = ctypes.addressof(body)
        output.body_length = 2
        return 1

    def _request(self, **kwargs):
        parameters = dict(
            store_uri="cng://?key=owned",
            certificate_path=r"C:\public-chain.pem",
            ca_bundle_path=r"C:\trust.pem",
            method="POST",
            url="https://login.example/tenant/oauth2/v2.0/token",
            headers={"Accept": "application/json"},
            body=b"grant_type=client_credentials")
        parameters.update(kwargs)
        return self.runtime.request(**parameters)

    def test_buffers_remain_live_and_success_is_freed_once_after_copy(self):
        def respond(request, response, error):
            value = ctypes.cast(request, ctypes.POINTER(_Request)).contents
            self.assertEqual(value.struct_size, ctypes.sizeof(_Request))
            self.assertEqual(value.provider_module_path,
                             str(self.runtime.directory / _ARTIFACTS[-2]).encode("utf-8"))
            self.assertEqual(value.host, b"login.example")
            self.assertEqual(value.target, b"/tenant/oauth2/v2.0/token")
            self.assertEqual(value.headers[0].name, b"Accept")
            self.assertEqual(ctypes.string_at(value.body, value.body_size),
                             b"grant_type=client_credentials")
            self.assertEqual((value.connect_timeout_ms, value.io_timeout_ms), (1000, 2000))
            self.assertEqual(bytes(ctypes.cast(response, ctypes.POINTER(_Response)).contents),
                             bytes(ctypes.sizeof(_Response)))
            return self._respond(request, response, error)

        self.library.microsoft_identity_native_request.side_effect = respond
        response = self._request(timeout=(1, 2))
        self.library.microsoft_identity_native_response_free.assert_called_once()
        self.assertEqual(response.json(), {})
        self.assertEqual(response.headers["content-type"], "application/json")
        self.buffers[1].value = b"xx"
        self.assertEqual(response.content, b"{}")

    def test_oversized_or_null_outputs_fail_before_copy_and_are_freed_once(self):
        mutations = (
            ("headers_length", 16387), ("body_length", _MAX_BODY_BYTES + 1),
            ("headers", None), ("body", None), ("http_status", 0))
        for field, value in mutations:
            with self.subTest(field=field):
                self.library.reset_mock()

                def respond(request, response, error):
                    self._respond(request, response, error)
                    setattr(ctypes.cast(response, ctypes.POINTER(_Response)).contents,
                            field, value)
                    return 1

                self.library.microsoft_identity_native_request.side_effect = respond
                with patch("msal._native_mtls.ctypes.string_at") as copy:
                    with self.assertRaises(_NativeMtlsError):
                        self._request()
                    copy.assert_not_called()
                self.library.microsoft_identity_native_response_free.assert_called_once()

    def test_native_failure_is_redacted_and_does_not_free_unowned_storage(self):
        def fail(request, response, error):
            value = ctypes.cast(error, ctypes.POINTER(_Error)).contents
            value.code, value.system_error = 8, 10054
            value.message = b"sensitive-native-payload"
            return 0

        self.library.microsoft_identity_native_request.side_effect = fail
        with self.assertRaises(_NativeMtlsError) as caught:
            self._request()
        self.assertEqual(caught.exception.native_code, 8)
        self.assertEqual(caught.exception.system_error, 10054)
        self.assertNotIn("sensitive-native-payload", str(caught.exception))
        self.library.microsoft_identity_native_response_free.assert_not_called()

    def test_input_failures_do_not_enter_native_code(self):
        for kwargs in (
                {"store_uri": "cng://?key=owned\0unseen"},
                {"body": b"x" * (_MAX_BODY_BYTES + 1)},
                {"body": iter([b"stream"])},
                {"headers": {"X-{}".format(index): "value" for index in range(17)}},
                {"timeout": float("nan")}, {"timeout": False}):
            with self.subTest(kwargs=list(kwargs)):
                with self.assertRaises(_NativeMtlsError):
                    self._request(**kwargs)
        self.library.microsoft_identity_native_request.assert_not_called()


class NativeKeyOwnershipTests(unittest.TestCase):
    """Mocked ABI ownership/signing evidence, not proof of VBS protection."""

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.fixture_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.library = Mock()
        self.runtime = _NativeRuntime.__new__(_NativeRuntime)
        self.runtime._library = self.library
        self.library.microsoft_identity_native_key_open_protected.side_effect = self._open
        self.library.microsoft_identity_native_key_public.side_effect = self._public
        self.library.microsoft_identity_native_key_sign_sha256_pss.side_effect = self._sign
        self.library.microsoft_identity_native_key_borrow_handle.side_effect = self._borrow

    def _open(self, name, output, error):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 1

    def _public(self, handle, output, error):
        public = ctypes.cast(output, ctypes.POINTER(_PublicKey)).contents
        self.assertEqual((public.struct_size, public.abi_version), (2068, 1))
        numbers = self.fixture_key.public_key().public_numbers()
        public.bits, public.exponent, public.modulus_length = 2048, numbers.e, 256
        public.modulus[:256] = numbers.n.to_bytes(256, "big")
        return 1

    def _sign(self, handle, digest, length, signature, capacity, written, error):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, utils
        self.assertEqual((length, capacity), (32, 256))
        result = self.fixture_key.sign(
            ctypes.string_at(digest, length),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            utils.Prehashed(hashes.SHA256()))
        ctypes.memmove(signature, result, len(result))
        ctypes.cast(written, ctypes.POINTER(ctypes.c_size_t))[0] = len(result)
        return 1

    @staticmethod
    def _borrow(handle, output, error):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_size_t))[0] = 456
        return 1

    def _key(self):
        key = _NativeKey(self.runtime, "microsoft-identity-native-vbs-fixture")
        self.addCleanup(key.close)
        return key

    def test_public_key_signing_and_borrow_keep_one_owner_until_close(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, utils
        key = self._key()
        digest = hashlib.sha256(b"public CSR information").digest()
        signature = key.sign_sha256_pss(digest)
        key.public_key.verify(
            signature, digest,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            utils.Prehashed(hashes.SHA256()))
        self.assertEqual(key.with_borrowed_handle(lambda value: value), 456)
        self.library.microsoft_identity_native_key_free.assert_not_called()
        key.close()
        key.close()
        self.library.microsoft_identity_native_key_free.assert_called_once()
        for operation in (lambda: key.sign_sha256_pss(digest),
                          lambda: key.with_borrowed_handle(lambda value: value)):
            with self.assertRaisesRegex(_NativeMtlsError, "closed"):
                operation()

    def test_bad_public_outputs_close_the_object_without_signing(self):
        for field, value in (("struct_size", 0), ("abi_version", 2),
                             ("modulus_length", 2049), ("bits", 1024), ("exponent", 2)):
            with self.subTest(field=field):
                self.library.reset_mock()

                def malformed(handle, output, error):
                    self._public(handle, output, error)
                    setattr(ctypes.cast(output, ctypes.POINTER(_PublicKey)).contents, field, value)
                    return 1

                self.library.microsoft_identity_native_key_public.side_effect = malformed
                with self.assertRaises(_NativeMtlsError):
                    _NativeKey(self.runtime, "microsoft-identity-native-vbs-fixture")
                self.library.microsoft_identity_native_key_free.assert_called_once()
                self.library.microsoft_identity_native_key_sign_sha256_pss.assert_not_called()

    def test_open_failure_is_redacted_and_does_not_free_an_unowned_handle(self):
        def fail(name, output, error):
            native_error = ctypes.cast(error, ctypes.POINTER(_Error)).contents
            native_error.code, native_error.system_error = 3, -2146893818
            native_error.message = b"sensitive-container-identifier"
            return 0

        self.library.microsoft_identity_native_key_open_protected.side_effect = fail
        with self.assertRaises(_NativeMtlsError) as caught:
            _NativeKey(self.runtime, "microsoft-identity-native-vbs-fixture")
        self.assertEqual(caught.exception.stage, "keyguard")
        self.assertEqual(caught.exception.system_error, -2146893818)
        self.assertNotIn("sensitive-container-identifier", str(caught.exception))
        gc.collect()
        self.library.microsoft_identity_native_key_public.assert_not_called()
        self.library.microsoft_identity_native_key_free.assert_not_called()

    def test_missing_export_fails_before_key_creation(self):
        del self.library.microsoft_identity_native_key_borrow_handle
        with self.assertRaisesRegex(_NativeMtlsError, "lacks protected-key primitives"):
            _NativeKey(self.runtime, "microsoft-identity-native-vbs-fixture")
        self.library.microsoft_identity_native_key_open_protected.assert_not_called()

    def test_borrow_blocks_close_until_the_native_consumer_finishes(self):
        from concurrent.futures import ThreadPoolExecutor
        key = self._key()
        borrowed, release, closing, closed = (threading.Event() for _ in range(4))

        def consume(handle):
            self.assertEqual(handle, 456)
            borrowed.set()
            self.assertTrue(release.wait(5))

        def close():
            closing.set()
            key.close()
            closed.set()

        with ThreadPoolExecutor(max_workers=2) as workers:
            operation = workers.submit(key.with_borrowed_handle, consume)
            try:
                self.assertTrue(borrowed.wait(5))
                cleanup = workers.submit(close)
                self.assertTrue(closing.wait(5))
                self.assertFalse(closed.wait(0.05))
                self.library.microsoft_identity_native_key_free.assert_not_called()
            finally:
                release.set()
            operation.result(timeout=5)
            cleanup.result(timeout=5)
        self.library.microsoft_identity_native_key_free.assert_called_once()

    def test_gc_frees_an_abandoned_owned_object_once(self):
        key = _NativeKey(self.runtime, "microsoft-identity-native-vbs-fixture")
        del key
        gc.collect()
        self.library.microsoft_identity_native_key_free.assert_called_once()

    def test_borrow_reentrancy_cannot_free_the_handle_before_return(self):
        key = self._key()
        with self.assertRaisesRegex(_NativeMtlsError, "handle is borrowed"):
            key.with_borrowed_handle(lambda handle: key.close())
        self.library.microsoft_identity_native_key_free.assert_not_called()
        key.close()
        self.library.microsoft_identity_native_key_free.assert_called_once()

    def test_signing_rejects_bad_digests_and_invalid_native_lengths(self):
        key = self._key()
        for digest in (b"", b"x" * 31, b"x" * 33, "x" * 32):
            with self.subTest(digest_type=type(digest), length=len(digest)):
                with self.assertRaises(_NativeMtlsError):
                    key.sign_sha256_pss(digest)
        self.library.microsoft_identity_native_key_sign_sha256_pss.assert_not_called()

        def invalid_length(handle, digest, length, signature, capacity, written, error):
            ctypes.cast(written, ctypes.POINTER(ctypes.c_size_t))[0] = 257
            return 1

        self.library.microsoft_identity_native_key_sign_sha256_pss.side_effect = invalid_length
        with self.assertRaisesRegex(_NativeMtlsError, "signature length"):
            key.sign_sha256_pss(b"x" * 32)

    def test_csr_preserves_platform_identity_cuid_der_and_pss_parameters(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.x509.oid import NameOID, SignatureAlgorithmOID
        key = self._key()
        client = "11111111-2222-3333-4444-555555555555"
        tenant = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        cuid = {"vmId": "vm-id", "vmssId": '/subscriptions/example/"escaped"',
                "ignored": "not a CUID field"}
        csr = x509.load_der_x509_csr(base64.b64decode(
            _create_csr(key, client, tenant, cuid), validate=True))
        self.assertEqual(csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, client)
        self.assertEqual(csr.subject.get_attributes_for_oid(NameOID.DOMAIN_COMPONENT)[0].value, tenant)
        self.assertEqual(csr.public_key().public_numbers(), key.public_key.public_numbers())
        self.assertEqual(csr.signature_algorithm_oid, SignatureAlgorithmOID.RSASSA_PSS)
        self.assertTrue(csr.is_signature_valid)
        key.public_key.verify(
            csr.signature, csr.tbs_certrequest_bytes,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256())
        self.assertEqual(
            json.loads(csr.attributes.get_attribute_for_oid(
                x509.ObjectIdentifier("1.3.6.1.4.1.311.90.2.10")).value),
            {"vmId": cuid["vmId"], "vmssId": cuid["vmssId"]})
        self.library.microsoft_identity_native_key_sign_sha256_pss.assert_called_once()

    def test_invalid_csr_metadata_never_reaches_the_private_operation(self):
        key = self._key()
        for client, tenant, cuid in (
                ("", "tenant", {"vmId": "vm"}),
                ("client", "tenant\0suffix", {"vmId": "vm"}),
                ("client", "t\u00e9nant", {"vmId": "vm"}),
                ("client", "tenant", "not-json"),
                ("client", "tenant", {"vmId": 123}),
                ("client", "tenant", {"vmId": ""}),
                ("client", "tenant", {"vmId": "x" * 4097})):
            with self.subTest(client=bool(client), cuid_type=type(cuid)):
                with self.assertRaises(ManagedIdentityError):
                    _create_csr(key, client, tenant, cuid)
        self.library.microsoft_identity_native_key_sign_sha256_pss.assert_not_called()

    def test_csr_omits_absent_cuid_fields(self):
        from cryptography import x509
        key = self._key()
        for cuid in ({"vmId": "vm"}, {"vmssId": "scale-set"}):
            with self.subTest(field=next(iter(cuid))):
                csr = x509.load_der_x509_csr(base64.b64decode(
                    _create_csr(key, "client", "tenant", cuid), validate=True))
                self.assertEqual(json.loads(csr.attributes.get_attribute_for_oid(
                    x509.ObjectIdentifier("1.3.6.1.4.1.311.90.2.10")).value), cuid)
                self.assertTrue(csr.is_signature_valid)


class NativeLoadingTests(unittest.TestCase):
    def test_manifest_requires_explicit_development_opt_in_and_matching_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in _ARTIFACTS:
                (root / name).write_bytes(b"fixture")
            manifest = dict(
                schema_version=1, abi_version=1, development=True,
                build_id="unit-fixture",
                files={name: hashlib.sha256(b"fixture").hexdigest() for name in _ARTIFACTS})
            (root / "native-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(_NativeMtlsError):
                _verified_manifest(root, allow_development=False)
            self.assertEqual(_verified_manifest(root, allow_development=True)[1], "unit-fixture")
            (root / _ARTIFACTS[0]).write_bytes(b"changed")
            with self.assertRaises(_NativeMtlsError):
                _verified_manifest(root, allow_development=True)

    def test_wrong_abi_or_missing_export_fails_before_requests(self):
        for wrong_version in (True, False):
            with self.subTest(wrong_version=wrong_version):
                library = Mock()
                library.microsoft_identity_native_abi_version.return_value = 2 if wrong_version else 1
                if not wrong_version:
                    del library.microsoft_identity_native_request
                with patch("msal._native_mtls.sys.platform", "win32"), patch(
                        "msal._native_mtls.sysconfig.get_platform", return_value="win-amd64"), patch(
                        "msal._native_mtls._verified_manifest",
                        return_value=(Path(r"C:\native"), "fixture")), patch.object(
                        _NativeRuntime, "_load_libraries", return_value=[library]):
                    with self.assertRaises(_NativeMtlsError):
                        _NativeRuntime(r"C:\native")

    def test_credentials_fragments_and_insecure_origins_are_rejected(self):
        for url in (
                "http://login.example/token", "https://user:pass@login.example/token",
                "https://login.example/token#fragment", "https://login.example:/token",
                "https://login.example:0/token", "https://login.example/\r\nbad"):
            with self.subTest(url=url):
                with self.assertRaises(_NativeMtlsError):
                    _https_origin(url)
        self.assertEqual(_https_origin("https://LOGIN.example:443/token")[1],
                         ("login.example", 443))

    def test_timeouts_are_bounded_and_do_not_round_to_native_default(self):
        self.assertEqual(_timeouts(0.00001), (1, 1))
        self.assertEqual(_timeouts((1, 120)), (1000, 120000))
        for value in (0, -1, 120.1, float("inf"), True, (1,), (1, None)):
            with self.subTest(value=value):
                with self.assertRaises(_NativeMtlsError):
                    _timeouts(value)


@unittest.skipUnless(
    sys.platform == "win32" and os.getenv("MSAL_TEST_NATIVE_MTLS_DIR"),
    "Set MSAL_TEST_NATIVE_MTLS_DIR to candidate Windows native artifacts.")
class WindowsRustOAuthTests(unittest.TestCase):
    """Actual software-CNG evidence, not VBS/KeyGuard or live MSI v2 evidence."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.artifacts = Path(os.environ["MSAL_TEST_NATIVE_MTLS_DIR"]).resolve(strict=True)
        cls.runtime = _NativeRuntime(cls.artifacts, allow_development=True)
        cls.key_name = "openssl-cng-integration-python-" + uuid.uuid4().hex
        cls.addClassCleanup(cls._delete_owned_key)
        cls.store_uri = "cng://?key=" + cls.key_name + "&provider=" + quote(
            "Microsoft Software Key Storage Provider", safe="")
        subprocess.run([
            str(cls.artifacts / "native_test_pki.exe"),
            str(cls.artifacts / "microsoft_identity_native_provider.dll"),
            str(cls.root), cls.key_name, "Microsoft Software Key Storage Provider",
            cls.store_uri, "pki.ready"], check=True, capture_output=True, timeout=30)

    @classmethod
    def _delete_owned_key(cls):
        ncrypt = ctypes.WinDLL("ncrypt")
        handle = ctypes.c_size_t
        ncrypt.NCryptOpenStorageProvider.argtypes = [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_uint32]
        ncrypt.NCryptOpenKey.argtypes = [handle, ctypes.POINTER(handle), ctypes.c_wchar_p,
                                       ctypes.c_uint32, ctypes.c_uint32]
        ncrypt.NCryptDeleteKey.argtypes = [handle, ctypes.c_uint32]
        ncrypt.NCryptFreeObject.argtypes = [handle]
        provider, key = handle(), handle()
        if ncrypt.NCryptOpenStorageProvider(
                ctypes.byref(provider), "Microsoft Software Key Storage Provider", 0) != 0:
            raise RuntimeError("Unable to open the provider for owned fixture cleanup.")
        try:
            status = ncrypt.NCryptOpenKey(provider, ctypes.byref(key), cls.key_name, 0, 0x40)
            if status & 0xFFFFFFFF in (0x80090016, 0x80090011):
                return
            if status != 0:
                raise RuntimeError("Unable to open the exact owned fixture key for cleanup.")
            if ncrypt.NCryptDeleteKey(key, 0) != 0:
                ncrypt.NCryptFreeObject(key)
                raise RuntimeError("Unable to delete the exact owned fixture key.")
        finally:
            ncrypt.NCryptFreeObject(provider)

    def test_real_oauth_form_posts_present_the_opaque_cng_key_in_both_modes(self):
        for token_type in ("mtls_pop", "bearer"):
            with self.subTest(token_type=token_type):
                ready = self.root / ("server-" + token_type + ".ready")
                process = subprocess.Popen([
                    str(self.artifacts / "native_mtls_test_server.exe"),
                    str(self.root / "ca.pem"), str(self.root / "server-cert.pem"),
                    str(self.root / "server-key.pem"), str(ready), "1",
                    "token-pop" if token_type == "mtls_pop" else "token-bearer"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("The native token fixture exited before readiness.")
                        time.sleep(0.05)
                    self.assertTrue(ready.exists(), "Native token fixture readiness timed out.")
                    url = "https://localhost:{}/tenant/oauth2/v2.0/token".format(
                        int(ready.read_text().strip()))

                    def post(url, *, headers, data, params=None):
                        self.assertIsNone(params)
                        self.assertFalse({"client_secret", "client_assertion", "req_cnf"} & data.keys())
                        # The native fixture checks exact form ordering and contents.
                        ordered = {name: data[name] for name in (
                            "client_id", "grant_type", "scope", "token_type")}
                        return self.runtime.request(
                            store_uri=self.store_uri,
                            certificate_path=self.root / "client-cert.pem",
                            ca_bundle_path=self.root / "ca.pem",
                            method="POST", url=url,
                            headers=dict(headers, **{"Content-Type": "application/x-www-form-urlencoded"}),
                            body=urlencode(ordered).encode("ascii"), timeout=5)

                    client = Client(
                        {"token_endpoint": url}, "canonical-client",
                        http_client=Mock(post=post),
                        default_headers={"client-request-id": "token-correlation"})
                    result = client.obtain_token_for_client(
                        scope=["https://vault.azure.net/.default"],
                        data={"token_type": token_type})
                    self.assertEqual(result["token_type"], token_type)
                    self.assertEqual(result["access_token"], "synthetic-token")
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, "The native token fixture rejected the call.")
                    self.assertIn(b"Verified client certificate", stdout)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)

    def test_returned_binding_adapters_present_actual_software_cng_certificate(self):
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from msal._managed_identity_binding import _BindingCertificate
        for use_async in (False, True):
            with self.subTest(use_async=use_async):
                ready = self.root / ("binding-" + str(use_async) + ".ready")
                process = subprocess.Popen([
                    str(self.artifacts / "native_mtls_test_server.exe"),
                    str(self.root / "ca.pem"), str(self.root / "server-cert.pem"),
                    str(self.root / "server-key.pem"), str(ready), "1", "success"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("The native binding fixture exited before readiness.")
                        time.sleep(0.05)
                    self.assertTrue(ready.exists(), "Native binding fixture readiness timed out.")
                    url = "https://localhost:{}/mtls".format(int(ready.read_text().strip()))
                    pem = (self.root / "client-cert.pem").read_bytes()
                    certificate = x509.load_pem_x509_certificate(pem, default_backend())
                    credential = dict(
                        client_id="software-unit-fixture", token_endpoint=url,
                        leaf_der=certificate.public_bytes(serialization.Encoding.DER),
                        chain_pem=pem, expires_on=time.time() + 3600)
                    binding = _BindingCertificate(
                        self.runtime, SimpleNamespace(store_uri=self.store_uri), credential,
                        url, str(self.root / "ca.pem"), attested=False)
                    try:
                        headers = {
                            "Authorization": "mtls_pop fixture-token",
                            "x-ms-tokenboundauth": "true",
                            "x-ms-client-request-id": "resource-correlation"}
                        if use_async:
                            async def send():
                                async with binding.open_async_session(max_concurrency=1) as session:
                                    return await session.post(url, headers=headers, data=b"fixture", timeout=5)
                            result = asyncio.run(send())
                        else:
                            with binding.open_session() as session:
                                result = session.post(url, headers=headers, data=b"fixture", timeout=5)
                        self.assertEqual(result.status_code, 200)
                        stdout, stderr = process.communicate(timeout=10)
                        self.assertEqual(process.returncode, 0, "The native resource fixture rejected the call.")
                        self.assertFalse(stderr, "The native resource fixture reported a TLS or HTTP error.")
                        binding._chain_path.write_bytes(b"tampered public-chain fixture")
                        from msal._managed_identity_v2 import _V2Error
                        with binding.open_session() as session, self.assertRaises(_V2Error) as caught:
                            session.post(url, headers=headers, data=b"fixture", timeout=5)
                        self.assertEqual(caught.exception.error_code, "chain_integrity_failed")
                    finally:
                        binding.close()
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)
