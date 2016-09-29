import re

import requests

from .exceptions import MsalServiceError


WELL_KNOWN_AUTHORITY_HOSTS = set([
    'login.windows.net',
    'login.microsoftonline.com',
    'login.chinacloudapi.cn',
    'login-us.microsoftonline.com',
    'login.microsoftonline.de',
    ])
AUTHORIZATION_ENDPOINT = "/oauth2/v2.0/authorize"
TOKEN_ENDPOINT = "/oauth2/v2.0/token"


class Authority(object):
    """This class represents an (already-validated) authority.

    Once constructed, it contains members named "*_endpoint" for this instance.
    TODO: It will also cache the previously-validated authority instances.
    """
    def __init__(self, authority_url, validate_authority=True):
        canonicalized, host, tenant = canonicalize(authority_url)
        if host not in WELL_KNOWN_AUTHORITY_HOSTS and validate_authority:
            # AAD only requires instance_discovery() passes, and ignores result
            instance_discovery(canonicalized + AUTHORIZATION_ENDPOINT)
        self.authorization_endpoint = canonicalized + AUTHORIZATION_ENDPOINT
        self.token_endpoint = canonicalized + TOKEN_ENDPOINT

def canonicalize(url):
    # Returns (canonicalized_url, host, tenant). Raises ValueError on errors.
    m = re.match("https://([^/]+)/([^/\?#]+)", url.lower())
    if not m:
        raise ValueError(
            "Your given address (%s) should consist of "
            "an https url with a minimum of one segment in a path: e.g. "
            "https://login.microsoftonline.com/<tenant_name>" % url)
    return m.group(0), m.group(1), m.group(2)

def instance_discovery(url, response=None):
    resp = requests.get(
        'https://login.windows.net/common/discovery/instance',  # World-wide
        params={'authorization_endpoint': url, 'api-version': '1.0'})
    payload = response or resp.json()
    if 'tenant_discovery_endpoint' not in payload:
        raise MsalServiceError(status_code=resp.status_code, **payload)
    return payload['tenant_discovery_endpoint']

