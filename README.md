# Microsoft Authentication Library (MSAL) for Python Preview

The MSAL library for Python enables your app to access the
[Microsoft Cloud](https://cloud.microsoft.com)
by supporting authentication of users with
[Microsoft Azure Active Directory accounts](https://azure.microsoft.com/en-us/services/active-directory/)
and [Microsoft Accounts](https://account.microsoft.com) using industry standard OAuth2 and OpenID Connect.
Soon MSAL Python will also support [Azure AD B2C](https://azure.microsoft.com/services/active-directory-b2c/).

More and more detail about MSAL Python functionality and usage will be documented in the
[Wiki](https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki).

## Important Note about the MSAL Preview

This library is suitable for use in a production environment.
We provide the same production level support for this library as we do our current production libraries.
During the preview we may make changes to the API, internal cache format, and other mechanisms of this library,
which you will be required to take along with bug fixes or feature improvements.
This may impact your application.
For instance, a change to the cache format may impact your users, such as requiring them to sign in again.
An API change may require you to update your code.
When we provide the General Availability release
we will require you to update to the General Availability version within six months,
as applications written using a preview version of library may no longer work.

## Installation

1. If you haven't already, [install and/or upgrade the pip](https://pip.pypa.io/en/stable/installing/)
   of your Python environment to a recent version. We tested with pip 18.1.
2. As usual, just run `pip install msal`.

## Usage

Before using MSAL Python (or any MSAL SDKs, for that matter), you will have to
[register your application with the AAD 2.0 endpoint](https://docs.microsoft.com/en-us/azure/active-directory/develop/quickstart-v2-register-an-app).

Acquiring tokens with MSAL Python need to follow this 3-step pattern.

1. MSAL proposes a clean separation between
   [public client applications, and confidential client applications](https://tools.ietf.org/html/rfc6749#section-2.1).
   So you will first create either a `PublicClientApplication` or a `ConfidentialClientApplication` instance,
   and ideally reuse it during the lifecycle of your app. The following example shows a `PublicClientApplication`:

   ```python
   from msal import PublicClientApplication
   app = PublicClientApplication("your_client_id", authority="...")
   ```

   Later, each time you would want an access token, you start by:
   ```python
   result = None  # It is just an initial value. Please follow instructions below.
   ```

2. The API model in MSAL provides you explicit control on how to utilize token cache.
   This cache part is technically optional, but we highly recommend you to harness the power of MSAL cache.
   It will automatically handle the token refresh for you.

   ```python
   # We now check the cache to see
   # whether we already have some accounts that the end user already used to sign in before.
   accounts = app.get_accounts()
   if accounts:
       # If so, you could then somehow display these accounts and let end user choose
       print("Pick the account you want to use to proceed:")
       for a in accounts:
           print(a["username"])
       # Assuming the end user chose this one
       chosen = accounts[0]
       # Now let's try to find a token in cache for this account
       result = app.acquire_token_silent(config["scope"], account=chosen)
   ```

3. Either there is no suitable token in the cache, or you chose to skip the previous step,
   now it is time to actually send a request to AAD to obtain a token.
   There are different methods based on your client type and scenario. Here we demonstrate a placeholder flow.

   ```python
   if not result:
       # So no suitable token exists in cache. Let's get a new one from AAD.
       result = app.acquire_token_by_one_of_the_actual_method(..., scopes=["user.read"])
   if "access_token" in result:
       print(result["access_token"])  # Yay!
   else:
       print(result.get("error"))
       print(result.get("error_description"))
       print(result.get("correlation_id"))  # You may need this when reporting a bug
   ```

That is it. There will be some variations for different flows.


## Samples and Documentation

The generic documents on
[Auth Scenarios](https://docs.microsoft.com/en-us/azure/active-directory/develop/authentication-scenarios)
and
[Auth protocols](https://docs.microsoft.com/en-us/azure/active-directory/develop/active-directory-v2-protocols)
are recommended reading.

The API reference of MSAL Python is coming soon.

You can try [runnable samples in this repo](https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample).


## Versions

This library follows [Semantic Versioning](http://semver.org/).

You can find the changes for each version under
[Releases](https://github.com/AzureAD/microsoft-authentication-library-for-python/releases).

## Community Help and Support

We leverage Stack Overflow to work with the community on supporting Azure Active Directory and its SDKs, including this one!
We highly recommend you ask your questions on Stack Overflow (we're all on there!)
Also browser existing issues to see if someone has had your question before.

We recommend you use the "msal" tag so we can see it!
Here is the latest Q&A on Stack Overflow for MSAL:
[http://stackoverflow.com/questions/tagged/msal](http://stackoverflow.com/questions/tagged/msal)

## Security Reporting

If you find a security issue with our libraries or services please report it to [secure@microsoft.com](mailto:secure@microsoft.com) with as much detail as possible. Your submission may be eligible for a bounty through the [Microsoft Bounty](http://aka.ms/bugbounty) program. Please do not post security issues to GitHub Issues or any other public site. We will contact you shortly upon receiving the information. We encourage you to get notifications of when security incidents occur by visiting [this page](https://technet.microsoft.com/en-us/security/dd252948) and subscribing to Security Advisory Alerts.

## Contributing

All code is licensed under the MIT license and we triage actively on GitHub. We enthusiastically welcome contributions and feedback. Please read the [contributing guide](./contributing.md) before starting.

## We Value and Adhere to the Microsoft Open Source Code of Conduct

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/). For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.
