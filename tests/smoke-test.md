# How to Smoke Test MSAL Python

The experimental `python -m msal` usage is designed to be an interactive tool,
which can impersonate arbitrary apps and test most of the MSAL Python APIs.
Note that MSAL Python API's behavior is modeled after OIDC behavior in browser,
which are not exactly the same as the broker API's behavior,
despite that the two sets of API happen to have similar names.

Tokens acquired during the tests will be cached by MSAL Python.
MSAL Python uses an in-memory token cache by default.
This test tool, however, saves a token cache snapshot on disk upon each exit,
and you may choose to reuse it or start afresh during start up.

Typical test cases are listed below.

1. The tool starts with an empty token cache.
   In this state, acquire_token_silent() shall always return empty result.

2. When testing with broker, apps would need to register a certain redirect_uri
   for the test cases below to work.
   We will also test an app without the required redirect_uri registration,
   MSAL Python shall return a meaningful error message on what URIs to register.

3. Interactive acquire_token_interactive() shall get a token. In particular,

   * The prompt=none option shall succeed when there is a default account,
     or error out otherwise.
   * The prompt=select_account option shall always prompt with an account picker.
   * The prompt=absent option shall prompt an account picker UI
     if there are multiple accounts available in browser
     and none of them is considered a default account.
     In such a case, an optional login_hint=`one_of_the_account@contoso.com`
     shall bypass the account picker.

     With a broker, the behavior shall largely match the browser behavior,
     unless stated otherwise below.

     * Broker (PyMsalRuntime) on Mac does not support silent signin,
       so the prompt=absent will also always prompt.

4. ROPC (Resource Owner Password Credential, a.k.a. the username password flow).
   The acquire_token_by_username_password() is supported by broker on Windows.
   As of Oct 2023, it is not yet supported by broker on Mac,
   so it will fall back to non-broker behavior.

5. After step 3 or 4, the acquire_token_silently() shall return a token fast,
   because that is the same token returned by step 3 or 4, cached in MSAL Python.
   We shall also retest this with the force_refresh=True,
   a new token shall be obtained,
   typically slower than a token served from MSAL Python's token cache.

6. POP token.
   POP token is supported via broker.
   This tool test the POP token by using a hardcoded Signed Http Request (SHR).
   A test is successful if the POP test function return a token with type as POP.

7. SSH Cert.
   The interactive test and silent test shall behave similarly to
   their non ssh-cert counterparts, only the `token_type` would be different.

8. Test the remove_account() API. It shall always be successful.
   This effectively signs out an account from MSAL Python,
   we can confirm that by running acquire_token_silent()
   and see that account was gone.

   The remove_account() shall also sign out from broker (if broker was enabled),
   it does not sign out account from browser (even when browser was used).

