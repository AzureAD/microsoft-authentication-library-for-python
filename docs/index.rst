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


mTLS Proof-of-Possession (SN/I certificate)
-------------------------------------------

New in MSAL Python 1.38

MSAL Python supports two different kinds of Proof-of-Possession (PoP) tokens:

* **Signed HTTP Request (SHR) PoP** -- used by public-client apps through the
  broker, and configured with the ``auth_scheme`` parameter and
  :py:class:`msal.PopAuthScheme` above.
* **mutual-TLS (mTLS) PoP** -- used by *confidential-client* apps. The app's
  Subject Name + Issuer (SN/I) certificate is presented as the **client TLS
  certificate** in a mutual-TLS handshake to Microsoft Entra, and the returned
  access token is cryptographically bound to that certificate
  (``token_type == "mtls_pop"``, ``cnf``/``x5t#S256``).

To request an mTLS-PoP token, configure the confidential client with a
certificate credential and pass ``mtls_proof_of_possession=True`` to
:py:meth:`msal.ConfidentialClientApplication.acquire_token_for_client`::

    app = msal.ConfidentialClientApplication(
        client_id,
        authority="https://login.microsoftonline.com/<tenant-id>",  # MUST be tenanted
        client_credential={"private_key_pfx_path": "sni.pfx", "public_certificate": True},
        # azure_region="westus3",  # optional; omit for the global mtls endpoint
    )
    result = app.acquire_token_for_client(
        ["https://graph.microsoft.com/.default"],
        mtls_proof_of_possession=True,
    )
    # result["token_type"] == "mtls_pop"
    binding = result["binding_certificate"]
    # binding.x5c
    # binding.thumbprint_sha256

    with binding.create_requests_session() as session:
        response = session.get(
            resource_url,
            headers={
                "Authorization":
                    "{} {}".format(
                        result["token_type"],
                        result["access_token"],
                    ),
            },
            timeout=30,
        )

Notes and requirements:

* The ``authority`` must be **tenanted** (not ``/common`` or ``/organizations``).
* MSAL must own the TLS transport, so a custom ``http_client`` is not supported
  together with ``mtls_proof_of_possession=True``.
  Per-request ``post`` and ``cert`` overrides are also rejected.
  The owned mTLS token transport never follows redirects; HTTP 3xx responses
  raise ``requests.exceptions.HTTPError`` and are not cached as tokens.
* mTLS transport and ``binding.create_requests_session()`` require
  Requests >= 2.25.0 with a compatible urllib3 that supports ``SSLContext``.
  Missing transport capabilities raise ``ValueError`` with upgrade instructions.
  This requirement does not change support for ordinary Bearer authentication.
* If the provider returns a different token type, MSAL returns
  ``token_type_mismatch`` without caching the rejected response.
* ``azure_region`` is **optional**: omission honors ``MSAL_FORCE_REGION`` and
  otherwise uses the global mTLS endpoint (``mtlsauth.microsoft.com``).
  Set ``azure_region=False`` to guarantee global routing, or provide a region
  (e.g. ``westus3``) to pin one.
* The existing SN/I + Bearer (client-assertion) flow is unchanged; the same
  certificate can be used either as an assertion signer (Bearer) or as the TLS
  client certificate (mtls_pop).
* mTLS PoP currently targets the public and Azure Government (Arlington) clouds.
* Only the mTLS-PoP path drops the signed ``client_assertion``: the certificate
  authenticates the client through the mutual-TLS handshake instead. The classic
  (non-mTLS) certificate path is unchanged and still sends a signed
  ``client_assertion`` (``private_key_jwt``) to obtain an ordinary token.
* ``result["binding_certificate"]`` is a process-local
  :py:class:`msal.MtlsBindingContext`. It exposes the public certificate chain
  and SHA-256 thumbprint and can create an ``ssl.SSLContext`` or
  ``requests.Session`` that presents the same certificate to the resource. It
  is not serializable and must not be persisted.
  The TLS helpers accept custom CA file/directory paths as strings or
  ``os.PathLike`` objects (including ``pathlib.Path``); these replace the default
  trust roots. ``True`` or ``None`` selects the Requests CA bundle, while
  ``False`` disables server verification.
* For a Federated Identity Credential (FIC) exchange, configure the leg-2 client
  with both a ``client_assertion`` callable (acquiring the leg-1 token) and an
  ``mtls_binding_certificate`` sub-key; the leg-1 assertion is then sent as a
  ``jwt-pop`` client assertion over mTLS using the same certificate as leg 1.
  A fixed assertion string expires and emits a deprecation warning; the callable
  lets the leg-1 application reuse cached tokens and acquire fresh ones as needed.
  The target application must have a federated identity credential trusting the
  leg-1 token's issuer, subject, and audience. See
  ``fic_two_leg_over_mtls_pop()`` in
  ``sample/confidential_client_mtls_pop_sample.py`` for the two-leg setup.
  A final ``mtls_pop`` result includes an ``MtlsBindingContext`` for the
  downstream resource connection. If leg 2 does not request PoP, its bound
  assertion still travels as ``jwt-pop`` over mTLS, but the final Bearer token
  is not certificate-bound and does not include ``binding_certificate``.
* To request Bearer-over-mTLS instead, set
  ``send_certificate_over_mtls=True`` in the certificate credential and omit
  ``mtls_proof_of_possession``. The resulting token uses the ``Bearer`` scheme,
  is not certificate-bound, and does not include ``binding_certificate``.

mTLS-bound access tokens are persisted in a separate ``AccessToken_mTLS`` cache
bucket. Older SDK versions ignore that bucket and retain their normal Bearer
and SSH behavior. Before rolling back, remove bound tokens using the newer
MSAL version or clear the entire persisted cache. An older version's
``remove_tokens_for_client()`` does not purge an unknown mTLS bucket.

.. autoclass:: msal.MtlsBindingContext
   :members:


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
