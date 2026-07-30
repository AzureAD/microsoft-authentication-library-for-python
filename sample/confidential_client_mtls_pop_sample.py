"""
This sample demonstrates a confidential-client (daemon) application that uses a
Subject Name + Issuer (SN/I) certificate as the client TLS certificate in a
mutual-TLS (mTLS) handshake to Microsoft Entra, and obtains an mTLS-bound
Proof-of-Possession (PoP) access token (``token_type == "mtls_pop"``).

Prerequisites
-------------
* A confidential-client app registration configured with a certificate.
* The SAME certificate available locally as a PFX file (SN/I). The certificate
  is used as the TLS client certificate -- it is NOT used to sign a client
  assertion here.
* A **tenanted** authority (mTLS PoP does not work with ``/common`` or
  ``/organizations``).
* The resource you request a token for must be allow-listed by Entra for mTLS
  PoP (for example Microsoft Graph or Azure Key Vault).

Configuration is read from environment variables so no secrets are hard-coded::

    CLIENT_ID          your application (client) id
    AUTHORITY          e.g. https://login.microsoftonline.com/<your-tenant-id>
    PFX_PATH           path to the .pfx file holding the SN/I cert + private key
    PFX_PASSPHRASE     (optional) passphrase protecting the .pfx
    SCOPE              e.g. https://graph.microsoft.com/.default
    AZURE_REGION       (optional) e.g. westus3; omit for the global mtls endpoint

Then run::

    python confidential_client_mtls_pop_sample.py
"""

import os

import msal


def _build_cert_credential():
    credential = {
        "private_key_pfx_path": os.environ["PFX_PATH"],
        # "public_certificate": True opts in to Subject Name/Issuer (SN/I) auth,
        # which is what makes the full cert chain (x5c) available for mTLS PoP.
        "public_certificate": True,
    }
    passphrase = os.getenv("PFX_PASSPHRASE")
    if passphrase:
        credential["passphrase"] = passphrase
    return credential


def _print_result_summary(result, header):
    """Print a *log-safe* summary of an MSAL token ``result``.

    The sensitive part of ``result`` is the ``access_token`` (a PoP/bearer
    secret) - that is what we must never log. The ``binding_certificate`` is the
    certificate's PUBLIC material (x5c + thumbprint), not a secret, so it is safe
    to inspect. We still avoid dumping the whole ``result`` dict and instead
    surface only a few derived booleans/fields, to model the habit of not
    routinely logging token responses.

    On success the interesting (non-sensitive) fields are::

        result["token_type"]          == "mtls_pop"
        result["binding_certificate"] == {"x5c": [...], "thumbprint_sha256": ...}
        result["token_source"]        in ("identity_provider", "cache")

    Inspect those directly (e.g. in a debugger) when you need their values.
    """
    print(header)
    if "access_token" in result:
        print("  acquired:                     yes")
        print("  token_type is mtls_pop:      ", result.get("token_type") == "mtls_pop")
        print("  carries binding_certificate: ", "binding_certificate" in result)
        print("  served from cache:           ", result.get("token_source") == "cache")
    else:
        print("  acquired:                     no")
        print("  (inspect result['error'] / result['error_description'] to diagnose)")


def vanilla_sni_mtls_pop():
    """Acquire an mTLS-bound PoP token using the SN/I cert as the TLS client cert."""
    app = msal.ConfidentialClientApplication(
        os.environ["CLIENT_ID"],
        authority=os.environ["AUTHORITY"],  # MUST be tenanted
        client_credential=_build_cert_credential(),
        # Note: do NOT pass a custom http_client here. MSAL must own the
        # transport to present the client certificate during the TLS handshake.
        azure_region=os.getenv("AZURE_REGION"),  # optional; None -> global endpoint
    )

    result = app.acquire_token_for_client(
        os.environ["SCOPE"].split(),
        mtls_proof_of_possession=True,  # <-- request an mTLS PoP token
    )

    _print_result_summary(result, "Vanilla SN/I -> mTLS PoP result:")
    return result


if __name__ == "__main__":
    print("=== Vanilla SN/I -> mTLS PoP ===")
    vanilla_sni_mtls_pop()
