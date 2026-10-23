"""Helpers for mTLS Proof-of-Possession (PoP).

This module owns two concerns for the "SN/I certificate over mTLS PoP" feature:

1. The endpoint transform + sovereign guardrail: mapping a tenanted ``login.*``
   authority to its ``mtlsauth.*`` counterpart (global or regional), and
   rejecting clouds/hosts where mTLS PoP is not (yet) available.
2. The mTLS transport: a ``requests`` session whose HTTPS connections present a
   client certificate for the mutual-TLS handshake to the token endpoint.

The wire contract and host mapping mirror the shipped MSAL.NET
``RegionAndMtlsDiscoveryProvider`` so MSAL Python stays cross-SDK consistent.
"""
import logging
import os
import threading
try:
    from collections.abc import Mapping
except ImportError:  # Python 2
    from collections import Mapping
try:
    from urllib.parse import urlparse, urlunparse
except ImportError:  # Python 2
    from urlparse import urlparse, urlunparse


logger = logging.getLogger(__name__)

_MTLS_POP_DOC_LINK = "https://aka.ms/msal-net-pop"

# The global public mTLS host. The three public "login.*" hosts all normalize to
# this single global endpoint (ESTSR provides regional failover), matching
# MSAL.NET's PublicEnvForRegionalMtlsAuth.
_PUBLIC_MTLS_HOST = "mtlsauth.microsoft.com"

# Public-cloud login hosts that normalize to the single global mTLS host above.
_PUBLIC_CLOUD_LOGIN_HOSTS = frozenset([
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.windows.net",
    ])

# ─────────────────────────────────────────────────────────────────────────────
# SOVEREIGN GUARDRAIL - single override point for mTLS cloud availability.
#
# Only public-cloud aliases and Azure Government are supported. Add an explicit
# mapping here when another cloud becomes available.
# ─────────────────────────────────────────────────────────────────────────────
_MTLS_POP_UNSUPPORTED_HOSTS = {
    "login.usgovcloudapi.net":
        "login.usgovcloudapi.net is not supported for mTLS PoP, "
        "please use login.microsoftonline.us",
    "login.chinacloudapi.cn":
        "login.chinacloudapi.cn is not supported for mTLS PoP.",
    }


def mtls_pop_host(instance, region=None):
    """Return the ``mtlsauth.*`` host for a given ``login.*`` authority instance.

    :param str instance: The authority host, e.g. ``login.microsoftonline.com``.
    :param region: Optional region, e.g. ``westus3``. When provided, a regional
        mTLS host ``{region}.mtlsauth...`` is returned; otherwise the global one.
    :raises ValueError: When mTLS PoP is not supported for the given host.
    """
    instance = instance.lower()
    if instance in _MTLS_POP_UNSUPPORTED_HOSTS:  # Sovereign guardrail
        raise ValueError(_MTLS_POP_UNSUPPORTED_HOSTS[instance])
    if instance in _PUBLIC_CLOUD_LOGIN_HOSTS:
        # Known public aliases (including legacy login.windows.net)
        # all normalize to the single global mTLS host.
        base = _PUBLIC_MTLS_HOST
    elif instance == "login.microsoftonline.us":
        base = "mtlsauth.microsoftonline.us"
    else:
        raise ValueError(
            "mTLS PoP is only supported for public-cloud and Azure Government "
            "authority hosts. The provided authority host ({}) is not supported. "
            "See {} for details.".format(instance, _MTLS_POP_DOC_LINK))
    return "{}.{}".format(region, base) if region else base


def transform_token_endpoint(token_endpoint, instance, region=None):
    """Return ``token_endpoint`` with its host swapped for the mTLS host.

    The path/tenant and everything else are preserved; only the network host is
    rewritten (e.g. ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token``
    -> ``https://mtlsauth.microsoft.com/{tenant}/oauth2/v2.0/token``).
    """
    parsed = urlparse(token_endpoint)
    host = mtls_pop_host(instance, region)
    # Preserve a non-default port if the original endpoint carried one (tests).
    netloc = "{}:{}".format(host, parsed.port) if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


class MtlsBindingContext(Mapping):
    """The certificate binding and TLS helpers for an mTLS-bound access token.

    Instances are created by MSAL and returned as ``binding_certificate`` in a
    successful token result. The certificate metadata remains available through
    mapping access for compatibility, while the private key is exposed only
    through TLS contexts and sessions created by this object.

    This object is process-local and must not be serialized or persisted.
    """

    __slots__ = (
        "__cert_pem", "__key_pem", "__x5c", "__thumbprint_sha256")
    _PUBLIC_KEYS = ("x5c", "thumbprint_sha256")

    def __init__(self, cert_pem, key_pem, x5c, thumbprint_sha256):
        self.__cert_pem = cert_pem
        self.__key_pem = key_pem
        self.__x5c = tuple(x5c)
        self.__thumbprint_sha256 = thumbprint_sha256

    @property
    def x5c(self):
        """The public certificate chain as base64-encoded DER certificates."""
        return list(self.__x5c)

    @property
    def thumbprint_sha256(self):
        """The base64url-encoded SHA-256 thumbprint (``x5t#S256``)."""
        return self.__thumbprint_sha256

    def create_ssl_context(self, verify=True):
        """Create an ``ssl.SSLContext`` that presents the binding certificate.

        ``verify`` follows ``requests`` semantics: ``True`` uses its default CA
        bundle, a path selects a custom CA file/directory, and ``False`` disables
        server verification.
        """
        return _create_ssl_context(self.__cert_pem, self.__key_pem, verify)

    def create_requests_session(self, verify=True, proxies=None):
        """Create a ``requests.Session`` that presents the binding certificate.

        The caller owns the returned session and should close it, preferably by
        using it as a context manager.
        """
        import requests
        _require_mtls_dependencies()
        effective_verify = _normalize_verify(verify)
        session = requests.Session()
        session.verify = effective_verify
        if proxies:
            session.proxies.update(proxies)
        session.mount(
            "https://",
            _make_mtls_adapter(ssl_context_factory=self.create_ssl_context))
        return session

    def __getitem__(self, key):
        if key == "x5c":
            return self.x5c
        if key == "thumbprint_sha256":
            return self.thumbprint_sha256
        raise KeyError(key)

    def __iter__(self):
        return iter(self._PUBLIC_KEYS)

    def __len__(self):
        return len(self._PUBLIC_KEYS)

    def __repr__(self):
        return (
            "MtlsBindingContext("
            "thumbprint_sha256={!r}, certificate_chain_length={})".format(
                self.thumbprint_sha256, len(self.__x5c)))

    def __reduce__(self):
        raise TypeError(
            "MtlsBindingContext is process-local and cannot be serialized")


class _MtlsHttpClient(object):
    """A minimal http client (``post``/``get``/``close``) whose HTTPS
    connections present a client certificate for mutual-TLS.

    MSAL owns this transport. A caller's plain custom ``http_client`` cannot
    perform the mTLS handshake, which is why requesting mTLS PoP with a
    non-mTLS-capable custom transport fails fast (see application.py).

    The ``ssl.SSLContext`` (and its temp key file) is built lazily on first use,
    so Bearer-only certificate apps never pay the cost nor touch the disk.
    """
    def __init__(self, cert_pem, key_pem, *,
                 verify=True, proxies=None, timeout=None):
        _require_mtls_dependencies()
        # cert_pem / key_pem are PEM-encoded bytes. key_pem must be an
        # unencrypted private key (the caller normalizes it).
        self._cert_pem = cert_pem
        self._key_pem = key_pem
        # Normalize None to True. Our custom ssl_context fully owns server
        # verification, but requests treats session.verify=None as "no verify"
        # and would force CERT_NONE onto a verifying context (ValueError). None
        # means "default", which for a security library is verify (via certifi).
        self._verify = _normalize_verify(verify)
        self._proxies = proxies
        self._timeout = timeout
        self._session = None
        self._sessions = {}
        self._session_lock = threading.Lock()

    def _ensure_session(self, url="", verify=None):
        import requests
        with self._session_lock:
            session = self._session or requests.Session()
            session.verify = self._verify
            if self._proxies:
                session.proxies = self._proxies
            effective_verify = _normalize_verify(session.merge_environment_settings(
                url, {}, None, verify, None)["verify"])
            if effective_verify not in self._sessions:
                if self._sessions:
                    session = requests.Session()
                    session.verify = self._verify
                    if self._proxies:
                        session.proxies = self._proxies
                adapter = _make_mtls_adapter(_create_ssl_context(
                    self._cert_pem, self._key_pem, effective_verify))
                session.mount("https://", adapter)
                self._sessions[effective_verify] = session
                self._session = self._session or session
            return self._sessions[effective_verify]

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def _request(self, method, url, **kwargs):
        from requests.exceptions import HTTPError

        if "cert" in kwargs:
            raise ValueError(
                "MSAL's mTLS transport does not support a cert override. "
                "Use the configured client certificate.")
        if self._timeout is not None:
            kwargs.setdefault("timeout", self._timeout)
        # The certificate and token request must stay at the selected authority.
        kwargs["allow_redirects"] = False
        response = self._ensure_session(url, kwargs.get("verify")).request(
            method, url, **kwargs)
        if 300 <= response.status_code < 400:
            response.close()
            raise HTTPError(
                "mTLS token transport does not support HTTP redirects "
                "(status {}).".format(response.status_code),
                response=response)
        return response

    def close(self):
        with self._session_lock:
            for session in self._sessions.values():
                session.close()


def _make_mtls_adapter(ssl_context=None, ssl_context_factory=None):
    """Return a ``requests`` HTTPAdapter that injects ``ssl_context`` (with the
    client certificate loaded) into every connection pool it creates."""
    from requests.adapters import HTTPAdapter  # Lazy import

    class _MtlsHTTPAdapter(HTTPAdapter):
        def __init__(self, ssl_context):
            self._ssl_context = ssl_context
            self._adapters = {}
            self._adapter_lock = threading.Lock()
            super(_MtlsHTTPAdapter, self).__init__(max_retries=1)

        def send(self, request, **kwargs):
            verify = _normalize_verify(kwargs.get("verify", True))
            kwargs["verify"] = verify
            if ssl_context_factory is None:
                return super(_MtlsHTTPAdapter, self).send(request, **kwargs)
            with self._adapter_lock:
                if verify not in self._adapters:
                    self._adapters[verify] = _make_mtls_adapter(
                        ssl_context_factory(verify))
                adapter = self._adapters[verify]
            return adapter.send(request, **kwargs)

        def close(self):
            for adapter in self._adapters.values():
                adapter.close()
            super(_MtlsHTTPAdapter, self).close()

        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = self._ssl_context
            return super(_MtlsHTTPAdapter, self).init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args, **kwargs):
            kwargs["ssl_context"] = self._ssl_context
            return super(_MtlsHTTPAdapter, self).proxy_manager_for(*args, **kwargs)

        def get_connection_with_tls_context(
                self, request, verify, proxies=None, cert=None):
            # Requests 2.32.0-2.32.2 otherwise replace our certificate-bearing
            # context with their preloaded, certificate-less context.
            from requests.utils import select_proxy, prepend_scheme_if_needed
            from requests.exceptions import InvalidProxyURL
            from urllib3.util import parse_url

            proxy = select_proxy(request.url, proxies)
            manager = self.poolmanager
            if proxy:
                proxy = prepend_scheme_if_needed(proxy, "http")
                if not parse_url(proxy).host:
                    raise InvalidProxyURL(
                        "Proxy URL is malformed or missing a host.")
                manager = self.proxy_manager_for(proxy)
            return manager.connection_from_url(
                request.url, pool_kwargs={"ssl_context": self._ssl_context})

        _get_connection = get_connection_with_tls_context  # Requests 2.32.0/1

    return _MtlsHTTPAdapter(ssl_context)


def _require_mtls_dependencies():
    """Reject unsupported mTLS transports without restricting Bearer users."""
    import inspect
    import re
    import requests

    message = (
        "mTLS requires Requests >= 2.25.0 and a compatible urllib3 with "
        "SSLContext support and Session.merge_environment_settings. "
        "Upgrade with: python -m pip install --upgrade 'requests>=2.25.0,<3'")
    version = re.match(r"^(\d+)\.(\d+)", requests.__version__)
    if (not version or tuple(map(int, version.groups())) < (2, 25)
            or not callable(getattr(
                requests.Session, "merge_environment_settings", None))):
        raise ValueError(message)
    try:
        from urllib3.connection import HTTPSConnection
        from requests.adapters import PoolManager
        supported = (
            "ssl_context" in inspect.signature(HTTPSConnection).parameters
            and "pool_kwargs" in inspect.signature(
                PoolManager.connection_from_url).parameters)
    except (ImportError, AttributeError, TypeError, ValueError):
        supported = False
    if not supported:
        raise ValueError(message)


def _normalize_verify(verify):
    if verify is None:
        return True
    if isinstance(verify, (str, bytes, os.PathLike)):
        return os.fsdecode(os.fspath(verify))
    return verify


def _new_verifying_context(verify):
    """Build an ssl context whose server verification matches ``verify`` (the
    same semantics as ``requests``). A custom ssl_context otherwise bypasses
    ``requests``' own ``verify`` handling, so we honor it here: ``True`` uses
    certifi (as the rest of MSAL does), a filesystem path uses that CA file/dir,
    and ``False`` disables verification (instead of the cryptic ValueError that
    ``requests`` would raise when it tries to set CERT_NONE on this context).
    """
    import ssl
    verify = _normalize_verify(verify)
    if verify is False:
        context = ssl.create_default_context()
        context.check_hostname = False  # Must be cleared before CERT_NONE
        context.verify_mode = ssl.CERT_NONE
        return context
    if isinstance(verify, str):
        return (ssl.create_default_context(capath=verify)
                if os.path.isdir(verify)
                else ssl.create_default_context(cafile=verify))
    try:  # Default: match requests' trust store (certifi), not the system one
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()


def _create_ssl_context(cert_pem, key_pem, verify=True):
    """Build a client ``ssl.SSLContext`` that presents ``cert_pem``/``key_pem``.

    ``ssl.SSLContext.load_cert_chain`` requires a file path, but our key is
    in memory. We write a ``0600`` temp PEM (mkstemp defaults to owner-only),
    load it, then unlink it immediately - the context keeps the material in
    memory, so nothing sensitive lingers on disk.
    """
    import os
    import tempfile
    context = _new_verifying_context(verify)  # Verifies the server (ESTS)
    fd, path = tempfile.mkstemp(suffix=".pem")  # Owner-only (0600) by default
    try:
        # os.fdopen() takes ownership of fd and guarantees it is closed even if
        # writing raises; file.write() writes every byte (os.write() may perform
        # a short write). Closing the fd also lets Windows unlink the file below.
        with os.fdopen(fd, "wb") as f:
            f.write(key_pem + b"\n" + cert_pem)
        context.load_cert_chain(path)  # Loads our client cert+key into memory
    finally:
        try:
            os.remove(path)  # Unlink immediately; minimal disk exposure
        except OSError:  # pragma: no cover
            logger.warning("Unable to remove temporary mTLS key file")
    return context
