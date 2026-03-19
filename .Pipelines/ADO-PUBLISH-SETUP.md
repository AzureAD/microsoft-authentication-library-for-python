# ADO Pipeline Setup Guide — MSAL Python → PyPI

This document describes every step needed to create an Azure DevOps (ADO)
pipeline that checks out the GitHub repo, runs tests, builds distributions,
and publishes to test.pypi.org (via the MSAL-Python environment) and PyPI.

The `.Pipelines/` folder follows the same template convention as [MSAL.NET](https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/tree/main/build):

| File | Purpose |
|------|---------|
| [`pipeline-publish.yml`](pipeline-publish.yml) | Top-level orchestrator — triggers, variables, stage wiring |
| [`template-run-tests.yml`](template-run-tests.yml) | Reusable step template — pytest across Python version matrix |
| [`template-build-package.yml`](template-build-package.yml) | Reusable step template — `python -m build` + `twine check` + artifact publish |
| [`template-publish-package.yml`](template-publish-package.yml) | Reusable step template — `TwineAuthenticate` + `twine upload` (parameterized for MSAL-Python/PyPI) |

---

## Overview

This pipeline is **manually triggered only** — no automatic branch or tag triggers.
Every publish requires explicitly entering a version and selecting a destination.

| Stage | Trigger | Target |
|-------|---------|--------|
| **Validate** | always | asserts `packageVersion` matches `msal/sku.py` |
| **CI** (tests on Py 3.9–3.13) | after Validate | — |
| **Build** (sdist + wheel) | after CI | dist artifact |
| **PublishMSALPython** | `publishTarget = test.pypi.org (Preview / RC)` | test.pypi.org |
| **PublishPyPI** | `publishTarget = pypi.org (Production)` | PyPI (production) |

---

## Step 1 — Prerequisites

| Requirement | Notes |
|-------------|-------|
| ADO Organization | [Create one](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/create-organization) if you don't have one |
| ADO Project | Under the org; enable **Pipelines** and **Artifacts** |
| GitHub account with admin rights | Needed to authorize the ADO GitHub App |
| PyPI API token | Scoped to the `msal` project — generate at <https://pypi.org/manage/account/token/> |
| MSAL-Python (test.pypi.org) API token | Scoped to the `msal` project on test.pypi.org |

---

## Step 2 — Connect ADO to the GitHub Repository

1. In your ADO project go to **Project Settings → Service connections → New service connection**.
2. Choose **GitHub** and click **Next**.
3. Under **Authentication**, select **Grant authorization** (OAuth) — do **not** use Personal Access Token.
   - Click **Authorize** — a GitHub OAuth popup will open.
   - Sign in with a GitHub account that has admin rights on the `AzureAD` organization.
   - Grant access to `microsoft-authentication-library-for-python`.
   - This installs the Azure Pipelines GitHub App and enables webhook and repository listing.

   > **Why OAuth and not PAT:** PAT-based connections cannot install the GitHub webhook
   > required for pipeline creation via CLI or API. The OAuth/GitHub App flow installs the
   > webhook using the browser's authenticated GitHub session.

4. Set **Service connection name**: `github-msal-python`
5. Check **Grant access permission to all pipelines**, click **Save**.

---

## Step 3 — Create PyPI Service Connections (Twine)

The `TwineAuthenticate@1` task uses "Python package upload" service connections
for external registries.

### 3a — MSAL-Python (test.pypi.org) connection

1. **Project Settings → Service connections → New service connection**
2. Choose **Python package upload**, click **Next**.
3. Fill in:
   | Field | Value |
   |-------|-------|
   | **Twine repository URL** | `https://test.pypi.org/legacy/` |
   | **EndpointName** (`-r` value) | `MSAL-Test-Python-Upload` |
   | **Authentication method** | **Authentication Token** |
   | **Token** | *(your test.pypi.org API token, full value including `pypi-` prefix)* |
   | **Service connection name** | `MSAL-Test-Python-Upload` |
4. Check **Grant access permission to all pipelines**, click **Save**.

### 3b — PyPI (production) connection

1. **Project Settings → Service connections → New service connection**
2. Choose **Python package upload**, click **Next**.
3. Fill in:
   | Field | Value |
   |-------|-------|
   | **Twine repository URL** | `https://upload.pypi.org/legacy/` |
   | **EndpointName** (`-r` value) | `MSAL-Prod-Python-Upload` |
   | **Authentication method** | **Authentication Token** |
   | **Token** | *(your PyPI API token, full value including `pypi-` prefix)* |
   | **Service connection name** | `MSAL-Prod-Python-Upload` |
4. Check **Grant access permission to all pipelines**, click **Save**.

> **Security note:** Never commit API tokens to source control.  All secrets
> are stored in ADO service connections and injected by `TwineAuthenticate@1`
> via the ephemeral `$(PYPIRC_PATH)` file at pipeline runtime.

---

## Step 4 — Create ADO Environments

Environments let you add approval gates before the deployment jobs run.

1. Go to **Pipelines → Environments → New environment**.
2. Create two environments:

   | Name | Description |
   |------|-------------|
   | `MSAL-Python` | Staging — test.pypi.org uploads |
   | `MSAL-Python-Release` | Production — PyPI uploads (**add approval check**) |

3. For the `MSAL-Python-Release` environment:
   - Click the `MSAL-Python-Release` environment → **Approvals and checks → +**
   - Add **Approvals** → add the release approver(s) (e.g., release manager).
   - This ensures a human must approve before the wheel is pushed to production.

---

## Step 5 — Create the Pipeline in ADO

1. Go to **Pipelines → New pipeline**.
2. Select **GitHub** as the code source.
3. Pick the repository **AzureAD/microsoft-authentication-library-for-python**.
   - ADO will use the `github-msal-python` service connection created in Step 2.
4. Choose **Existing Azure Pipelines YAML file**.
5. Set the path to: `/.Pipelines/pipeline-publish.yml`
6. Click **Continue** → review the YAML → click **Save** (not *Run*).
7. Rename the pipeline to something descriptive, e.g.
   `msal-python · publish`.

> **Note:** The existing `azure-pipelines.yml` (CI-only, runs on `dev`) is a
> separate pipeline and is not affected.

---

## Step 6 — Authorize Pipelines to use Service Connections

When the pipeline first uses a service connection you may be prompted to
authorize it.  To pre-authorize:

1. **Project Settings → Service connections** → click a connection →
   **Security** tab.
2. Set the **Pipeline permissions** to include the new publish pipeline.

Repeat for all three connections: `github-msal-python`, `MSAL-Test-Python-Upload`,
`MSAL-Prod-Python-Upload`.

---

## Step 7 — Pipeline Parameters (Run Pipeline UI)

This pipeline is **always manually queued**. Both fields are required — the Validate stage fails if either is missing or the version doesn’t match `msal/sku.py`:

| Parameter | Required | Description | Example values |
|-----------|----------|-------------|----------------|
| **Package version to publish** | Yes | Must exactly match `msal/sku.py __version__`. PEP 440 format only — no `-Preview` suffix. | `1.36.0` (release), `1.36.0rc1` (RC), `1.36.0b1` (beta) |
| **Publish target** | Yes | Explicit destination — no auto-routing. | `test.pypi.org (Preview / RC)` or `pypi.org (Production)` |

> **Version format:** PyPI enforces [PEP 440](https://peps.python.org/pep-0440/). Versions with `-` (e.g. `1.36.0-Preview`) are rejected. Use `rc1`, `b1`, or `a1` suffixes instead.

> **Version must be in sync:** Before queuing, update `msal/sku.py __version__` to the target version and push the change. The Validate stage checks the value on the branch the run is sourced from, not the pipeline default branch.

---

## Step 8 — End-to-End Release Walkthrough

### Publishing a preview / release candidate to test.pypi.org

1. Set `msal/sku.py __version__ = "1.36.0rc1"` and push the change
2. Go to **Pipelines → MSAL-Python · Publish → Run pipeline**
3. Select the branch/tag to run from (e.g. the release branch)
4. Enter **Package version to publish**: `1.36.0rc1`
5. Select **Publish target**: `test.pypi.org (Preview / RC)`
6. Click **Run** — pipeline runs: Validate → CI → Build → Publish to test.pypi.org
7. Verify at <https://test.pypi.org/project/msal/>

### Publishing a production release to PyPI

1. Set `msal/sku.py __version__ = "1.36.0"` and push to the release branch
2. Go to **Pipelines → MSAL-Python · Publish → Run pipeline**
3. Select the release branch
4. Enter **Package version to publish**: `1.36.0`
5. Select **Publish target**: `pypi.org (Production)`
6. Click **Run** — pipeline runs: Validate → CI → Build → Publish to PyPI (Production)
7. Verify: `pip install msal==1.36.0` or check <https://pypi.org/project/msal/>

## Pipeline Trigger Reference

```
Manual queue (publishTarget = MSAL-Python)
  └─►  Validate  ─►  CI  ─►  Build  ─►  PublishMSALPython
                                              (test.pypi.org, auto)

Manual queue (publishTarget = pypi)
  └─►  Validate  ─►  CI  ─►  Build  ─►  PublishPyPI
                                              (PyPI, requires approval)
```

---

## Known Requirements

The following requirements were identified during initial setup and testing:

- The GitHub service connection **must** be created via OAuth (Grant authorization) in the ADO UI, not via CLI or PAT. The CLI `az pipelines create` command requires webhook installation on the GitHub repo, which requires org admin rights not available to service accounts.
- The pipeline **must** be created via the ADO REST API (`/_apis/build/definitions`) or UI — not via `az pipelines create` — when using an OAuth GitHub service connection without org-level admin rights.
- The `msal/sku.py __version__` must be updated and pushed to the source branch **before** the pipeline run is queued. The Validate stage reads the file from the checked-out branch at runtime.
- The `requirements.txt` file includes `-e .` which causes pip to install `msal` from PyPI as a transitive dependency of `azure-identity`, overwriting the local editable install. The template handles this by removing the `-e .` line and reinstalling the local package last with `--no-deps`.
- The `1.35.1` version bump (hotfix) was released from `origin/release-1.35.0` and was never merged back into `dev`. Before the next release from `dev`, this should be backfilled via PR: `https://github.com/AzureAD/microsoft-authentication-library-for-python/compare/dev...release-1.35.0`

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `403` on twine upload | Token expired or wrong scope | Regenerate API token on pypi.org; update the service connection |
| `File already exists` error | Version already published; PyPI does not allow overwriting | Bump version in `msal/sku.py` |
| Validate stage: `msal/sku.py ''` (empty version) | Python import silently failed | The template uses `grep`/`sed` to read the version — verify `msal/sku.py` contains a `__version__ = "..."` line |
| Validate stage: version mismatch | `sku.py` on the source branch doesn't match the parameter entered | Update `msal/sku.py` on the branch the run is sourced from, not just the pipeline default branch |
| Tests: collection failure across all modules | PyPI `msal` installed over the local editable install | Ensure the template installs local package last with `--no-deps` |
| `az pipelines create` fails with webhook error | GitHub service connection PAT/account lacks org admin rights | Create the pipeline via the ADO UI using a browser session with org admin GitHub access |
| Pipeline creation fails: `Value cannot be null. Parameter name: Connection` | GitHub SC ID is wrong or SC was recreated | Re-query the SC ID with `az devops service-endpoint list` and use the current ID |
| Service connection shows `Authentication: PersonalAccessToken` | SC was created via CLI with a PAT | Delete and recreate via UI using OAuth (Grant authorization) so repos are enumerable |
| `TwineAuthenticate` says endpoint not found | Service connection name mismatch | Ensure `pythonUploadServiceConnection` value exactly matches the service connection name |

---

## References

- [Publish Python packages with Azure Pipelines](https://learn.microsoft.com/en-us/azure/devops/pipelines/artifacts/pypi?view=azure-devops)
- [TwineAuthenticate@1 task reference](https://learn.microsoft.com/en-us/azure/devops/pipelines/tasks/reference/twine-authenticate-v1?view=azure-devops)
- [Publish and download Python packages with Azure Artifacts](https://learn.microsoft.com/en-us/azure/devops/artifacts/quickstarts/python-packages?view=azure-devops)
- [Python package upload service connection](https://learn.microsoft.com/en-us/azure/devops/pipelines/library/service-endpoints#python-package-upload-service-connection)
- [ADO Environments – approvals and checks](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals?view=azure-devops)
