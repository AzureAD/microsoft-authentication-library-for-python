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

### FIC over mTLS lab parity

The FIC tests reuse the existing MSAL.NET/Go SN/I lab configuration for both
legs: app `163ffef9-a313-45b4-ab2f-c7e2f5e0e23e`, authority
`https://login.microsoftonline.com/bea21ebe-8b64-4d06-9f6d-6a889b120a7c`,
and the shared LabAuth certificate supplied through
`LAB_APP_CLIENT_CERT_PFX_PATH`. No Python-only `LAB_FIC_*` configuration or
new app provisioning is needed.

Leg 1 requests `api://AzureADTokenExchange/.default` as an mTLS PoP assertion.
The PoP-output cell uses global endpoints for both legs, requests
`https://graph.microsoft.com/.default`, and calls the mTLS Graph resource with
the final token's `binding_certificate`. The Bearer-output cell uses `westus3`
for both legs and requests `https://storage.azure.com/.default`; its final
token must not contain `binding_certificate` or trigger a downstream mTLS call.
Missing or invalid certificates and real token-acquisition errors fail the
FIC tests rather than skipping or accepting an error as success.

Both cells also run through a caller-owned recording HTTP factory. The factory
uses the sample's real certificate-scoped Requests adapter, preserves its policy
header and response middleware, and owns cleanup of the derived client. These
are live exchanges, not mocked factory responses.

Run all four live FIC variants with:

```powershell
python -m pytest -q tests\test_e2e.py::FicTwoLegTestCase
```

## 4) Run unit/integration tests

Run all non-E2E tests quickly:

```powershell
python -m pytest -q tests -k "not e2e"
```

Run full E2E unattended suite:

```powershell
python -m pytest -q tests\test_e2e.py
```

Run only the required certificate scenarios:

```powershell
python -m pytest -q tests\test_e2e.py::CertificateScenariosTestCase
```

The certificate matrix uses client `163ffef9-a313-45b4-ab2f-c7e2f5e0e23e`
in tenant `bea21ebe-8b64-4d06-9f6d-6a889b120a7c`, matching the .NET and Go
lab setup. Global Graph PoP and regional `westus3` Key Vault PoP are separate
cases. Ordinary regional signed-certificate authentication and regional
Bearer-over-mTLS are also separate cases.

The global Graph case reuses the same real token for bound-certificate success
and three 401/403 controls: no certificate (`MissingClientCertificate`), the
correct certificate with the Bearer scheme (`Cnf claim not supported over`),
and an unrelated locally generated certificate
(`MtlsCnfClaimRequestDataValidationFailed`). It then verifies a cached repeat.
Both acquisition orders also verify Bearer/PoP isolation in the same
application, cache, and Key Vault scope.

OBO, refresh-token, and authorization-code captures delegate to the real
transport and require an actual expected HTTP/OAuth rejection for intentionally
invalid grants. They check the selected endpoint, signed client assertion,
grant-specific fields, and absence of PoP request markers. They do not claim
successful user-token issuance. A flag-off OBO control must use the ordinary
endpoint. Default and custom-pipeline variants run independently; a flag-off
custom client must not invoke the mTLS factory.

All 21 required live methods (17 certificate and four FIC) run without skips,
including tagged releases. Missing configuration, credential failures, and
DNS/TLS failures are failures, not acceptable rejection evidence. They do not
fetch unrelated user configuration from Key Vault. Legacy interactive and
other optional E2E skips are unchanged.

Offline recorder/guard tests are separate from live evidence:

```powershell
python -m pytest -q tests\test_lab_config.py tests\test_mtls_e2e_helpers.py tests\test_mtls_http_client.py
```

These checks use local fixtures or synthetic responses to verify strict
binding, safe recording, failure gates, the exact required-method inventory,
and real unittest-runner failure rather than skip when credentials are missing.
The existing custom-HTTP loopback TLS tests remain in
`tests\test_mtls_http_client.py`; they complement, not replace, live factory
coverage. Recorders retain bounded status/error metadata, never raw forms,
tokens, private keys, or client assertions.

### Pinned confidential-client mTLS parity mapping

The applicable active reference cases below are pinned, rather than inferred
from similarly named scenarios:

- .NET `c8b90e9de5bc4974de9928835532faac5c88e2d2`:
  [client-credentials/FIC cases][dotnet-cc] and [transport/factory cases][dotnet-transport].
- Go `903eb9f1e54e361525025ea27040ac9644b64358`:
  [PoP, ordinary certificate, cache, and regional cases][go-pop].
- Go `30f4770ffcbd9c4c51d278cdab6dbda71a0f7337`:
  [regional FIC Bearer addition][go-fic] and [Bearer transport cases][go-bom].
- Go HTTP follow-up `45e70aa9590f231fc1ef739da78eb91804c11dd6`:
  [same active Bearer transport assertions][go-http], with API/comment changes,
  not additional active custom-factory E2E cases.

`C` means `tests.test_e2e.CertificateScenariosTestCase`; `F` means
`tests.test_e2e.FicTwoLegTestCase`. Every row is applicable and live.

| Active reference case(s) | Python method(s) | Required observation |
| --- | --- | --- |
| Go `TestCredential_X509_Output_Pop`; .NET `Credential_X509_Output_Pop_TestAsync` | `C.test_credential_x509_output_pop`, `C.test_credential_x509_custom_http_output_pop` | Nonempty Graph PoP, returned certificate and JWT SHA256 `cnf` binding, resource HTTP 200; default case adds all three same-token rejection markers and cached repeat. Custom case proves the derived factory pipeline. |
| Go `TestCredential_X509_Output_Bearer` | `C.test_credential_x509_output_bearer` | Ordinary westus3 Key Vault Bearer, no output binding, signed certificate assertion, cache repeat. The ordinary regional POST retains the central assertion audience by design. |
| Go `TestCredential_X509_Output_Pop_Regional` | `C.test_credential_x509_output_pop_regional` | westus3 Key Vault PoP, certificate/JWT binding, same cached token and type; not a redirected global Graph cell. |
| Go `TestConfidentialClientSNIBearerAndMtlsPoPCacheIsolated` | `C.test_bearer_then_mtls_pop_cache_isolated` | One app/cache/scope, Bearer then PoP, distinct tokens and schemes, no Bearer binding, both cache results preserved. |
| Go `TestConfidentialClientSNIMtlsPoPThenBearerCacheIsolated` | `C.test_mtls_pop_then_bearer_cache_isolated` | Reverse acquisition order with the same isolation and reusable PoP result. |
| Go `TestSendCertificateOverMtls_ClientCredential_Live`; .NET `Credential_X509_Output_Bearer_TestAsync` | `C.test_credential_x509_send_certificate_over_mtls_output_bearer` | Built-in real mTLS POST, westus3 Key Vault Bearer, signed client assertion, no PoP markers/output binding, cached repeat. |
| Go `TestTwoLegFICMtlsPoP_SNI`; .NET `Credential_Fic_Output_Pop_TestAsync` / `RunTwoLegS2sFicBothLegsPopAsync` | `F.test_credential_fic_output_pop`, `F.test_credential_fic_custom_http_output_pop` | Global exchange leg 1 to Graph leg 2, provider invoked, exact leg-1 assertion sent as `jwt-pop`, certificate continuity, final audience/type/`cnf`, resource HTTP 200. |
| Go `TestTwoLegFICMtlsBearer_SNI`; .NET `Credential_Fic_Output_Bearer_TestAsync` | `F.test_credential_fic_output_bearer`, `F.test_credential_fic_custom_http_output_bearer` | westus3 exchange to Storage, provider and actual `jwt-pop` wire shape, certificate continuity, Storage Bearer audience, no final binding or downstream mTLS resource call. |
| Go `TestSendCertificateOverMtls_OnBehalfOf_RequestCapture`; .NET `OboFlow_WithSendCertificateOverMtls_BothMtlsConditionsMet` | `C.test_send_certificate_over_mtls_on_behalf_of_request_capture`, `C.test_custom_http_send_certificate_over_mtls_on_behalf_of_request_capture` | Live invalid-grant rejection over real mTLS; OBO grant and `requested_token_use`, signed `jwt-bearer`, mtlsauth URL, no PoP markers. |
| .NET `RefreshTokenFlow_WithSendCertificateOverMtls_BothMtlsConditionsMet` | `C.test_send_certificate_over_mtls_refresh_token_request_capture`, `C.test_custom_http_send_certificate_over_mtls_refresh_token_request_capture` | Live invalid-grant rejection, actual refresh-token grant and signed assertion over the real selected mTLS pipeline. |
| Go `TestSendCertificateOverMtls_AuthCode_RequestCapture`; .NET `AuthCodeFlow_WithSendCertificateOverMtls_BothMtlsConditionsMetAsync` | `C.test_send_certificate_over_mtls_auth_code_request_capture`, `C.test_custom_http_send_certificate_over_mtls_auth_code_request_capture` | Live invalid-grant rejection, authorization-code/redirect fields, signed assertion, real mTLS endpoint, no PoP markers. |
| .NET `OboFlow_WithoutSendCertificateOverMtls_UsesRegularEndpointAsync` | `C.test_without_send_certificate_over_mtls_on_behalf_of_regular_endpoint`, `C.test_custom_http_without_send_certificate_over_mtls_on_behalf_of_regular_endpoint` | Live rejection capture at the ordinary endpoint with signed assertion; custom control creates no certificate-scoped child. |
| .NET `ClientCredentials_WithSendCertificateOverMtls_BothMtlsConditionsMet` | `C.test_credential_x509_custom_http_send_certificate_over_mtls_output_bearer` | Successful westus3 Key Vault Bearer acquisition through `create_mtls_client(binding_context)` and its derived real POST pipeline, middleware and caller-owned cleanup intact. |

The retained `C.test_subject_name_issuer_authentication` is an additional
required legacy SN/I lab cell. Supported PoP/FIC custom-pipeline combinations
exercise the combined features without adding SDK APIs.

Excluded explicitly: IMDSv2; non-exportable/VBS/provider-backed keys; managed
identity; unrelated legacy skips; Go's disabled/opt-in successful OBO and
refresh acquisitions; .NET's disabled `OboFlow_AcquiresTokenAsync` and
`RefreshTokenFlow_AcquiresTokenAsync`; and .NET-only callback correlation-ID
options without a Python API equivalent. This suite does not provision apps
or claim repository-wide skip elimination.

[dotnet-cc]: https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/c8b90e9de5bc4974de9928835532faac5c88e2d2/tests/Microsoft.Identity.Test.Integration.netcore/HeadlessTests/ClientCredentialsMtlsPopTests.cs
[dotnet-transport]: https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/c8b90e9de5bc4974de9928835532faac5c88e2d2/tests/Microsoft.Identity.Test.Integration.netcore/HeadlessTests/MtlsTransportUserFlowTests.cs
[go-pop]: https://github.com/AzureAD/microsoft-authentication-library-for-go/blob/903eb9f1e54e361525025ea27040ac9644b64358/apps/tests/integration/mtls_pop_integration_test.go
[go-fic]: https://github.com/AzureAD/microsoft-authentication-library-for-go/blob/30f4770ffcbd9c4c51d278cdab6dbda71a0f7337/apps/tests/integration/mtls_pop_integration_test.go
[go-bom]: https://github.com/AzureAD/microsoft-authentication-library-for-go/blob/30f4770ffcbd9c4c51d278cdab6dbda71a0f7337/apps/tests/integration/bearer_mtls_integration_test.go
[go-http]: https://github.com/AzureAD/microsoft-authentication-library-for-go/blob/45e70aa9590f231fc1ef739da78eb91804c11dd6/apps/tests/integration/bearer_mtls_integration_test.go

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
