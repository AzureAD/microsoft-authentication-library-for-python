# MSAL Python — Release Guide

This document provides step-by-step instructions for releasing a new version of `msal` to PyPI.

---

## Prerequisites

- You have push access to the [AzureAD/microsoft-authentication-library-for-python](https://github.com/AzureAD/microsoft-authentication-library-for-python) repository.
- The following GitHub repository secrets are configured:
  - `TEST_PYPI_API_TOKEN` — API token for [TestPyPI](https://test.pypi.org/)
  - `PYPI_API_TOKEN` — API token for [PyPI](https://pypi.org/)

---

## Version Location

The package version is defined in a single file:

```
msal/sku.py  →  __version__ = "x.y.z"
```

`setup.cfg` reads it dynamically via `version = attr: msal.__version__`, so **no other file needs updating**.

---

## Branch Strategy

```
dev  (all development happens here)
 │
 │── feature/fix PR → merged into dev
 │
 ├──► release-1.35.0  (version branch, cut from dev when ready)
 │       │
 │       ├── TestPyPI publish (automatic on push)
 │       │
 │       ├── bug found? fix on dev, merge dev → release-1.35.0
 │       │       │
 │       │       └── TestPyPI re-publish (automatic)
 │       │
 │       ├── tag 1.35.0 (via GitHub Release) → PyPI publish
 │       │
 │       │   ── post-release hotfix needed? ──
 │       │
 │       ├── fix on dev, merge dev → release-1.35.0
 │       │       │
 │       │       ├── bump sku.py to 1.35.1
 │       │       │
 │       │       ├── TestPyPI re-publish (automatic)
 │       │       │
 │       │       └── tag 1.35.1 (via GitHub Release) → PyPI publish
 │       │
 │       └── (repeat for further patches: 1.35.2, 1.35.3, ...)
 │
 ├──► release-1.36.0  (next minor version, cut from dev)
 │       │
 │       ├── TestPyPI publish
 │       │
 │       ├── tag 1.36.0 → PyPI publish
 │       │
 │       └── patches: merge dev → bump → tag 1.36.1, 1.36.2, ...
 ...
```

- **`dev`** — All feature work, bug fixes, and PRs land here.
- **`release-x.y.z`** — Version branch cut from `dev` when ready to release. Used for final validation and TestPyPI testing.
- **Tags** — Created from the version branch via GitHub Releases to trigger production PyPI publish.

---

## Step-by-Step Release Process

### 1. Complete All Work on `dev`

- All features, fixes, and version bumps should be merged into `dev` via PRs.
- Ensure CI passes on `dev`.
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

This push triggers the CD pipeline:
- CI runs tests (must pass).
- CD publishes to **TestPyPI** automatically.
- Verify at: https://test.pypi.org/project/msal/

### 3. Apply Patches (If Needed)

If bugs are found during validation:

1. Fix the bug on `dev` first (via a PR to `dev`).
2. Merge `dev` into the version branch:
   ```bash
   git checkout release-1.35.0
   git merge dev
   git push origin release-1.35.0
   ```
3. This triggers another TestPyPI publish. Bump the version to `1.35.1` if the previous version was already published.

### 4. Create a GitHub Release (Production Publish)

Once the version branch is validated:

1. Go to **GitHub → Releases → Create a new release**.
2. Click **"Choose a tag"** and type the version (e.g., `1.35.0`) — select **"Create new tag on publish"**.
3. Set **Target** to the `release-1.35.0` branch.
4. Set **Release title** to `1.35.0`.
5. Add release notes (changelog, breaking changes, etc.).
6. Click **"Publish release"**.

This creates a tag, which triggers the CD pipeline to publish to **PyPI**.

Verify at: https://pypi.org/project/msal/

### 5. Post-Release

- Verify installation: `pip install msal==1.35.0`
- If the version on `dev` hasn't been bumped yet, open a PR to bump `msal/sku.py` to the next dev version (e.g., `1.36.0`).

---

## Hotfix Releases

For urgent fixes on an already-released version:

1. Fix the issue on `dev` (via PR).
2. Merge `dev` into the existing `release-x.y.z` branch.
3. Update `msal/sku.py` to the patch version (e.g., `1.35.0` → `1.35.1`).
4. Push the version branch (triggers TestPyPI).
5. Create a GitHub Release with tag `1.35.1` targeting `release-1.35.0`.

---

## How the CI/CD Pipeline Works

| Job | Trigger | Purpose |
|-----|---------|---------|
| **ci** | Every push and labeled PR to `dev` | Runs tests on Python 3.8–3.14 |
| **cb** | After CI passes | Runs benchmarks |
| **cd** | Push to `release-*` branch or tag | Builds and publishes the package |

| Trigger | Target |
|---------|--------|
| Push to `release-*` branch | **TestPyPI** |
| Tag (created via GitHub Release) | **PyPI** (production) |

---

## Quick Reference

```bash
# 1. Ensure dev is ready, version bumped in msal/sku.py
# 2. Cut version branch
git checkout dev && git pull
git checkout -b release-1.35.0
git push origin release-1.35.0
# → TestPyPI publish happens automatically

# 3. If patches needed: fix on dev, then merge into release branch
git checkout release-1.35.0
git merge dev
git push origin release-1.35.0

# 4. Production release: create a GitHub Release
#    → GitHub.com → Releases → New release
#    → Tag: 1.35.0, Target: release-1.35.0
#    → PyPI publish happens automatically
```
