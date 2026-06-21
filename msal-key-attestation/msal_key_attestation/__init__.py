# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
"""msal-key-attestation: KeyGuard attestation support for MSAL Python MSI v2."""

__version__ = "0.1.0"

from .attestation import create_attestation_provider

__all__ = ["create_attestation_provider"]
