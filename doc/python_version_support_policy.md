# MSAL Python Version Support Policy

This page describes the Python version support policy for the
Microsoft Authentication Library for Python (MSAL Python), including
end-of-support timelines for each Python version.

This policy is aligned with the
[Azure SDK for Python version support policy](https://github.com/Azure/azure-sdk-for-python/blob/main/doc/python_version_support_policy.md)
so that MSAL Python and the Azure SDK can be consumed together without
version conflicts.

End of support means, in the MSAL Python context, that **new MSAL Python
releases will no longer install on, be tested against, or accept bug
fixes for those Python versions**. Older MSAL Python releases that did
support those Python versions remain installable from PyPI via pip's
`requires-python` resolution, so existing applications continue to work
without change — they simply stop receiving new features and security
fixes.

## Policy

MSAL Python supports a Python version while it is supported upstream by
the Python core team (PSF), plus an additional **6-month grace window**
after the PSF end-of-support date to give applications time to migrate.

Concretely:

- MSAL Python adds support for a new Python release as soon as practical
  after that Python release ships a stable `.0`.
- MSAL Python drops support for a Python version on the **first MSAL
  Python release published on or after the SDK end-of-support date** for
  that Python version (PSF end-of-support + ~6 months).
- Dropping a Python version is a **breaking change** and is delivered in
  a new minor or major release of MSAL Python, never in a patch.
- The release notes (`RELEASES.md`) call out every Python-version
  removal, and `setup.cfg` is updated in the same change to bump
  `python_requires`, the trove classifiers, and any environment markers.

> **Note:** The "MSAL Python End Of Support" date is inclusive — the
> listed day is the last supported day, and the next day is the first
> unsupported day.

## Currently supported versions

| Python Version | PSF End of Support | MSAL Python End Of Support |
|----------------|--------------------|----------------------------|
| 3.9 ([PEP 596](https://peps.python.org/pep-0596/#lifespan))  | October 2025 | April 30, 2026 *(see note)* |
| 3.10 ([PEP 619](https://peps.python.org/pep-0619/#lifespan)) | October 2026 | April 30, 2027 |
| 3.11 ([PEP 664](https://peps.python.org/pep-0664/#lifespan)) | October 2027 | April 30, 2028 |
| 3.12 ([PEP 693](https://peps.python.org/pep-0693/#lifespan)) | October 2028 | April 30, 2029 |
| 3.13 ([PEP 719](https://peps.python.org/pep-0719/#lifespan)) | October 2029 | April 30, 2030 |
| 3.14 ([PEP 745](https://peps.python.org/pep-0745/#lifespan)) | October 2030 | April 30, 2031 |

> **Note on Python 3.9:** Python 3.9 is past its policy end-of-support
> date but is granted a one-time transition grace window in MSAL Python
> while we adopt this policy and complete the removal of Python 3.8. It
> will be removed in a subsequent MSAL Python release; the date will be
> announced in `RELEASES.md` ahead of removal.

## End-of-life versions (no longer supported)

| Python Version | PSF End of Support | MSAL Python End Of Support |
|----------------|--------------------|----------------------------|
| 3.8 ([PEP 569](https://peps.python.org/pep-0569/#lifespan)) | October 2024 | April 2026 |
| 3.7 ([PEP 537](https://peps.python.org/pep-0537/#lifespan)) | June 2023    | December 2023 |
| 3.6 ([PEP 494](https://peps.python.org/pep-0494/#lifespan)) | December 2021 | August 2022 |
| 2.7 ([PEP 373](https://peps.python.org/pep-0373/))           | April 2020    | January 2022 |

## Implementation

The supported Python versions are encoded in three places, which must be
kept in sync with this policy:

1. **`setup.cfg`** — `python_requires`, the `Programming Language ::
   Python :: 3.x` trove classifiers, and any `python_version`
   environment markers on optional dependencies (e.g. `pymsalruntime`).
2. **`azure-pipelines.yml`** and **`.Pipelines/template-pipeline-stages.yml`**
   — the `python-version` matrix used by the PR-gate unit and E2E test stages.
3. **`tests/test_cryptography.py`** — the N+3 ceiling test that enforces
   tracking the latest `cryptography` release. Newer `cryptography`
   versions routinely drop EOL Python versions, which is the most common
   forcing function for this policy.

## Rationale

MSAL Python depends transitively on `cryptography`, `requests`, and
`PyJWT`. These libraries follow a similar policy and drop EOL Python
versions roughly six months after PSF end-of-support. Continuing to
support an EOL Python version in MSAL Python forces us to either pin
those dependencies to old, unmaintained versions — exposing MSAL users
to known CVEs — or to maintain conditional install metadata that breaks
on every dependency bump. Aligning with the Azure SDK and upstream
policies keeps MSAL Python simple, secure, and predictable.
