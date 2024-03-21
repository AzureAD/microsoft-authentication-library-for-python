"""
The configuration file would look like this:

{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "username": "your_username@your_tenant.com",
    "scope": ["User.ReadBasic.All"],
        // You can find the other permission names from this document
        // https://docs.microsoft.com/en-us/graph/permissions-reference
    "endpoint": "https://graph.microsoft.com/v1.0/users"
        // You can find more Microsoft Graph API endpoints from Graph Explorer
        // https://developer.microsoft.com/en-us/graph/graph-explorer
}

You can then run this sample with a JSON configuration file:

    python sample.py parameters.json
"""

import sys  # For simplicity, we'll read config file from 1st CLI param sys.argv[1]
import json
import logging
import time

import requests
import msal
from msal.token_cache import  TokenCache


# Optional logging
# logging.basicConfig(level=logging.DEBUG)  # Enable DEBUG log for entire script
# logging.getLogger("msal").setLevel(logging.INFO)  # Optionally disable MSAL DEBUG logs

config = json.load(open(sys.argv[1]))

# If for whatever reason you plan to recreate same ClientApplication periodically,
# you shall create one global token cache and reuse it by each ClientApplication
global_token_cache = msal.TokenCache()  # The TokenCache() is in-memory.
# See more options in https://msal-python.readthedocs.io/en/latest/#tokencache

# Create a preferably long-lived app instance, to avoid the overhead of app creation
global_app = msal.PublicClientApplication(
    config["client_id"], authority=config["authority"],
    client_credential=config.get("client_secret"),
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
    # If absent, ClientApplication will create its own empty token cache
)


def acquire_and_use_token():
    # The pattern to acquire a token looks like this.
    result = None

    # Firstly, check the cache to see if this end user has signed in before
    accounts = global_app.get_accounts(username=config["username"])
    if accounts:
        print("Account(s) exists in cache, probably with token too. Let's try.")
        result = global_app.acquire_token_silent(config["scope"], account=accounts[0])

    if not result:
        print("No suitable token exists in cache. Let's get a new one from AAD.")
        # See this page for constraints of Username Password Flow.
        # https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki/Username-Password-Authentication
        result = global_app.acquire_token_integrated_windows_auth(
            config["username"], scopes=config["scope"])

    if "access_token" in result:
        print("Token was obtained from:", result["token_source"])  # Since MSAL 1.25
        # Calling graph using the access token
        graph_data = requests.get(  # Use token to call downstream service
            config["endpoint"],
            headers={'Authorization': 'Bearer ' + result['access_token']},).json()
        print("Graph API call result: %s" % json.dumps(graph_data, indent=2))
    else:
        print("Token acquisition failed")  # Examine result["error_description"] etc. to diagnose error
        print(result)
        if 65001 in result.get("error_codes", []):  # Not mean to be coded programatically, but...
            raise RuntimeError(
                "AAD requires user consent for U/P flow to succeed. "
                "Run acquire_token_interactive() instead.")


while True:  # Here we mimic a long-lived daemon
    acquire_and_use_token()
    print("Press Ctrl-C to stop.")
    time.sleep(5)  # Let's say your app would run a workload every X minutes.

