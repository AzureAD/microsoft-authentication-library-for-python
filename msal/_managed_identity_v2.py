# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Managed identity credential bootstrap using the optional opaque Rust signer."""

import base64
import hashlib
import ipaddress
import importlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timezone
from urllib.parse import urlsplit
from pathlib import Path

import requests

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from .managed_identity import ManagedIdentity, ManagedIdentityError, _adjust_param
from ._native_mtls import _NativeKey, _NativeMtlsError, _NativeRuntime, _https_origin
from .token_cache import _compute_ext_cache_key, _merge_claims, _parse_claims_or_raise
from .oauth2cli.oauth2 import Client


_CUID_OID = bytes.fromhex("060a2b0601040182375a020a")
_SHA256_OID = bytes.fromhex("0609608648016503040201")
_PSS_OID = bytes.fromhex("06092a864886f70d01010a")
_MGF1_OID = bytes.fromhex("06092a864886f70d010108")
_SELECTORS = dict(ManagedIdentity._types_mapping, ResourceId="mi_res_id")
_CLOUD_SUFFIXES = {
    "azurepubliccloud": "attestation.microsoft.com",
    "azureusgovernmentcloud": "attestation.microsoftonline.us",
    "azurechinacloud": "attestation.partner.microsoftonline.cn",
    "azurebleucloud": "attestation.sovcloud-identity.fr",
}
_BINDINGS, _POLICY_LOCKS = OrderedDict(), weakref.WeakValueDictionary()
# Failed store deletion must stay invalid across clients until replacement persists.
_FORCE_MINT = set()
_CACHE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class _V2Error(ManagedIdentityError):
    def __init__(self, message, *, stage, error_code, status_code=None):
        super().__init__(message)
        self.stage, self.error_code, self.status_code = stage, error_code, status_code


class _Deadline:
    def __init__(self, timeout, *, clock=time.monotonic, sleep=time.sleep):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 3600):
            raise ManagedIdentityError("The managed identity timeout must be finite and in (0, 3600].")
        self._clock, self._sleep = clock, sleep
        self._end = clock() + timeout

    def remaining(self):
        remaining = self._end - self._clock()
        if remaining <= 0:
            raise _V2Error("The managed identity operation deadline expired.",
                           stage="discovery", error_code="timeout")
        return remaining

    def wait(self, seconds):
        remaining = self.remaining()
        self._sleep(min(seconds, remaining))
        self.remaining()


def _endpoint(value, *, stage):
    try:
        parsed, _ = _https_origin(value)
        if parsed.query:
            raise ValueError()
        return parsed._replace(netloc=parsed.netloc.lower()).geturl().rstrip("/")
    except (ManagedIdentityError, ValueError):
        raise _V2Error("A valid credential-free HTTPS service endpoint is required.",
                       stage=stage, error_code="invalid_endpoint") from None


def _imds_base():
    value = os.getenv("AZURE_POD_IDENTITY_AUTHORITY_HOST", "http://169.254.169.254")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback or host == "169.254.169.254"
        if (not local or parsed.scheme not in ("http", "https")
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.netloc.endswith(":")
                or parsed.port is not None and not 0 < parsed.port <= 65535
                or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
            raise ValueError()
        return parsed._replace(netloc=parsed.netloc.lower()).geturl().rstrip("/")
    except (ValueError, TypeError):
        raise _V2Error("The managed identity endpoint must be a local IMDS endpoint.",
                       stage="imds", error_code="invalid_endpoint") from None


def _text(payload, field, *, stage, limit=4096):
    value = payload.get(field)
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise _V2Error("The managed identity response contains an invalid required field.",
                       stage=stage, error_code="invalid_response")
    return value


class _ImdsClient:
    def __init__(self, http_client, identity, *, base=None):
        self.http_client, self.identity = http_client, identity
        self.base = _imds_base() if base is None else base

    def request(self, method, path, *, budget, correlation_id, body=None,
                probe=False, version="2.0"):
        params = ({"api-version": "2018-02-01"} if version == "1.0"
                  else {"cred-api-version": version})
        _adjust_param(params, self.identity,
                      ManagedIdentity._types_mapping if version == "1.0" else _SELECTORS)
        headers = {"x-ms-client-request-id": correlation_id}
        if not probe:
            headers["Metadata"] = "true"
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = self.base + path
        limit = None
        for attempt in range(8):
            options = dict(
                params=params, headers=headers, allow_redirects=False,
                timeout=min(2, budget.remaining()),
                proxies={"http": None, "https": None, "all": None})
            if body is not None:
                options["data"] = json.dumps(body, separators=(",", ":"))
            try:
                response = getattr(self.http_client, method.lower())(url, **options)
            except (requests.ConnectionError, requests.Timeout):
                budget.remaining()
                if limit is None:
                    limit = 3
                if attempt >= limit:
                    raise _V2Error("The IMDS transport failed.",
                                   stage="imds", error_code="transport_error") from None
                budget.wait(min(2 ** attempt, 10))
                continue
            budget.remaining()
            status = response.status_code
            if probe and status == 404:
                return response
            if status not in (404, 408, 410, 429) and not 500 <= status <= 599:
                return response
            if limit is None:
                limit = 7 if status == 410 else 3
            if attempt >= limit:
                return response
            budget.wait(10 if status == 410 else min(2 ** attempt, 10))
        raise AssertionError("The bounded IMDS retry loop exhausted unexpectedly.")

    def _json(self, response, path):
        stage = "imds"
        marker = next((value for name, value in response.headers.items()
                       if name.lower() == "server"), "")
        if not isinstance(marker, str) or not re.fullmatch(r"IMDS(?:/[^\s]+)?", marker, re.IGNORECASE):
            raise _V2Error("The response did not contain the expected IMDS server marker.",
                           stage=stage, error_code="invalid_origin")
        actual = getattr(response, "url", None)
        if actual and (not isinstance(actual, str)
                       or urlsplit(actual)._replace(query="").geturl() != self.base + path):
            raise _V2Error("The IMDS response came from an unexpected endpoint.",
                           stage=stage, error_code="invalid_origin")
        if len(response.text) > 1048576:
            raise _V2Error("The IMDS response exceeds its size bound.",
                           stage=stage, error_code="invalid_response")
        try:
            payload = json.loads(response.text, object_pairs_hook=_unique_members)
        except (ValueError, TypeError):
            if response.status_code != 200:
                payload = {}
            else:
                raise _V2Error("The IMDS response was not a JSON object.",
                               stage=stage, error_code="invalid_response") from None
        if response.status_code != 200:
            code = "imds_v2_not_supported" if response.status_code == 404 else "request_failed"
            service_code = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(service_code, dict):
                service_code = service_code.get("code")
            if isinstance(service_code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,128}", service_code):
                code = service_code
            raise _V2Error("The IMDS credential operation failed.",
                           stage=stage, error_code=code, status_code=response.status_code)
        if not isinstance(payload, dict):
            raise _V2Error("The IMDS response was not a JSON object.",
                           stage=stage, error_code="invalid_response")
        return payload

    def platform(self, budget, correlation_id):
        path = "/metadata/identity/getplatformmetadata"
        payload = self._json(self.request(
            "GET", path, budget=budget, correlation_id=correlation_id), path)
        client = _text(payload, "clientId", stage="imds", limit=256)
        tenant = _text(payload, "tenantId", stage="imds", limit=256)
        try:
            uuid.UUID(client)
            uuid.UUID(tenant)
        except ValueError:
            raise _V2Error("IMDS platform identity identifiers must be valid GUIDs.",
                           stage="imds", error_code="invalid_response") from None
        cuid = payload.get("cuId")
        if (not isinstance(cuid, dict)
                or not any(isinstance(cuid.get(key), str) and cuid[key] for key in ("vmId", "vmssId"))
                or any(value is not None and (not isinstance(value, str) or len(value) > 4096)
                       for key in ("vmId", "vmssId") for value in (cuid.get(key),))):
            raise _V2Error("IMDS platform metadata lacks valid CUID identifiers.",
                           stage="imds", error_code="invalid_response")
        if (self.identity.get(ManagedIdentity.ID_TYPE) == ManagedIdentity.CLIENT_ID
                and client.lower() != self.identity[ManagedIdentity.ID].lower()):
            raise _V2Error("IMDS selected a different managed identity client.",
                           stage="imds", error_code="identity_mismatch")
        payload["attestationEndpoint"] = _endpoint(
            _text(payload, "attestationEndpoint", stage="imds"), stage="imds")
        return payload

    def attestation_endpoint(self, platform, budget, correlation_id):
        fallback = platform["attestationEndpoint"]
        path = "/metadata/instance/compute"
        # Compute has a distinct API version and no identity selector.
        try:
            response = self.http_client.get(
                self.base + path, params={"api-version": "2021-02-01"},
                headers={"Metadata": "true", "x-ms-client-request-id": correlation_id},
                allow_redirects=False, timeout=min(2, budget.remaining()),
                proxies={"http": None, "https": None, "all": None})
            budget.remaining()
            payload = self._json(response, path)
            cloud, region = payload.get("azEnvironment"), payload.get("location")
            if (not isinstance(cloud, str) or not isinstance(region, str)
                    or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", region)
                    or cloud.lower() not in _CLOUD_SUFFIXES):
                return fallback
            return "https://{}.{}".format(region.lower(), _CLOUD_SUFFIXES[cloud.lower()])
        except (requests.ConnectionError, requests.Timeout):
            budget.remaining()
            return fallback
        except _V2Error as error:
            if error.error_code == "timeout":
                raise
            budget.remaining()
            return fallback

    def issue(self, platform, key, *, budget, correlation_id, attestation_token=None):
        body = {"csr": _create_csr(key, platform["clientId"], platform["tenantId"], platform["cuId"])}
        if attestation_token is not None:
            if not isinstance(attestation_token, str) or not attestation_token.strip():
                raise _V2Error("Required key attestation produced no token.",
                               stage="attestation", error_code="attestation_failed")
            body["attestation_token"] = attestation_token
        path = "/metadata/identity/issuecredential"
        payload = self._json(self.request(
            "POST", path, body=body, budget=budget, correlation_id=correlation_id), path)
        for field in ("client_id", "tenant_id", "identity_type", "mtls_authentication_endpoint", "certificate"):
            _text(payload, field, stage="issuance", limit=1048576 if field == "certificate" else 4096)
        expected = "SAMI" if ManagedIdentity.is_system_assigned(self.identity) else "UAMI"
        if (payload["client_id"].lower() != platform["clientId"].lower()
                or payload["tenant_id"].lower() != platform["tenantId"].lower()
                or payload["identity_type"].upper() != expected):
            raise _V2Error("The issued credential does not match the selected platform identity.",
                           stage="issuance", error_code="identity_mismatch")
        try:
            tenant = str(uuid.UUID(payload["tenant_id"].strip("/")))
            certificate = x509.load_der_x509_certificate(
                base64.b64decode(payload["certificate"], validate=True), backend=default_backend())
            public = certificate.public_key()
            if not hasattr(public, "public_numbers") or public.public_numbers() != key.public_key.public_numbers():
                raise ValueError()
            not_before = getattr(certificate, "not_valid_before_utc", None)
            not_after = getattr(certificate, "not_valid_after_utc", None)
            not_before = not_before or certificate.not_valid_before.replace(tzinfo=timezone.utc)
            not_after = not_after or certificate.not_valid_after.replace(tzinfo=timezone.utc)
            if not not_before.timestamp() <= time.time() < not_after.timestamp():
                raise ValueError()
        except (ValueError, TypeError):
            raise _V2Error("The issued certificate is invalid or does not match the live protected key.",
                           stage="issuance", error_code="invalid_certificate") from None
        endpoint = _endpoint(payload["mtls_authentication_endpoint"], stage="issuance")
        if urlsplit(endpoint).path:
            raise _V2Error("The issued mTLS endpoint must not already contain a tenant or path.",
                           stage="issuance", error_code="invalid_endpoint")
        payload["token_endpoint"] = endpoint + "/" + tenant + "/oauth2/v2.0/token"
        payload["leaf_der"] = certificate.public_bytes(serialization.Encoding.DER)
        payload["chain_pem"] = certificate.public_bytes(serialization.Encoding.PEM)
        payload["expires_on"] = not_after.timestamp()
        return payload


class _MaaTokenCache:
    def __init__(self, *, clock=time.time):
        self._entries, self._lock, self._clock = OrderedDict(), threading.Lock(), clock

    def get_or_attest(self, endpoint, generation, operation):
        cache_key = (_endpoint(endpoint, stage="attestation"), generation)
        with self._lock:
            for name in list(self._entries):
                if self._entries[name][1] <= self._clock() + 300:
                    del self._entries[name]
            cached = self._entries.get(cache_key)
            if cached and cached[1] > self._clock() + 300:
                self._entries.move_to_end(cache_key)
                return cached[0]
            jwt = operation()
            try:
                if not isinstance(jwt, str) or not 0 < len(jwt) <= 1048576:
                    raise ValueError()
                segments = jwt.split(".")
                if len(segments) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]+", segments[1]):
                    raise ValueError()
                payload = json.loads(base64.urlsafe_b64decode(
                    segments[1] + "=" * (-len(segments[1]) % 4)), object_pairs_hook=_unique_members)
                expiry = payload.get("exp") if isinstance(payload, dict) else None
                if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                        or not math.isfinite(expiry) or expiry <= self._clock() + 300):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, OverflowError):
                raise _V2Error("Key attestation returned an invalid or insufficiently fresh JWT.",
                               stage="attestation", error_code="attestation_failed") from None
            self._entries[cache_key] = jwt, expiry
            while len(self._entries) > 128:
                self._entries.popitem(last=False)
            return jwt


_MAA_CACHE = _MaaTokenCache()


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member.")
        result[key] = value
    return result


def _package_directory(package, environment):
    override = os.getenv(environment)
    if override:
        return Path(override)
    try:
        module = importlib.import_module(package)
    except ModuleNotFoundError as error:
        if error.name != package:
            raise
        raise _V2Error("Install the optional {} Windows package.".format(package),
                       stage="native_loading", error_code="native_dependency_unavailable") from None
    return Path(module.__file__).parent / "lib"


def _load_runtime():
    return _NativeRuntime(
        _package_directory("msal_native_primitives", "MSAL_NATIVE_PRIMITIVES_DIR"),
        allow_development=os.getenv("MSAL_ALLOW_DEVELOPMENT_NATIVE") == "1")


def _load_attestation():
    from ._native_attestation import _NativeAttestation
    return _NativeAttestation(
        _package_directory("msal_key_attestation", "MSAL_KEY_ATTESTATION_DIR"),
        allow_development=os.getenv("MSAL_ALLOW_DEVELOPMENT_NATIVE") == "1")


def _policy_lock(policy):
    with _CACHE_LOCK:
        lock = _POLICY_LOCKS.get(policy)
        if lock is None:
            lock = threading.RLock()
            _POLICY_LOCKS[policy] = lock
        return lock


class _CapabilityDiscovery:
    def __init__(self, owner):
        self.owner = owner
        self._lock = threading.Lock()
        self._cached = self._inflight = None

    @staticmethod
    def _result(source, strength=0, reason=None):
        return dict(source=source, error_reason=reason,
                    max_supported_binding_strength=strength,
                    is_mtls_pop_supported_by_host=strength > 0)

    def get(self, *, timeout=None):
        from .managed_identity import DEFAULT_TO_VM, get_managed_identity_source
        budget = _Deadline(90 if timeout is None else timeout)
        source = get_managed_identity_source()
        if source is not DEFAULT_TO_VM:
            return self._result(source)
        disabled = os.getenv("MSAL_MI_DISABLE_IMDS_V2", "").lower() in ("1", "true")
        base = _imds_base()
        key = (base, disabled, os.getenv("MSAL_NATIVE_PRIMITIVES_DIR"))
        while True:
            with self._lock:
                if self._cached is not None and self._cached[0] == key:
                    return dict(self._cached[1])
                if self._inflight is not None and self._inflight[1].done():
                    completed_key, completed = self._inflight
                    self._inflight = None
                    if completed.exception() is None:
                        self._cached = completed_key, completed.result()
                        continue
                if self._inflight is None:
                    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="msal-mi-discovery")
                    future = executor.submit(self._probe, base, disabled, budget)
                    executor.shutdown(wait=False)
                    self._inflight = key, future
                future_key, future = self._inflight
            try:
                result = future.result(timeout=budget.remaining())
            except FutureTimeout:
                raise _V2Error("The managed identity discovery deadline expired.",
                               stage="discovery", error_code="timeout") from None
            except _V2Error as error:
                if error.error_code != "timeout":
                    raise
                budget.remaining()
                continue
            if future_key == key:
                return dict(result)
            budget.remaining()

    def _probe(self, base, disabled, budget):
        client = _ImdsClient(self.owner._http_client.http_client,
                             self.owner._managed_identity, base=base)
        correlation = str(uuid.uuid4())
        failures = []
        versions = ("1.0",) if disabled else ("2.0", "1.0")
        for version in versions:
            path = ("/metadata/identity/getplatformmetadata" if version == "2.0"
                    else "/metadata/identity/oauth2/token")
            try:
                response = client.request("GET", path, budget=budget,
                                          correlation_id=correlation, probe=True, version=version)
            except _V2Error as error:
                if error.error_code == "timeout":
                    raise
                failures.append("IMDS {}: {}".format(version, error.error_code))
                continue
            marker = response.headers.get("Server", "")
            valid_marker = isinstance(marker, str) and re.fullmatch(r"IMDS(?:/[^\s]+)?", marker, re.I)
            if response.status_code != 400 or not valid_marker:
                failures.append("IMDS {}: status {} or invalid marker".format(version, response.status_code))
                continue
            strength = 0
            if not disabled and version == "2.0":
                strength = 1
                try:
                    key = _NativeKey(_load_runtime(), "microsoft-identity-native-vbs-capability")
                    key.close()
                    strength = 3
                except (_NativeMtlsError, _V2Error) as error:
                    logger.warning("KeyGuard capability probe unavailable (stage %s).", error.stage)
            elif not disabled:
                compute_path = "/metadata/instance/compute"
                try:
                    response = client.http_client.get(
                        base + compute_path, params={"api-version": "2021-02-01"},
                        headers={"Metadata": "true", "x-ms-client-request-id": correlation},
                        timeout=min(2, budget.remaining()), allow_redirects=False,
                        proxies={"http": None, "https": None, "all": None})
                    budget.remaining()
                    payload = client._json(response, compute_path)
                    security = payload.get("securityProfile")
                    strength = int(
                        str(payload.get("osType", "")).lower() == "windows"
                        and isinstance(security, dict)
                        and str(security.get("securityType", "")).lower() in (
                            "trustedlaunch", "confidentialvm"))
                except (requests.ConnectionError, requests.Timeout):
                    budget.remaining()
                    logger.warning("IMDS capability compute metadata transport failed.")
                except _V2Error as error:
                    if error.error_code == "timeout":
                        raise
                    logger.warning("IMDS capability compute metadata unavailable (%s).", error.error_code)
            budget.remaining()
            return self._result("IMDS", strength)
        budget.remaining()
        return self._result(None, reason="; ".join(failures))


def _is_binding_rejection(error):
    seen = set()
    rejected = False
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, _NativeMtlsError):
            packed = error.openssl_error or 0
            library, reason = (packed >> 23) & 0xFF, packed & 0x7FFFFF
            if library == 20 and reason == 134:  # SSL_R_CERTIFICATE_VERIFY_FAILED
                return False
            if (error.system_error == 10054
                    or library == 20 and reason in (1042, 1043, 1044, 1045, 1046, 1048)):
                rejected = True
        if isinstance(error, ConnectionResetError) and (
                error.errno == 10054 or getattr(error, "winerror", None) == 10054):
            rejected = True
        error = error.__cause__ or error.__context__
    return rejected


def _claims(capabilities, challenge):
    try:
        parsed = _parse_claims_or_raise(challenge) if challenge else {}
    except ValueError:
        raise _V2Error("The managed identity claims challenge is invalid.",
                       stage="request", error_code="invalid_claims") from None
    if capabilities:
        capability_claims = json.dumps({"access_token": {"xms_cc": {"values": capabilities}}})
        return _merge_claims(json.dumps(parsed) if parsed else None, capability_claims)
    return json.dumps(parsed) if parsed else None


def _resource_scope(resource):
    if (not isinstance(resource, str) or not resource or len(resource) > 4096
            or not resource.isascii() or any(ord(char) <= 32 for char in resource)
            or "?" in resource or "#" in resource):
        raise _V2Error("The managed identity resource must be bounded, query-free text.",
                       stage="request", error_code="invalid_resource")
    value = resource.rstrip("/")
    return value if value.endswith("/.default") else value + "/.default"


def _trust_bundle(http_client):
    if (type(http_client) is not requests.Session or http_client.cert is not None
            or any(type(adapter) is not requests.adapters.HTTPAdapter
                   for adapter in http_client.adapters.values())
            or any(set(adapter.poolmanager.connection_pool_kw) - {"maxsize", "block"}
                   for adapter in http_client.adapters.values())):
        raise _V2Error("Explicit mTLS requires a standard requests.Session with a CA-file trust policy.",
                       stage="request", error_code="unsupported_transport")
    verify = getattr(http_client, "verify", True)
    if verify is False:
        raise _V2Error("Explicit mTLS requests cannot disable peer verification.",
                       stage="request", error_code="unsupported_transport")
    if getattr(http_client, "trust_env", False) is True:
        verify = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or verify
    if verify is True or verify is None:
        verify = requests.certs.where()
    if not isinstance(verify, (str, os.PathLike)):
        raise _V2Error("The mTLS transport requires a configured CA bundle file.",
                       stage="request", error_code="unsupported_transport")
    try:
        path = Path(verify).resolve(strict=True)
    except OSError:
        raise _V2Error("The configured mTLS trust bundle could not be opened.",
                       stage="request", error_code="unsupported_transport") from None
    if not path.is_file():
        raise _V2Error("The mTLS trust bundle must be a file.",
                       stage="request", error_code="unsupported_transport")
    return str(path)


class _V2Client:
    def __init__(self, owner):
        from ._managed_identity_store import _CertificateStore
        self.owner = owner
        self.http = owner._http_client.http_client
        self.imds = _ImdsClient(self.http, owner._managed_identity)
        self._runtime = self._key = self._attestation = None
        self._store = _CertificateStore()

    def _binding(self, policy, resource, attested, ca_bundle, *, force=False):
        from ._managed_identity_binding import _BindingCertificate
        from ._managed_identity_store import _store_alias
        alias = _store_alias(policy)
        with _CACHE_LOCK:
            binding = _BINDINGS.get(policy)
            if binding and (force or binding._closed or binding.expires_on <= time.time() + 86400):
                del _BINDINGS[policy]
                binding = None
        if force:
            _FORCE_MINT.add(policy)
            self._store.remove(alias)
        correlation = str(uuid.uuid4())
        budget = _Deadline(90)
        metadata = self.imds.platform(budget, correlation) if binding is None else None
        if self._runtime is None:
            self._runtime = _load_runtime()
        identity = self.owner._managed_identity
        key_name = "microsoft-identity-native-vbs-" + hashlib.sha256(json.dumps(
            [self.imds.base, identity.get(ManagedIdentity.ID_TYPE), identity.get(ManagedIdentity.ID)],
            separators=(",", ":")).encode("utf-8")).hexdigest()
        try:
            key = _NativeKey(self._runtime, key_name)
        except _NativeMtlsError:
            with _CACHE_LOCK:
                if _BINDINGS.get(policy) is binding:
                    _BINDINGS.pop(policy, None)
            raise
        if binding:
            with binding._lock:
                reusable = (not binding._closed and binding._key is not None
                            and binding._key.generation_id == key.generation_id)
            if reusable:
                key.close()
                with _CACHE_LOCK:
                    _BINDINGS.move_to_end(policy)
                return binding
        if binding:
            with _CACHE_LOCK:
                if _BINDINGS.get(policy) is binding:
                    del _BINDINGS[policy]
            self._store.remove(alias, leaf_der=binding.leaf_der)
        self._key = key
        credential = None if policy in _FORCE_MINT else self._store.read(alias, key)
        if credential is not None:
            binding = _BindingCertificate(
                self._runtime, key, credential, resource, ca_bundle, attested=attested)
            with _CACHE_LOCK:
                _BINDINGS[policy] = binding
                _BINDINGS.move_to_end(policy)
                while len(_BINDINGS) > 128:
                    _BINDINGS.popitem(last=False)
            return binding
        metadata = metadata or self.imds.platform(budget, correlation)
        budget.remaining()
        token = None
        if attested:
            if self._attestation is None:
                self._attestation = _load_attestation()
            endpoint = self.imds.attestation_endpoint(metadata, budget, correlation)
            token = _MAA_CACHE.get_or_attest(
                endpoint, key.generation_id,
                lambda: self._attestation.attest(endpoint, key, metadata["tenantId"]))
            budget.remaining()
        credential = self.imds.issue(
            metadata, key, budget=budget, correlation_id=correlation, attestation_token=token)
        binding = _BindingCertificate(
            self._runtime, key, credential, resource, ca_bundle, attested=attested)
        if self._store.write(alias, key, credential):
            _FORCE_MINT.discard(policy)
        with _CACHE_LOCK:
            _BINDINGS[policy] = binding
            _BINDINGS.move_to_end(policy)
            while len(_BINDINGS) > 128:
                _BINDINGS.popitem(last=False)
        return binding

    def _exchange(self, binding, mode, resource, claims):
        from urllib3.exceptions import InvalidHeader, MaxRetryError
        from urllib3.response import HTTPResponse
        from urllib3.util.retry import Retry
        configured = self.http.get_adapter(binding.token_endpoint).max_retries
        retries = configured if configured.total != 0 else Retry(
            total=3, backoff_factor=1, allowed_methods=frozenset(("POST",)),
            status_forcelist=(408, 429, 500, 502, 503, 504))
        budget = _Deadline(90)

        def post(url, **kwargs):
            nonlocal retries
            for attempt in range(4):
                answer = binding.post(url, timeout=min(10, budget.remaining()), **kwargs)
                budget.remaining()
                if attempt == 3 or not retries.is_retry(
                        "POST", answer.status_code, "Retry-After" in answer.headers):
                    break
                try:
                    retries = retries.increment(
                        method="POST", url=url,
                        response=HTTPResponse(status=answer.status_code, headers=answer.headers))
                except MaxRetryError:
                    break
                try:
                    delay = (retries.get_retry_after(HTTPResponse(headers=answer.headers))
                             if retries.respect_retry_after_header else None)
                except InvalidHeader:
                    raise _V2Error("The mTLS token endpoint returned an invalid retry delay.",
                                   stage="ests", error_code="invalid_response") from None
                budget.wait(delay or retries.get_backoff_time())
            if answer.status_code < 500:
                try:
                    payload = json.loads(answer.text)
                except ValueError:
                    raise _V2Error("The mTLS token endpoint returned invalid JSON.",
                                   stage="ests", error_code="invalid_response") from None
                if not isinstance(payload, dict):
                    raise _V2Error("The mTLS token endpoint returned an invalid object.",
                                   stage="ests", error_code="invalid_response")
            return answer

        client = Client({"token_endpoint": binding.token_endpoint},
                        binding.client_id, http_client=binding)
        try:
            result = client.obtain_token_for_client(
                scope=[_resource_scope(resource)], data={"token_type": mode, "claims": claims},
                headers={"client-request-id": str(uuid.uuid4())}, post=post)
        except requests.HTTPError as error:
            raise _V2Error("The mTLS token endpoint failed.",
                           stage="ests", error_code="request_failed",
                           status_code=error.response.status_code if error.response is not None else None) from None
        if result.get("error"):
            code = result["error"]
            code = code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,128}", code) else "request_failed"
            raise _V2Error("The mTLS token endpoint rejected the request.",
                           stage="ests", error_code=code)
        if (not isinstance(result.get("access_token"), str) or not result["access_token"].strip()
                or not isinstance(result.get("token_type"), str)
                or result["token_type"].lower() != mode):
            raise _V2Error("The token endpoint did not explicitly return the requested token type and token.",
                           stage="ests", error_code="invalid_response")
        try:
            expiry = int(result["expires_in"])
            if isinstance(result["expires_in"], bool) or expiry <= 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError, OverflowError):
            raise _V2Error("The mTLS token endpoint returned an invalid token lifetime.",
                           stage="ests", error_code="invalid_response") from None
        result["expires_in"] = expiry
        return result

    def acquire(self, resource, *, pop, attested, floor, force, claims_challenge):
        _resource_scope(resource)
        ca_bundle = _trust_bundle(self.http)
        claims = _claims(self.owner._client_capabilities, claims_challenge)
        identity = self.owner._managed_identity
        mode = "mtls_pop" if pop else "bearer"
        policy_data = dict(
            mi_v2_mode=mode, mi_v2_attestation=str(int(attested)),
            mi_v2_strength=str(floor), mi_v2_identity_type=identity[ManagedIdentity.ID_TYPE],
            mi_v2_endpoint=self.imds.base, mi_v2_claims=claims or "")
        client_id = identity.get(ManagedIdentity.ID) or "SYSTEM_ASSIGNED_MANAGED_IDENTITY"
        origin = _https_origin(resource)[1] if pop else None
        policy = (client_id, tuple(sorted(policy_data.items())), origin,
                  ca_bundle, os.getenv("MSAL_NATIVE_PRIMITIVES_DIR", "installed"))
        with _policy_lock(policy):
            try:
                binding = self._binding(policy, resource if pop else None, attested, ca_bundle, force=force)
            except _NativeMtlsError as error:
                if floor != 3:
                    raise
                failure = _V2Error("A usable KeyGuard binding could not satisfy the requested strength floor.",
                                   stage="keyguard", error_code="minimum_binding_strength_not_met")
                failure.native_code, failure.system_error = error.native_code, error.system_error
                raise failure from error
            data = dict(policy_data, mi_v2_binding_id=binding.key_id if pop else "")
            cache = self.owner._token_cache
            if not force and not claims_challenge:
                for entry in cache.search(cache.CredentialType.ACCESS_TOKEN, target=[resource], query=dict(
                        client_id=client_id, environment="localhost", realm="managed_identity",
                        home_account_id=None, ext_cache_key=_compute_ext_cache_key(data))):
                    now = time.time()
                    if (isinstance(entry.get("token_type"), str)
                            and entry["token_type"].lower() == mode
                            and int(entry["expires_on"]) > now + 300
                            and int(entry.get("refresh_on", entry["expires_on"])) > now):
                        return dict(access_token=entry["secret"], token_type=entry["token_type"],
                                    expires_in=int(int(entry["expires_on"]) - now), token_source="cache",
                                    binding_certificate=binding if pop else None)
            for attempt in range(2):
                try:
                    result = self._exchange(binding, mode, resource, claims)
                    break
                except (_V2Error, _NativeMtlsError, ConnectionResetError, requests.ConnectionError) as error:
                    rejected = (isinstance(error, _V2Error) and error.error_code == "invalid_client"
                                or _is_binding_rejection(error))
                    if not rejected:
                        raise
                    with _CACHE_LOCK:
                        if _BINDINGS.get(policy) is binding:
                            del _BINDINGS[policy]
                    from ._managed_identity_store import _store_alias
                    self._store.remove(_store_alias(policy), leaf_der=binding.leaf_der)
                    _FORCE_MINT.add(policy)
                    if attempt:
                        raise
                    binding = self._binding(policy, resource if pop else None, attested, ca_bundle)
                    data["mi_v2_binding_id"] = binding.key_id if pop else ""
            cache.add(dict(
                client_id=client_id, scope=[resource], token_endpoint="https://localhost/managed_identity",
                response=result, params={}, data=data))
            result.update(token_source="identity_provider", binding_certificate=binding if pop else None)
            return result


def _der(tag, contents):
    length = len(contents)
    if length > 65535:
        raise ManagedIdentityError("Managed identity CSR data exceeds its bound.")
    if length < 128:
        prefix = bytes((length,))
    else:
        encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
        prefix = bytes((0x80 | len(encoded),)) + encoded
    return bytes((tag,)) + prefix + contents


def _create_csr(key, client_id, tenant_id, cuid):
    """Return base64 PKCS#10; only its SHA-256 digest crosses the signing ABI."""
    for identifier in (client_id, tenant_id):
        if (not isinstance(identifier, str) or not identifier or not identifier.isascii()
                or len(identifier) > 256 or any(ord(char) < 32 for char in identifier)):
            raise ManagedIdentityError("Managed identity CSR requires valid platform identifiers.")
    if not isinstance(cuid, dict):
        raise ManagedIdentityError("Managed identity CSR requires structured platform CUID data.")
    identifiers = {}
    for name in ("vmId", "vmssId"):
        value = cuid.get(name)
        if value is not None:
            if not isinstance(value, str) or len(value) > 4096:
                raise ManagedIdentityError("Managed identity CUID values must be bounded strings.")
            if value:
                identifiers[name] = value
    if not identifiers:
        raise ManagedIdentityError("Managed identity CSR requires a platform CUID identifier.")
    if key.public_key.key_size != 2048:
        raise ManagedIdentityError("Managed identity CSR requires the protected RSA-2048 key.")

    try:
        subject = x509.Name((
            x509.NameAttribute(NameOID.COMMON_NAME, client_id),
            x509.NameAttribute(NameOID.DOMAIN_COMPONENT, tenant_id),
        )).public_bytes(backend=default_backend())
    except ValueError:
        raise ManagedIdentityError("Platform identifiers cannot be encoded in the CSR subject.") from None
    public = key.public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    cuid_json = json.dumps(identifiers, separators=(",", ":")).encode("utf-8")
    attribute = _der(0x30, _CUID_OID + _der(0x31, _der(0x0C, cuid_json)))
    # PKCS#10 attributes use an IMPLICIT constructed [0], not a nested SET.
    information = _der(0x30, b"\x02\x01\x00" + subject + public + _der(0xA0, attribute))

    sha256 = _der(0x30, _SHA256_OID + b"\x05\x00")
    parameters = _der(0x30, _der(0xA0, sha256)
                      + _der(0xA1, _der(0x30, _MGF1_OID + sha256))
                      + _der(0xA2, b"\x02\x01\x20"))
    algorithm = _der(0x30, _PSS_OID + parameters)
    signature = key.sign_sha256_pss(hashlib.sha256(information).digest())
    if not isinstance(signature, bytes) or len(signature) != 256:
        raise ManagedIdentityError("The protected CSR signature is invalid.")
    return base64.b64encode(
        _der(0x30, information + algorithm + _der(0x03, b"\x00" + signature))).decode("ascii")
