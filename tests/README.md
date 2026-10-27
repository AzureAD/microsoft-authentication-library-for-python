# Local test setup

This document explains how to set up a local development environment to run tests in this repo, including E2E tests.

## 1) Prerequisites

- Windows, macOS, or Linux
- Python 3.9+
- Access to the MSAL lab secrets (Key Vault) for E2E tests
- A registered lab app credential: i.e. certificate `.pfx` file path

## 2) Create and activate a virtual environment

From repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 3) Configure environment variables

Create a local `.env` file in repo root (same folder as `setup.py`):

```dotenv
LAB_APP_CLIENT_ID=<your-lab-app-client-id>
LAB_APP_CLIENT_CERT_PFX_PATH=C:/path/to/your/cert.pfx

```

Notes:
- `tests/test_e2e.py` loads `.env` automatically when `python-dotenv` is installed.
- For certificate auth, `LAB_APP_CLIENT_CERT_PFX_PATH` should be an absolute path.
- Required certificate scenarios use the exportable `LabAuth.MSIDLab.com` PFX
  from the MSID Lab `LabAuth` secret, without a PFX password. Missing, unreadable,
  or invalid PFX files fail these scenarios; they never count as skipped coverage.

## 4) Run unit/integration tests

Run all non-E2E tests quickly:

```powershell
python -m pytest -q tests -k "not e2e"
```

Run full E2E unattended suite:

```powershell
python -m pytest -q tests/test_e2e.py
```

Run only the required certificate scenarios:

```powershell
python -m pytest -q tests/test_e2e.py::CertificateScenariosTestCase
```

The six inherited core certificate methods use the MSID Lab certificate and the
preconfigured client `163ffef9-a313-45b4-ab2f-c7e2f5e0e23e` in tenant
`bea21ebe-8b64-4d06-9f6d-6a889b120a7c`, matching the .NET and Go lab setup.
Global Graph PoP and regional `westus3` Key Vault PoP are separate cases.
Ordinary regional signed-certificate Bearer authentication is not
Bearer-over-mTLS: it uses the ordinary endpoint and retains the central
authority's signed-assertion audience.

The global Graph case reuses the same real token for bound-certificate success
and three 401/403 controls: no certificate (`MissingClientCertificate`), the
correct certificate with the Bearer scheme (`Cnf claim not supported over`),
and an unrelated locally generated certificate
(`MtlsCnfClaimRequestDataValidationFailed`). It then verifies a cached repeat.
Both acquisition orders also verify Bearer/PoP isolation in the same
application, cache, and Key Vault scope.

Graph and Key Vault PoP tokens require readable JWTs, matching SHA256 `cnf`
bindings, and the requested resource audience. Key Vault accepts its verified
URI or application-ID alias `cfa8b339-82a2-471a-a3c9-0fc0be7a4093`.
Recorders delegate to the real default `Session.send`, preserving the TLS
adapter, and verify signed certificate assertions against the certificate,
client, and expected audience. They retain bounded status/error metadata,
never raw forms, tokens, private keys, or client assertions.

All six inherited core methods run without skips, including tagged releases. Missing
configuration, credential failures, and DNS/TLS failures are failures, not
acceptable rejection evidence. They do not fetch unrelated user configuration
from Key Vault. Legacy interactive and other optional E2E skips are unchanged.

Offline guards are separate from live evidence:

```powershell
python -m pytest -q tests\test_lab_config.py tests\test_mtls_e2e_helpers.py tests\test_token_cache.py
```

These checks use local fixtures or synthetic responses to verify strict
binding, safe recording, Graph rejection gates, the exact required-method
inventory, and real unittest-runner failure rather than skip when credentials
are missing. They render failure tracebacks with argument display enabled and
locals disabled to check token-result, cache, and Graph helpers for complete
tokens and truncated token fragments. The sample-output guard rejects dynamic
result values in printed summaries.

Cache compatibility and write-scaling regressions count actual access-token
bucket visits and identity operations, requiring no steady-state bucket scans
and linear population work while preserving legacy renewal and persistence.

### Pinned core certificate parity mapping

The applicable active reference cases below are pinned:

- .NET `c8b90e9de5bc4974de9928835532faac5c88e2d2`:
  [client-credentials cases][dotnet-cc].
- Go `903eb9f1e54e361525025ea27040ac9644b64358`:
  [PoP, ordinary certificate, cache, and regional cases][go-pop].

`C` means `tests.test_e2e.CertificateScenariosTestCase`. Every row is live.

| Active reference case(s) | Python method | Required observation |
| --- | --- | --- |
| Go `TestCredential_X509_Output_Pop`; .NET `Credential_X509_Output_Pop_TestAsync` | `C.test_credential_x509_output_pop` | Graph PoP, returned certificate and JWT SHA256 `cnf`, resource HTTP 200, all three same-token rejection markers, cached repeat. |
| Go `TestCredential_X509_Output_Bearer` | `C.test_credential_x509_output_bearer` | Ordinary westus3 Key Vault Bearer, no output binding, signed certificate assertion with central audience, cached repeat. |
| Go `TestCredential_X509_Output_Pop_Regional` | `C.test_credential_x509_output_pop_regional` | westus3 Key Vault PoP, certificate/JWT binding, same cached token and type; not a redirected global Graph cell. |
| Go `TestConfidentialClientSNIBearerAndMtlsPoPCacheIsolated` | `C.test_bearer_then_mtls_pop_cache_isolated` | One app/cache/scope, Bearer then PoP, distinct tokens and schemes, no Bearer binding, both cache results preserved. |
| Go `TestConfidentialClientSNIMtlsPoPThenBearerCacheIsolated` | `C.test_mtls_pop_then_bearer_cache_isolated` | Reverse acquisition order with the same isolation and reusable PoP result. |

`C.test_subject_name_issuer_authentication` remains an additional required
legacy SN/I lab cell. FIC, Bearer-over-mTLS, and custom HTTP pipeline scenarios
belong to their feature PRs, not this core inventory. This suite does not
provision apps or claim repository-wide skip elimination.

[dotnet-cc]: https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/c8b90e9de5bc4974de9928835532faac5c88e2d2/tests/Microsoft.Identity.Test.Integration.netcore/HeadlessTests/ClientCredentialsMtlsPopTests.cs
[go-pop]: https://github.com/AzureAD/microsoft-authentication-library-for-go/blob/903eb9f1e54e361525025ea27040ac9644b64358/apps/tests/integration/mtls_pop_integration_test.go

### Custom HTTP coverage in the extensibility follow-up

This branch adds two required cells to the six inherited X.509/default-transport
cells, for **eight required certificate scenarios**, not the combined suite's
21-cell inventory. They run in `CertificateScenariosTestCase` and fail, rather
than skip, when the required lab certificate is missing or invalid.

| Required method | Observation |
| --- | --- |
| `test_credential_x509_custom_http_output_pop` | Real Graph PoP acquisition through the derived certificate-aware factory pipeline, returned/JWT certificate binding, the same cached token, resource HTTP 200, preserved sample middleware, and caller-owned cleanup. |
| `test_custom_http_without_send_certificate_over_mtls_on_behalf_of_regular_endpoint` | Ordinary custom-client OBO POST with the mTLS flag off, signed certificate assertion and exact HTTP 400 / `invalid_request` / AADSTS50027 rejection; no derived certificate-bearing child is created. This is a malformed-user-assertion request capture, not a successful OBO acquisition. |

These are source-specific tests of the existing factory API. They do not add
Bearer-over-mTLS or FIC APIs, and do not claim live custom-Bearer or real
custom-FIC integration coverage; those combined cases remain in the combined
follow-up. Collecting these tests or passing the offline guards is not evidence
that a live service run passed.

The custom recorder is isolated in `tests\mtls_http_e2e_helpers.py`; generic
recording/binding helpers remain in `tests\mtls_e2e_helpers.py`, without a
dependency on the custom sample in the foundation branch. Run its offline
privacy/lifecycle guards and existing loopback suite with:

```powershell
python -m pytest -q tests\test_lab_config.py tests\test_mtls_e2e_helpers.py tests\test_mtls_http_e2e_helpers.py tests\test_mtls_http_client.py
```

The custom-recorder privacy guards render actual pytest failures with argument
display enabled and locals disabled to check constructor/POST/capture redaction.
The lifecycle guard retains the sample's response middleware and owner/child cleanup.
`test_custom_capture_failure_traceback_hides_the_expected_assertion` mocks
acquisition: it checks generic recorder privacy, not real FIC support.
The custom sample's output guard prevents dynamic error values from being
printed, and the real loopback TLS fixture explicitly requires TLS 1.2 or newer.

## 5) Manual-intervention E2E tests

Manual tests (interactive browser/device-flow/POP manual scenarios) are separated into:

- `tests/test_e2e_manual.py`

By default they are skipped. To enable:

```powershell
$env:RUN_MANUAL_E2E = "1"
python -m pytest -q tests/test_e2e_manual.py
```

To disable again in the current shell:

```powershell
Remove-Item Env:RUN_MANUAL_E2E
```

## 6) Common troubleshooting

### AADSTS700027 / invalid_client for certificate flow

If you see errors indicating SNI/x5c is required, your app registration may only accept certificate auth with x5c chain. In this repo, that path is covered by SNI-oriented cert tests.

### Key Vault access failures

Verify:
- `LAB_APP_CLIENT_ID` is correct
- `LAB_APP_CLIENT_CERT_PFX_PATH` points to a valid `.pfx` file
- Your principal has access to:
  - `https://msidlabs.vault.azure.net`
  - `https://id4skeyvault.vault.azure.net`

### Interactive tests unexpectedly skipped

Interactive/manual tests are intentionally gated. Set `RUN_MANUAL_E2E=1` and run `tests/test_e2e_manual.py`.
