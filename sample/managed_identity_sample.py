"""
This sample demonstrates a daemon application that acquires a token using a
managed identity and then calls a web API with the token.

This sample loads its configuration from a .env file.

To make this sample work, you need to choose this template:

    .env.sample.managed_identity

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
global_app = msal.ManagedIdentityClient(
    os.getenv('MANAGED_IDENTITY') or msal.SystemAssignedManagedIdentity(),
    http_client=requests.Session(),
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
        # If absent, ClientApplication will create its own empty token cache
    )
resource = os.getenv("RESOURCE")


def acquire_and_use_token():
    # ManagedIdentityClient.acquire_token_for_client(...) will automatically look up
    # a token from cache, and fall back to acquire a fresh token when needed.
    result = global_app.acquire_token_for_client(resource=resource)

    if "access_token" in result:
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

