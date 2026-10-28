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
* The default HTTP transport handles mTLS automatically. A custom
  ``http_client`` must explicitly implement the certificate-aware factory
  described below; legacy custom clients fail with ``ValueError`` for mTLS.
  Per-request ``post`` and ``cert`` overrides and redirects are rejected for
  mTLS token requests, including when a token could otherwise come from cache.
  The built-in mTLS token transport never follows redirects; HTTP 3xx responses
  raise ``requests.exceptions.HTTPError`` and are not cached as tokens.
* MSAL's built-in mTLS transport and ``binding.create_requests_session()`` require
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

Custom HTTP pipelines for mTLS token requests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An application that does not supply ``http_client`` needs no new configuration.
MSAL owns its certificate-bearing Requests transport, separate from discovery
and ordinary token traffic.

A custom HTTP client opts in by implementing this additional method::

    def create_mtls_client(self, binding_context):
        # binding_context is an MSAL-created msal.MtlsBindingContext.
        # Return a NEW pipeline with the same middleware and network policy,
        # but isolated certificate-bearing connections.
        ...

There is no marker class to inherit and no additional constructor parameter.
The ordinary client's ``get`` and ``post`` still handle discovery and non-mTLS
requests. On the first mTLS token network request, MSAL calls the factory with
the application's selected certificate. The returned object must have a
callable ``post(url, **kwargs)`` using the normal MSAL HTTP response contract
(``status_code``, ``text``, optional ``headers``, and ``raise_for_status()``).
It must accept and honor ``allow_redirects=False``. Factory and request
exceptions propagate; MSAL never falls back to the ordinary client.

The same certificate-scoped pipeline handles mTLS PoP, Bearer-over-mTLS, and
FIC leg-2 requests. A FIC assertion remains ``jwt-pop`` even when the final
token is ``Bearer``; only a final ``mtls_pop`` token includes
``binding_certificate`` in its result.

For Requests-based pipelines, mount
``binding_context.create_requests_adapter(...)`` on a **new** session. This
helper accepts ``HTTPAdapter`` options for retry and pooling policy, retains
MSAL's Requests-version compatibility handling, and selects separate
certificate-bearing TLS contexts and pools using the effective Requests
``verify`` value. Session/request CA paths, ``REQUESTS_CA_BUNDLE`` and
``CURL_CA_BUNDLE`` (when ``session.trust_env`` allows them), and HTTPS proxies
retain their Requests semantics. For other HTTP libraries,
``binding_context.create_ssl_context(verify=...)`` creates the certificate-bearing
``ssl.SSLContext``; the implementer must resolve its own library's effective
server verification and proxy settings before using it.

For example, inside a Requests-based factory::

    session = MyPolicySession()  # A fresh session retaining your middleware
    session.verify = self.ca_bundle
    session.trust_env = self.trust_env
    session.proxies.update(self.proxies)
    session.mount("https://", binding_context.create_requests_adapter(
        max_retries=self.retries,
        pool_connections=self.pool_connections,
        pool_maxsize=self.pool_maxsize,
        pool_block=True,
    ))
    # Set the pipeline's timeout, hooks and other policy as for ordinary traffic.
    # Keep session alive until all applications using this factory have finished.
    return session

The runnable ``sample/confidential_client_mtls_http_client_sample.py`` shows a
factory that retains response middleware, a header policy, CA/proxy/environment
settings, timeouts and pooling, and closes its derived pipelines explicitly.
It uses the same certificate and environment configuration as the default
mTLS-PoP sample.

Trust, isolation and lifetime contract
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* Return a fresh pipeline for every factory call. Do not mutate or return the
  ordinary client, its shared transport, or a previously returned pipeline.
  Never share certificate-bearing connection pools, adapters, mutable TLS
  contexts or TLS session caches with another certificate or ordinary traffic.
  Shared immutable policy is fine.
* Preserve all required middleware and networking policy explicitly: MSAL cannot
  clone an opaque client. Constructor ``verify``, ``proxies`` and ``timeout``
  options still do not override a custom HTTP client's settings.
* MSAL serializes factory creation within each application and caches one
  returned pipeline for that application's certificate. Global and regional
  token endpoints reuse it. Separate applications invoke the factory separately,
  even when they share an HTTP factory or caches. The factory must be safe to
  call concurrently from different applications, and its returned pipelines
  must support the application's concurrency requirements.
* Certificate credentials are fixed for the application's lifetime. To rotate,
  create a new application and pipeline, stop in-flight work using the old
  application, then release its pipelines. Do not mutate a supplied binding
  context or a TLS context already used by a connection pool.
* The custom factory owns and must retain/close every client it creates,
  including partial constructions when its own code raises. MSAL does not
  close caller-owned clients or the ordinary pipeline. Close them only after
  all applications and in-flight requests using them have finished.
* MSAL rejects a missing/non-callable factory, a return value without callable
  ``post``, returning the ordinary client itself, and returning a client already
  assigned to another live application. This identity check does not keep
  applications alive or require custom clients to support weak references.
  MSAL restricts its token calls to the configured HTTPS endpoint, passes
  ``allow_redirects=False``,
  and rejects redirect responses or observable redirect history/final-endpoint
  changes (excluding query parameters). These checks cannot undo a redirect
  already followed by a faulty custom implementation.
* This is a **trusted implementation contract**, not proof that arbitrary code
  uses the supplied certificate. MSAL cannot inspect nested transports, detect
  all shared pools (or all reuse after an application is collected), enforce an
  opaque client's verification/proxy policy, or prevent custom middleware from
  ignoring TLS or redirect instructions. The implementer is responsible for
  those behaviors; do not opt in unless the complete pipeline honors them.

The factory receives a process-local certificate capability, not a successful
token result. ``binding_certificate`` remains present only on a final
``mtls_pop`` result, never on an ordinary Bearer result. This extension does not
change downstream resource TLS or the requirements for mTLS PoP.


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
       are allowed; credentials used for Basic authentication must be
       Latin-1 encodable after URL decoding.
       SOCKS and forwarding without endpoint authentication are
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
``IDENTITY_SERVER_THUMBPRINT`` using urllib3's ``assert_fingerprint`` before
sending the environment-sourced ``Secret``. Direct and HTTP CONNECT routes
use urllib3's standard HTTPS connection and pool classes.
Self-signed certificates remain supported by this exact pin;
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
