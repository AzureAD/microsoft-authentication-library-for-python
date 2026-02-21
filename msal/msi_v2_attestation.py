# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
Attestation handler for MSI v2 (mTLS PoP) flow.

Provides attestation JWT acquisition for use in the IMDS issuecredential
request. On Windows, attempts to use AttestationClientLib.dll via ctypes.
On all other platforms (or when the DLL is unavailable), returns None,
allowing the caller to proceed with CSR-only credential issuance.
"""
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)


def _try_windows_attestation(
    csr_der: bytes,
    attestation_endpoint: str,
) -> Optional[str]:
    """Attempt to get an attestation JWT using Windows AttestationClientLib.dll.

    :param csr_der: DER-encoded CSR bytes to include in the attestation.
    :param attestation_endpoint: MAA attestation endpoint URL.
    :returns: Attestation JWT string, or None if unavailable.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        lib = ctypes.CDLL("AttestationClientLib.dll")
        logger.debug("Loaded AttestationClientLib.dll for Windows attestation")
        # The exact DLL interface is platform/version-specific.
        # Without access to the DLL ABI, we log and return None.
        # Production implementations should call the appropriate exported
        # function with the CSR and attestation endpoint.
        logger.debug(
            "Windows AttestationClientLib.dll loaded but DLL ABI not "
            "configured; skipping attestation")
        return None
    except OSError as exc:
        logger.debug("AttestationClientLib.dll not available: %s", exc)
        return None


def get_attestation_jwt(
    http_client,
    csr_der: bytes,
    attestation_endpoint: str,
    private_key,
) -> Optional[str]:
    """Obtain an attestation JWT for the MSI v2 credential issuance.

    Tries platform-specific attestation first (Windows AttestationClientLib.dll),
    then falls back to returning None, which causes the caller to proceed
    with a CSR-only issuecredential request.

    :param http_client: HTTP client (reserved for future cross-platform MAA calls).
    :param csr_der: DER-encoded CSR bytes.
    :param attestation_endpoint: MAA endpoint URL from IMDS platform metadata.
    :param private_key: RSA private key (reserved for future signing needs).
    :returns: Attestation JWT string, or None if attestation is unavailable.
    """
    attestation_jwt = _try_windows_attestation(csr_der, attestation_endpoint)
    if attestation_jwt:
        logger.debug("Obtained Windows attestation JWT")
        return attestation_jwt
    logger.debug(
        "No platform attestation available; proceeding with CSR-only flow")
    return None
