# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""
MSI v2 (mTLS Proof-of-Possession) implementation for MSAL Python.

This module implements the Managed Identity v2 flow, which uses mTLS PoP
token binding via the IMDS /issuecredential endpoint with KeyGuard attestation
support.

Flow:
1. Generate RSA key (Windows: KeyGuard-protected, cross-platform: standard RSA)
2. GET /metadata/identity/getplatformmetadata for clientId, tenantId, cuId,
   attestationEndpoint
3. Build PKCS#10 CSR with OID attribute 1.3.6.1.4.1.311.90.2.10 (cuId JSON)
4. Obtain attestation JWT (Windows: AttestationClientLib.dll, others: skipped)
5. POST /metadata/identity/issuecredential with CSR + attestation JWT
6. Use issued certificate for mTLS connection to ESTS token endpoint
7. Return mtls_pop token with cnf.x5t#S256 binding
"""
import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import datetime

logger = logging.getLogger(__name__)

_IMDS_BASE = "http://169.254.169.254"
_IMDS_API_VERSION = "2021-12-13"

# OID for MSI v2 cuId attribute: 1.3.6.1.4.1.311.90.2.10
_CU_ID_OID = x509.ObjectIdentifier("1.3.6.1.4.1.311.90.2.10")


def _generate_rsa_key():
    """Generate a 2048-bit RSA private key."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )


def _encode_der_octet_string(data: bytes) -> bytes:
    """Encode bytes as a DER OCTET STRING (tag 0x04)."""
    length = len(data)
    if length < 0x80:
        return bytes([0x04, length]) + data
    # Multi-byte length encoding
    length_bytes = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x04, 0x80 | len(length_bytes)]) + length_bytes + data


def _build_csr(private_key, common_name: str, cu_id: str) -> bytes:
    """Build a PKCS#10 CSR (DER-encoded) with the MSI v2 cuId OID extension.

    :param private_key: RSA private key for signing.
    :param common_name: Certificate subject common name (typically clientId).
    :param cu_id: The cuId value obtained from IMDS platform metadata.
    :returns: DER-encoded CSR bytes.
    """
    cu_id_json = json.dumps({"cuId": cu_id}).encode("utf-8")
    der_value = _encode_der_octet_string(cu_id_json)

    builder = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]))
        .add_extension(
            x509.UnrecognizedExtension(_CU_ID_OID, der_value),
            critical=False,
        )
    )
    csr = builder.sign(private_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.DER)


def _get_platform_metadata(http_client) -> Dict[str, Any]:
    """GET IMDS platform metadata for MSI v2.

    :returns: Dict containing clientId, tenantId, cuId, attestationEndpoint.
    :raises MsiV2Error: If the request fails or returns unexpected data.
    """
    from .managed_identity import MsiV2Error
    imds_base = os.getenv(
        "AZURE_POD_IDENTITY_AUTHORITY_HOST", _IMDS_BASE
    ).strip("/")
    url = "{}/metadata/identity/getplatformmetadata".format(imds_base)
    logger.debug("Fetching MSI v2 platform metadata from IMDS: %s", url)
    resp = http_client.get(
        url,
        params={"api-version": _IMDS_API_VERSION},
        headers={"Metadata": "true"},
    )
    if resp.status_code != 200:
        raise MsiV2Error(
            "Failed to get platform metadata: HTTP {}: {}".format(
                resp.status_code, resp.text))
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as exc:
        raise MsiV2Error(
            "Invalid platform metadata response: {}".format(resp.text)
        ) from exc


def _issue_credential(
    http_client,
    csr_der: bytes,
    attestation_jwt: Optional[str],
) -> Dict[str, Any]:
    """POST /metadata/identity/issuecredential to obtain mTLS cert.

    :param http_client: HTTP client for IMDS calls.
    :param csr_der: DER-encoded CSR bytes.
    :param attestation_jwt: Optional attestation JWT (None for CSR-only flow).
    :returns: Dict with 'certificate' (PEM) and 'tokenEndpoint'.
    :raises MsiV2Error: If the request fails.
    """
    from .managed_identity import MsiV2Error
    imds_base = os.getenv(
        "AZURE_POD_IDENTITY_AUTHORITY_HOST", _IMDS_BASE
    ).strip("/")
    url = "{}/metadata/identity/issuecredential".format(imds_base)
    logger.debug("Requesting mTLS credential from IMDS issuecredential endpoint")
    body = {"csr": base64.b64encode(csr_der).decode("ascii")}
    if attestation_jwt:
        body["attestation"] = attestation_jwt

    resp = http_client.post(
        url,
        params={"api-version": _IMDS_API_VERSION},
        headers={"Metadata": "true", "Content-Type": "application/json"},
        data=json.dumps(body),
    )
    if resp.status_code != 200:
        raise MsiV2Error(
            "Failed to issue credential: HTTP {}: {}".format(
                resp.status_code, resp.text))
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as exc:
        raise MsiV2Error(
            "Invalid issuecredential response: {}".format(resp.text)
        ) from exc


def get_cert_thumbprint_sha256(cert_pem: str) -> str:
    """Compute the SHA-256 thumbprint of a certificate (cnf.x5t#S256 format).

    Per RFC 7638 / RFC 8705, x5t#S256 is the base64url-encoded SHA-256
    of the DER-encoded X.509 certificate.

    :param cert_pem: PEM-encoded certificate string.
    :returns: Base64url-encoded SHA-256 thumbprint (no padding).
    """
    cert = x509.load_pem_x509_certificate(
        cert_pem.encode("utf-8"), default_backend())
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(cert_der).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_cnf_binding(token: str, cert_pem: str) -> bool:
    """Verify that an mtls_pop token's cnf.x5t#S256 matches the certificate.

    :param token: The JWT access token (mtls_pop type).
    :param cert_pem: PEM-encoded certificate string.
    :returns: True if the binding is valid, False otherwise.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            logger.debug("Token is not a valid JWT (wrong number of parts)")
            return False
        # Decode payload with padding
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        cnf = claims.get("cnf", {})
        token_thumbprint = cnf.get("x5t#S256")
        if not token_thumbprint:
            logger.debug("Token has no cnf.x5t#S256 claim")
            return False
        cert_thumbprint = get_cert_thumbprint_sha256(cert_pem)
        match = (token_thumbprint == cert_thumbprint)
        if not match:
            logger.debug(
                "cnf.x5t#S256 mismatch: token=%s, cert=%s",
                token_thumbprint, cert_thumbprint)
        return match
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Failed to verify cnf binding: %s", exc)
        return False


def _acquire_token_via_mtls(
    token_endpoint: str,
    cert_pem: str,
    private_key,
    client_id: str,
    resource: str,
) -> Dict[str, Any]:
    """Acquire an mtls_pop token from the ESTS token endpoint via mTLS.

    Creates a new requests.Session configured with the client certificate
    for the mTLS handshake.

    :param token_endpoint: The token endpoint URL from issuecredential.
    :param cert_pem: PEM-encoded client certificate string.
    :param private_key: RSA private key matching the certificate.
    :param client_id: The managed identity client ID.
    :param resource: The resource for which to acquire the token.
    :returns: OAuth2 token response dict.
    :raises MsiV2Error: If token acquisition fails.
    """
    from .managed_identity import MsiV2Error
    import requests as _requests

    logger.debug("Acquiring mTLS PoP token from ESTS: %s", token_endpoint)
    key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )

    # Write cert and key to temp files (requests requires file paths for mTLS)
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
    key_fd, key_path = tempfile.mkstemp(suffix=".key")
    try:
        try:
            os.write(cert_fd, cert_pem.encode("utf-8"))
        finally:
            os.close(cert_fd)
        try:
            os.write(key_fd, key_pem)
        finally:
            os.close(key_fd)

        session = _requests.Session()
        session.cert = (cert_path, key_path)
        resp = session.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "resource": resource,
            },
        )
        if resp.status_code != 200:
            raise MsiV2Error(
                "mTLS token acquisition failed: HTTP {}: {}".format(
                    resp.status_code, resp.text))
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise MsiV2Error(
                "Invalid mTLS token response: {}".format(resp.text)
            ) from exc
    finally:
        for path in (cert_path, key_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def obtain_token(
    http_client,
    managed_identity,
    resource: str,
    attestation_enabled: bool = False,
) -> Dict[str, Any]:
    """Acquire a token using the MSI v2 (mTLS PoP) flow.

    :param http_client: HTTP client for IMDS requests.
    :param managed_identity: ManagedIdentity configuration dict.
    :param resource: Resource URL for token acquisition.
    :param attestation_enabled: When True, attempt KeyGuard / platform attestation
        before issuing credentials (Windows only; silently skipped on other platforms).
        Defaults to False.
    :returns: OAuth2 token response dict with access_token on success,
              or error dict on failure.
    :raises MsiV2Error: If the flow fails at a non-recoverable step.
    """
    from .managed_identity import MsiV2Error

    # 1. Generate RSA key (KeyGuard on Windows via attestation, else standard)
    private_key = _generate_rsa_key()

    # 2. Fetch IMDS platform metadata
    metadata = _get_platform_metadata(http_client)
    client_id = metadata.get("clientId")
    cu_id = metadata.get("cuId")
    attestation_endpoint = metadata.get("attestationEndpoint")

    if not client_id or not cu_id:
        raise MsiV2Error(
            "Platform metadata missing required fields (clientId, cuId): "
            "{}".format(metadata))

    # 3. Build PKCS#10 CSR with cuId OID extension
    csr_der = _build_csr(private_key, client_id, cu_id)

    # 4. Attempt attestation only when explicitly requested by the caller
    attestation_jwt = None
    if attestation_enabled and attestation_endpoint:
        try:
            from .msi_v2_attestation import get_attestation_jwt
            attestation_jwt = get_attestation_jwt(
                http_client, csr_der, attestation_endpoint, private_key)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug(
                "Attestation unavailable, proceeding without it: %s", exc)

    # 5. Issue credential (POST to IMDS issuecredential)
    credential = _issue_credential(http_client, csr_der, attestation_jwt)
    cert_pem = credential.get("certificate")
    token_endpoint = credential.get("tokenEndpoint")

    if not cert_pem or not token_endpoint:
        raise MsiV2Error(
            "issuecredential response missing required fields "
            "(certificate, tokenEndpoint): {}".format(credential))

    # 6. Acquire mtls_pop token via mTLS
    result = _acquire_token_via_mtls(
        token_endpoint, cert_pem, private_key, client_id, resource)

    # 7. Normalize response into OAuth2 format
    if result.get("access_token") and result.get("expires_in"):
        return {
            "access_token": result["access_token"],
            "expires_in": int(result["expires_in"]),
            "token_type": result.get("token_type", "mtls_pop"),
            "resource": result.get("resource"),
        }
    return result
