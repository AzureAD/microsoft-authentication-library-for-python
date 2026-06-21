#!/usr/bin/env python
"""
MSI v2 mTLS PoP — Console E2E Test
===================================
Run this on a KeyGuard-enabled Azure VM:

    python run_e2e.py

It will:
  1. Call obtain_token() to get an mTLS PoP token + binding_certificate
  2. Print token metadata and cert info
  3. Use SchannelSession to call Azure Key Vault over mTLS
  4. Print the result

No pytest needed — just: pip install requests
"""

import json
import sys
import os

# Add the local msal package to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Add sub-packages to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "msal-key-attestation"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "msal-schannel-transport"))

def main():
    print("=" * 60)
    print("MSI v2 mTLS PoP — E2E Console Test")
    print("=" * 60)

    # Check platform
    if sys.platform != "win32":
        print("ERROR: This test requires Windows with KeyGuard.")
        sys.exit(1)

    import requests
    from msal.msi_v2 import obtain_token
    from msal.managed_identity import SystemAssignedManagedIdentity
    from msal.windows_certificate import WindowsCertificate

    # Config
    resource = os.environ.get("MSI_V2_RESOURCE", "https://graph.microsoft.com")
    # Full downstream URL to call over mTLS
    downstream_url = os.environ.get(
        "MSI_V2_DOWNSTREAM_URL",
        "https://mtlstb.graph.microsoft.com/v1.0/applications")

    print(f"\nConfig:")
    print(f"  Resource:       {resource}")
    print(f"  Downstream URL: {downstream_url}")

    # Step 1: Acquire token
    print(f"\n{'─' * 60}")
    print("Step 1: Acquiring mTLS PoP token via obtain_token()...")
    print(f"{'─' * 60}")

    http_client = requests.Session()
    mi = SystemAssignedManagedIdentity()

    # Try attestation
    attestation_provider = None
    try:
        from msal_key_attestation import create_attestation_provider
        attestation_provider = create_attestation_provider()
        print("  ✓ msal-key-attestation loaded (attested flow)")
    except ImportError:
        print("  ⚠ msal-key-attestation not found (non-attested flow)")
    except Exception as e:
        print(f"  ⚠ msal-key-attestation failed to init: {e}")

    try:
        result = obtain_token(
            http_client=http_client,
            managed_identity=mi,
            resource=resource,
            attestation_enabled=True,
            attestation_token_provider=attestation_provider,
        )
    except Exception as e:
        print(f"\n✗ obtain_token FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    # Step 2: Inspect result
    print(f"\n{'─' * 60}")
    print("Step 2: Auth result")
    print(f"{'─' * 60}")

    if "error" in result:
        print(f"  ✗ Error: {result.get('error')}")
        print(f"    {result.get('error_description', '')}")
        sys.exit(1)

    print(f"  ✓ access_token: {result['access_token'][:30]}...")
    print(f"  ✓ token_type:   {result.get('token_type')}")
    print(f"  ✓ expires_in:   {result.get('expires_in')}s")

    binding_cert = result.get("binding_certificate")
    if binding_cert is None:
        print(f"  ✗ binding_certificate is None!")
        sys.exit(1)

    print(f"  ✓ binding_certificate: {binding_cert}")
    print(f"    has_private_key: {binding_cert.has_private_key}")
    print(f"    thumbprint_sha1: {binding_cert.thumbprint_sha1}")
    print(f"    thumbprint_sha256: {binding_cert.thumbprint_sha256}")
    print(f"    x5t#S256: {binding_cert.x5t_s256}")

    # Verify cnf claim in token
    try:
        import base64
        parts = result["access_token"].split(".")
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        cnf = payload.get("cnf", {})
        token_x5t = cnf.get("x5t#S256", "NOT FOUND")
        cert_x5t = binding_cert.x5t_s256
        match = "✓ MATCH" if token_x5t == cert_x5t else "✗ MISMATCH"
        print(f"\n  Token cnf.x5t#S256: {token_x5t}")
        print(f"  Cert x5t#S256:      {cert_x5t}")
        print(f"  Binding: {match}")
    except Exception as e:
        print(f"  ⚠ Could not decode token: {e}")

    # Step 3: Downstream mTLS call to AKV
    print(f"\n{'─' * 60}")
    print("Step 3: Calling downstream API over mTLS")
    print(f"{'─' * 60}")

    try:
        from msal_schannel_transport import SchannelSession

        token_type = result.get("token_type", "mtls_pop")
        auth_header = f"{token_type} {result['access_token']}"

        print(f"  URL: {downstream_url}")
        print(f"  Authorization: {token_type} <token>")

        with SchannelSession(client_certificate=binding_cert) as session:
            response = session.get(
                downstream_url,
                headers={"Authorization": auth_header},
            )

        print(f"\n  Response: HTTP {response.status_code}")

        if response.status_code == 200:
            body = response.json()
            print(f"  ✓ Success!")
            # Print first few keys of response
            keys = list(body.keys())[:5]
            for k in keys:
                v = body[k]
                if isinstance(v, str) and len(v) > 80:
                    v = v[:80] + "..."
                elif isinstance(v, list):
                    v = f"[{len(v)} items]"
                print(f"    {k}: {v}")
        elif response.status_code == 403:
            print(f"  ⚠ 403 Forbidden — identity may lack permissions")
            print(f"    {response.text[:300]}")
        elif response.status_code == 401:
            print(f"  ✗ 401 Unauthorized — mTLS binding failed?")
            print(f"    {response.text[:300]}")
        else:
            print(f"  ? HTTP {response.status_code}")
            print(f"    {response.text[:300]}")

    except Exception as e:
        print(f"\n  ✗ Downstream call FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    # Cleanup
    binding_cert.close()
    print(f"\n{'─' * 60}")
    print("Done.")
    print(f"{'─' * 60}")


if __name__ == "__main__":
    main()
