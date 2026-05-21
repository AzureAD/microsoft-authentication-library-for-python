# MSAL Python — Release Guide

How to ship a new version of `msal` to PyPI. Everything happens in Azure DevOps
(no GitHub Releases, no Git-tag-triggered automation).

---

## Before you start — one-time prerequisites

Confirm these are set up in ADO (`IdentityDivision` → `IDDP` project) — the
release will fail at runtime if any are missing:

- [ ] Pipeline **MSAL.Python-Publish** (definition `3067`) exists
- [ ] Service connection **`MSAL-ESRP-AME`** exists and is authorized
- [ ] Environment **`MSAL-Python-Release`** has a **required manual approval**
      configured under *Approvals and checks*
- [ ] Key Vault **`MSALVault`** contains cert **`MSAL-ESRP-Release-Signing`**

> **Note on TestPyPI:** The TestPyPI publish path
> (`publishTarget = test.pypi.org (Preview / RC)`) is currently a **no-op** —
> the `MSAL-Test-Python-Upload` service connection has not been created yet,
> so that stage prints a skip message and uploads nothing. Until it's wired up,
> use an RC version (e.g. `1.36.0rc1`) on the production path for dry runs.

---

## Release in 4 steps

### 1. Bump the version on `dev`

The package version lives in **one file**: [msal/sku.py](msal/sku.py).

```python
__version__ = "1.36.0"     # final release
# or
__version__ = "1.36.0rc1"  # RC / dry run
```

Open a PR, get it merged into `dev`.

### 2. Cut the release branch

```bash
git checkout dev && git pull
git checkout -b release-1.36.0
git push origin release-1.36.0
```

Pushing the branch does **not** publish anything — the pipeline is manual.

### 3. Queue the publish pipeline

ADO → **IDDP** → **MSAL.Python-Publish (3067)** → **Run pipeline**:

| Field | Value |
|-------|-------|
| Branch | `release-1.36.0` |
| **Package version to publish** | `1.36.0` (must match `msal/sku.py` exactly) |
| **Publish target** | `pypi.org (ESRP Production)` |

The pipeline runs:

```
PreBuildCheck → Validate → UnitTests → E2ETests → Build → PublishPyPI
```

### 4. Approve, then verify

When `PublishPyPI` starts, it pauses for approval on the
`MSAL-Python-Release` environment. An approver clicks **Approve** in
ADO → Pipelines → Environments → MSAL-Python-Release.

ESRP signs the artifact and uploads to PyPI. Verify:

- https://pypi.org/project/msal/
- `pip install msal==1.36.0`

---

## Dry run (before the real release)

Recommended flow to exercise the pipeline end-to-end without burning a real
version number:

1. Set `msal/sku.py` to an RC version: `__version__ = "1.36.0rc1"`
2. Open a PR, merge to `dev`
3. Cut `release-1.36.0` from `dev`
4. Queue **MSAL.Python-Publish**:
   - Package version: `1.36.0rc1`
   - Publish target: `pypi.org (ESRP Production)`
5. Approve when prompted
6. Confirm `pip install msal==1.36.0rc1 --pre` works
7. Then bump to `1.36.0` and run the real release

RC versions are not installed by default by `pip install msal` (they require
`--pre`), so they're safe to push to production PyPI for verification.

---

## Hotfix (patch a released version)

```bash
# Fix on dev first
git checkout dev
# ...PR, merge fix into dev...

# Merge into the existing release branch
git checkout release-1.36.0
git merge dev

# Bump patch version in msal/sku.py: 1.36.0 → 1.36.1
git commit -am "bump to 1.36.1"
git push origin release-1.36.0
```

Then re-queue **MSAL.Python-Publish** with `packageVersion = 1.36.1` and
`pypi.org (ESRP Production)`. Approve.

---

## What changed from the old flow

| Old (GitHub-Actions) | New (ADO) |
|---|---|
| Push to `release-*` auto-published to TestPyPI | Manual queue with `test.pypi.org` target (currently no-op) |
| Push a Git tag auto-published to PyPI | Manual queue with `pypi.org (ESRP Production)` target |
| `PYPI_API_TOKEN` GitHub secret | ESRP service connection `MSAL-ESRP-AME` |
| Anyone with tag-push could ship | Required approver on `MSAL-Python-Release` environment |

Git tags are still allowed for source-tracking, but they trigger nothing.

---

## Pipeline architecture

See [.Pipelines/CI-AND-RELEASE-PIPELINES.md](.Pipelines/CI-AND-RELEASE-PIPELINES.md)
for stage-by-stage detail, triggers, and the shared template structure.
