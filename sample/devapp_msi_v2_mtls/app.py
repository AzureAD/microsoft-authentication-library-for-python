#!/usr/bin/env python
"""
MSI v2 mTLS PoP Dev App
========================
Demonstrates the full mTLS PoP flow using ONLY public MSAL APIs:

  1. ManagedIdentityClient.acquire_token_for_client() → token + binding_certificate
  2. App developer builds Authorization header from token_type + access_token
  3. App developer uses SchannelSession (separate package) for downstream mTLS call

MSAL hands out the token and cert. The app owns the downstream call.
This is the Python equivalent of the .NET MsiV2DemoApp.

Prerequisites:
    - Windows Azure VM with Credential Guard / KeyGuard
    - pip install requests
    - msal-key-attestation package with AttestationClientLib.dll
    - msal-schannel-transport package

Usage:
    python app.py

Environment variables (optional):
    RESOURCE          - Token audience (default: https://graph.microsoft.com)
    DOWNSTREAM_URL    - URL to call over mTLS (default: Graph mTLS endpoint)
    UAMI_CLIENT_ID    - User-assigned MI client ID (default: system-assigned)
"""

import json
import os
import sys
import base64

# Ensure local packages (msal/, msal-key-attestation/, msal-schannel-transport/)
# take precedence over system-installed versions.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _subpkg in [
    _REPO_ROOT,
    os.path.join(_REPO_ROOT, "msal-key-attestation"),
    os.path.join(_REPO_ROOT, "msal-schannel-transport"),
]:
    if _subpkg not in sys.path:
        sys.path.insert(0, _subpkg)


def main():
    print("=" * 60)
    print("  MSI v2 mTLS PoP — Dev App")
    print("  (uses ONLY public MSAL APIs)")
    print("=" * 60)

    if sys.platform != "win32":
        print("ERROR: Requires Windows with KeyGuard.")
        return 1

    # ── Import MSAL public API only ──────────────────────────
    import msal
    import requests

    # ── Configuration ────────────────────────────────────────
    resource = os.environ.get("RESOURCE", "https://vault.azure.net")
    downstream_url = os.environ.get(
        "DOWNSTREAM_URL",
        "https://tokenbinding.vault.azure.net/secrets/boundsecret/?api-version=2015-06-01")
    uami_client_id = os.environ.get("UAMI_CLIENT_ID", "")

    print(f"\n  Resource:       {resource}")
    print(f"  Downstream:     {downstream_url}")
    if uami_client_id:
        print(f"  UAMI Client ID: {uami_client_id}")
    else:
        print(f"  Identity:       System-Assigned")

    # ── Step 1: Create MSAL client (public API) ─────────────
    print(f"\n{'─' * 60}")
    print("  Step 1: Create ManagedIdentityClient")
    print(f"{'─' * 60}")

    if uami_client_id:
        mi = msal.UserAssignedManagedIdentity(client_id=uami_client_id)
    else:
        mi = msal.SystemAssignedManagedIdentity()

    http_client = requests.Session()

    client = msal.ManagedIdentityClient(
        mi,
        http_client=http_client,
    )
    print("  ✓ Client created")

    # ── Step 2: Acquire token (public API) ───────────────────
    print(f"\n{'─' * 60}")
    print("  Step 2: acquire_token_for_client(mtls_proof_of_possession=True)")
    print(f"{'─' * 60}")

    try:
        result = client.acquire_token_for_client(
            resource=resource,
            mtls_proof_of_possession=True,
            with_attestation_support=True,
        )
    except Exception as e:
        print(f"\n  ✗ FAILED: {type(e).__name__}: {e}")
        return 1

    if "error" in result:
        print(f"  ✗ Error: {result['error']}")
        print(f"    {result.get('error_description', '')}")
        return 1

    # ── Step 3: Inspect auth result ──────────────────────────
    print(f"\n{'─' * 60}")
    print("  Step 3: Auth result")
    print(f"{'─' * 60}")

    access_token = result["access_token"]
    token_type = result.get("token_type", "unknown")
    expires_in = result.get("expires_in", 0)

    print(f"  ✓ access_token: {access_token[:30]}...")
    print(f"  ✓ token_type:   {token_type}")
    print(f"  ✓ expires_in:   {expires_in}s")

    # Get binding_certificate — the WindowsCertificate object
    # This is what MSAL hands out. The app developer uses it for downstream.
    binding_cert = result.get("binding_certificate")

    if binding_cert is None:
        print(f"  ✗ binding_certificate is None!")
        return 1

    print(f"  ✓ binding_certificate: {binding_cert}")
    print(f"    has_private_key:  {binding_cert.has_private_key}")
    print(f"    thumbprint_sha1:  {binding_cert.thumbprint_sha1}")
    print(f"    thumbprint_sha256: {binding_cert.thumbprint_sha256}")
    print(f"    x5t#S256:         {binding_cert.x5t_s256}")

    # Verify token binding (cnf.x5t#S256 matches cert)
    try:
        parts = access_token.split(".")
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        cnf = payload.get("cnf", {})
        token_x5t = cnf.get("x5t#S256", "NOT FOUND")
        cert_x5t = binding_cert.x5t_s256
        match = "✓ MATCH" if token_x5t == cert_x5t else "✗ MISMATCH"
        print(f"\n  Token cnf.x5t#S256: {token_x5t}")
        print(f"  Cert  x5t#S256:     {cert_x5t}")
        print(f"  Binding: {match}")
        if token_x5t != cert_x5t:
            print("  ERROR: Token is NOT bound to the certificate!")
            return 1
    except Exception as e:
        print(f"  ⚠ Could not verify binding: {e}")

    # ── Step 4: Downstream mTLS call (app developer's job) ───
    print(f"\n{'─' * 60}")
    print("  Step 4: Downstream mTLS call (using SchannelSession)")
    print(f"{'─' * 60}")

    try:
        from msal_schannel_transport import SchannelSession
    except ImportError:
        print("  ✗ msal-schannel-transport not installed")
        print("    pip install msal-schannel-transport")
        binding_cert.close()
        return 1

    # App developer builds the auth header from token_type + access_token
    auth_header = f"{token_type} {access_token}"

    print(f"  URL: {downstream_url}")
    print(f"  Authorization: {token_type} <token>")
    print(f"  Client cert: {binding_cert.thumbprint_sha1[:16]}...")

    try:
        with SchannelSession(client_certificate=binding_cert) as session:
            response = session.get(
                downstream_url,
                headers={
                    "Authorization": auth_header,
                    "x-ms-tokenboundauth": "true",
                },
            )

        print(f"\n  Response: HTTP {response.status_code}")

        if response.status_code == 200:
            body = response.json()
            print(f"  ✓ Success!")
            for k in list(body.keys())[:5]:
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
            print(f"  ✗ 401 — mTLS binding may have failed")
            print(f"    {response.text[:300]}")
        else:
            print(f"  ? HTTP {response.status_code}")
            print(f"    {response.text[:300]}")

    except Exception as e:
        print(f"\n  ✗ Downstream call FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    # ── Cleanup ──────────────────────────────────────────────
    binding_cert.close()

    print(f"\n{'─' * 60}")
    print("  Done.")
    print(f"{'─' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
