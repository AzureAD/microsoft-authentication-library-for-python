"""
The configuration file would look like this (sans those // comments):
{
    "tenant": "your_tenant_name",
        // Your target tenant, DNS name
    "client_id": "your_client_id",
        // Target app ID in Azure AD
    "scope": ["https://graph.microsoft.com/.default"],
        // Specific to Client Credentials Grant i.e. acquire_token_for_client(),
        // you don't specify, in the code, the individual scopes you want to access.
        // Instead, you statically declared them when registering your application.
        // Therefore the only possible scope is "resource/.default"
        // (here "https://graph.microsoft.com/.default")
        // which means "the static permissions defined in the application".
    "vault_tenant": "your_vault_tenant_name",
        // Your Vault tenant may be different to your target tenant
        // If that's not the case, you can set this to the same
        // as "tenant"
    "vault_clientid": "your_vault_client_id",
        // Client ID of your vault app in your vault tenant
    "vault_clientsecret": "your_vault_client_secret",
        // Secret for your vault app
    "vault_url": "your_vault_url",
        // URL of your vault app
    "cert": "your_cert_name",
        // Name of your certificate in your vault
    "cert_thumb": "your_cert_thumbprint",
        // Thumbprint of your certificate
    "endpoint": "https://graph.microsoft.com/v1.0/users"
        // For this resource to work, you need to visit Application Permissions
        // page in portal, declare scope User.Read.All, which needs admin consent
        // https://github.com/Azure-Samples/ms-identity-python-daemon/blob/master/2-Call-MsGraph-WithCertificate/README.md
}
You can then run this sample with a JSON configuration file:
    python sample.py parameters.json
"""

import base64
import json
import logging
import requests
import sys
import time
import uuid
import msal

# Optional logging
# logging.basicConfig(level=logging.DEBUG)  # Enable DEBUG log for entire script
# logging.getLogger("msal").setLevel(logging.INFO)  # Optionally disable MSAL DEBUG logs

from azure.keyvault import KeyVaultClient, KeyVaultAuthentication
from azure.common.credentials import ServicePrincipalCredentials
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

config = json.load(open(sys.argv[1]))

def auth_vault_callback(server, resource, scope):
    credentials = ServicePrincipalCredentials(
        client_id=config['vault_clientid'],
        secret=config['vault_clientsecret'],
        tenant=config['vault_tenant'],
        resource='https://vault.azure.net'
    )
    token = credentials.token
    return token['token_type'], token['access_token']


def make_vault_jwt():

    header = {
              'alg': 'RS256',
              'typ': 'JWT',
              'x5t': base64.b64encode(
                        config['cert_thumb'].decode('hex'))
             }
    header_b64 = base64.b64encode(json.dumps(header).encode('utf-8'))

    body = {
            'aud': "https://login.microsoftonline.com/%s/oauth2/token" %
                   config['tenant'],
            'exp': (int(time.time()) + 600),
            'iss': config['client_id'],
            'jti': str(uuid.uuid4()),
            'nbf': int(time.time()),
            'sub': config['client_id']
            }
    body_b64 = base64.b64encode(json.dumps(body).encode('utf-8'))

    full_b64 = b'.'.join([header_b64, body_b64])

    client = KeyVaultClient(KeyVaultAuthentication(auth_vault_callback))
    chosen_hash = hashes.SHA256()
    hasher = hashes.Hash(chosen_hash, default_backend())
    hasher.update(full_b64)
    digest = hasher.finalize()
    signed_digest = client.sign(config['vault_url'],
                                config['cert'], '', 'RS256',
                                digest).result

    full_token = b'.'.join([full_b64, base64.b64encode(signed_digest)])

    return full_token


authority = "https://login.microsoftonline.com/%s" % config['tenant']

app = msal.ConfidentialClientApplication(
        config['client_id'], authority=authority, client_credential={"jwt": make_vault_jwt()}
      )

# The pattern to acquire a token looks like this.
result = None

# Firstly, looks up a token from cache
# Since we are looking for token for the current app, NOT for an end user,
# notice we give account parameter as None.
result = app.acquire_token_silent(config["scope"], account=None)

if not result:
    logging.info("No suitable token exists in cache. Let's get a new one from AAD.")
    result = app.acquire_token_for_client(scopes=config["scope"])

if "access_token" in result:
    # Calling graph using the access token
    graph_data = requests.get(  # Use token to call downstream service
        config["endpoint"],
        headers={'Authorization': 'Bearer ' + result['access_token']},).json()
    print("Graph API call result: %s" % json.dumps(graph_data, indent=2))
else:
    print(result.get("error"))
    print(result.get("error_description"))
    print(result.get("correlation_id"))  # You may need this when reporting a bug

