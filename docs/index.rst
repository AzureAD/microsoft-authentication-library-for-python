MSAL Python documentation
=========================

.. toctree::
   :maxdepth: 2
   :caption: Contents:
   :hidden:

   MSAL Documentation <https://docs.microsoft.com/en-au/azure/active-directory/develop/msal-authentication-flows>
   GitHub Repository <https://github.com/AzureAD/microsoft-authentication-library-for-python>

You can find high level conceptual documentations in the project
`README <https://github.com/AzureAD/microsoft-authentication-library-for-python>`_
and
`workable samples inside the project code base
<https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample>`_
.

The documentation hosted here is for API Reference.

API
===

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

ConfidentialClientApplication
-----------------------------

.. autoclass:: msal.ConfidentialClientApplication
   :members:
   :inherited-members:

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
