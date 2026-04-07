# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
msal-key-attestation — KeyGuard attestation support for MSAL Python MSI v2.

This package provides the ``create_attestation_provider()`` function that
returns a callable suitable for the ``attestation_token_provider`` parameter
in ``msal.msi_v2.obtain_token()``.

It loads the Windows-only ``AttestationClientLib.dll`` (Azure Attestation
native library) via ctypes and exposes a high-level API that:

- Initializes the native attestation library
- Calls ``AttestKeyGuardImportKey`` with the CNG key handle
- Returns the attestation JWT
- Caches the JWT in-memory until ~90 % of its lifetime

Usage::

    from msal_key_attestation import create_attestation_provider

    # Pass to MSAL's MSI v2 flow:
    result = client.acquire_token_for_client(
        resource="https://graph.microsoft.com",
        mtls_proof_of_possession=True,
        with_attestation_support=True,  # auto-discovers this package
    )

    # Or use the provider directly:
    provider = create_attestation_provider()
    jwt = provider(attestation_endpoint, key_handle_int, client_id)
"""

__version__ = "0.1.0"

from .attestation import create_attestation_provider, get_attestation_jwt

__all__ = ["create_attestation_provider", "get_attestation_jwt"]
