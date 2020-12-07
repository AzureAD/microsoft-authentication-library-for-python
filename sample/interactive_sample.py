"""
The configuration file would look like this:

{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "scope": ["User.ReadBasic.All"],
        // You can find the other permission names from this document
        // https://docs.microsoft.com/en-us/graph/permissions-reference
    "username": "your_username@your_tenant.com",  // This is optional
    "endpoint": "https://graph.microsoft.com/v1.0/users"
        // You can find more Microsoft Graph API endpoints from Graph Explorer
        // https://developer.microsoft.com/en-us/graph/graph-explorer
}

You can then run this sample with a JSON configuration file:

    python sample.py parameters.json
"""

import sys  # For simplicity, we'll read config file from 1st CLI param sys.argv[1]
import json, logging, msal, requests

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

# Firstly, check the cache to see if this end user has signed in before
accounts = app.get_accounts(username=config.get("username"))
if accounts:
    logging.info("Account(s) exists in cache, probably with token too. Let's try.")
    print("Account(s) already signed in:")
    for a in accounts:
        print(a["username"])
    chosen = accounts[0]  # Assuming the end user chose this one to proceed
    print("Proceed with account: %s" % chosen["username"])
    # Now let's try to find a token in cache for this account
    result = app.acquire_token_silent(config["scope"], account=chosen)

if not result:
    logging.info("No suitable token exists in cache. Let's get a new one from AAD.")
    print("A local browser window will be open for you to sign in. CTRL+C to cancel.")
    result = app.acquire_token_interactive(
        config["scope"],
        login_hint=config.get("username"),  # You can use this parameter to pre-fill
            # the username (or email address) field of the sign-in page for the user,
            # if you know the username ahead of time.
            # Often, apps use this parameter during reauthentication,
            # after already extracting the username from an earlier sign-in
            # by using the preferred_username claim from returned id_token_claims.
        )

if "access_token" in result:
    # Calling graph using the access token
    graph_response = requests.get(  # Use token to call downstream service
        config["endpoint"],
        headers={'Authorization': 'Bearer ' + result['access_token']},)
    print("Graph API call result: %s ..." % graph_response.text[:100])
else:
    print(result.get("error"))
    print(result.get("error_description"))
    print(result.get("correlation_id"))  # You may need this when reporting a bug
