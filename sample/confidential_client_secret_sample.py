"""
This sample demonstrates a daemon application that acquires a token using a
client secret and then calls a web API with the token.

This sample loads its configuration from a .env file.

To make this sample work, you need to choose one of the following templates:

    .env.sample.entra-id
    .env.sample.external-id
    .env.sample.external-id-with-custom-domain

Copy the chosen template to a new file named .env, and fill in the values.

You can then run this sample:

    python name_of_this_script.py
"""

import json
import logging
import os
import time

from dotenv import load_dotenv  # Need "pip install python-dotenv"
import msal
import requests


# Optional logging
# logging.basicConfig(level=logging.DEBUG)  # Enable DEBUG log for entire script
# logging.getLogger("msal").setLevel(logging.INFO)  # Optionally disable MSAL DEBUG logs

load_dotenv()  # We use this to load configuration from a .env file

# If for whatever reason you plan to recreate same ClientApplication periodically,
# you shall create one global token cache and reuse it by each ClientApplication
global_token_cache = msal.TokenCache()  # The TokenCache() is in-memory.
    # See more options in https://msal-python.readthedocs.io/en/latest/#tokencache

# Create a preferably long-lived app instance, to avoid the overhead of app creation
global_app = msal.ConfidentialClientApplication(
    os.getenv('CLIENT_ID'),
    authority=os.getenv('AUTHORITY'),  # For Entra ID or External ID
    oidc_authority=os.getenv('OIDC_AUTHORITY'),  # For External ID with custom domain
    client_credential=os.getenv('CLIENT_SECRET'),
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
        # If absent, ClientApplication will create its own empty token cache
    )
scopes = os.getenv("SCOPE", "").split()


def acquire_and_use_token():
    # Since MSAL 1.23, acquire_token_for_client(...) will automatically look up
    # a token from cache, and fall back to acquire a fresh token when needed.
    result = global_app.acquire_token_for_client(scopes=scopes)

    if "access_token" in result:
        print("Token was obtained from:", result["token_source"])  # Since MSAL 1.25
        if os.getenv('ENDPOINT'):
            # Calling a web API using the access token
            api_result = requests.get(
                os.getenv('ENDPOINT'),
                headers={'Authorization': 'Bearer ' + result['access_token']},
                ).json()  # Assuming the response is JSON
            print("Web API call result", json.dumps(api_result, indent=2))
        else:
            print("Token acquisition result", json.dumps(result, indent=2))
    else:
        print("Token acquisition failed", result)  # Examine result["error_description"] etc. to diagnose error


while True:  # Here we mimic a long-lived daemon
    acquire_and_use_token()
    print("Press Ctrl-C to stop.")
    time.sleep(5)  # Let's say your app would run a workload every X minutes

