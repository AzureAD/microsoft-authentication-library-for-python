"""
The configuration file would look like this:

{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "username": "your_username@your_tenant.com",
    "password": "This is a sample only. You better NOT persist your password.",
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

import requests
import msal


# Optional logging
# logging.basicConfig(level=logging.DEBUG)  # Enable DEBUG log for entire script
# logging.getLogger("msal").setLevel(logging.INFO)  # Optionally disable MSAL DEBUG logs

config = json.load(open(sys.argv[1]))

# Create a preferably long-lived app instance which maintains a token cache.
app = msal.PublicClientApplication(
    config["client_id"], authority=config["authority"],
    # token_cache=...  # Default cache is in memory only.
                       # You can learn how to use SerializableTokenCache from
                       # https://msal-python.rtfd.io/en/latest/#msal.SerializableTokenCache
    )

# The pattern to acquire a token looks like this.
result = None
errors = []

# Firstly, check the cache to see if this end user has signed in before
accounts = app.get_accounts(username=config["username"])
if accounts:
    logging.info("Account(s) exists in cache, probably with token too. Let's try.")
    result = app.acquire_token_silent(config["scope"], account=accounts[0],
        errors=errors,  # Optional. Only needed if you want to subscribe CA errors
        )

if not result:

    if errors:  # This allows you to handle Conditional Access errors specifically
        print("There is a refresh token in cache, but it was rejected.")
        if errors[0].get("classification") == "basic_action":
            print("""Condition can be resolved by user interaction
                during the interactive authentication flow.""")
            # After this if...else... we'll fall back to acquire_token_by_xyz(...)
        elif errors[0].get("classification") == "additional_action":
            print("""Condition can be resolved by additional remedial interaction
                with the system, outside of the interactive authentication flow.""")
            # After this if...else... we'll fall back to acquire_token_by_xyz(...)
        elif errors[0].get("classification") == "message_only":
            print("""Condition cannot be resolved at this time.
                Launching interactive authentication flow will show a message
                explaining the condition.""")
            sys.exit("Abort without bothering to try acquire_token_by_xyz()")
        else:
            print("Invoke default error handling routine")
            # After this if...else... we'll fall back to acquire_token_by_xyz(...)

    logging.info("No suitable token exists in cache. Let's get a new one from AAD.")
    # See this page for constraints of Username Password Flow.
    # https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki/Username-Password-Authentication
    result = app.acquire_token_by_username_password(
        config["username"], config["password"], scopes=config["scope"])

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
    if 65001 in result.get("error_codes", []):  # Not mean to be coded programatically, but...
        # AAD requires user consent for U/P flow
        print("Visit this to consent:", app.get_authorization_request_url(config["scope"]))
