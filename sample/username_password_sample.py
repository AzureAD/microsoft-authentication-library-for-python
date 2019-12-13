"""
The configuration file would look like this:
{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "username": "your_username@your_tenant.com",
    "scope": ["User.Read"],
    "password": "This is a sample only. You better NOT persist your password."
}
You can then run this sample with a JSON configuration file:
    python sample.py parameters.json
"""

import sys  # For simplicity, we'll read config file from 1st CLI param sys.argv[1]
import json
import logging

import requests

import msal
import os, atexit
from msal.exceptions import Error

cache = msal.SerializableTokenCache()
if os.path.exists("my_cache.bin"):
    cache.deserialize(open("my_cache.bin", "r").read())
atexit.register(lambda:
    open("my_cache.bin", "w").write(cache.serialize())
    # Hint: The following optional line persists only when state changed
    if cache.has_state_changed else None
    )


# Optional logging
# logging.basicConfig(level=logging.DEBUG)

config = json.load(open(sys.argv[1]))

# Create a preferably long-lived app instance which maintains a token cache.
app = msal.PublicClientApplication(
    config["client_id"], authority=config["authority"], validate_authority=False, token_cache=cache
    # token_cache=...  # Default cache is in memory only.
                       # You can learn how to use SerializableTokenCache from
                       # https://msal-python.rtfd.io/en/latest/#msal.SerializableTokenCache
    )

# The pattern to acquire a token looks like this.
result = None

# Firstly, check the cache to see if this end user has signed in before
accounts = app.get_accounts(username=config["username"])
print("Accounts"+ str(accounts))
if accounts:
    logging.info("Account(s) exists in cache, probably with token too. Let's try.")
    result = app.acquire_token_silent(config["scope"], account=accounts[0], error_response=True)
    if result:
        if isinstance(result, Error):
            option = result.classification
            if option == "basic_action":
                print("""Condition can be resolved by user interaction 
                during the interactive authentication flow.""")
            elif option == "additional_action":
                print("""Condition can be resolved by additional remedial interaction
                with the system, outside of the interactive authentication flow.""")
            elif option == "message_only":
                print("""Condition cannot be resolved at this time.
                Launching interactive authentication flow will show a message 
                explaining the condition.""")
                sys.exit("Abort without bothering to try acquire_token_by_xyz()")
            elif option == "consent_required":
                print("""Condition can be resolved by giving consent 
                during the interactive authentication flow.""")
            elif option == "user_password_expired":
                print("User password expired")
            else:
                result = app.acquire_token_by_username_password(
                    config["username"], config["password"], scopes=config["scope"])
                print(json.dumps(result, indent=4))

else:
    result = app.acquire_token_by_username_password(
            config["username"], config["password"], scopes=config["scope"])
    print(json.dumps(result, indent=4))

if type(result) is dict:
    if "access_token" in result:
        print(result["access_token"])
        print(result["token_type"])
        print(result["expires_in"])  # You don't normally need to care about this.
        # It will be good for at least 5 minutes.
        graph_data = requests.get(  # Use token to call downstream service
            config["endpoint"],
            headers={'Authorization': 'Bearer ' + result['access_token']}, ).json()
        print("Graph API call result: %s" % json.dumps(graph_data, indent=2))
    else:
        print(result.get("error"))
        print(result.get("error_description"))
        print(result.get("correlation_id"))  # You may need this when reporting a bug
        if 65001 in result.get("error_codes", []):  # Not mean to be coded programatically, but...
            # AAD requires user consent for U/P flow
            print("Visit this to consent:", app.get_authorization_request_url(config['scope']))
