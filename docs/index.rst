.. MSAL Python documentation master file, created by
   sphinx-quickstart on Tue Dec 18 10:53:22 2018.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

.. This file is also inspired by
   https://pythonhosted.org/an_example_pypi_project/sphinx.html#full-code-example

Welcome to MSAL Python's documentation!
=======================================

.. toctree::
   :maxdepth: 2
   :caption: Contents:

You can find high level conceptual documentations in the project
`README <https://github.com/AzureAD/microsoft-authentication-library-for-python>`_
and
`workable samples inside the project code base
<https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample>`_
.

The documentation hosted here is for API Reference.


PublicClientApplication and ConfidentialClientApplication
=========================================================

MSAL proposes a clean separation between
`public client applications and confidential client applications
<https://tools.ietf.org/html/rfc6749#section-2.1>`_.

They are implemented as two separated classes,
with different methods for different authentication scenarios.

PublicClientApplication
-----------------------
.. autoclass:: msal.PublicClientApplication
   :members:

ConfidentialClientApplication
-----------------------------
.. autoclass:: msal.ConfidentialClientApplication
   :members:


Shared Methods
--------------
Both PublicClientApplication and ConfidentialClientApplication
have following methods inherited from their base class.
You typically do not need to initiate this base class, though.

.. autoclass:: msal.ClientApplication
   :members:

   .. automethod:: __init__


TokenCache
==========

One of the parameter accepted by
both `PublicClientApplication` and `ConfidentialClientApplication`
is the `TokenCache`.

.. autoclass:: msal.TokenCache
   :members:

You can subclass it to add new behavior, such as, token serialization.
See `SerializableTokenCache` for example.

.. autoclass:: msal.SerializableTokenCache
   :members:


Indices and tables
==================

* :ref:`genindex`
* :ref:`search`

