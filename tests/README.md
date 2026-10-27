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

Both default-transport cells require real token POST evidence for each leg,
the expected global or `westus3` mTLS endpoint, and the same lab certificate.
Leg 2 must invoke the assertion provider and send its exact, unchanged leg-1
value as a `jwt-pop` client assertion. The exchange assertion may be opaque;
it is not assumed to be a resource JWT. Final resource tokens must be JWTs
with the Graph or Storage audience. Final Graph PoP additionally requires a
matching SHA256 `cnf` and returned `MtlsBindingContext`, then HTTP 200 from the
mTLS Graph resource using that context.

The required FIC inventory is exactly these two methods in
`tests.test_e2e.FicTwoLegTestCase`:

- `test_credential_fic_output_bearer`
- `test_credential_fic_output_pop`

They run without skips, including on tagged releases. Custom HTTP-factory
acquisitions are not part of this default-FIC branch.

Run these two live cells with:

```powershell
python -m pytest -q tests\test_e2e.py::FicTwoLegTestCase
```

The offline `tests\test_fic_e2e_helpers.py` checks the two-leg provider/wire
contract, opaque exchange assertions, strict final binding/audience, and exact
assertion handoff. Its privacy regression uses pytest's actual traceback
renderer with arguments shown and locals disabled, and rejects full tokens
and truncated token fragments. These mock-driven guards are not evidence of
live acquisition or a custom HTTP factory.

```powershell
python -m pytest -q tests\test_fic_e2e_helpers.py tests\test_lab_config.py tests\test_e2e.py::FicScenarioTestCase
```

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
