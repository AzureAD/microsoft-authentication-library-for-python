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

These cover ordinary SN/I certificate Bearer authentication, global X509 mTLS
PoP acquisition with a certificate-bound Graph resource call and cache reuse,
and ordinary certificate Bearer authentication in `westus3`. The X509 matrix
uses client `163ffef9-a313-45b4-ab2f-c7e2f5e0e23e` in tenant
`bea21ebe-8b64-4d06-9f6d-6a889b120a7c`, matching the .NET and Go lab setup.
They do not require fetching unrelated user configuration from Key Vault.
Legacy interactive and other optional E2E skips are unchanged.

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

The four custom-recorder guards render actual pytest failures with argument
display enabled and locals disabled, check constructor/POST/capture redaction,
and retain the sample's response middleware and owner/child cleanup.
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
