"""
The configuration file would look like this:

{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "scope": ["https://graph.microsoft.com/.default"]
}

You can then run this sample with a JSON configuration file:

    python sample.py parameters.json
"""

import sys  # For simplicity, we'll read config file from 1st CLI param sys.argv[1]
import json
import logging

import msal


# Optional logging
# logging.basicConfig(level=logging.DEBUG)

config = json.load(open(sys.argv[1]))

# Create a preferrably long-lived app instance which maintains a token cache.
app = msal.PublicClientApplication(
    config["client_id"], authority=config["authority"],
    # token_cache=...  # Default cache is in memory only.
                       # See SerializableTokenCache for more details.
    )

# The pattern to acquire a token looks like this.
result = None

# If your device-flow app does not have any interactive ability,
# you can completely skip the following cache part. Here we demonstrate it anyway.
# We now check the cache to see if we have some end users signed in before.
accounts = app.get_accounts()
if accounts:
    # If so, you could then somehow display these accounts and let end user choose
    print("Pick the account you want to use to proceed:")
    for a in accounts:
        print(a["username"])
    # Assumeing the end user chose this one
    chosen = accounts[0]
    # Now let's try to find a token in cache for this account
    result = app.acquire_token_silent(config["scope"], account=accounts[0])

if not result:
    # So no suitable token exists in cache. Let's get a new one from AAD.
    flow = app.initiate_device_flow(scopes=config["scope"])
    print(flow["message"])
    # Ideally you should wait here, in order to save some unnecessary polling
    # input("Press Enter after you successfully login from another device...")
    result = app.acquire_token_by_device_flow(flow)  # By default it will block

if "access_token" in result:
    print(result["access_token"])
    print(result["token_type"])
    print(result["expires_in"])  # You don't normally need to care about this.
                                 # It will be good for at least 5 minutes.
else:
    print(result.get("error"))
    print(result.get("error_description"))
    print(result.get("correlation_id"))  # You may need this when reporting a bug

