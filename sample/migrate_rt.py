"""
The configuration file would look like this:

{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "scope": ["User.ReadBasic.All"],
        // You can find the other permission names from this document
        // https://docs.microsoft.com/en-us/graph/permissions-reference
}

You can then run this sample with a JSON configuration file:

    python sample.py parameters.json
"""

import sys  # For simplicity, we'll read config file from 1st CLI param sys.argv[1]
import json
import logging

import msal


# Optional logging
# logging.basicConfig(level=logging.DEBUG)  # Enable DEBUG log for entire script
# logging.getLogger("msal").setLevel(logging.INFO)  # Optionally disable MSAL DEBUG logs

config = json.load(open(sys.argv[1]))

def get_rt_via_old_app():
    # Let's pretend this is an old app powered by ADAL
    app = msal.PublicClientApplication(
        config["client_id"], authority=config["authority"])
    flow = app.initiate_device_flow(scopes=config["scope"])
    if "user_code" not in flow:
        raise ValueError(
            "Fail to create device flow. Err: %s" % json.dumps(flow, indent=4))
    print(flow["message"])
    sys.stdout.flush()  # Some terminal needs this to ensure the message is shown

    # Ideally you should wait here, in order to save some unnecessary polling
    # input("Press Enter after signing in from another device to proceed, CTRL+C to abort.")

    result = app.acquire_token_by_device_flow(flow)  # By default it will block
    assert "refresh_token" in result, "We should have a successful result"
    return result["refresh_token"]

try:  # For easier testing, we try to reload a RT from previous run
    old_rt = json.load(open("rt.json"))[0]
except:  # If that is not possible, we acquire a RT
    old_rt = get_rt_via_old_app()
    json.dump([old_rt], open("rt.json", "w"))

# Now we will try to migrate this old_rt into a new app powered by MSAL

token_cache = msal.SerializableTokenCache()
assert token_cache.serialize() == '{}', "Token cache is initially empty"
app = msal.PublicClientApplication(
    config["client_id"], authority=config["authority"], token_cache=token_cache)
result = app.import_refresh_token(old_rt, config["scope"])
if "error" in result:
    print("Migration unsuccessful. Error: ", json.dumps(result, indent=2))
else:
    print("Migration is successful")
    logging.debug("Token cache contains: %s", token_cache.serialize())

# From now on, the RT is saved inside MSAL's cache,
# and becomes available in normal MSAL coding pattern. For example:
accounts = app.get_accounts()
if accounts:
    account = accounts[0]  # Assuming end user pick this account
    result = app.acquire_token_silent(config["scope"], account)
    if "access_token" in result:
        print("RT is available in MSAL's cache, and can be used to acquire new AT")

