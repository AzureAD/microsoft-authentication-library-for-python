"""
MSI v2 (mTLS PoP + KeyGuard Attestation) minimal sample for MSAL Python.

Behavior:
- Requests mtls_pop + attestation
- STRICT: succeeds only if token_type == mtls_pop
- Prints ONLY: "token received"
- No resource call
"""

import json
import os
import sys

import msal
import requests


DEFAULT_RESOURCE = "https://graph.microsoft.com"
RESOURCE = os.getenv("RESOURCE", DEFAULT_RESOURCE).strip().rstrip("/")

client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
    token_cache=msal.TokenCache(),
)


def acquire_mtls_pop_token_strict():
    result = client.acquire_token_for_client(
        resource=RESOURCE,
        mtls_proof_of_possession=True,
        with_attestation_support=True,
    )

    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {json.dumps(result, indent=2)}")

    token_type = (result.get("token_type") or "Bearer").lower()
    if token_type != "mtls_pop":
        raise RuntimeError(
            f"Strict MSI v2 requested, but got token_type={result.get('token_type')}. "
            f"Full result: {json.dumps(result, indent=2)}"
        )

    return result


if __name__ == "__main__":
    try:
        acquire_mtls_pop_token_strict()
        print("token received")
        sys.exit(0)
    except Exception as ex:
        print("FAIL:", ex)
        sys.exit(2)