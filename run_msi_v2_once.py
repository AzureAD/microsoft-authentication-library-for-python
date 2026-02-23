import os
import sys
import msal
import requests

def main():
    resource = os.getenv("RESOURCE", "https://management.azure.com/")
    timeout = int(os.getenv("HTTP_TIMEOUT_SEC", "10"))

    # IMPORTANT: long-lived session, but this tool runs once
    session = requests.Session()
    session.headers.update({"User-Agent": "msal-python-msi-v2-sample-exe"})
    session.timeout = timeout  # harmless if unused

    client = msal.ManagedIdentityClient(
        msal.SystemAssignedManagedIdentity(),
        http_client=session,
        msi_v2_enabled=True,   # force MSI v2 attempt (will still fall back if code does)
    )

    result = client.acquire_token_for_client(resource=resource)

    if "access_token" not in result:
        print("FAIL: token acquisition failed")
        return 2

    token_type = result.get("token_type", "mtls_pop")
    print("SUCCESS: token acquired")
    print("  resource   =", resource)
    print("  is_mtls_pop =", token_type == "mtls_pop")

    # Minimal proof we got a real JWT-ish token (don’t print it)
    at = result["access_token"]
    print("  token_len  =", len(at))

    # Exit codes:
    # 0 = MSI v2 worked (mtls_pop)
    # 1 = fell back to bearer (still a success, but not v2)
    return 0 if token_type == "mtls_pop" else 1

if __name__ == "__main__":
    sys.exit(main())