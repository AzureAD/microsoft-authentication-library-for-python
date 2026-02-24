"""
MSI v2 (mTLS PoP + KeyGuard Attestation) sample for MSAL Python.

This sample requests an *attested*, certificate-bound access token (token_type=mtls_pop)
using the IMDSv2 /issuecredential endpoint and ESTS mTLS token endpoint.

Key points (based on our E2E debugging):
- Use a resource that supports certificate-bound tokens. In this environment, Graph mTLS test
  resource is supported; ARM typically is NOT (AADSTS392196).
- Run in strict mode: if mtls_pop + attestation is requested, we fail if we receive Bearer.
- Designed for Windows Azure VM where Credential Guard (VBS/KeyGuard) is available and
  AttestationClientLib.dll is present.

Environment variables:
- RESOURCE: defaults to https://mtlstb.graph.microsoft.com
- ENDPOINT: optional URL to call after acquiring token (e.g., Graph mTLS test endpoint)
- VERBOSE_LOGGING: "1"/"true" enables debug logs
- ATTESTATION_CLIENTLIB_PATH: optional absolute path to AttestationClientLib.dll (recommended)
- PYTHONNET_RUNTIME: must be "coreclr" for CSR OtherRequestAttributes (if using pythonnet path)

Usage:
  set RESOURCE=https://mtlstb.graph.microsoft.com
  set ENDPOINT=https://mtlstb.graph.microsoft.com/v1.0/applications?$top=1
  python msi_v2_sample.py
"""

import json
import logging
import os
import sys
import time

import msal
import requests


# ------------------------- Logging -------------------------

def _truthy(s: str) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "y", "on")

if _truthy(os.getenv("VERBOSE_LOGGING", "")):
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("msal").setLevel(logging.DEBUG)

log = logging.getLogger("msi_v2_sample")


# ------------------------- Defaults -------------------------

# Graph mTLS test resource (known-good for mtls_pop in your environment)
DEFAULT_RESOURCE = "https://mtlstb.graph.microsoft.com"

# ARM will often fail for mtls_pop with AADSTS392196
ARM_RESOURCE = "https://management.azure.com/"

RESOURCE = os.getenv("RESOURCE", DEFAULT_RESOURCE).strip().rstrip("/")
ENDPOINT = os.getenv("ENDPOINT", "").strip()

# Token cache is optional; keep it simple for E2E
token_cache = msal.TokenCache()

client = msal.ManagedIdentityClient(
    msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
    token_cache=token_cache,
)


# ------------------------- Helpers -------------------------

def _print_env_hints():
    if RESOURCE.lower().startswith(ARM_RESOURCE):
        print("NOTE: RESOURCE is ARM. mtls_pop usually fails for ARM with AADSTS392196.")
        print(f"      Try: set RESOURCE={DEFAULT_RESOURCE}")

    if sys.platform != "win32":
        print("NOTE: This sample is designed for Windows KeyGuard + attestation.")


def _call_endpoint_bearer(endpoint: str, token_type: str, access_token: str):
    """
    Simple HTTP call using Authorization header.
    NOTE: If the *resource* requires client cert at the resource layer too, this may not work.
    For your current E2E, token acquisition is the primary goal.
    """
    headers = {"Authorization": f"{token_type} {access_token}", "Accept": "application/json"}
    r = requests.get(endpoint, headers=headers, timeout=30)
    try:
        return r.status_code, r.headers, r.json()
    except Exception:
        return r.status_code, r.headers, r.text


# ------------------------- Main flow -------------------------

def acquire_mtls_pop_token_strict():
    """
    Acquire MSI v2 token in STRICT mode:
    - We request mtls_proof_of_possession=True and with_attestation_support=True
    - If we don't get token_type=mtls_pop, treat as failure
    """
    result = client.acquire_token_for_client(
        resource=RESOURCE,
        mtls_proof_of_possession=True,   # MSI v2 path
        with_attestation_support=True,   # KeyGuard attestation required for your scenario
    )

    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {json.dumps(result, indent=2)}")

    token_type = (result.get("token_type") or "Bearer").lower()
    if token_type != "mtls_pop":
        # In strict mode, bearer is a failure
        raise RuntimeError(
            "Strict MSI v2 requested, but got non-mtls_pop token.\n"
            f"token_type={result.get('token_type')}\n"
            "This usually means MSI v2 failed or you requested a resource that doesn't support "
            "certificate-bound tokens.\n"
            f"Try RESOURCE={DEFAULT_RESOURCE}\n"
            f"Full result: {json.dumps(result, indent=2)}"
        )

    return result


def main_once():
    _print_env_hints()

    # For pythonnet-based CSR attribute support, coreclr is required.
    # If you're running via pythonnet and hit OtherRequestAttributes issues, set:
    #   set PYTHONNET_RUNTIME=coreclr
    if os.getenv("PYTHONNET_RUNTIME"):
        log.debug("PYTHONNET_RUNTIME=%s", os.getenv("PYTHONNET_RUNTIME"))

    print("Requesting MSI v2 token (mtls_pop + attestation)...")
    result = acquire_mtls_pop_token_strict()

    print("SUCCESS: token acquired")
    print("  resource   =", RESOURCE)
    print("  token_len  =", len(result["access_token"]))

    if ENDPOINT:
        print("\nCalling ENDPOINT (best-effort using Authorization header):")
        status, headers, body = _call_endpoint_bearer(
            ENDPOINT, result.get("token_type", "mtls_pop"), result["access_token"]
        )
        print("  status =", status)
        # Print a small response preview
        if isinstance(body, (dict, list)):
            print(json.dumps(body, indent=2)[:2000])
        else:
            print(str(body)[:2000])


if __name__ == "__main__":
    # Run once by default (simpler for debugging)
    # Set LOOP=1 if you want repeated calls
    loop = _truthy(os.getenv("LOOP", ""))
    if not loop:
        try:
            main_once()
            sys.exit(0)
        except Exception as ex:
            print("FAIL:", ex)
            sys.exit(2)

    # Optional loop mode
    while True:
        try:
            main_once()
        except Exception as ex:
            print("FAIL:", ex)
        print("Sleeping 10 seconds... (Ctrl-C to stop)")
        time.sleep(10)
