"""This script is used to impersonate Azure CLI
and run 3 pairs of end-to-end tests with broker.
Although not fully automated, it requires only several clicks to finish.

Each time a new PyMsalRuntime is going to be released,
we can use this script to test it with a given version of MSAL Python.
"""
import msal
import getpass
import os
import sys
try:
    from dotenv import load_dotenv  # Use this only in local dev machine
    load_dotenv()  # take environment variables from .env.
except:
    pass

_AZURE_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
SCOPE_ARM = "https://management.azure.com/.default"
placeholder_auth_scheme = msal.PopAuthScheme(
    http_method=msal.PopAuthScheme.HTTP_GET,
    url="https://example.com/endpoint",
    nonce="placeholder",
    )
_JWK1 = """{"kty":"RSA", "n":"2tNr73xwcj6lH7bqRZrFzgSLj7OeLfbn8216uOMDHuaZ6TEUBDN8Uz0ve8jAlKsP9CQFCSVoSNovdE-fs7c15MxEGHjDcNKLWonznximj8pDGZQjVdfK-7mG6P6z-lgVcLuYu5JcWU_PeEqIKg5llOaz-qeQ4LEDS4T1D2qWRGpAra4rJX1-kmrWmX_XIamq30C9EIO0gGuT4rc2hJBWQ-4-FnE1NXmy125wfT3NdotAJGq5lMIfhjfglDbJCwhc8Oe17ORjO3FsB5CLuBRpYmP7Nzn66lRY3Fe11Xz8AEBl3anKFSJcTvlMnFtu3EpD-eiaHfTgRBU7CztGQqVbiQ", "e":"AQAB"}"""
_SSH_CERT_DATA = {"token_type": "ssh-cert", "key_id": "key1", "req_cnf": _JWK1}
_SSH_CERT_SCOPE = "https://pas.windows.net/CheckMyAccess/Linux/.default"

if sys.platform == "win32":
    pca = msal.PublicClientApplication(
        _AZURE_CLI,
        authority="https://login.microsoftonline.com/organizations",
        enable_broker_on_windows=True)
elif sys.platform == "darwin":
    pca = msal.PublicClientApplication(
        _AZURE_CLI,
        authority="https://login.microsoftonline.com/organizations",
        enable_broker_on_mac=True)
else:
    print("Platform not supported yet.")
    exit()

def interactive_and_silent(scopes, auth_scheme, data, expected_token_type):
    print("An account picker shall be pop up, possibly behind this console. Continue from there.")
    result = pca.acquire_token_interactive(
        scopes,
        prompt="select_account",  # "az login" does this
        parent_window_handle=pca.CONSOLE_WINDOW_HANDLE,  # This script is a console app
        enable_msa_passthrough=True,  # Azure CLI is an MSA-passthrough app
        auth_scheme=auth_scheme,
        data=data or {},
        )
    _assert(result, expected_token_type)

    accounts = pca.get_accounts()
    assert accounts, "The logged in account should have been established by interactive flow"
    result = pca.acquire_token_silent(
        scopes,
        account=accounts[0],
        force_refresh=True,  # Bypass MSAL Python's token cache to test PyMsalRuntime
        auth_scheme=auth_scheme,
        data=data or {},
        )
    _assert(result, expected_token_type)

def test_broker_username_password(scopes, expected_token_type):
    print("Testing broker username password flows by using accounts in local .env")
    username = os.getenv("BROKER_TEST_ACCOUNT") or input("Input test account for broker test: ")
    password = os.getenv("BROKER_TEST_ACCOUNT_PASSWORD") or getpass.getpass("Input test account's password: ")
    assert(username and password, "You need to provide a test account and its password")
    result = pca.acquire_token_by_username_password(username, password, scopes)
    _assert(result, expected_token_type)
    assert(result.get("token_source") == "broker")
    print("Username password test succeeds.")

def _assert(result, expected_token_type):
    assert result.get("access_token"), f"We should obtain a token. Got {result} instead."
    assert result.get("token_source") == "broker", "Token should be obtained via broker"
    assert result.get("token_type").lower() == expected_token_type.lower(), f"{expected_token_type} not found"

for i in range(2):  # Mimic Azure CLI's issue report
    interactive_and_silent(
        scopes=[SCOPE_ARM], auth_scheme=None, data=None, expected_token_type="bearer")

interactive_and_silent(
    scopes=[SCOPE_ARM], auth_scheme=placeholder_auth_scheme, data=None, expected_token_type="pop")
interactive_and_silent(
    scopes=[_SSH_CERT_SCOPE],
    data=_SSH_CERT_DATA,
    auth_scheme=None,
    expected_token_type="ssh-cert",
    )

test_broker_username_password(scopes=[SCOPE_ARM], expected_token_type="bearer")
