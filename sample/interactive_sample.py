"""
This sample demonstrates a desktop application that acquires a token interactively
and then calls a web API with the token.

Prerequisite is documented here:
https://msal-python.readthedocs.io/en/latest/#msal.PublicClientApplication.acquire_token_interactive

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
global_app = msal.PublicClientApplication(
    os.getenv('CLIENT_ID'),
    authority=os.getenv('AUTHORITY'),  # For Entra ID or External ID
    oidc_authority=os.getenv('OIDC_AUTHORITY'),  # For External ID with custom domain
    #enable_broker_on_windows=True,  # Opted in. You will be guided to meet the prerequisites, if your app hasn't already
        # See also: https://docs.microsoft.com/en-us/azure/active-directory/develop/scenario-desktop-acquire-token-wam#wam-value-proposition
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
        print("Account(s) already signed in:")
        for a in accounts:
            print(a["username"])
        chosen = accounts[0]  # Assuming the end user chose this one to proceed
        print("Proceed with account: %s" % chosen["username"])
        # Now let's try to find a token in cache for this account
        result = global_app.acquire_token_silent(scopes, account=chosen)

    if not result:
        logging.info("No suitable token exists in cache. Let's get a new one from AAD.")
        print("A local browser window will be open for you to sign in. CTRL+C to cancel.")
        result = global_app.acquire_token_interactive(  # Only works if your app is registered with redirect_uri as http://localhost
            scopes,
            #parent_window_handle=...,  # If broker is enabled, you will be guided to provide a window handle
            login_hint=os.getenv("USERNAME"),  # Optional.
                # If you know the username ahead of time, this parameter can pre-fill
                # the username (or email address) field of the sign-in page for the user,
                # Often, apps use this parameter during reauthentication,
                # after already extracting the username from an earlier sign-in
                # by using the preferred_username claim from returned id_token_claims.

            #prompt=msal.Prompt.SELECT_ACCOUNT,  # Or simply "select_account". Optional. It forces to show account selector page
            #prompt=msal.Prompt.CREATE,  # Or simply "create". Optional. It brings user to a self-service sign-up flow.
                # Prerequisite: https://docs.microsoft.com/en-us/azure/active-directory/external-identities/self-service-sign-up-user-flow
            )

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
        # The first acquire_and_use_token() call will prompt. Others hit the cache.

