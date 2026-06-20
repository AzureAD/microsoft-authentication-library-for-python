# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
msal-schannel-transport — Windows SChannel/WinHTTP-backed HTTP transport
for downstream mTLS calls using platform-backed certificates.

This package provides an HTTP session that uses WinHTTP + SChannel for TLS,
allowing app developers to make downstream API calls with certificates that
have non-exportable private keys (TPM/KeyGuard/VBS).

This is the Python equivalent of using HttpClient + X509Certificate2 in .NET
for downstream mTLS API calls.

Usage:
    from msal import WindowsCertificate
    from msal_schannel_transport import SchannelSession

    # Get certificate reference from MSAL auth result
    cert = result["binding_certificate"]  # WindowsCertificate object

    # Make downstream mTLS call
    with SchannelSession(client_certificate=cert) as session:
        response = session.get(
            "https://my-vault.vault.azure.net/secrets/foo?api-version=7.5",
            headers={"Authorization": result["authorization_header"]},
        )
        print(response.status_code, response.json())
"""

from .session import SchannelSession, SchannelResponse

__all__ = ["SchannelSession", "SchannelResponse"]
__version__ = "0.1.0"
