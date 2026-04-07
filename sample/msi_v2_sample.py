#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""
MSI v2 (mTLS PoP) Sample — Managed Identity with KeyGuard Attestation.

This sample demonstrates acquiring an mTLS Proof-of-Possession token using
MSAL Python's MSI v2 flow on a Windows Azure VM with Credential Guard.

Prerequisites:
    - Windows Azure VM with Credential Guard / KeyGuard enabled
    - AttestationClientLib.dll accessible (next to script or via env var)
    - pip install msal msal-key-attestation requests

Usage:
    python msi_v2_sample.py

Environment variables (optional):
    RESOURCE        - Resource URI (default: https://graph.microsoft.com)
    RESOURCE_URL    - URL to call with the token (default: Graph /applications)
    MSI_V2_VERBOSE  - Set to "1" for verbose logging
"""

import logging
import os
import sys

import requests
import msal

# Optional: enable verbose logging
if os.getenv("MSI_V2_VERBOSE", "").strip() in ("1", "true"):
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


def main():
    # --- Configuration ---
    resource = os.getenv(
        "RESOURCE", "https://graph.microsoft.com")
    resource_url = os.getenv(
        "RESOURCE_URL",
        "https://mtlstb.graph.microsoft.com/v1.0/applications?$top=5")

    logger.info("=" * 60)
    logger.info("MSI v2 (mTLS PoP) Sample")
    logger.info("=" * 60)
    logger.info("Resource:     %s", resource)
    logger.info("Resource URL: %s", resource_url)

    # --- Create client ---
    http_session = requests.Session()

    # Optionally add retry
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
        retries = Retry(total=3, backoff_factor=0.5,
                        status_forcelist=[429, 500, 502, 503, 504])
        http_session.mount("https://", HTTPAdapter(max_retries=retries))
        http_session.mount("http://", HTTPAdapter(max_retries=retries))
    except ImportError:
        pass

    client = msal.ManagedIdentityClient(
        msal.SystemAssignedManagedIdentity(),
        http_client=http_session,
    )

    # --- Acquire token ---
    logger.info("Acquiring mTLS PoP token...")
    result = client.acquire_token_for_client(
        resource=resource,
        mtls_proof_of_possession=True,
        with_attestation_support=True,
    )

    if "access_token" not in result:
        # Only log error code/description — never log tokens or secrets
        logger.error("Token acquisition failed: %s - %s",
                     result.get("error", "unknown"),
                     result.get("error_description", "no description"))
        sys.exit(1)

    token_type = result.get("token_type", "unknown")
    expires_in = result.get("expires_in", 0)

    logger.info("Token acquired successfully!")
    logger.info("  token_type: %s", token_type)
    logger.info("  expires_in: %s seconds", expires_in)

    if token_type != "mtls_pop":
        logger.warning(
            "Expected token_type='mtls_pop' but got '%s'.", token_type)

    # --- Verify binding ---
    from msal.msi_v2 import verify_cnf_binding
    cert_pem = result.get("cert_pem", "")
    if cert_pem:
        bound = verify_cnf_binding(result["access_token"], cert_pem)
        logger.info("  cnf binding: %s", "VERIFIED" if bound else "FAILED")
        if not bound:
            logger.error("Token is NOT bound to the certificate!")
            sys.exit(1)

    # --- Call resource over mTLS (optional) ---
    if resource_url:
        logger.info("Calling resource: %s", resource_url)

        # Note: mTLS resource calls require presenting the same cert.
        # The cert + private key are bound via KeyGuard; a real mTLS call
        # would use WinHTTP/SChannel. This demonstrates the auth header.
        access_token = result["access_token"]
        headers = {
            "Authorization": f"{token_type} {access_token}",
            "Accept": "application/json",
        }

        try:
            resp = http_session.get(resource_url, headers=headers)
            logger.info("  Status: %d", resp.status_code)
            if not resp.ok:
                logger.warning("  Request failed with status %d",
                               resp.status_code)
        except Exception as exc:
            logger.warning("  Resource call failed: %s", type(exc).__name__)
            logger.info(
                "Note: mTLS resource calls may require WinHTTP/SChannel; "
                "the requests library may not present the mTLS cert.")

    logger.info("=" * 60)
    logger.info("Done!")


if __name__ == "__main__":
    main()
