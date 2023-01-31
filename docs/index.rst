MSAL Python documentation
=========================

.. toctree::
   :maxdepth: 2
   :caption: Contents:
   :hidden:

   MSAL Documentation <https://docs.microsoft.com/en-au/azure/active-directory/develop/msal-authentication-flows>
   GitHub Repository <https://github.com/AzureAD/microsoft-authentication-library-for-python>

You can find high level conceptual documentations in the project
`README <https://github.com/AzureAD/microsoft-authentication-library-for-python>`_.

Scenarios
=========

There are many `different application scenarios <https://docs.microsoft.com/en-us/azure/active-directory/develop/authentication-flows-app-scenarios>`_.
MSAL Python supports some of them.
**The following diagram serves as a map. Locate your application scenario on the map.**
**If the corresponding icon is clickable, it will bring you to an MSAL Python sample for that scenario.**

* Most authentication scenarios acquire tokens on behalf of signed-in users.

  .. raw:: html

    <!-- Original diagram came from https://docs.microsoft.com/en-us/azure/active-directory/develop/media/scenarios/scenarios-with-users.svg -->
    <!-- Don't know how to include images into Sphinx, so we host it from github repo instead -->
    <img src="https://raw.githubusercontent.com/AzureAD/microsoft-authentication-library-for-python/dev/docs/scenarios-with-users.svg"
        usemap="#public-map"><!-- Derived from http://www.image-map.net/ but we had to manually add unique map id -->
    <map name="public-map">
        <area target="_blank" coords="110,150,59,94" shape="rect"
            alt="Web app" title="Web app" href="https://docs.microsoft.com/en-us/azure/active-directory/develop/quickstart-v2-python-webapp">
        <area target="_blank" coords="58,281,108,338" shape="rect"
            alt="Web app" title="Web app" href="https://docs.microsoft.com/en-us/azure/active-directory/develop/quickstart-v2-python-webapp">
        <area target="_blank" coords="57,529,127,470" shape="rect"
            alt="Desktop App" title="Desktop App" href="https://github.com/AzureAD/microsoft-authentication-library-for-python/blob/dev/sample/interactive_sample.py">
            <!-- TODO: Upgrade this sample to use Interactive Flow: https://github.com/Azure-Samples/ms-identity-python-desktop/blob/master/1-Call-MsGraph-WithUsernamePassword/username_password_sample.py -->
        <area target="_blank" coords="56,637,122,566" shape="rect"
            alt="Browserless app" title="Browserless app" href="https://github.com/Azure-Samples/ms-identity-python-devicecodeflow">
    </map>

* There are also daemon apps. In these scenarios, applications acquire tokens on behalf of themselves with no user.

  .. raw:: html

    <!-- Original diagram came from https://docs.microsoft.com/en-us/azure/active-directory/develop/media/scenarios/daemon-app.svg -->
    <!-- Don't know how to include images into Sphinx, so we host it from github repo instead -->
    <img src="https://raw.githubusercontent.com/AzureAD/microsoft-authentication-library-for-python/dev/docs/daemon-app.svg"
        usemap="#confidential-map"><!-- Derived from http://www.image-map.net/ but we had to manually add unique map id -->
    <map name="confidential-map">
        <area target="_blank" coords="48,1,165,260" shape="rect"
            alt="Daemon App acquires token for themselves" title="Daemon App acquires token for themselves" href="https://github.com/Azure-Samples/ms-identity-python-daemon">
    </map>

* There are other less common samples, such for ADAL-to-MSAL migration,
  `available inside the project code base
  <https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample>`_.


API
===

The following section is the API Reference of MSAL Python.
The API Reference is like a dictionary. You **read this API section when and only when**:

* You already followed our sample(s) above and have your app up and running,
  but want to know more on how you could tweak the authentication experience
  by using other optional parameters (there are plenty of them!)
* You read the MSAL Python source code and found a helper function that is useful to you,
  then you would want to double check whether that helper is documented below.
  Only documented APIs are considered part of the MSAL Python public API,
  which are guaranteed to be backward-compatible in MSAL Python 1.x series.
  Undocumented internal helpers are subject to change anytime, without prior notice.

.. note::

    Only APIs and their parameters documented in this section are part of public API,
    with guaranteed backward compatibility for the entire 1.x series.

    Other modules in the source code are all considered as internal helpers,
    which could change at anytime in the future, without prior notice.

MSAL proposes a clean separation between
`public client applications and confidential client applications
<https://tools.ietf.org/html/rfc6749#section-2.1>`_.

They are implemented as two separated classes,
with different methods for different authentication scenarios.

PublicClientApplication
-----------------------

.. autoclass:: msal.PublicClientApplication
   :members:
   :inherited-members:

   .. automethod:: __init__

ConfidentialClientApplication
-----------------------------

.. autoclass:: msal.ConfidentialClientApplication
   :members:
   :inherited-members:

   .. automethod:: __init__

TokenCache
----------

One of the parameters accepted by
both `PublicClientApplication` and `ConfidentialClientApplication`
is the `TokenCache`.

.. autoclass:: msal.TokenCache
   :members:

You can subclass it to add new behavior, such as, token serialization.
See `SerializableTokenCache` for example.

.. autoclass:: msal.SerializableTokenCache
   :members:
