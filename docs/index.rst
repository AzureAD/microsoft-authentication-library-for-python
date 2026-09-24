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

.. _service-fabric-http-options:

Service Fabric HTTP options
---------------------------

.. autoclass:: msal.ServiceFabricHttpOptions

Pass ``service_fabric_http_options={}`` to :class:`msal.ManagedIdentityClient`
to use MSAL's isolated, certificate-pinned Service Fabric transport.
The required ``http_client`` remains the transport for other managed identity
providers. On this new Service Fabric path, MSAL does not inspect, open, invoke,
mutate, or close that client. It need not be a Requests session or be opened::

    import msal

    class UnopenedConsumerClient:
        def get(self, url, **kwargs):
            raise RuntimeError("Open the consumer transport before non-Service-Fabric use")

    client = msal.ManagedIdentityClient(
        msal.SystemAssignedManagedIdentity(),
        http_client=UnopenedConsumerClient(),
        service_fabric_http_options={},
    )
    # In a Service Fabric environment:
    result = client.acquire_token_for_client(resource="https://management.azure.com/")

All five keys are optional. Unknown keys and explicit ``None`` field values
are invalid; omission is different from ``None``.

.. list-table::
   :header-rows: 1
   :widths: 15 20 65

   * - Key
     - Default
     - Accepted values and behavior
   * - ``headers``
     - ``{}``
     - A dict of string HTTP header names and values, overlaid on standard
       session headers case-insensitively (last supplied value wins).
       ``Secret`` and ``Host`` overrides, in any case, are forbidden. Names
       must be ASCII HTTP tokens; values must be transport-encodable, without
       CR/LF, invalid control characters, or leading whitespace. Empty values
       are allowed.
   * - ``proxies``
     - ``{}``
     - A dict of Requests-style selection keys (``http``, ``https``, ``all``,
       or scheme plus ``://hostname``) to HTTP/HTTPS proxy URLs. Host-specific
       keys cannot contain credentials, ports, paths, queries, or fragments.
       URLs require a transport-valid hostname (including IDNA validation)
       and valid explicit port, if supplied, and
       cannot contain queries, fragments, or non-root paths. Proxy credentials
       are allowed. SOCKS and forwarding without endpoint authentication are
       unsupported. HTTPS endpoints use CONNECT tunnels and retain pinning.
   * - ``trust_env``
     - ``False``
     - A bool. Only ``True`` opts into Requests environment-derived settings,
       including proxy selection, ``NO_PROXY``, and netrc authentication.
       For HTTPS proxy authentication, ``REQUESTS_CA_BUNDLE`` (or, if unset,
       ``CURL_CA_BUNDLE``) selects a CA bundle file or directory.
       Requests precedence rules apply; environment proxies can take
       precedence over session proxy settings. Neither environment settings
       nor explicit proxies can bypass the endpoint pin.
   * - ``timeout``
     - ``(5, 30)``
     - Positive finite int/float seconds, or a two-item tuple
       ``(connect, read)``. A scalar applies to both waits. Booleans, lists,
       zero, negative values, NaN, infinity, and disabled timeouts are invalid.
       Limits apply to each wait on every attempt, not total wall-clock time
       (DNS resolution and multiple addresses may add elapsed time).
   * - ``max_retries``
     - ``0``
     - Non-negative int, excluding bool. Counts additional attempts only for
       connection failures before request transmission: at most ``1 + N``
       attempts. No retry for TLS/certificate failures, reads, redirects,
       HTTP statuses, or ``Retry-After``; no backoff.

MSAL snapshots the dictionary and its supported nested dictionaries at
construction. Later mutations have no effect; create a new client to
reconfigure. Validation occurs only when Service Fabric needs network I/O:
not at construction, on a token-cache-only call, or in another environment.
Invalid explicit options raise :class:`msal.ManagedIdentityError` before session
allocation and cannot be hidden by proactive-refresh cached-token fallback.
Selected environment proxies are checked before Requests parses them for
transmission. Malformed or unsupported selected proxies raise
:class:`msal.ManagedIdentityError` without exposing proxy credentials; these
environment failures retain the existing eligible cached-token fallback.

MSAL owns and closes a separate session and response for each network
acquisition, including its retries. There is no new close API or persistent
pool. Every connection authenticates the actual endpoint certificate against
``IDENTITY_SERVER_THUMBPRINT`` before sending the environment-sourced
``Secret``. Self-signed certificates remain supported by this exact pin;
there is no caller TLS override. Endpoints must use HTTPS. Redirects are never
followed, even on the same origin; all HTTP 300-399 responses raise
:class:`msal.ManagedIdentityError` without exposing the redirect target.
Transport/TLS and existing endpoint/response errors retain their exception
behavior and existing eligible cached-token fallback.

HTTPS proxies are authenticated independently of the Service Fabric endpoint.
Before sending CONNECT or proxy credentials, MSAL validates the proxy's
certificate chain and hostname against Requests' default CA bundle.
Only ``trust_env=True`` enables the environment CA selection described above,
including for explicitly configured proxies. This trust never replaces the
inner endpoint pin or requires its self-signed certificate to be CA-trusted.
There is no option to disable proxy verification. HTTP CONNECT proxies remain
supported; their proxy credentials travel over plain HTTP, so use them only
on trusted networks. Neither proxy type receives ``Secret`` outside the
endpoint's pinned TLS connection.

HTTPS proxy tunneling requires urllib3's pre-CONNECT TLS and TLS-in-TLS
capabilities (available in supported urllib3 1.26 and 2.x configurations).
Older stacks such as urllib3 1.25 cannot provide this authenticated route;
MSAL raises :class:`msal.ManagedIdentityError` before connection or credential
transmission, rather than treating an HTTPS proxy as HTTP. This does not raise
the global dependency minimum or disable direct/HTTP-proxy acquisition.
Capability failures retain eligible cached-token fallback. Proxy TLS
validation failures are not retried, just like endpoint pin failures.

Compatibility and migration
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Omitting ``service_fabric_http_options`` or passing top-level ``None`` preserves
the legacy Service Fabric contract: a Requests session with an HTTPAdapter
or subclass is required, and its settings are inherited by MSAL's pinned
transport (not its custom sending behavior). ``None`` does **not** mean ``{}``.
Other providers, token protocol, token cache, and HTTP cache behavior are
unchanged.

Consumers of the new keyword or public type must require the first MSAL release
that provides it. Older MSAL versions reject the keyword/type with
``TypeError``/``ImportError``. Do not catch these and silently fall back.
Rollback by removing the argument is valid only when the consumer can satisfy
the legacy Requests-session contract. Downgrades must update consumer code
and dependency requirements together; an unopened arbitrary transport is not
a legacy Service Fabric fallback.
