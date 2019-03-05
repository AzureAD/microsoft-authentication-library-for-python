import re

import requests

from .exceptions import MsalServiceError


WORLD_WIDE = 'login.microsoftonline.com'  # There was an alias login.windows.net
WELL_KNOWN_AUTHORITY_HOSTS = set([
    WORLD_WIDE,
    'login.chinacloudapi.cn',
    'login-us.microsoftonline.com',
    'login.microsoftonline.us',
    'login.microsoftonline.de',
    ])


class Authority(object):
    """This class represents an (already-validated) authority.

    Once constructed, it contains members named "*_endpoint" for this instance.
    TODO: It will also cache the previously-validated authority instances.
    """
    def __init__(self, authority_url, validate_authority=True,
            verify=True, proxies=None, timeout=None,
            ):
        """Creates an authority instance, and also validates it.

        :param validate_authority:
            The Authority validation process actually checks two parts:
            instance (a.k.a. host) and tenant. We always do a tenant discovery.
            This parameter only controls whether an instance discovery will be
            performed.
        """
        self.verify = verify
        self.proxies = proxies
        self.timeout = timeout
        canonicalized, self.instance, tenant = canonicalize(authority_url)
        tenant_discovery_endpoint = (  # Hard code a V2 pattern as default value
            'https://{}/{}/v2.0/.well-known/openid-configuration'
            .format(WORLD_WIDE, tenant))
        if validate_authority and self.instance not in WELL_KNOWN_AUTHORITY_HOSTS:
            tenant_discovery_endpoint = instance_discovery(
                canonicalized + "/oauth2/v2.0/authorize",
                verify=verify, proxies=proxies, timeout=timeout)
        openid_config = tenant_discovery(
            tenant_discovery_endpoint,
            verify=verify, proxies=proxies, timeout=timeout)
        self.authorization_endpoint = openid_config['authorization_endpoint']
        self.token_endpoint = openid_config['token_endpoint']
        _, _, self.tenant = canonicalize(self.token_endpoint)  # Usually a GUID
        self.is_adfs = self.tenant.lower() == 'adfs'

    def user_realm_discovery(self, username):
        resp = requests.get(
            "https://{netloc}/common/userrealm/{username}?api-version=1.0".format(
                netloc=self.instance, username=username),
            headers={'Accept':'application/json'},
            verify=self.verify, proxies=self.proxies, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
        # It will typically contain "ver", "account_type",
        # "federation_protocol", "cloud_audience_urn",
        # "federation_metadata_url", "federation_active_auth_url", etc.

def canonicalize(url):
    # Returns (canonicalized_url, netloc, tenant). Raises ValueError on errors.
    match_object = re.match("https://([^/]+)/([^/\?#]+)", url.lower())
    if not match_object:
        raise ValueError(
            "Your given address (%s) should consist of "
            "an https url with a minimum of one segment in a path: e.g. "
            "https://login.microsoftonline.com/<tenant_name>" % url)
    return match_object.group(0), match_object.group(1), match_object.group(2)

def instance_discovery(url, response=None, **kwargs):
    # Returns tenant discovery endpoint
    resp = requests.get(  # Note: This URL seemingly returns V1 endpoint only
        'https://{}/common/discovery/instance'.format(WORLD_WIDE),
        params={'authorization_endpoint': url, 'api-version': '1.0'},
        **kwargs)
    payload = response or resp.json()
    if 'tenant_discovery_endpoint' not in payload:
        raise MsalServiceError(status_code=resp.status_code, **payload)
    return payload['tenant_discovery_endpoint']

def tenant_discovery(tenant_discovery_endpoint, **kwargs):
    # Returns Openid Configuration
    resp = requests.get(tenant_discovery_endpoint, **kwargs)
    payload = resp.json()
    if 'authorization_endpoint' in payload and 'token_endpoint' in payload:
        return payload
    raise MsalServiceError(status_code=resp.status_code, **payload)

