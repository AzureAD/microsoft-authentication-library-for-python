# CI/CD Pipelines

This document describes the pipeline structure for the `msal` Python package,
including what each pipeline does, when it runs, and how to trigger a release.

---

## Pipeline Files

| File | ADO Pipeline | Purpose |
|------|-------------|---------|
| [`azure-pipelines.yml`](../azure-pipelines.yml) | [MSAL.Python-PR-OneBranch-Official (3064)](https://dev.azure.com/IdentityDivision/IDDP/_build?definitionId=3064) | PR gate, post-merge CI, and performance benchmarks — calls the shared template with `runPublish: false`; runs benchmarks on post-merge pushes to `dev` |
| [`pipeline-publish.yml`](pipeline-publish.yml) | [MSAL.Python-Publish (3067)](https://dev.azure.com/IdentityDivision/IDDP/_build?definitionId=3067) | Release pipeline — manually queued, builds and publishes to PyPI |
| [`template-pipeline-stages.yml`](template-pipeline-stages.yml) | — | Shared stages template — PreBuildCheck, Validate, UnitTests, and E2ETests stages reused by both pipelines |
| [`credscan-exclusion.json`](credscan-exclusion.json) | — | CredScan suppression file for known test fixtures |

---

## PR / CI Pipeline — [MSAL.Python-PR-OneBranch-Official (3064)](https://dev.azure.com/IdentityDivision/IDDP/_build?definitionId=3064)

### Triggers

| Event | Branches |
|-------|----------|
| Pull request opened / updated | `dev` (PRs targeting `dev` only) |
| Push / merge | `dev` |
| Scheduled | Daily at 11:45 PM Pacific, `dev` branch (only when there are new changes) |

Fast unit-test feedback for PRs targeting **other** branches (e.g. `release-x.y.z`)
is provided separately by the GitHub Actions workflow
[`.github/workflows/python-package.yml`](../.github/workflows/python-package.yml),
which runs the package build and unit tests on every PR.

### Stages

```
PreBuildCheck ─► UnitTests ─► E2ETests ─► Benchmark (post-merge to dev only)
```

| Stage | What it does | When it runs |
|-------|-------------|-------------|
| **PreBuildCheck** | Runs SDL security scans: PoliCheck (policy/offensive content), CredScan (leaked credentials), and PostAnalysis (breaks the build on findings) | Always |
| **UnitTests** | Runs the unit test suite on Python 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14 (no Key Vault required) | After PreBuildCheck |
| **E2ETests** | Fetches the MSID Lab certificate from Key Vault and runs `tests/test_e2e.py` + `tests/test_fmi_e2e.py` on the same Python matrix. On forked PRs the stage still runs, but the Key Vault tasks are skipped and the E2E tests self-skip (because `LAB_APP_CLIENT_CERT_PFX_PATH` is unset), so the stage reports green with all E2E tests marked Skipped in the Tests tab. | After UnitTests |
| **Benchmark** | Runs performance benchmarks on Python 3.9 and publishes `benchmark-results` artifact | Post-merge pushes to `dev` and manual runs only |

The `Validate` stage is **skipped** on PR/CI runs (it only applies to release builds).

> **SDL coverage:** The PreBuildCheck stage satisfies the OneBranch SDL requirement.
> It runs on every PR, every merge to `dev`, and on the daily schedule — ensuring
> continuous security scanning without a separate dedicated SDL pipeline.

---

## Release Pipeline — [MSAL.Python-Publish (3067)](https://dev.azure.com/IdentityDivision/IDDP/_build?definitionId=3067)

### Triggers

**Manual only** — no automatic branch or tag triggers. Must be queued explicitly
with both parameters filled in.

### Parameters

| Parameter | Description | Example values |
|-----------|-------------|----------------|
| **Package version to publish** | Must exactly match `msal/sku.py __version__`. [PEP 440](https://peps.python.org/pep-0440/) format. | `1.36.0`, `1.36.0rc1`, `1.36.0b1` |
| **Publish target** | Destination for this release. | `test.pypi.org (Preview / RC)` or `pypi.org (ESRP Production)` |

### Stage Flow

```
PreBuildCheck ─► Validate ─► UnitTests ─► E2ETests ─► Build ─┬─► PublishMSALPython  (publishTarget == 'test.pypi.org (Preview / RC)')
                                                       └─► PublishPyPI        (publishTarget == 'pypi.org (ESRP Production)')
```

| Stage | What it does | Condition |
|-------|-------------|-----------|
| **PreBuildCheck** | PoliCheck + CredScan scans | Always |
| **Validate** | Asserts the `packageVersion` parameter matches `msal/sku.py __version__` | Always (release runs only) |
| **UnitTests** | Unit test matrix (Python 3.9–3.14) | After Validate passes |
| **E2ETests** | E2E test matrix (Python 3.9–3.14) with MSID Lab cert from Key Vault | After UnitTests passes |
| **Build** | Builds `sdist` and `wheel` via `python -m build`; publishes `python-dist` artifact | After E2ETests passes |
| **PublishMSALPython** | Uploads to test.pypi.org | `publishTarget == test.pypi.org (Preview / RC)` |
| **PublishPyPI** | Uploads to PyPI via ESRP (`EsrpRelease@12`); requires manual approval | `publishTarget == pypi.org (ESRP Production)` |

> ⚠️ **TestPyPI publishing is currently a no-op.** The `MSAL-Test-Python-Upload`
> service connection has not yet been created (pending a test.pypi.org API
> token), so the `PublishMSALPython` stage prints a skip message rather than
> uploading. Until the SC exists, use the `pypi.org (ESRP Production)` path
> with an RC version (e.g. `1.36.0rc1`) for end-to-end validation.

---

## How to Publish a Release

### Step 1 — Update the version

Edit `msal/sku.py` and set `__version__` to the target version:

```python
__version__ = "1.36.0rc1"   # RC / preview
__version__ = "1.36.0"      # production release
```

Push the change to the branch you intend to release from.

### Step 2 — Queue the pipeline

1. Go to the **MSAL.Python-Publish** pipeline in ADO.
2. Click **Run pipeline**.
3. Select the branch to release from.
4. Enter the **Package version to publish** (must match `msal/sku.py` exactly).
5. Select the **Publish target**:
   - `test.pypi.org (Preview / RC)` — for release candidates and previews
   - `pypi.org (ESRP Production)` — for final releases (requires approval gate)
6. Click **Run**.

### Step 3 — Approve (production releases only)

The `pypi.org (ESRP Production)` path includes a required manual approval before
the package is uploaded. An approver must review and approve in the ADO
**Environments** panel before the `PublishPyPI` stage proceeds.

### Step 4 — Verify

- **test.pypi.org:** https://test.pypi.org/project/msal/
- **PyPI:** https://pypi.org/project/msal/

---

## Version Format

PyPI enforces [PEP 440](https://peps.python.org/pep-0440/). Versions with `-` (e.g. `1.36.0-Preview`) are rejected at upload time. Use standard suffixes:

| Release type | Format |
|-------------|--------|
| Production | `1.36.0` |
| Release candidate | `1.36.0rc1` |
| Beta | `1.36.0b1` |
| Alpha | `1.36.0a1` |
