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

## 4) Run unit/integration tests

Run all non-E2E tests quickly:

```powershell
python -m pytest -q tests -k "not e2e"
```

Run full E2E unattended suite:

```powershell
python -m pytest -q tests/test_e2e.py
```

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
