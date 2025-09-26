"""
This sample demonstrates a desktop application that acquires a token using a
pair of username and password, and then calls a web API with the token.

This sample loads its configuration from a .env file.

To make this sample work, you need to choose one of the following templates:

    .env.sample.entra-id
    .env.sample.external-id
    .env.sample.external-id-with-custom-domain

Copy the chosen template to a new file named .env, and fill in the values.

You can then run this sample:

    python name_of_this_script.py
"""

import getpass
import json
import logging
import os
import sys
import time

from dotenv import load_dotenv  # Need "pip install python-dotenv"
import msal
import requests


# Optional logging
# logging.basicConfig(level=logging.DEBUG)  # Enable DEBUG log for entire script
# logging.getLogger("msal").setLevel(logging.INFO)  # Optionally disable MSAL DEBUG logs

load_dotenv()  # We use this to load configuration from a .env file
if not os.getenv("USERNAME"):
    sys.exit("Please provide a username in the environment variable USERNAME.")
password = getpass.getpass("Password for {}: ".format(os.getenv("USERNAME")))

# If for whatever reason you plan to recreate same ClientApplication periodically,
# you shall create one global token cache and reuse it by each ClientApplication
global_token_cache = msal.TokenCache()  # The TokenCache() is in-memory.
    # See more options in https://msal-python.readthedocs.io/en/latest/#tokencache

# Create a preferably long-lived app instance, to avoid the overhead of app creation
global_app = msal.ClientApplication(
    os.getenv('CLIENT_ID'),
    authority=os.getenv('AUTHORITY'),  # For Entra ID or External ID
    oidc_authority=os.getenv('OIDC_AUTHORITY'),  # For External ID with custom domain
    client_credential=os.getenv('CLIENT_SECRET') or None,  # Treat empty string as None
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
        # If absent, ClientApplication will create its own empty token cache
    )
scopes = os.getenv("SCOPE", "").split()


def acquire_and_use_token():
    # The pattern to acquire a token looks like this.
    result = None

    # Firstly, check the cache to see if this end user has signed in before
    accounts = global_app.get_accounts(username=os.getenv("USERNAME"))
    if accounts:
        logging.info("Account(s) exists in cache, probably with token too. Let's try.")
        result = global_app.acquire_token_silent(scopes, account=accounts[0])

    if not result:
        logging.info("No suitable token exists in cache. Let's get a new one from AAD.")
        # See this page for constraints of Username Password Flow.
        # https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki/Username-Password-Authentication
        result = global_app.acquire_token_by_username_password(
            os.getenv("USERNAME"), password, scopes=scopes)

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
        if 65001 in result.get("error_codes", []):  # Not mean to be coded programatically, but...
            raise RuntimeError(
                "Microsoft Entra ID requires user consent for U/P flow to succeed. "
                "Run acquire_token_interactive() instead.")


while True:  # Here we mimic a long-lived daemon
    acquire_and_use_token()
    print("Press Ctrl-C to stop.")
    time.sleep(5)  # Let's say your app would run a workload every X minutes.

