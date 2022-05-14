import getpass, logging, pprint, sys, msal


def _input_boolean(message):
    return input(
        "{} (N/n/F/f or empty means False, otherwise it is True): ".format(message)
        ) not in ('N', 'n', 'F', 'f', '')

def _input(message, default=None):
    return input(message.format(default=default)).strip() or default

def _select_options(
        options, header="Your options:", footer="    Your choice? ", option_renderer=str,
        accept_nonempty_string=False,
        ):
    assert options, "options must not be empty"
    if header:
        print(header)
    for i, o in enumerate(options, start=1):
        print("    {}: {}".format(i, option_renderer(o)))
    if accept_nonempty_string:
        print("    Or you can just type in your input.")
    while True:
        raw_data = input(footer)
        try:
            choice = int(raw_data)
            if 1 <= choice <= len(options):
                return options[choice - 1]
        except ValueError:
            if raw_data and accept_nonempty_string:
                return raw_data

def _input_scopes():
    return _select_options([
        "https://graph.microsoft.com/.default",
        "https://management.azure.com/.default",
        "User.Read",
        "User.ReadBasic.All",
        ],
        header="Select a scope (multiple scopes can only be input by manually typing them, delimited by space):",
        accept_nonempty_string=True,
        ).split()

def _select_account(app):
    accounts = app.get_accounts()
    if accounts:
        return _select_options(
            accounts,
            option_renderer=lambda a: a["username"],
            header="Account(s) already signed in inside MSAL Python:",
            )
    else:
        print("No account available inside MSAL Python. Use other methods to acquire token first.")

def acquire_token_silent(app):
    """acquire_token_silent() - with an account already signed into MSAL Python."""
    account = _select_account(app)
    if account:
        pprint.pprint(app.acquire_token_silent(
            _input_scopes(),
            account=account,
            force_refresh=_input_boolean("Bypass MSAL Python's token cache?"),
            ))

def _acquire_token_interactive(app, scopes, data=None):
    prompt = _select_options([
        {"value": None, "description": "Unspecified. Proceed silently with a default account (if any), fallback to prompt."},
        {"value": "none", "description": "none. Proceed silently with a default account (if any), or error out."},
        {"value": "select_account", "description": "select_account. Prompt with an account picker."},
        ],
        option_renderer=lambda o: o["description"],
        header="Prompt behavior?")["value"]
    raw_login_hint = _select_options(
        # login_hint is unnecessary when prompt=select_account,
        # but we still let tester input login_hint, just for testing purpose.
        [None] + [a["username"] for a in app.get_accounts()],
        header="login_hint? (If you have multiple signed-in sessions in browser, and you specify a login_hint to match one of them, you will bypass the account picker.)",
        accept_nonempty_string=True,
        )
    login_hint = raw_login_hint["username"] if isinstance(raw_login_hint, dict) else raw_login_hint
    result = app.acquire_token_interactive(
        scopes, prompt=prompt, login_hint=login_hint, data=data or {})
    if login_hint and "id_token_claims" in result:
        signed_in_user = result.get("id_token_claims", {}).get("preferred_username")
        if signed_in_user != login_hint:
            logging.warning('Signed-in user "%s" does not match login_hint', signed_in_user)
    return result

def acquire_token_interactive(app):
    """acquire_token_interactive() - User will be prompted if app opts to do select_account."""
    pprint.pprint(_acquire_token_interactive(app, _input_scopes()))

def acquire_token_by_username_password(app):
    """acquire_token_by_username_password() - See constraints here: https://docs.microsoft.com/en-us/azure/active-directory/develop/msal-authentication-flows#constraints-for-ropc"""
    pprint.pprint(app.acquire_token_by_username_password(
        _input("username: "), getpass.getpass("password: "), scopes=_input_scopes()))

_JWK1 = """{"kty":"RSA", "n":"2tNr73xwcj6lH7bqRZrFzgSLj7OeLfbn8216uOMDHuaZ6TEUBDN8Uz0ve8jAlKsP9CQFCSVoSNovdE-fs7c15MxEGHjDcNKLWonznximj8pDGZQjVdfK-7mG6P6z-lgVcLuYu5JcWU_PeEqIKg5llOaz-qeQ4LEDS4T1D2qWRGpAra4rJX1-kmrWmX_XIamq30C9EIO0gGuT4rc2hJBWQ-4-FnE1NXmy125wfT3NdotAJGq5lMIfhjfglDbJCwhc8Oe17ORjO3FsB5CLuBRpYmP7Nzn66lRY3Fe11Xz8AEBl3anKFSJcTvlMnFtu3EpD-eiaHfTgRBU7CztGQqVbiQ", "e":"AQAB"}"""
SSH_CERT_DATA = {"token_type": "ssh-cert", "key_id": "key1", "req_cnf": _JWK1}
SSH_CERT_SCOPE = ["https://pas.windows.net/CheckMyAccess/Linux/.default"]

def acquire_ssh_cert_silently(app):
    """Acquire an SSH Cert silently- This typically only works with Azure CLI"""
    account = _select_account(app)
    if account:
        result = app.acquire_token_silent(
            SSH_CERT_SCOPE,
            account,
            data=SSH_CERT_DATA,
            force_refresh=_input_boolean("Bypass MSAL Python's token cache?"),
            )
        pprint.pprint(result)
        if result and result.get("token_type") != "ssh-cert":
            logging.error("Unable to acquire an ssh-cert.")

def acquire_ssh_cert_interactive(app):
    """Acquire an SSH Cert interactively - This typically only works with Azure CLI"""
    result = _acquire_token_interactive(app, SSH_CERT_SCOPE, data=SSH_CERT_DATA)
    pprint.pprint(result)
    if result.get("token_type") != "ssh-cert":
        logging.error("Unable to acquire an ssh-cert")

def remove_account(app):
    """remove_account() - Invalidate account and/or token(s) from cache, so that acquire_token_silent() would be reset"""
    account = _select_account(app)
    if account:
        app.remove_account(account)
        print('Account "{}" and/or its token(s) are signed out from MSAL Python'.format(account["username"]))

def exit(_):
    """Exit"""
    bug_link = "https://github.com/AzureAD/microsoft-authentication-library-for-python/issues/new/choose"
    print("Bye. If you found a bug, please report it here: {}".format(bug_link))
    sys.exit()

def main():
    print("Welcome to the Msal Python Console Test App, committed at 2022-5-2\n")
    chosen_app = _select_options([
        {"client_id": "04b07795-8ddb-461a-bbee-02f9e1bf7b46", "name": "Azure CLI (Correctly configured for MSA-PT)"},
        {"client_id": "04f0c124-f2bc-4f59-8241-bf6df9866bbd", "name": "Visual Studio (Correctly configured for MSA-PT)"},
        {"client_id": "95de633a-083e-42f5-b444-a4295d8e9314", "name": "Whiteboard Services (Non MSA-PT app. Accepts AAD & MSA accounts.)"},
        ],
        option_renderer=lambda a: a["name"],
        header="Impersonate this app (or you can type in the client_id of your own app)",
        accept_nonempty_string=True)
    app = msal.PublicClientApplication(
        chosen_app["client_id"] if isinstance(chosen_app, dict) else chosen_app,
        authority=_select_options([
            "https://login.microsoftonline.com/common",
            "https://login.microsoftonline.com/organizations",
            "https://login.microsoftonline.com/microsoft.onmicrosoft.com",
            "https://login.microsoftonline.com/msidlab4.onmicrosoft.com",
            "https://login.microsoftonline.com/consumers",
            ],
            header="Input authority (Note that MSA-PT apps would NOT use the /common authority)",
            accept_nonempty_string=True,
            ),
        )
    if _input_boolean("Enable MSAL Python's DEBUG log?"):
        logging.basicConfig(level=logging.DEBUG)
    while True:
        func = _select_options([
            acquire_token_silent,
            acquire_token_interactive,
            acquire_token_by_username_password,
            acquire_ssh_cert_silently,
            acquire_ssh_cert_interactive,
            remove_account,
            exit,
            ], option_renderer=lambda f: f.__doc__, header="MSAL Python APIs:")
        try:
            func(app)
        except KeyboardInterrupt:  # Useful for bailing out a stuck interactive flow
            print("Aborted")

if __name__ == "__main__":
    main()

