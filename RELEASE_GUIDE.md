# MSAL Python — Release Guide

This document provides step-by-step instructions for releasing a new version of `msal` to PyPI.

The release pipeline runs in **Azure DevOps (ADO)** — `MSAL.Python-Publish`
(definition `3067`) — and is **manually queued**. There is no longer any
GitHub-Actions or GitHub-Release based publishing path.

For pipeline architecture details, see [.Pipelines/CI-AND-RELEASE-PIPELINES.md](.Pipelines/CI-AND-RELEASE-PIPELINES.md).

---

## Prerequisites

- You have push access to the [AzureAD/microsoft-authentication-library-for-python](https://github.com/AzureAD/microsoft-authentication-library-for-python) repository.
- You have permission to queue runs on the **MSAL.Python-Publish** pipeline in ADO (`IDDP` project).
- For production releases (`pypi.org`), an approver listed on the
  `MSAL-Python-Release` environment must approve the run before publishing
  proceeds.

---

## Version Location

The package version is defined in a single file:

```
msal/sku.py  →  __version__ = "x.y.z"
```

`setup.cfg` reads it dynamically via `version = attr: msal.__version__`, so **no other file needs updating**.

The release pipeline's `Validate` stage asserts that the `packageVersion`
parameter you pass when queuing the pipeline matches `msal/sku.py` exactly —
if they disagree, the run fails before any build or publish happens.

---

## Branch Strategy

```
dev  (all development happens here)
 │
 │── feature/fix PR → merged into dev
 │
 ├──► release-1.35.0  (version branch, cut from dev when ready)
 │       │
 │       ├── queue MSAL.Python-Publish on release-1.35.0
 │       │       with publishTarget = test.pypi.org (Preview / RC)
 │       │
 │       ├── bug found? fix on dev, merge dev → release-1.35.0
 │       │       │
 │       │       └── re-queue MSAL.Python-Publish (test.pypi.org)
 │       │
 │       ├── queue MSAL.Python-Publish on release-1.35.0
 │       │       with publishTarget = pypi.org (ESRP Production)
 │       │       → approval gate → publishes to PyPI
 │       │
 │       │   ── post-release hotfix needed? ──
 │       │
 │       ├── fix on dev, merge dev → release-1.35.0
 │       │       │
 │       │       ├── bump sku.py to 1.35.1
 │       │       │
 │       │       ├── re-queue MSAL.Python-Publish (test.pypi.org)
 │       │       │
 │       │       └── re-queue MSAL.Python-Publish (pypi.org)
 │       │
 │       └── (repeat for further patches: 1.35.2, 1.35.3, ...)
 │
 ├──► release-1.36.0  (next minor version, cut from dev)
 ...
```

- **`dev`** — All feature work, bug fixes, and PRs land here.
- **`release-x.y.z`** — Version branch cut from `dev` when ready to release. Used for final validation and TestPyPI testing. The publish pipeline is queued **against this branch**.
- **Publishing** — Triggered exclusively by queuing the **MSAL.Python-Publish** pipeline in ADO. There are no automatic publishes on push or tag.

---

## Step-by-Step Release Process

### 1. Complete All Work on `dev`

- All features, fixes, and version bumps should be merged into `dev` via PRs.
- Ensure CI passes on `dev` (PR pipeline `MSAL.Python-PR-OneBranch-Official`, definition `3064`).
- Update the version in `msal/sku.py` before cutting the release branch:
  ```python
  __version__ = "1.35.0"
  ```

### 2. Create a Version Branch from `dev`

```bash
git checkout dev
git pull origin dev
git checkout -b release-1.35.0
git push origin release-1.35.0
```

> Pushing the branch does **not** publish anything by itself. Publishing only happens when you queue the MSAL.Python-Publish pipeline (next step).

### 3. Publish to TestPyPI

1. Go to the **MSAL.Python-Publish** pipeline in ADO (`IDDP` project, definition `3067`).
2. Click **Run pipeline**.
3. Select the branch **`release-1.35.0`**.
4. Fill in the parameters:
   - **Package version to publish** = `1.35.0` (must match `msal/sku.py` exactly).
   - **Publish target** = `test.pypi.org (Preview / RC)`.
5. Click **Run**.

The pipeline runs `PreBuildCheck → Validate → UnitTests → E2ETests → Build → PublishMSALPython`.

Verify at: https://test.pypi.org/project/msal/

### 4. Apply Patches (If Needed)

If bugs are found during TestPyPI validation:

1. Fix the bug on `dev` first (via a PR to `dev`).
2. Merge `dev` into the version branch:
   ```bash
   git checkout release-1.35.0
   git merge dev
   git push origin release-1.35.0
   ```
3. Bump `msal/sku.py` to the next version (e.g. `1.35.0rc2` or `1.35.1`) if the previous one is already on TestPyPI — TestPyPI does not allow re-uploading the same version.
4. Re-queue the MSAL.Python-Publish pipeline with the new version and `test.pypi.org (Preview / RC)`.

### 5. Publish to PyPI (Production)

Once the release branch is validated on TestPyPI:

1. Go to the **MSAL.Python-Publish** pipeline in ADO.
2. Click **Run pipeline**.
3. Select the branch **`release-1.35.0`**.
4. Fill in the parameters:
   - **Package version to publish** = `1.35.0`.
   - **Publish target** = `pypi.org (ESRP Production)`.
5. Click **Run**.
6. **Approve the deployment.** The `PublishPyPI` stage targets the
   `MSAL-Python-Release` environment, which has a required manual approval.
   An approver listed on that environment must approve in
   **ADO → Pipelines → Environments → MSAL-Python-Release** before publishing
   proceeds.
7. ESRP signs and publishes the artifact to PyPI.

Verify at: https://pypi.org/project/msal/

### 6. Tag the Release (Optional, for Source Reference Only)

Tagging is no longer part of the publish flow, but you may still create a Git
tag on the released commit for source-tracking purposes:

```bash
git checkout release-1.35.0
git tag 1.35.0
git push origin 1.35.0
```

This is purely informational — it does **not** trigger any pipeline.

### 7. Post-Release

- Verify installation: `pip install msal==1.35.0`
- If the version on `dev` hasn't been bumped yet, open a PR to bump
  `msal/sku.py` to the next dev version (e.g. `1.36.0`).

---

## Hotfix Releases

For urgent fixes on an already-released version:

1. Fix the issue on `dev` (via PR).
2. Merge `dev` into the existing `release-x.y.z` branch.
3. Update `msal/sku.py` to the patch version (e.g. `1.35.0` → `1.35.1`).
4. Queue **MSAL.Python-Publish** on the release branch with
   `test.pypi.org (Preview / RC)`, validate.
5. Queue **MSAL.Python-Publish** again with `pypi.org (ESRP Production)` and
   approve when prompted.

---

## How the ADO Pipelines Work

Two pipelines back this repo:

| Pipeline | Definition | Trigger | Purpose |
|----------|-----------|---------|---------|
| **MSAL.Python-PR-OneBranch-Official** | `3064` | PRs to `dev`, pushes to `dev`, daily schedule | PR gate, post-merge CI, performance benchmarks (no publishing) |
| **MSAL.Python-Publish** | `3067` | **Manual queue only** | Validate version, build, publish to TestPyPI or PyPI |

Stage flow of `MSAL.Python-Publish`:

```
PreBuildCheck ─► Validate ─► UnitTests ─► E2ETests ─► Build ─┬─► PublishMSALPython  (publishTarget == 'test.pypi.org (Preview / RC)')
                                                              └─► PublishPyPI        (publishTarget == 'pypi.org (ESRP Production)')
```

| Publish target | Destination | Service connection | Approval |
|----------------|-------------|--------------------|----------|
| `test.pypi.org (Preview / RC)` | https://test.pypi.org/project/msal/ | `MSAL-Test-Python-Upload` | None |
| `pypi.org (ESRP Production)` | https://pypi.org/project/msal/ | `MSAL-ESRP-AME` (ESRP signing) | Required on `MSAL-Python-Release` environment |

---

## Version Format

PyPI enforces [PEP 440](https://peps.python.org/pep-0440/). Versions with `-`
(e.g. `1.36.0-Preview`) are rejected at upload time. Use standard suffixes:

| Release type | Format |
|--------------|--------|
| Production | `1.36.0` |
| Release candidate | `1.36.0rc1` |
| Beta | `1.36.0b1` |
| Alpha | `1.36.0a1` |

---

## Quick Reference

```bash
# 1. Ensure dev is ready, version bumped in msal/sku.py
# 2. Cut version branch
git checkout dev && git pull
git checkout -b release-1.35.0
git push origin release-1.35.0

# 3. TestPyPI publish — manually queue ADO pipeline
#    ADO → IDDP → MSAL.Python-Publish (3067) → Run pipeline
#      Branch: release-1.35.0
#      Package version to publish: 1.35.0
#      Publish target: test.pypi.org (Preview / RC)

# 4. If patches needed: fix on dev, merge into release branch, bump version
git checkout release-1.35.0
git merge dev
# edit msal/sku.py to bump version
git commit -am "bump to 1.35.1"
git push origin release-1.35.0
#    → re-queue MSAL.Python-Publish with new version on test.pypi.org

# 5. Production release — re-queue ADO pipeline
#    ADO → IDDP → MSAL.Python-Publish (3067) → Run pipeline
#      Branch: release-1.35.0
#      Package version to publish: 1.35.0
#      Publish target: pypi.org (ESRP Production)
#    → approve on MSAL-Python-Release environment
#    → ESRP signs and publishes to PyPI
```
