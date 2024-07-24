"""
This sample demonstrates a headless application that acquires a token using
the device code flow and then calls a web API with the token.
The configuration file would look like this:

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
import sys
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
global_app = msal.PublicClientApplication(
    os.getenv('CLIENT_ID'),
    authority=os.getenv('AUTHORITY'),  # For Entra ID or External ID
    oidc_authority=os.getenv('OIDC_AUTHORITY'),  # For External ID with custom domain
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
        # If absent, ClientApplication will create its own empty token cache
    )
scopes = os.getenv("SCOPE", "").split()


def acquire_and_use_token():
    # The pattern to acquire a token looks like this.
    result = None

    # Note: If your device-flow app does not have any interactive ability, you can
    #   completely skip the following cache part. But here we demonstrate it anyway.
    # We now check the cache to see if we have some end users signed in before.
    accounts = global_app.get_accounts()
    if accounts:
        logging.info("Account(s) exists in cache, probably with token too. Let's try.")
        print("Pick the account you want to use to proceed:")
        for a in accounts:
            print(a["username"])
        # Assuming the end user chose this one
        chosen = accounts[0]
        # Now let's try to find a token in cache for this account
        result = global_app.acquire_token_silent(scopes, account=chosen)

    if not result:
        logging.info("No suitable token exists in cache. Let's get a new one from AAD.")

        flow = global_app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise ValueError(
                "Fail to create device flow. Err: %s" % json.dumps(flow, indent=4))

        print(flow["message"])
        sys.stdout.flush()  # Some terminal needs this to ensure the message is shown

        # Ideally you should wait here, in order to save some unnecessary polling
        # input("Press Enter after signing in from another device to proceed, CTRL+C to abort.")

        result = global_app.acquire_token_by_device_flow(flow)  # By default it will block
            # You can follow this instruction to shorten the block time
            #    https://msal-python.readthedocs.io/en/latest/#msal.PublicClientApplication.acquire_token_by_device_flow
            # or you may even turn off the blocking behavior,
            # and then keep calling acquire_token_by_device_flow(flow) in your own customized loop.

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
    time.sleep(5)  # Let's say your app would run a workload every X minutes.
        # The first acquire_and_use_token() call will prompt. Others hit the cache.

