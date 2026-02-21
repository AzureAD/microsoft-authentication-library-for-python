"""
MSI v2 (mTLS PoP) sample for MSAL Python.

This sample demonstrates Managed Identity v2 token acquisition using
mTLS Proof-of-Possession (PoP) via the IMDS /issuecredential endpoint.

MSI v2 provides enhanced security compared to MSI v1 by binding the
access token to an mTLS client certificate, making the token unusable
without the corresponding private key.

Prerequisites:
- Run on an Azure VM with managed identity enabled
- Set RESOURCE environment variable to the target resource URL, e.g.
    export RESOURCE=https://management.azure.com/

To enable MSI v2 (required):
    export MSAL_ENABLE_MSI_V2=true
  or pass msi_v2_enabled=True to ManagedIdentityClient.

Usage:
    python msi_v2_sample.py
"""
import json
import logging
import os
import time

import msal
import requests


# Optional: enable debug logging to see the MSI v2 flow in detail
# logging.basicConfig(level=logging.DEBUG)
# logging.getLogger("msal").setLevel(logging.DEBUG)

RESOURCE = os.getenv("RESOURCE", "https://management.azure.com/")

# Create a long-lived app instance (for token cache reuse)
global_token_cache = msal.TokenCache()

client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
    token_cache=global_token_cache,
    msi_v2_enabled=True,  # Enable MSI v2 (mTLS PoP) flow
)


def acquire_and_use_token():
    """Acquire an mtls_pop token via MSI v2 and optionally call an API."""
    result = client.acquire_token_for_client(resource=RESOURCE)

    if "access_token" in result:
        token_type = result.get("token_type", "Bearer")
        print("Token acquired successfully")
        print("  token_type :", token_type)
        print("  token_source:", result.get("token_source"))
        print("  expires_in  :", result.get("expires_in"), "seconds")

        if token_type == "mtls_pop":
            print("  MSI v2 (mTLS PoP) token acquired")
        else:
            print("  MSI v1 (Bearer) token acquired (MSI v2 unavailable or fell back)")

        endpoint = os.getenv("ENDPOINT")
        if endpoint:
            # For mtls_pop tokens, the API call must also use the mTLS connection.
            # For demonstration, we show a standard Bearer call (works with Bearer tokens).
            api_result = requests.get(
                endpoint,
                headers={"Authorization": "{} {}".format(
                    token_type, result["access_token"])},
            ).json()
            print("API call result:", json.dumps(api_result, indent=2))
    else:
        print("Token acquisition failed:")
        print("  error            :", result.get("error"))
        print("  error_description:", result.get("error_description"))


if __name__ == "__main__":
    while True:
        acquire_and_use_token()
        print("Press Ctrl-C to stop. Sleeping 5 seconds...")
        time.sleep(5)
