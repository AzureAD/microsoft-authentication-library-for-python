=========================
MSAL Python Documentation
=========================

.. toctree::
   :maxdepth: 2
   :caption: Contents:
   :hidden:

..
    Comment: Perhaps because of the theme, only the first level sections will show in TOC,
    regardless of maxdepth setting.
    UPDATE: And now (early 2024) suddenly a function-level, long TOC is generated,
    even though maxdepth is set to 2.

You can find high level conceptual documentations in the project
`README <https://github.com/AzureAD/microsoft-authentication-library-for-python>`_.

Scenarios
=========

There are many `different application scenarios <https://docs.microsoft.com/azure/active-directory/develop/authentication-flows-app-scenarios>`_.
MSAL Python supports some of them.
**The following diagram serves as a map. Locate your application scenario on the map.**
**If the corresponding icon is clickable, it will bring you to an MSAL Python sample for that scenario.**

* Most authentication scenarios acquire tokens representing the signed-in user.

  .. raw:: html

    <!-- Original diagram came from https://docs.microsoft.com/azure/active-directory/develop/media/scenarios/scenarios-with-users.svg -->
    <!-- Don't know how to include images into Sphinx, so we host it from github repo instead -->
    <img src="https://raw.githubusercontent.com/AzureAD/microsoft-authentication-library-for-python/dev/docs/scenarios-with-users.svg"
        usemap="#public-map"><!-- Derived from http://www.image-map.net/ but we had to manually add unique map id -->
    <map name="public-map">
        <area target="_blank" coords="110,150,59,94" shape="rect"
            alt="Web app" title="Web app" href="https://learn.microsoft.com/azure/active-directory/develop/web-app-quickstart?pivots=devlang-python">
        <area target="_blank" coords="58,281,108,338" shape="rect"
            alt="Web app" title="Web app" href="https://learn.microsoft.com/azure/active-directory/develop/web-app-quickstart?pivots=devlang-python">
        <area target="_blank" coords="57,529,127,470" shape="rect"
            alt="Desktop App" title="Desktop App" href="https://github.com/AzureAD/microsoft-authentication-library-for-python/blob/dev/sample/interactive_sample.py">
            <!-- TODO: Upgrade this sample to use Interactive Flow: https://github.com/Azure-Samples/ms-identity-python-desktop/blob/master/1-Call-MsGraph-WithUsernamePassword/username_password_sample.py -->
        <area target="_blank" coords="56,637,122,566" shape="rect"
            alt="Browserless app" title="Browserless app" href="https://github.com/Azure-Samples/ms-identity-python-devicecodeflow">
    </map>

* There are also daemon apps, who acquire tokens representing themselves, not a user.

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


API Reference
=============
.. note::

    Only the contents inside
    `this source file <https://github.com/AzureAD/microsoft-authentication-library-for-python/blob/dev/msal/__init__.py>`_
    and their documented methods (unless otherwise marked as deprecated)
    are MSAL Python public API,
    which are guaranteed to be backward-compatible until the next major version.

    Everything else, regardless of their naming, are all internal helpers,
    which could change at anytime in the future, without prior notice.

The following section is the API Reference of MSAL Python.
The API Reference is like a dictionary, which is useful when:

* You already followed our sample(s) above and have your app up and running,
  but want to know more on how you could tweak the authentication experience
  by using other optional parameters (there are plenty of them!)
* Some important features have their in-depth documentations in the API Reference.

MSAL proposes a clean separation between
`public client applications and confidential client applications
<https://tools.ietf.org/html/rfc6749#section-2.1>`_.

They are implemented as two separated classes,
with different methods for different authentication scenarios.

ClientApplication
-----------------

.. autoclass:: msal.ClientApplication
   :members:
   :inherited-members:

   .. automethod:: __init__

PublicClientApplication
-----------------------

.. autoclass:: msal.PublicClientApplication
   :members:

   .. autoattribute:: msal.PublicClientApplication.CONSOLE_WINDOW_HANDLE
   .. automethod:: __init__

ConfidentialClientApplication
-----------------------------

.. autoclass:: msal.ConfidentialClientApplication
   :members:


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

Prompt
------
.. autoclass:: msal.Prompt
   :members:

   .. autoattribute:: msal.Prompt.SELECT_ACCOUNT
   .. autoattribute:: msal.Prompt.NONE
   .. autoattribute:: msal.Prompt.CONSENT
   .. autoattribute:: msal.Prompt.LOGIN

PopAuthScheme
-------------

This is used as the `auth_scheme` parameter in many of the acquire token methods
to support for Proof of Possession (PoP) tokens.

New in MSAL Python 1.26

.. autoclass:: msal.PopAuthScheme
   :members:

   .. autoattribute:: msal.PopAuthScheme.HTTP_GET
   .. autoattribute:: msal.PopAuthScheme.HTTP_POST
   .. autoattribute:: msal.PopAuthScheme.HTTP_PUT
   .. autoattribute:: msal.PopAuthScheme.HTTP_DELETE
   .. autoattribute:: msal.PopAuthScheme.HTTP_PATCH
   .. automethod:: __init__


Exceptions
----------
These are exceptions that MSAL Python may raise.
You should not need to create them directly.
You may want to catch them to provide a better error message to your end users.

.. autoclass:: msal.IdTokenError


Managed Identity
================
MSAL supports
`Managed Identity <https://learn.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/overview>`_.

You can create one of these two kinds of managed identity configuration objects:

.. autoclass:: msal.SystemAssignedManagedIdentity
   :members:

.. autoclass:: msal.UserAssignedManagedIdentity
   :members:

And then feed the configuration object into a :class:`ManagedIdentityClient` object.

.. autoclass:: msal.ManagedIdentityClient
   :members:

   .. automethod:: __init__
