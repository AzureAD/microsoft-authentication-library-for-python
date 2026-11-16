# Security policy

Microsoft.Identity.Native.Primitives is pre-release software. Do not treat the
current API or binaries as a supported workload-isolation boundary. Only the
current `dev` branch is evaluated during pre-release development. An explicit
production support and servicing policy will be published before general
availability.

<!-- BEGIN MICROSOFT SECURITY.MD V0.0.9 BLOCK -->

## Security

Microsoft takes the security of our software products and services seriously,
which includes all source code repositories managed through our GitHub
organizations.

If you believe you have found a security vulnerability in any Microsoft-owned
repository that meets
[Microsoft's definition of a security vulnerability](https://aka.ms/security.md/definition),
please report it to us as described below.

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them to the Microsoft Security Response Center (MSRC) at
[https://msrc.microsoft.com/create-report](https://aka.ms/security.md/msrc/create-report).

You should receive a response within 24 hours. If for some reason you do not,
please follow up using the messaging functionality found at the bottom of the
Activity tab on your vulnerability report on
[https://msrc.microsoft.com/report/vulnerability](https://msrc.microsoft.com/report/vulnerability/)
or via email as described in the instructions at the bottom of
[https://msrc.microsoft.com/create-report](https://aka.ms/security.md/msrc/create-report).
Additional information can be found at
[microsoft.com/msrc](https://www.microsoft.com/msrc) or on MSRC's
[FAQ page for reporting an issue](https://www.microsoft.com/en-us/msrc/faqs-report-an-issue).

Please include the requested information listed below (as much as you can
provide) to help us better understand the nature and scope of the possible
issue:

- Type of issue (e.g. buffer overflow, SQL injection, cross-site scripting,
  etc.)
- Full paths of source file(s) related to the manifestation of the issue
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit the issue

This information will help us triage your report more quickly.

If you are reporting for a bug bounty, more complete reports can contribute to
a higher bounty award. Please visit our
[Microsoft Bug Bounty Program](https://aka.ms/security.md/msrc/bounty) page for
more details about our active programs.

## Preferred Languages

We prefer all communications to be in English.

## Policy

Microsoft follows the principle of
[Coordinated Vulnerability Disclosure](https://aka.ms/security.md/cvd).

<!-- END MICROSOFT SECURITY.MD BLOCK -->

If the MSRC reporting portal is unavailable, you may email
[secure@microsoft.com](mailto:secure@microsoft.com). If possible, encrypt the
message using the key published on the
[MSRC PGP key page](https://www.microsoft.com/msrc/pgp-key-msrc).

## Project-specific reporting information

Include the following when relevant and safe to provide:

- the affected version, commit, branch, or release artifact;
- Windows version and build;
- Rust and OpenSSL versions;
- selected CNG provider/KSP and non-sensitive key properties;
- whether exploitation requires code execution in the host process or access
  to the persisted key;
- the native error category and system/OpenSSL error codes;
- minimal reproduction steps; and
- expected security impact.

Do not send access tokens, private keys, private-key blobs, credentials, tenant
secrets, production certificates containing private material, or sensitive
production request/response bodies unless MSRC specifically requests them
through an approved secure channel.

## Security-sensitive areas

Changes in these areas require explicit security review:

- unsafe Rust and FFI;
- the public C ABI and native allocation/free contract;
- CNG and BCrypt handle ownership;
- key creation, usage, ACL, export, KSP, and attestation policy;
- OpenSSL provider loading, algorithms, and property selection;
- certificate/private-key binding;
- TLS configuration, certificate validation, hostname/IP verification, and
  peer verification;
- HTTP parsing and framing; and
- token, request, response, key identifier, and diagnostic handling.

See [Security model](docs/security-model.md) and
[Security review](docs/security-review.md).
