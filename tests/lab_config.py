# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""
Lab configuration helpers for integration tests.

This module provides access to test user and app configuration stored in
Azure Key Vault. Configuration is retrieved as JSON and parsed into
dataclasses for type-safe access.

Usage::

    from tests.lab_config import (
        get_user_config, get_app_config, get_user_password,
        UserSecrets, AppSecrets,
    )

    user = get_user_config(UserSecrets.PUBLIC_CLOUD)
    password = get_user_password(user)
    app = get_app_config(AppSecrets.PCA_CLIENT)

Environment Variables:
    LAB_APP_CLIENT_ID: Client ID for Key Vault authentication (required)
    LAB_APP_CLIENT_CERT_PFX_PATH: Path to .pfx certificate file (required)
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

from azure.identity import CertificateCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger(__name__)

__all__ = [
    # Constants
    "UserSecrets",
    "AppSecrets",
    # Data classes
    "UserConfig",
    "AppConfig",
    # Functions
    "get_secret",
    "get_user_config",
    "get_app_config",
    "get_user_password",
    "get_client_certificate",
]

# =============================================================================
# Key Vault URLs
# =============================================================================

_MSID_LAB_VAULT = "https://msidlabs.vault.azure.net"
_MSAL_TEAM_VAULT = "https://id4skeyvault.vault.azure.net"

# =============================================================================
# Secret Name Constants
# =============================================================================

class UserSecrets:
    """
    Secret names for test user configuration in Key Vault.
    
    Each constant maps to a JSON blob containing user details like UPN,
    tenant ID, and the lab name (used to retrieve the user's password).
    """
    PUBLIC_CLOUD = "User-PublicCloud-Config"
    FEDERATED = "User-Federated-Config"
    B2C = "MSAL-USER-B2C-JSON"
    ARLINGTON = "MSAL-USER-Arlington-JSON"
    CIAM = "MSAL-USER-CIAM-JSON"

class AppSecrets:
    """
    Secret names for test app configuration in Key Vault.
    
    Each constant maps to a JSON blob containing app registration details
    like client ID, authority, redirect URI, and client secret reference.
    """
    PCA_CLIENT = "App-PCAClient-Config"
    S2S_CLIENT = "App-S2S-Config"
    WEB_API_CLIENT = "App-WebAPI-Config"
    WEB_APP_CLIENT = "App-WebAPP-Config"
    B2C_CLIENT = "MSAL-App-B2C-JSON"
    CIAM_CLIENT = "MSAL-App-CIAM-JSON"
    ARLINGTON_CLIENT = "MSAL-App-Arlington-JSON"

# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class UserConfig:
    """
    Test user configuration retrieved from Key Vault.
    
    Attributes:
        upn: User principal name (email format).
        tenant_id: Azure AD tenant ID.
        lab_name: Key Vault secret name for the user's password.
        home_upn: UPN in the user's home tenant (for federated scenarios).
        b2c_provider: Identity provider for B2C users (e.g., 'local', 'google').
        federation_provider: Federation provider type (e.g., 'ADFSv4').
    """
    app_id: Optional[str] = None
    object_id: Optional[str] = None
    user_type: Optional[str] = None
    display_name: Optional[str] = None
    licenses: Optional[str] = None
    upn: Optional[str] = None
    mfa: Optional[str] = None
    protection_policy: Optional[str] = None
    home_domain: Optional[str] = None
    home_upn: Optional[str] = None
    b2c_provider: Optional[str] = None
    lab_name: Optional[str] = None
    last_updated_by: Optional[str] = None
    last_updated_date: Optional[str] = None
    tenant_id: Optional[str] = None
    federation_provider: Optional[str] = None

    @property
    def authority(self) -> str:
        """Construct the Azure AD authority URL from tenant_id."""
        if self.tenant_id:
            return f"https://login.microsoftonline.com/{self.tenant_id}"
        return "https://login.microsoftonline.com/common"


@dataclass
class AppConfig:
    """
    Test app registration configuration retrieved from Key Vault.
    
    Attributes:
        app_id: Application (client) ID.
        authority: Azure AD authority URL for the app's tenant.
        redirect_uri: OAuth redirect URI configured for the app.
        defaultscopes: Space-separated default scopes for the app.
        client_secret: Key Vault secret name containing the app's client secret.
        secret_name: Alternative field for the client secret reference.
    """
    app_type: Optional[str] = None
    app_name: Optional[str] = None
    app_id: Optional[str] = None
    defaultscopes: Optional[str] = None
    redirect_uri: Optional[str] = None
    authority: Optional[str] = None
    lab_name: Optional[str] = None
    client_secret: Optional[str] = None
    secret_name: Optional[str] = None


# =============================================================================
# Key Vault Client Setup
# =============================================================================

# Module-level client cache (lazy initialized)
_msid_lab_client: Optional[SecretClient] = None
_msal_team_client: Optional[SecretClient] = None


def _clean_env(name: str) -> Optional[str]:
    """Return the env var value, or None if unset or it contains an unexpanded
    ADO pipeline variable literal such as ``$(VAR_NAME)``.

    Azure DevOps injects the literal string ``$(VAR_NAME)`` when a ``$(...)``
    reference in a step ``env:`` block refers to a variable that has not been
    defined at runtime.  That literal is truthy, so a plain ``os.getenv()``
    check would incorrectly proceed as if the variable were set.
    """
    value = os.getenv(name)
    if value and value.startswith("$("):
        return None
    return value or None


def _get_credential():
    """
    Create an Azure credential for Key Vault access.
    
    Reads authentication details from environment variables and uses
    certificate-based authentication via LAB_APP_CLIENT_CERT_PFX_PATH.
    
    Returns:
        A credential object suitable for Azure SDK clients.
    
    Raises:
        EnvironmentError: If required environment variables are not set.
    """
    client_id = _clean_env("LAB_APP_CLIENT_ID")
    cert_path = _clean_env("LAB_APP_CLIENT_CERT_PFX_PATH")
    tenant_id = "72f988bf-86f1-41af-91ab-2d7cd011db47"  # Microsoft tenant
    
    if not client_id:
        raise EnvironmentError(
            "LAB_APP_CLIENT_ID environment variable is required for Key Vault access")
    
    if cert_path:
        logger.debug("Using certificate credential for Key Vault access")
        return CertificateCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            certificate_path=cert_path,
            send_certificate_chain=True,
        )
    else:
        raise EnvironmentError(
            "LAB_APP_CLIENT_CERT_PFX_PATH is required")


def _get_msid_lab_client() -> SecretClient:
    """Return the MSID Lab Key Vault client, creating it if needed."""
    global _msid_lab_client
    if _msid_lab_client is None:
        logger.debug("Initializing MSID Lab Key Vault client")
        _msid_lab_client = SecretClient(
            vault_url=_MSID_LAB_VAULT,
            credential=_get_credential(),
        )
    return _msid_lab_client


def _get_msal_team_client() -> SecretClient:
    """Return the MSAL Team Key Vault client, creating it if needed."""
    global _msal_team_client
    if _msal_team_client is None:
        logger.debug("Initializing MSAL Team Key Vault client")
        _msal_team_client = SecretClient(
            vault_url=_MSAL_TEAM_VAULT,
            credential=_get_credential(),
        )
    return _msal_team_client


# =============================================================================
# Secret Retrieval Functions
# =============================================================================

# Module-level caches for config objects (keyed by secret_name)
_user_config_cache: Dict[str, UserConfig] = {}
_app_config_cache: Dict[str, AppConfig] = {}


def _lowercase_keys(d: dict) -> dict:
    """Recursively convert all dictionary keys to lowercase for case-insensitive access."""
    return {k.lower(): (_lowercase_keys(v) if isinstance(v, dict) else v) for k, v in d.items()}


def get_secret(secret_name: str, vault: str = "msid_lab") -> str:
    """
    Retrieve a raw secret value from Key Vault.
    
    Args:
        secret_name: The name of the secret in Key Vault.
        vault: Which vault to use - ``"msid_lab"`` (default, for passwords) 
            or ``"msal_team"`` (for configuration JSON blobs).
    
    Returns:
        The secret value as a string.
    
    Raises:
        ValueError: If the vault name is unknown or the secret is empty.
    """
    logger.debug("Retrieving secret '%s' from %s vault", secret_name, vault)
    
    if vault == "msid_lab":
        client = _get_msid_lab_client()
    elif vault == "msal_team":
        client = _get_msal_team_client()
    else:
        raise ValueError(f"Unknown vault: {vault}. Use 'msid_lab' or 'msal_team'")
    
    secret = client.get_secret(secret_name)
    
    if not secret.value:
        raise ValueError(f"Secret '{secret_name}' is empty in Key Vault")
    
    logger.debug("Successfully retrieved secret '%s'", secret_name)
    return secret.value


def get_user_config(secret_name: str) -> UserConfig:
    """
    Retrieve and parse a test user configuration from Key Vault.
    
    Results are cached for subsequent calls with the same secret name.
    
    Args:
        secret_name: The secret name (use ``UserSecrets`` constants).
    
    Returns:
        A ``UserConfig`` instance populated from the JSON.
    """
    # Check cache first
    if secret_name in _user_config_cache:
        return _user_config_cache[secret_name]
    
    logger.info("Retrieving user config from secret '%s'", secret_name)
    
    raw = get_secret(secret_name, vault="msal_team")
    data = _lowercase_keys(json.loads(raw))
    
    # The JSON has a "user" wrapper object
    user_data = data.get("user", data)
    
    config = UserConfig(
        app_id=user_data.get("appid"),
        object_id=user_data.get("objectid"),
        user_type=user_data.get("usertype"),
        display_name=user_data.get("displayname"),
        licenses=user_data.get("licenses"),
        upn=user_data.get("upn"),
        mfa=user_data.get("mfa"),
        protection_policy=user_data.get("protectionpolicy"),
        home_domain=user_data.get("homedomain"),
        home_upn=user_data.get("homeupn"),
        b2c_provider=user_data.get("b2cprovider"),
        lab_name=user_data.get("labname"),
        last_updated_by=user_data.get("lastupdatedby"),
        last_updated_date=user_data.get("lastupdateddate"),
        tenant_id=user_data.get("tenantid"),
        federation_provider=user_data.get("federationprovider"),
    )
    
    _user_config_cache[secret_name] = config
    return config


def get_app_config(secret_name: str) -> AppConfig:
    """
    Retrieve and parse an app registration configuration from Key Vault.
    
    Results are cached for subsequent calls with the same secret name.
    
    Args:
        secret_name: The secret name (use ``AppSecrets`` constants).
    
    Returns:
        An ``AppConfig`` instance populated from the JSON.
    """
    # Check cache first
    if secret_name in _app_config_cache:
        return _app_config_cache[secret_name]
    
    logger.info("Retrieving app config from Key Vault app configuration secret")
    
    raw = get_secret(secret_name, vault="msal_team")
    data = _lowercase_keys(json.loads(raw))
    
    # The JSON has an "app" wrapper object
    app_data = data.get("app", data)
    
    config = AppConfig(
        app_type=app_data.get("apptype"),
        app_name=app_data.get("appname"),
        app_id=app_data.get("appid"),
        defaultscopes=app_data.get("defaultscopes"),
        redirect_uri=app_data.get("redirecturi"),
        authority=app_data.get("authority"),
        lab_name=app_data.get("labname"),
        client_secret=app_data.get("clientsecret"),
        secret_name=app_data.get("secretname")
    )
    
    _app_config_cache[secret_name] = config
    return config


def get_user_password(user_config: UserConfig) -> str:
    """
    Retrieve a test user's password from Key Vault.
    
    The password is stored in the MSID Lab vault under the secret name
    specified by the user's ``lab_name`` field.
    
    Args:
        user_config: A UserConfig instance with lab_name set.
    
    Returns:
        The user's password as a string.
    
    Raises:
        ValueError: If the user_config has no lab_name.
    """
    if not user_config.lab_name:
        raise ValueError("UserConfig has no lab_name configured")
    return get_secret(user_config.lab_name.lower(), vault="msid_lab")


def get_client_certificate() -> Dict[str, object]:
    """
    Get the client certificate credential for ConfidentialClientApplication.
    
    Reads the certificate path from the LAB_APP_CLIENT_CERT_PFX_PATH
    environment variable and returns a dict configured for Subject Name/Issuer
    (SNI) authentication.
    
    Returns:
        A dict suitable for MSAL's ``client_credential`` parameter::
        
            {
                "private_key_pfx_path": "/path/to/cert.pfx",
                "public_certificate": True
            }
    
    Raises:
        EnvironmentError: If LAB_APP_CLIENT_CERT_PFX_PATH is not set.
    """
    cert_path = _clean_env("LAB_APP_CLIENT_CERT_PFX_PATH")
    if not cert_path:
        raise EnvironmentError(
            "LAB_APP_CLIENT_CERT_PFX_PATH environment variable is required "
            "for certificate authentication"
        )
    
    logger.debug("Using client certificate from: %s", cert_path)
    return {
        "private_key_pfx_path": cert_path,
        "public_certificate": True,  # Enable SNI (send certificate chain)
    }