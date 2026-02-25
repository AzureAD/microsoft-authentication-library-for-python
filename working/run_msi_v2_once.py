import json
import os
import sys

import msal
import requests

DEFAULT_RESOURCE = "https://graph.microsoft.com"
RESOURCE = os.getenv("RESOURCE", DEFAULT_RESOURCE).strip().rstrip("/")

session = requests.Session()
cache = msal.TokenCache()

client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=session,
    token_cache=cache,
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
        r1 = acquire_mtls_pop_token_strict()
        print("token received (1)")

        r2 = acquire_mtls_pop_token_strict()
        print("token received (2)")

        # If MSAL exposes a cache indicator, print it (optional)
        ts1 = r1.get("token_source") or r1.get("source") or ""
        ts2 = r2.get("token_source") or r2.get("source") or ""
        if ts1 or ts2:
            print(f"source1={ts1} source2={ts2}")

        sys.exit(0)
    except Exception as ex:
        print("FAIL:", ex)
        sys.exit(2)