import json
import re
try:
    from urllib.parse import urlparse
except ImportError:  # Fall back to Python 2
    from urlparse import urlparse
import logging

logger = logging.getLogger(__name__)
# Endpoints were copied from here
# https://docs.microsoft.com/en-us/azure/active-directory/develop/authentication-national-cloud#azure-ad-authentication-endpoints
AZURE_US_GOVERNMENT = "login.microsoftonline.us"
DEPRECATED_AZURE_CHINA = "login.chinacloudapi.cn"
AZURE_PUBLIC = "login.microsoftonline.com"
AZURE_GOV_FR = "login.sovcloud-identity.fr"
AZURE_GOV_DE = "login.sovcloud-identity.de"
AZURE_GOV_SG = "login.sovcloud-identity.sg"

WORLD_WIDE = 'login.microsoftonline.com'  # There was an alias login.windows.net

# Sovereign-cloud sentinels. Aliases of the same cloud map to the same value
# so callers can compare clouds with simple equality.
_CLOUD_PUBLIC = "PUBLIC"
_CLOUD_CHINA = "CHINA"
_CLOUD_GERMANY = "GERMANY"
_CLOUD_US_GOV = "US_GOV"
_CLOUD_US_ALT = "US_ALT"
_CLOUD_PPE = "PPE"
_CLOUD_BLEU = "BLEU"
_CLOUD_DELOS = "DELOS"
_CLOUD_GOV_SG = "GOV_SG"

# Single source of truth for known Microsoft authority hosts. Add an alias
# here and WELL_KNOWN_AUTHORITY_HOSTS / _KNOWN_HOST_TO_CLOUD pick it up.
_HOSTS_BY_CLOUD = {
    _CLOUD_PUBLIC: (
        AZURE_PUBLIC,
        "login.microsoft.com",
        "login.windows.net",
        "sts.windows.net",
    ),
    _CLOUD_CHINA: (
        "login.partner.microsoftonline.cn",
        DEPRECATED_AZURE_CHINA,
    ),
    _CLOUD_GERMANY: ("login.microsoftonline.de",),  # deprecated
    _CLOUD_US_GOV: (
        AZURE_US_GOVERNMENT,
        "login.usgovcloudapi.net",
    ),
    _CLOUD_US_ALT: ("login-us.microsoftonline.com",),
    _CLOUD_BLEU: (AZURE_GOV_FR,),
    _CLOUD_DELOS: (AZURE_GOV_DE,),
    _CLOUD_GOV_SG: (AZURE_GOV_SG,),
}

# Hosts that resolve to a cloud for the cross-cloud check but MUST NOT enter
# WELL_KNOWN_AUTHORITY_HOSTS (which gates instance-discovery skipping).
# - PPE: non-production.
# - ciamlogin.com: bare suffix, never a usable authority on its own; tenant
#   subdomains resolve to Public via _resolve_known_cloud's regional logic.
_EXTRA_HOSTS_BY_CLOUD = {
    _CLOUD_PPE: (
        "login.windows-ppe.net",
        "sts.windows-ppe.net",
        "login.microsoft-ppe.com",
    ),
    _CLOUD_PUBLIC: ("ciamlogin.com",),
}

# Derived from _HOSTS_BY_CLOUD so a new alias cannot drift out of sync.
WELL_KNOWN_AUTHORITY_HOSTS = frozenset(
    host for hosts in _HOSTS_BY_CLOUD.values() for host in hosts)

_KNOWN_HOST_TO_CLOUD = {
    host: cloud
    for cloud, hosts in _HOSTS_BY_CLOUD.items()
    for host in hosts
}
_KNOWN_HOST_TO_CLOUD.update({
    host: cloud
    for cloud, hosts in _EXTRA_HOSTS_BY_CLOUD.items()
    for host in hosts
})

# Catch a duplicated host at import time rather than in production.
_all_listed_hosts = [
    h for hosts in _HOSTS_BY_CLOUD.values() for h in hosts
] + [
    h for hosts in _EXTRA_HOSTS_BY_CLOUD.values() for h in hosts
]
assert len(_all_listed_hosts) == len(_KNOWN_HOST_TO_CLOUD), (
    "Duplicate host in cloud tables: {}".format(
        sorted(h for h in set(_all_listed_hosts) if _all_listed_hosts.count(h) > 1)))
del _all_listed_hosts

WELL_KNOWN_B2C_HOSTS = [
    "b2clogin.com",
    "b2clogin.cn",
    "b2clogin.us",
    "b2clogin.de",
    "ciamlogin.com",
    ]
_CIAM_DOMAIN_SUFFIX = ".ciamlogin.com"

# RFC 1035 / RFC 1123 DNS label: 1-63 chars, no leading/trailing hyphen.
# Used as a shape gate on the region prefix; not an allow-list of regions.
_REGION_PREFIX_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _resolve_known_cloud(host):
    """Return the cloud sentinel for *host*, or None.

    Trust gate for the cross-cloud check (used by `_are_in_same_cloud`,
    `_ensure_endpoint_same_cloud_as_authority`, and Rule 2 of
    `has_valid_issuer`). Tightening here is safe; loosening here weakens
    all three call sites at once.

    Matches an alias in :data:`_KNOWN_HOST_TO_CLOUD` or a ``{region}.{alias}``
    sub-host where ``{region}`` matches :data:`_REGION_PREFIX_PATTERN`.
    Hosts are lowercased here, so callers may pass either the raw
    ``urlparse(...).hostname`` (already lowercased per RFC 3986) or any
    untrusted string.
    """
    if not host:
        return None
    host = host.lower()
    cloud = _KNOWN_HOST_TO_CLOUD.get(host)
    if cloud is not None:
        return cloud
    dot = host.find(".")
    if dot <= 0:
        return None
    prefix = host[:dot]
    base = host[dot + 1:]
    if _REGION_PREFIX_PATTERN.match(prefix) and base in _KNOWN_HOST_TO_CLOUD:
        return _KNOWN_HOST_TO_CLOUD[base]
    return None


def _are_in_same_cloud(host_a, host_b):
    """Default-deny: True iff both hosts resolve to the same known cloud."""
    cloud_a = _resolve_known_cloud(host_a)
    if cloud_a is None:
        return False
    cloud_b = _resolve_known_cloud(host_b)
    if cloud_b is None:
        return False
    return cloud_a == cloud_b


def _ensure_endpoint_same_cloud_as_authority(
        authority_url, endpoint_url, endpoint_name):
    """Reject an OIDC discovery endpoint that crosses sovereign clouds.

    No-op when *authority_url* is a custom domain (custom OIDC IdPs are
    unconstrained) or when *endpoint_url* is empty / not absolute. Raises
    :class:`ValueError` naming the authority, endpoint kind, and offending
    URL; no tokens or secrets are surfaced.
    """
    if not endpoint_url:
        return
    endpoint_parsed = urlparse(endpoint_url)
    if not endpoint_parsed.scheme or not endpoint_parsed.hostname:
        return  # Let downstream parsing surface a non-absolute URL
    authority_host = urlparse(authority_url).hostname if authority_url else None
    authority_cloud = _resolve_known_cloud(authority_host)
    if authority_cloud is None:
        return
    endpoint_cloud = _resolve_known_cloud(endpoint_parsed.hostname)
    if endpoint_cloud is None or endpoint_cloud != authority_cloud:
        raise ValueError(
            "OIDC discovery for authority '{authority}' returned a "
            "{name} '{endpoint}' whose host is not in the same Microsoft "
            "sovereign cloud as the authority. MSAL refused to use that "
            "endpoint. Verify the OIDC discovery endpoint is not being "
            "intercepted and that the configured authority points at the "
            "correct sovereign cloud.".format(
                authority=authority_url,
                name=endpoint_name,
                endpoint=endpoint_url,
            ))


def _get_instance_discovery_host(instance):
    return instance if instance in WELL_KNOWN_AUTHORITY_HOSTS else WORLD_WIDE


def _get_instance_discovery_endpoint(instance):
    return 'https://{}/common/discovery/instance'.format(
        _get_instance_discovery_host(instance))


class AuthorityBuilder(object):
    def __init__(self, instance, tenant):
        """A helper to save caller from doing string concatenation.

        Usage is documented in :func:`application.ClientApplication.__init__`.
        """
        self._instance = instance.rstrip("/")
        self._tenant = tenant.strip("/")

    def __str__(self):
        return "https://{}/{}".format(self._instance, self._tenant)


class Authority(object):
    """This class represents an (already-validated) authority.

    Once constructed, it contains members named "*_endpoint" for this instance.
    TODO: It will also cache the previously-validated authority instances.
    """
    _domains_without_user_realm_discovery = set([])

    def __init__(
            self, authority_url, http_client,
            validate_authority=True,
            instance_discovery=None,
            oidc_authority_url=None,
            ):
        """Creates an authority instance, and also validates it.

        :param validate_authority:
            The Authority validation process actually checks two parts:
            instance (a.k.a. host) and tenant. We always do a tenant discovery.
            This parameter only controls whether an instance discovery will be
            performed.
        """
        self._http_client = http_client
        self._oidc_authority_url = oidc_authority_url
        if oidc_authority_url:
            tenant_discovery_endpoint = self._initialize_oidc_authority(
                oidc_authority_url)
        else:
            tenant_discovery_endpoint = self._initialize_entra_authority(
                authority_url, validate_authority, instance_discovery)
        try:
            openid_config = tenant_discovery(
                tenant_discovery_endpoint,
                self._http_client)
        except ValueError:
            error_message = (
                "Unable to get OIDC authority configuration for {url} "
                "because its OIDC Discovery endpoint is unavailable at "
                "{url}/.well-known/openid-configuration ".format(url=oidc_authority_url)
                if oidc_authority_url else
                "Unable to get authority configuration for {}. "
                "Authority would typically be in a format of "
                "https://login.microsoftonline.com/your_tenant "
                "or https://tenant_name.ciamlogin.com "
                "or https://tenant_name.b2clogin.com/tenant.onmicrosoft.com/policy. "
                .format(authority_url)
                ) + " Also please double check your tenant name or GUID is correct."
            raise ValueError(error_message)
        self._issuer = openid_config.get('issuer')
        self.authorization_endpoint = openid_config['authorization_endpoint']
        self.token_endpoint = openid_config['token_endpoint']
        self.device_authorization_endpoint = openid_config.get('device_authorization_endpoint')
        _, _, self.tenant = canonicalize(self.token_endpoint)  # Usually a GUID

        # Validate the issuer and enforce same-cloud endpoints (OIDC only).
        # See #5927 for the cross-cloud hardening.
        if self._oidc_authority_url:
            if not self.has_valid_issuer():
                raise ValueError((
                    "The issuer '{iss}' does not match the authority '{auth}' or a known pattern. "
                    "When using the 'oidc_authority' parameter in ClientApplication, the authority "
                    "will be validated against the issuer from {auth}/.well-known/openid-configuration ."
                    "If using a known Entra authority (e.g. login.microsoftonline.com) the "
                    "'authority' parameter should be used instead of 'oidc_authority'. "
                    ""
                ).format(iss=self._issuer, auth=self._oidc_authority_url))
            _ensure_endpoint_same_cloud_as_authority(
                self._oidc_authority_url, self.token_endpoint, "token_endpoint")
            _ensure_endpoint_same_cloud_as_authority(
                self._oidc_authority_url, self.authorization_endpoint,
                "authorization_endpoint")
            if self.device_authorization_endpoint:
                _ensure_endpoint_same_cloud_as_authority(
                    self._oidc_authority_url,
                    self.device_authorization_endpoint,
                    "device_authorization_endpoint")

    def _initialize_oidc_authority(self, oidc_authority_url):
        authority, self.instance, tenant = canonicalize(oidc_authority_url)
        self.is_adfs = tenant.lower() == 'adfs'  # As a convention
        self._is_b2c = True  # Not exactly true, but
            # OIDC Authority was designed for CIAM which is the next gen of B2C.
            # Besides, application.py uses this to bypass broker.
        self._is_known_to_developer = True  # Not really relevant, but application.py uses this to bypass authority validation
        return oidc_authority_url + "/.well-known/openid-configuration"

    def _initialize_entra_authority(
            self, authority_url, validate_authority, instance_discovery):
        # :param instance_discovery:
        #    By default, the known-to-Microsoft validation will use an
        #    instance discovery endpoint located at ``login.microsoftonline.com``.
        #    You can customize the endpoint by providing a url as a string.
        #    Or you can turn this behavior off by passing in a False here.
        if isinstance(authority_url, AuthorityBuilder):
            authority_url = str(authority_url)
        authority, self.instance, tenant = canonicalize(authority_url)
        is_ciam = self.instance.endswith(_CIAM_DOMAIN_SUFFIX)
        self.is_adfs = tenant.lower() == 'adfs' and not is_ciam
        parts = authority.path.split('/')
        self._is_b2c = any(
            self.instance.endswith("." + d) for d in WELL_KNOWN_B2C_HOSTS
            ) or (len(parts) == 3 and parts[2].lower().startswith("b2c_"))
        self._is_known_to_developer = self.is_adfs or self._is_b2c or not validate_authority
        is_known_to_microsoft = self.instance in WELL_KNOWN_AUTHORITY_HOSTS
        instance_discovery_endpoint = _get_instance_discovery_endpoint(  # Note: This URL seemingly returns V1 endpoint only
            self.instance
            ) if instance_discovery in (None, True) else instance_discovery
        if instance_discovery_endpoint and not (
                is_known_to_microsoft or self._is_known_to_developer):
            payload = _instance_discovery(
                "https://{}{}/oauth2/v2.0/authorize".format(
                    self.instance, authority.path),
                self._http_client,
                instance_discovery_endpoint)
            if payload.get("error") == "invalid_instance":
                raise ValueError(
                    "invalid_instance: "
                    "The authority you provided, %s, is not known. "
                    "If it is a valid domain name known to you, "
                    "you can turn off this check by passing in "
                    "instance_discovery=False"
                    % authority_url)
            tenant_discovery_endpoint = payload['tenant_discovery_endpoint']
        else:
            tenant_discovery_endpoint = authority._replace(
                path="{prefix}{version}/.well-known/openid-configuration".format(
                    prefix=tenant if is_ciam and len(authority.path) <= 1  # Path-less CIAM
                        else authority.path,  # In B2C, it is "/tenant/policy"
                    version="" if self.is_adfs else "/v2.0",
                    )
                ).geturl()  # Keeping original port and query. Query is useful for test.
        return tenant_discovery_endpoint

    def user_realm_discovery(self, username, correlation_id=None, response=None):
        # It will typically return a dict containing "ver", "account_type",
        # "federation_protocol", "cloud_audience_urn",
        # "federation_metadata_url", "federation_active_auth_url", etc.
        if self.instance not in self.__class__._domains_without_user_realm_discovery:
            resp = response or self._http_client.get(
                "https://{netloc}/common/userrealm/{username}?api-version=1.0".format(
                    netloc=self.instance, username=username),
                headers={'Accept': 'application/json',
                         'client-request-id': correlation_id},)
            if resp.status_code != 404:
                resp.raise_for_status()
                return json.loads(resp.text)
            self.__class__._domains_without_user_realm_discovery.add(self.instance)
        return {}  # This can guide the caller to fall back normal ROPC flow

    def has_valid_issuer(self):
        """True if the OIDC issuer is valid for this authority.

        Steps below are evaluated in this order; the bracketed labels are
        the historical rule names retained for cross-reference with the
        MSAL.NET port (#5927). Order is security-sensitive.

          Step 1 [Case 1]: Exact match.
          Step 2 [Case 4]: Same scheme + netloc (paths may differ).
          Step 3 [Rule 3]: CIAM tenant pattern (cross-host only). Must run
             before Step 4 so a ``<x>.ciamlogin.com`` issuer cannot bypass
             tenant matching via Rule 2b (CIAM resolves to Public).
          Step 4 [Rule 2]: Same Microsoft cloud. 2a accepts any known-MS
             issuer under a custom-domain authority (#5927 federation);
             2b accepts a known-MS issuer under a known-MS authority only
             when the two clouds are identical.
          Step 5 [Case 3b]: Region-shaped prefix on the authority host.
          Step 6 [Case 5]: B2C subdomain (excluding ``.ciamlogin.com``,
             handled by Step 3).
        """
        if not self._issuer or not self._oidc_authority_url:
            return False

        # Step 1 [Case 1]: exact match (trailing slash insensitive)
        if self._issuer.rstrip("/") == self._oidc_authority_url.rstrip("/"):
            return True

        issuer_parsed = urlparse(self._issuer)
        authority_parsed = urlparse(self._oidc_authority_url)
        issuer_host = issuer_parsed.hostname.lower() if issuer_parsed.hostname else None
        authority_host = (
            authority_parsed.hostname.lower() if authority_parsed.hostname else "")

        if not issuer_host:
            return False

        # Step 2 [Case 4]: same scheme + host. Runs before Step 3 so a CIAM
        # authority/issuer pair on the same host (different paths) passes.
        if (authority_parsed.scheme == issuer_parsed.scheme and
            authority_parsed.netloc == issuer_parsed.netloc):
            return True

        # Step 3 [Rule 3]: cross-host CIAM issuer. Tenant must match
        # authority's first path segment (or first hostname label). Must run
        # before Step 4 to block the Rule 2b CIAM bypass.
        if issuer_host.endswith(_CIAM_DOMAIN_SUFFIX):
            issuer_tenant = issuer_host[:-len(_CIAM_DOMAIN_SUFFIX)]
            auth_path_parts = [p for p in authority_parsed.path.split("/") if p]
            if auth_path_parts:
                authority_tenant = auth_path_parts[0].lower()
            else:
                authority_tenant = authority_host.split(".", 1)[0]
            if issuer_tenant and issuer_tenant == authority_tenant:
                normalized_issuer_path = issuer_parsed.path.rstrip("/").lower()
                if normalized_issuer_path in (
                        "",
                        "/" + issuer_tenant,
                        "/" + issuer_tenant + "/v2.0"):
                    return True
            return False  # Tenant mismatch: reject.

        # Step 4 [Rule 2]: known Microsoft issuer over HTTPS.
        # 2a: custom-domain authority -> accept (#5927 federation).
        # 2b: known-MS authority -> accept only if same cloud.
        issuer_cloud = _resolve_known_cloud(issuer_host)
        if issuer_cloud is not None and issuer_parsed.scheme == "https":
            authority_cloud = _resolve_known_cloud(authority_host)
            if authority_cloud is None:
                return True  # 2a
            if authority_cloud == issuer_cloud:
                return True  # 2b
            # Cross-cloud: fall through to reject.

        # Step 5 [Case 3b]: region-shaped prefix on the authority host
        # (e.g. issuer=us.someweb.com, authority=someweb.com).
        dot_index = issuer_host.find(".")
        if dot_index > 0:
            prefix = issuer_host[:dot_index]
            potential_base = issuer_host[dot_index + 1:]
            if (_REGION_PREFIX_PATTERN.match(prefix)
                    and potential_base == authority_host):
                return True

        # Step 6 [Case 5]: B2C subdomain. .ciamlogin.com handled by Step 3.
        if any(
                issuer_host.endswith("." + h)
                for h in WELL_KNOWN_B2C_HOSTS
                if h != "ciamlogin.com"):
            return True

        return False

def canonicalize(authority_or_auth_endpoint):
    # Returns (url_parsed_result, hostname_in_lowercase, tenant)
    authority = urlparse(authority_or_auth_endpoint)
    if authority.scheme == "https" and authority.hostname:
        parts = authority.path.split("/")
        first_part = parts[1] if len(parts) >= 2 and parts[1] else None
        if authority.hostname.endswith(_CIAM_DOMAIN_SUFFIX):  # CIAM
            # Use path in CIAM authority. It will be validated by OIDC Discovery soon
            tenant = first_part if first_part else "{}.onmicrosoft.com".format(
                # Fallback to sub domain name. This variation may not be advertised
                authority.hostname.rsplit(_CIAM_DOMAIN_SUFFIX, 1)[0])
            return authority, authority.hostname, tenant
        # AAD
        if len(parts) >= 2 and parts[1]:
            return authority, authority.hostname, parts[1]
    raise ValueError(
        "Your given address (%s) should consist of "
        "an https url with hostname and a minimum of one segment in a path: e.g. "
        "https://login.microsoftonline.com/{tenant} "
        "or https://{tenant_name}.ciamlogin.com/{tenant} "
        "or https://{tenant_name}.b2clogin.com/{tenant_name}.onmicrosoft.com/policy"
        % authority_or_auth_endpoint)

def _instance_discovery(url, http_client, instance_discovery_endpoint, **kwargs):
    resp = http_client.get(
        instance_discovery_endpoint,
        params={'authorization_endpoint': url, 'api-version': '1.0'},
        **kwargs)
    return json.loads(resp.text)

def tenant_discovery(tenant_discovery_endpoint, http_client, **kwargs):
    # Returns Openid Configuration
    resp = http_client.get(tenant_discovery_endpoint, **kwargs)
    if resp.status_code == 200:
        return json.loads(resp.text)  # It could raise ValueError
    if 400 <= resp.status_code < 500:
        # Nonexist tenant would hit this path
        # e.g. https://login.microsoftonline.com/nonexist_tenant/v2.0/.well-known/openid-configuration
        raise ValueError("OIDC Discovery failed on {}. HTTP status: {}, Error: {}".format(
            tenant_discovery_endpoint,
            resp.status_code,
            resp.text,  # Expose it as-is b/c OIDC defines no error response format
            ))
    # Transient network error would hit this path
    resp.raise_for_status()
    raise RuntimeError(  # A fallback here, in case resp.raise_for_status() is no-op
        "Unable to complete OIDC Discovery: %d, %s" % (resp.status_code, resp.text))
