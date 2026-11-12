Managed Identity v2 Windows candidate
=====================================

This local integration is based on Python PR #982
(``10b146ad937ad823b2b5cd2da4e66df3b942f5c2``), with Python dev
``c53d5ea47f7713d8fbc8e3d9832436fd1028b5c1`` already included.
It is a development candidate, not an approved release. Native ABI 1 remains
compatible; protected-key bootstrap requires native product 0.3 or later.
Ordinary managed identity requires neither optional Windows package.

Build and install locally
-------------------------

Use Windows x64 Python 3.9 or later. Build the Rust foundation using
``native\CONTRIBUTING.md``. Then, from the repository root::

    .\tools\prepare_mi_attestation.ps1 -DestinationDirectory C:\mi-candidate\attestation
    .\tools\build_mi_native_wheels.ps1 -NativeArtifacts .\native\artifacts\Release -AttestationLibrary C:\mi-candidate\attestation\AttestationClientLib.dll -WheelDirectory C:\mi-candidate\wheels
    python -m pip wheel --no-deps --no-build-isolation --wheel-dir C:\mi-candidate\wheels .
    python -m venv C:\mi-candidate\venv
    C:\mi-candidate\venv\Scripts\python.exe -m pip install C:\mi-candidate\wheels\msal-1.39.0-py3-none-any.whl C:\mi-candidate\wheels\msal_native_primitives-0.3.0.dev0-py3-none-win_amd64.whl C:\mi-candidate\wheels\msal_key_attestation-1.1.8.dev0-py3-none-win_amd64.whl
    $env:MSAL_ALLOW_DEVELOPMENT_NATIVE = "1"

The independent wheel projects are under ``packages``. The bundler can omit
``-AttestationLibrary`` for a native-only candidate. The attestation test
dependency is KeyGuardAttestation 1.1.8; preparation checks its pinned SHA256
and Microsoft Authenticode signature. Do not equate a mutable hash manifest
with publisher authentication. Production signing/release review remains
required. No package is published by these commands.

Normal loading uses installed package directories. Explicit development paths
are ``MSAL_NATIVE_PRIMITIVES_DIR`` and ``MSAL_KEY_ATTESTATION_DIR``. Loading
checks the absolute DLL locations, manifest hashes and native ABI. Development
manifests require the explicit opt-in above.

Caller contract
---------------

``ManagedIdentityClient.acquire_token_for_client`` adds keyword-only flags
``mtls_proof_of_possession``, ``request_over_mtls``,
``with_attestation_support``, ``minimum_binding_strength`` and ``force_refresh``.
The two modes are mutually exclusive. Attestation and a strength floor require
an explicit mode. Floors 0, 1 and 3 never permit a weaker fallback; 2 is reserved.
Explicit acquisition always requires a verified VBS-isolated signing key.

PoP results carry ``token_type="mtls_pop"`` and ``binding_certificate``,
including cache hits. Bearer-over-mTLS results carry an explicit bearer type
and ``binding_certificate=None``. Private key bytes never cross the CNG boundary.
Tokens remain opaque in product code.

The public sample supports both modes and bounded worker-backed async requests::

    python sample\managed_identity_v2.py pop --resource https://vault.azure.net --url https://YOUR-VAULT.vault.azure.net/secrets/NAME?api-version=7.5
    python sample\managed_identity_v2.py async-pop --resource https://vault.azure.net --url https://YOUR-VAULT.vault.azure.net/secrets/NAME?api-version=7.5
    python sample\managed_identity_v2.py bearer --resource https://vault.azure.net --url https://BEARER-PERMITTED-VAULT.vault.azure.net/secrets/NAME?api-version=7.5

Add ``--client-id GUID`` for a user-assigned identity. Run samples against the
installed candidate outside the checkout when checking installation.

``binding.open_session(resource_origin=resource_url)`` explicitly pins the
actual resource origin. Without that argument, the audience origin is used.
The audience ``vault.azure.net`` is not a wildcard for arbitrary vault hosts.
The adapter never redirects or proxies, preserves the configured CA file,
buffers bounded responses, and rejects replacement TLS credentials or streams.
It uses the Rust TLS 1.2/HTTP 1.1 transaction, not Python's SSL context.

``binding.open_async_session(max_concurrency=4, resource_origin=resource_url)``
offers async ``request/get/post`` and ``aclose`` / async context management.
It is not an aiohttp integration. Cancellation stops waiting, not the native
transaction: worker slots, buffers, the exact binding and cleanup ownership
remain held until native work exits. Sessions stay on one event loop.

``client.get_managed_identity_capabilities(timeout=seconds)`` returns a dict
with ``source``, ``error_reason``, ``max_supported_binding_strength`` and
``is_mtls_pop_supported_by_host``. The default total budget is 90 seconds;
a timed-out waiter does not cancel the owner. Both protocol versions report
``IMDS``. Host capability does not mean a particular request was attested or
that this implementation enables a software-key fallback.
If the discovery owner's deadline expires, a longer-budget waiter can restart
discovery within its own remaining budget.

ESTS transient HTTP failures use up to three retries with a 90-second total
budget and bounded per-request timeouts, without reminting the certificate.
A configured standard HTTPS adapter retry policy supplies the status/method
rules and delays, still capped at three retries. Certificate rejection recovery
is separate. Trusted IMDS service error codes are preserved as bounded
``error_code`` values; response descriptions and bodies are not included.

``MSAL_MI_DISABLE_IMDS_V2=1`` or case-insensitive ``true`` disables v2 before
protected cache reuse and native work. Ordinary MI routing is unchanged.

Public binding certificates are persisted best-effort in ``CurrentUser\My``
with an SDK policy tag and CNG provider association. Reuse reopens and verifies
the current protected key, requires matching public-key generation and more
than 24 hours of certificate lifetime, and reattaches the same full-leaf proof
before a persisted PoP token can be returned. Store failures are diagnosed and
do not block a live mint. Set ``MSAL_MI_DISABLE_PERSISTENT_CERT_CACHE=1`` or
case-insensitive ``true`` to opt out. Forced refresh never restores an evicted
generation after a failed mint, including through another client in the same
process when certificate-store deletion fails. Suppression remains in place
until a replacement certificate is successfully persisted.

The pinned attestation DLL also requires the Windows Microsoft VC++ x64
runtime (MSVCP140/VCRUNTIME140), in addition to Windows system libraries.
Only explicitly verified package DLLs are loaded by absolute path; other
dependencies resolve through the Windows system search, not neighboring files.

Validation profiles
-------------------

Portable/mocked protocol and actual software-CNG tests are separate from live
VBS/MAA/Key Vault evidence::

    python -m pytest -q tests\test_managed_identity_v2.py tests\test_native_mtls.py tests\test_mi.py
    $env:MSAL_TEST_NATIVE_MTLS_DIR = "C:\checkout\native\artifacts\Release"
    python -m pytest -q tests\test_native_mtls.py

For the collected live suite, configure ``MSAL_TEST_MI_V2_UAMI_CLIENT_ID``,
``MSAL_TEST_MI_V2_BOUND_SECRET_URL``, ``MSAL_TEST_MI_V2_BEARER_SECRET_URL``,
``MSAL_TEST_MI_V2_REQUIRE_ATTESTATION=1`` and optionally
``MSAL_TEST_MI_V2_EXPECTED_SECRET``. The vault URLs identify authorized secret
operations; the first must enforce binding and the second must permit bearer.
The runner must have TPM ready, Secure Boot, VBS/KeyGuard and actual MAA access.

Record the selected source fingerprint from the checkout::

    python -c "from tests.test_mi_v2_e2e import _sdk_fingerprint; print(_sdk_fingerprint())"

Set ``MSAL_TEST_MI_V2_SOURCE_FINGERPRINT`` to that value and
``MSAL_TEST_MI_V2_NATIVE_BUILD_ID`` to the selected native manifest's build ID.
Copy the live test module outside the repository, use the installed candidate,
and set ``MSAL_TEST_MI_V2_REQUIRE_INSTALLED=1``::

    $env:MSAL_TEST_MI_IMDS_V2 = "1"
    python -m pytest -vv -ra test_mi_v2_e2e.py --junitxml=test-results\junit-mi-v2.xml

Missing configuration fails the selected live profile rather than silently
passing. ``MSAL_TEST_MI_V2_TOKEN_ONLY=1`` is an explicitly incomplete smoke
profile: skipped resource/negative-control cases are not live binding evidence.
JUnit records package paths, versions, build IDs and the profile, never tokens,
native key handles or secret values.

The dedicated ``MIE2EImdsV2`` pipeline stage is opt-in. Set
``MSAL_RUN_MI_V2=1`` and ``MSAL_MI_V2_CANDIDATE_DIR`` to a trusted directory
containing exactly one core, native and attestation wheel. Configure the UAMI
and both vault URLs as pipeline variables. The stage preserves the non-fork
guard, verifies TPM/Secure Boot/VBS, installs the selected wheels and runs the
copied live suite outside the checkout. It never uses the token-only profile.
