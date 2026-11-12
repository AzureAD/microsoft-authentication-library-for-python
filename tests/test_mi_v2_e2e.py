"""Opt-in installed-candidate KeyGuard/Key Vault tests; never software-only evidence."""

import base64
import hashlib
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import uuid

import pytest
import requests
import msal


pytestmark = pytest.mark.skipif(
    os.getenv("MSAL_TEST_MI_IMDS_V2") != "1", reason="Live MSI v2 was not explicitly selected.")


def _sdk_fingerprint():
    digest = hashlib.sha256()
    for name in ("managed_identity", "_managed_identity_v2", "_managed_identity_binding",
                 "_native_mtls", "_native_attestation", "_managed_identity_store"):
        module = importlib.import_module("msal." + name)
        digest.update(name.encode("ascii"))
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()


def assert_binding_rejected(response):
    """Only the precise Key Vault binding rejection is a successful control."""
    if response.status_code != 401:
        raise AssertionError("The binding negative control did not return HTTP 401.")
    try:
        body = response.json()
    except ValueError:
        raise AssertionError("The binding negative control returned invalid JSON.") from None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or error.get("code") != "Unauthorized":
        raise AssertionError("The binding negative control lacked the expected Unauthorized code.")


def _success(response):
    if response.status_code != 200:
        raise AssertionError("The authorized resource operation did not return HTTP 200.")
    try:
        value = response.json().get("value")
    except (ValueError, AttributeError):
        raise AssertionError("The authorized resource operation returned invalid JSON.") from None
    if not isinstance(value, str) or not value:
        raise AssertionError("The authorized resource operation returned no secret value.")
    expected = os.getenv("MSAL_TEST_MI_V2_EXPECTED_SECRET")
    if expected is not None and value != expected:
        raise AssertionError("The authorized resource operation returned an unexpected secret value.")


def _require(name):
    value = os.getenv(name)
    if not value or value == "$(" + name + ")":
        pytest.fail("Missing required live MSI v2 input: " + name)
    return value


@pytest.fixture(scope="module", autouse=True)
def candidate(record_testsuite_property):
    if sys.platform != "win32":
        pytest.fail("Live MSI v2 requires a Windows x64 KeyGuard runner.")
    if _sdk_fingerprint() != _require("MSAL_TEST_MI_V2_SOURCE_FINGERPRINT"):
        pytest.fail("The imported SDK is not the selected implementation candidate.")
    if os.getenv("MSAL_TEST_MI_V2_REQUIRE_INSTALLED") == "1":
        if "site-packages" not in Path(msal.__file__).parts:
            pytest.fail("The live profile must import an installed candidate outside the repository.")
        if os.getenv("MSAL_NATIVE_PRIMITIVES_DIR") or os.getenv("MSAL_KEY_ATTESTATION_DIR"):
            pytest.fail("The installed live profile cannot use dependency-directory overrides.")
    from msal._managed_identity_v2 import _load_runtime, _load_attestation
    runtime, attestation = _load_runtime(), _load_attestation()
    if runtime.build_id != _require("MSAL_TEST_MI_V2_NATIVE_BUILD_ID"):
        pytest.fail("The imported native build is not the selected candidate.")
    for name, value in (
            ("msal_version", msal.__version__), ("msal_location", str(Path(msal.__file__).resolve())),
            ("native_version", version("msal-native-primitives")),
            ("attestation_version", version("msal-key-attestation")),
            ("native_build", runtime.build_id), ("attestation_build", attestation.build_id),
            ("profile", "token-only" if os.getenv("MSAL_TEST_MI_V2_TOKEN_ONLY") == "1" else "full")):
        record_testsuite_property(name, value)


@pytest.fixture(params=["sami", "uami"])
def identity(request):
    return (msal.SystemAssignedManagedIdentity() if request.param == "sami" else
            msal.UserAssignedManagedIdentity(client_id=_require("MSAL_TEST_MI_V2_UAMI_CLIENT_ID")))


@pytest.fixture
def identity_client(identity):
    with requests.Session() as metadata:
        yield msal.ManagedIdentityClient(identity, http_client=metadata)


def _acquire(client, *, pop=True, **kwargs):
    result = client.acquire_token_for_client(
        resource="https://vault.azure.net", with_attestation_support=True,
        **({"mtls_proof_of_possession": True} if pop else {"request_over_mtls": True}), **kwargs)
    if not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise AssertionError("Live MSI v2 did not return a nonempty access token.")
    if result.get("token_type", "").lower() != ("mtls_pop" if pop else "bearer"):
        raise AssertionError("Live MSI v2 did not explicitly return the requested token type.")
    if not pop:
        if result.get("binding_certificate") is not None:
            raise AssertionError("Bearer-over-mTLS unexpectedly exposed a resource binding.")
        return result
    binding = result.get("binding_certificate")
    if binding is None or binding.binding_strength != 3 or not binding.attested:
        raise AssertionError("Live PoP did not return its attested KeyGuard binding.")
    try:
        payload = result["access_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        thumbprint = claims["cnf"]["x5t#S256"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise AssertionError("The live fixture did not supply its expected readable bound JWT.") from None
    digest = base64.urlsafe_b64encode(hashlib.sha256(binding.leaf_der).digest()).rstrip(b"=").decode()
    if thumbprint != binding.key_id or binding.key_id != digest:
        raise AssertionError("The token, returned binding and full-leaf digest do not match.")
    return result


def _headers(result):
    return {"Authorization": result["token_type"] + " " + result["access_token"],
            "x-ms-tokenboundauth": "true", "x-ms-client-request-id": str(uuid.uuid4())}


def _bound(result, *, binding=None):
    url = _require("MSAL_TEST_MI_V2_BOUND_SECRET_URL")
    with (binding or result["binding_certificate"]).open_session(resource_origin=url) as session:
        return session.get(url, headers=_headers(result), timeout=(30, 30))


def _full_profile():
    if os.getenv("MSAL_TEST_MI_V2_TOKEN_ONLY") == "1":
        pytest.skip("Explicit token-only smoke profile: no resource or negative-control evidence.")


def test_cold_cache_and_forced_pop(identity_client, record_property):
    first = _acquire(identity_client)
    assert first["token_source"] == "identity_provider"
    cached = _acquire(identity_client)
    assert cached["token_source"] == "cache"
    assert cached["binding_certificate"].key_id == first["binding_certificate"].key_id
    refreshed = _acquire(identity_client, force_refresh=True)
    assert refreshed["token_source"] == "identity_provider"
    assert refreshed["binding_certificate"].key_id != first["binding_certificate"].key_id
    record_property("cache_hit_and_forced_refresh_checked", "true")
    if os.getenv("MSAL_TEST_MI_V2_TOKEN_ONLY") == "1":
        record_property("resource_checks_run", "false")
        return
    for result in (first, cached, refreshed):
        _success(_bound(result))
    with requests.Session() as ordinary:
        ordinary.trust_env = False
        response = ordinary.get(_require("MSAL_TEST_MI_V2_BOUND_SECRET_URL"),
                                headers=_headers(first), timeout=(30, 30), allow_redirects=False)
    assert_binding_rejected(response)
    record_property("resource_checks_run", "true")
    record_property("no_certificate_status", response.status_code)
    record_property("no_certificate_error_code", "Unauthorized")


def test_bearer_over_mtls_has_no_resource_binding(identity_client):
    first, cached = _acquire(identity_client, pop=False), _acquire(identity_client, pop=False)
    assert first["token_source"] == "identity_provider"
    assert cached["token_source"] == "cache"
    if os.getenv("MSAL_TEST_MI_V2_TOKEN_ONLY") == "1":
        return
    with requests.Session() as ordinary:
        ordinary.trust_env = False
        for result in (first, cached):
            _success(ordinary.get(_require("MSAL_TEST_MI_V2_BEARER_SECRET_URL"),
                                 headers={"Authorization": "Bearer " + result["access_token"]},
                                 timeout=(30, 30), allow_redirects=False))


def test_distinct_identities_mismatched_certificate_is_rejected(record_property):
    _full_profile()
    with requests.Session() as metadata:
        sami = msal.ManagedIdentityClient(msal.SystemAssignedManagedIdentity(), http_client=metadata)
        uami = msal.ManagedIdentityClient(msal.UserAssignedManagedIdentity(
            client_id=_require("MSAL_TEST_MI_V2_UAMI_CLIENT_ID")), http_client=metadata)
        first, second = _acquire(sami), _acquire(uami)
        assert first["binding_certificate"].key_id != second["binding_certificate"].key_id
        _success(_bound(first))
        _success(_bound(second))
        rejected = _bound(first, binding=second["binding_certificate"])
        assert_binding_rejected(rejected)
        record_property("mismatched_certificate_status", rejected.status_code)
        record_property("mismatched_certificate_error_code", "Unauthorized")


@pytest.mark.parametrize("ordinary_first", [False, True])
def test_ordinary_and_mtls_bearer_cache_orders(identity, ordinary_first):
    with requests.Session() as metadata:
        client = msal.ManagedIdentityClient(identity, http_client=metadata)
        protected = lambda: _acquire(client, pop=False)
        ordinary = lambda: client.acquire_token_for_client(resource="https://vault.azure.net")
        order = (ordinary, protected) if ordinary_first else (protected, ordinary)
        for acquire in order:
            result = acquire()
            assert result["token_source"] == "identity_provider"
            assert result.get("token_type", "").lower() == "bearer"
            assert result.get("binding_certificate") is None
        for acquire in reversed(order):
            assert acquire()["token_source"] == "cache"


def test_enrolled_host_rejects_unattested_credential():
    _full_profile()
    if os.getenv("MSAL_TEST_MI_V2_REQUIRE_ATTESTATION") != "1":
        pytest.fail("The full live job must select an enrolled attestation-required fixture.")
    with requests.Session() as metadata:
        client = msal.ManagedIdentityClient(msal.SystemAssignedManagedIdentity(), http_client=metadata)
        with pytest.raises(msal.ManagedIdentityError) as caught:
            client.acquire_token_for_client(resource="https://vault.azure.net",
                                             mtls_proof_of_possession=True, force_refresh=True)
    assert caught.value.stage == "imds"
    assert caught.value.status_code in (400, 401, 403)
