"""
The configuration file would look like this (sans those // comments):
{
    "tenant": "your_tenant_name",
        // Your target tenant, DNS name
    "client_id": "your_client_id came from https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
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



# If for whatever reason you plan to recreate same ClientApplication periodically,
# you shall create one global token cache and reuse it by each ClientApplication
global_token_cache = msal.TokenCache()  # The TokenCache() is in-memory.
    # See more options in https://msal-python.readthedocs.io/en/latest/#tokencache

# Create a preferably long-lived app instance, to avoid the overhead of app creation
global_app = msal.ConfidentialClientApplication(
    config['client_id'],
    authority="https://login.microsoftonline.com/%s" % config['tenant'],
    client_credential={"client_assertion": make_vault_jwt()},
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
        # If absent, ClientApplication will create its own empty token cache
    )


def acquire_and_use_token():
    # Since MSAL 1.23, acquire_token_for_client(...) will automatically look up
    # a token from cache, and fall back to acquire a fresh token when needed.
    result = global_app.acquire_token_for_client(scopes=config["scope"])

    if "access_token" in result:
        print("Token was obtained from:", result["token_source"])  # Since MSAL 1.25
        # Calling graph using the access token
        graph_data = requests.get(  # Use token to call downstream service
            config["endpoint"],
            headers={'Authorization': 'Bearer ' + result['access_token']},).json()
        print("Graph API call result: %s" % json.dumps(graph_data, indent=2))
    else:
        print("Token acquisition failed", result)  # Examine result["error_description"] etc. to diagnose error


while True:  # Here we mimic a long-lived daemon
    acquire_and_use_token()
    print("Press Ctrl-C to stop.")
    time.sleep(5)  # Let's say your app would run a workload every X minutes

