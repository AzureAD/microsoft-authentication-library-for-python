"""
Prerequisite is documented here:
https://msal-python.readthedocs.io/en/latest/#msal.PublicClientApplication.acquire_token_interactive

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
import json, logging, time, msal, requests

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
    #enable_broker_on_windows=True,  # Opted in. You will be guided to meet the prerequisites, if your app hasn't already
        # See also: https://docs.microsoft.com/en-us/azure/active-directory/develop/scenario-desktop-acquire-token-wam#wam-value-proposition
    token_cache=global_token_cache,  # Let this app (re)use an existing token cache.
        # If absent, ClientApplication will create its own empty token cache
    )


def acquire_and_use_token():
    # The pattern to acquire a token looks like this.
    result = None

    # Firstly, check the cache to see if this end user has signed in before
    accounts = global_app.get_accounts(username=config.get("username"))
    if accounts:
        logging.info("Account(s) exists in cache, probably with token too. Let's try.")
        print("Account(s) already signed in:")
        for a in accounts:
            print(a["username"])
        chosen = accounts[0]  # Assuming the end user chose this one to proceed
        print("Proceed with account: %s" % chosen["username"])
        # Now let's try to find a token in cache for this account
        result = global_app.acquire_token_silent(config["scope"], account=chosen)

    if not result:
        logging.info("No suitable token exists in cache. Let's get a new one from AAD.")
        print("A local browser window will be open for you to sign in. CTRL+C to cancel.")
        result = global_app.acquire_token_interactive(  # Only works if your app is registered with redirect_uri as http://localhost
            config["scope"],
            #parent_window_handle=...,  # If broker is enabled, you will be guided to provide a window handle
            login_hint=config.get("username"),  # Optional.
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
        # Calling graph using the access token
        graph_response = requests.get(  # Use token to call downstream service
            config["endpoint"],
            headers={'Authorization': 'Bearer ' + result['access_token']},)
        print("Graph API call result: %s ..." % graph_response.text[:100])
    else:
        print("Token acquisition failed")  # Examine result["error_description"] etc. to diagnose error


while True:  # Here we mimic a long-lived daemon
    acquire_and_use_token()
    print("Press Ctrl-C to stop.")
    time.sleep(5)  # Let's say your app would run a workload every X minutes
        # The first acquire_and_use_token() call will prompt. Others hit the cache.

