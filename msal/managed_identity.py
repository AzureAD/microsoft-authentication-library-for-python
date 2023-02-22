# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
import json
import logging
import os
import socket
import time
try:  # Python 2
    from urlparse import urlparse
except:  # Python 3
    from urllib.parse import urlparse
try:  # Python 3
    from collections import UserDict
except:
    UserDict = dict  # The real UserDict is an old-style class which fails super()
from .token_cache import TokenCache
from .throttled_http_client import ThrottledHttpClient


logger = logging.getLogger(__name__)

class ManagedIdentity(UserDict):
    """Feed an instance of this class to :class:`msal.ManagedIdentityClient`
    to acquire token for the specified managed identity.
    """
    # The key names used in config dict
    ID_TYPE = "ManagedIdentityIdType"  # Contains keyword ManagedIdentity so its json equivalent will be more readable
    ID = "Id"

    # Valid values for key ID_TYPE
    CLIENT_ID = "ClientId"
    RESOURCE_ID = "ResourceId"
    OBJECT_ID = "ObjectId"
    SYSTEM_ASSIGNED = "SystemAssigned"

    _types_mapping = {  # Maps type name in configuration to type name on wire
        CLIENT_ID: "client_id",
        RESOURCE_ID: "mi_res_id",
        OBJECT_ID: "object_id",
    }

    @classmethod
    def is_managed_identity(cls, unknown):
        return isinstance(unknown, dict) and cls.ID_TYPE in unknown

    @classmethod
    def is_system_assigned(cls, unknown):
        return isinstance(unknown, dict) and unknown.get(cls.ID_TYPE) == cls.SYSTEM_ASSIGNED

    @classmethod
    def is_user_assigned(cls, unknown):
        return (
            isinstance(unknown, dict)
            and unknown.get(cls.ID_TYPE) in cls._types_mapping
            and unknown.get(cls.ID))

    def __init__(self, identifier=None, id_type=None):
        # Undocumented. Use subclasses instead.
        super(ManagedIdentity, self).__init__({
            self.ID_TYPE: id_type,
            self.ID: identifier,
        })


class SystemAssignedManagedIdentity(ManagedIdentity):
    """Represent a system-assigned managed identity, which is equivalent to::

        {"ManagedIdentityIdType": "SystemAssigned", "Id": None}
    """
    def __init__(self):
        super(SystemAssignedManagedIdentity, self).__init__(id_type=self.SYSTEM_ASSIGNED)


class UserAssignedManagedIdentity(ManagedIdentity):
    """Represent a user-assigned managed identity.

    Depends on the id you provided, the outcome is equivalent to one of the below::

        {"ManagedIdentityIdType": "ClientId", "Id": "foo"}
        {"ManagedIdentityIdType": "ResourceId", "Id": "foo"}
        {"ManagedIdentityIdType": "ObjectId", "Id": "foo"}
    """
    def __init__(self, client_id=None, resource_id=None, object_id=None):
        if client_id and not resource_id and not object_id:
            super(UserAssignedManagedIdentity, self).__init__(
                id_type=self.CLIENT_ID, identifier=client_id)
        elif not client_id and resource_id and not object_id:
            super(UserAssignedManagedIdentity, self).__init__(
                id_type=self.RESOURCE_ID, identifier=resource_id)
        elif not client_id and not resource_id and object_id:
            super(UserAssignedManagedIdentity, self).__init__(
                id_type=self.OBJECT_ID, identifier=object_id)
        else:
            raise ValueError(
                "You shall specify one of the three parameters: "
                "client_id, resource_id, object_id")


class ManagedIdentityClient(object):
    """This API encapulates multiple managed identity backends:
    VM, App Service, Azure Automation (Runbooks), Azure Function, Service Fabric,
    and Azure Arc.

    It also provides token cache support.
    """
    _instance, _tenant = socket.getfqdn(), "managed_identity"  # Placeholders

    def __init__(self, managed_identity, http_client, token_cache=None):
        """Create a managed identity client.

        :param dict managed_identity:
            It accepts an instance of :class:`SystemAssignedManagedIdentity`
            or :class:`UserAssignedManagedIdentity`.
            They are equivalent to a dict with a certain shape,
            which may be loaded from a json configuration file or an env var,

        :param http_client:
            An http client object. For example, you can use ``requests.Session()``,
            optionally with exponential backoff behavior demonstrated in this recipe::

                import requests
                from requests.adapters import HTTPAdapter, Retry
                s = requests.Session()
                retries = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
                s.mount('https://', HTTPAdapter(max_retries=retries))
                client = ManagedIdentityClient(managed_identity, s)

        :param token_cache:
            Optional. It accepts a :class:`msal.TokenCache` instance to store tokens.
            It will use an in-memory token cache by default.

        Recipe 1: Hard code a managed identity for your app::

            import msal, requests
            client = msal.ManagedIdentityClient(
                msal.UserAssignedManagedIdentity(client_id="foo"),
                requests.Session(),
                )
            token = client.acquire_token_for_client("resource")

        Recipe 2: Write once, run everywhere.
        If you use different managed identity on different deployment,
        you may use an environment variable (such as AZURE_MANAGED_IDENTITY)
        to store a json blob like
        ``{"ManagedIdentityIdType": "ClientId", "Id": "foo"}`` or
        ``{"ManagedIdentityIdType": "SystemAssignedManagedIdentity", "Id": null})``.
        The following app can load managed identity configuration dynamically::

            import json, os, msal, requests
            config = os.getenv("AZURE_MANAGED_IDENTITY")
            assert config, "An ENV VAR with value should exist"
            client = msal.ManagedIdentityClient(
                json.loads(config),
                requests.Session(),
                )
            token = client.acquire_token_for_client("resource")
        """
        self._managed_identity = managed_identity
        if isinstance(http_client, ThrottledHttpClient):
            raise ValueError(
                # It is a precaution to reject application.py's throttled http_client,
                # whose cache life on HTTP GET 200 is too long for Managed Identity.
                "This class does not currently accept a ThrottledHttpClient.")
        self._http_client = http_client
        self._token_cache = token_cache or TokenCache()

    def acquire_token_for_client(self, resource=None):
        """Acquire token for the managed identity.

        The result will be automatically cached.
        """
        if not resource:
            raise ValueError(
                "The resource parameter is currently required. "
                "It is only declared as optional in method signature, "
                "in case we want to support scope parameter in the future.")
        access_token_from_cache = None
        client_id_in_cache = self._managed_identity.get(
            ManagedIdentity.ID, "SYSTEM_ASSIGNED_MANAGED_IDENTITY")
        if True:  # Does not offer an "if not force_refresh" option, because
                  # there would be built-in token cache in the service side anyway
            matches = self._token_cache.find(
                self._token_cache.CredentialType.ACCESS_TOKEN,
                target=[resource],
                query=dict(
                    client_id=client_id_in_cache,
                    environment=self._instance,
                    realm=self._tenant,
                    home_account_id=None,
                ),
            )
            now = time.time()
            for entry in matches:
                expires_in = int(entry["expires_on"]) - now
                if expires_in < 5*60:  # Then consider it expired
                    continue  # Removal is not necessary, it will be overwritten
                logger.debug("Cache hit an AT")
                access_token_from_cache = {  # Mimic a real response
                    "access_token": entry["secret"],
                    "token_type": entry.get("token_type", "Bearer"),
                    "expires_in": int(expires_in),  # OAuth2 specs defines it as int
                }
                if "refresh_on" in entry and int(entry["refresh_on"]) < now:  # aging
                    break  # With a fallback in hand, we break here to go refresh
                return access_token_from_cache  # It is still good as new
        try:
            result = _obtain_token(self._http_client, self._managed_identity, resource)
            if "access_token" in result:
                expires_in = result.get("expires_in", 3600)
                if "refresh_in" not in result and expires_in >= 7200:
                    result["refresh_in"] = int(expires_in / 2)
                self._token_cache.add(dict(
                    client_id=client_id_in_cache,
                    scope=[resource],
                    token_endpoint="https://{}/{}".format(self._instance, self._tenant),
                    response=result,
                    params={},
                    data={},
                ))
            if (result and "error" not in result) or (not access_token_from_cache):
                return result
        except:  # The exact HTTP exception is transportation-layer dependent
            # Typically network error. Potential AAD outage?
            if not access_token_from_cache:  # It means there is no fall back option
                raise  # We choose to bubble up the exception
        return access_token_from_cache


def _scope_to_resource(scope):  # This is an experimental reasonable-effort approach
    u = urlparse(scope)
    if u.scheme:
        return "{}://{}".format(u.scheme, u.netloc)
    return scope  # There is no much else we can do here


def _obtain_token(http_client, managed_identity, resource):
    # A unified low-level API that talks to different Managed Identity
    if ("IDENTITY_ENDPOINT" in os.environ and "IDENTITY_HEADER" in os.environ
            and "IDENTITY_SERVER_THUMBPRINT" in os.environ
    ):
        if managed_identity:
            logger.debug(
                "Ignoring managed_identity parameter. "
                "Managed Identity in Service Fabric is configured in the cluster, "
                "not during runtime. See also "
                "https://learn.microsoft.com/en-us/azure/service-fabric/configure-existing-cluster-enable-managed-identity-token-service")
        return _obtain_token_on_service_fabric(
            http_client,
            os.environ["IDENTITY_ENDPOINT"],
            os.environ["IDENTITY_HEADER"],
            os.environ["IDENTITY_SERVER_THUMBPRINT"],
            resource,
        )
    if "IDENTITY_ENDPOINT" in os.environ and "IDENTITY_HEADER" in os.environ:
        return _obtain_token_on_app_service(
            http_client,
            os.environ["IDENTITY_ENDPOINT"],
            os.environ["IDENTITY_HEADER"],
            managed_identity,
            resource,
        )
    if "IDENTITY_ENDPOINT" in os.environ and "IMDS_ENDPOINT" in os.environ:
        if ManagedIdentity.is_user_assigned(managed_identity):
            raise ValueError(  # Note: Azure Identity for Python raised exception too
                "Ignoring managed_identity parameter. "
                "Azure Arc supports only system-assigned managed identity, "
                "See also "
                "https://learn.microsoft.com/en-us/azure/service-fabric/configure-existing-cluster-enable-managed-identity-token-service")
        return _obtain_token_on_arc(
            http_client,
            os.environ["IDENTITY_ENDPOINT"],
            resource,
        )
    return _obtain_token_on_azure_vm(http_client, managed_identity, resource)


def _adjust_param(params, managed_identity):
    id_name = ManagedIdentity._types_mapping.get(
        managed_identity.get(ManagedIdentity.ID_TYPE))
    if id_name:
        params[id_name] = managed_identity[ManagedIdentity.ID]

def _obtain_token_on_azure_vm(http_client, managed_identity, resource):
    # Based on https://docs.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/how-to-use-vm-token#get-a-token-using-http
    logger.debug("Obtaining token via managed identity on Azure VM")
    params = {
        "api-version": "2018-02-01",
        "resource": resource,
        }
    _adjust_param(params, managed_identity)
    resp = http_client.get(
        "http://169.254.169.254/metadata/identity/oauth2/token",
        params=params,
        headers={"Metadata": "true"},
        )
    try:
        payload = json.loads(resp.text)
        if payload.get("access_token") and payload.get("expires_in"):
            return {  # Normalizing the payload into OAuth2 format
                "access_token": payload["access_token"],
                "expires_in": int(payload["expires_in"]),
                "resource": payload.get("resource"),
                "token_type": payload.get("token_type", "Bearer"),
                }
        return payload  # Typically an error, but it is undefined in the doc above
    except ValueError:
        logger.debug("IMDS emits unexpected payload: %s", resp.text)
        raise

def _obtain_token_on_app_service(
    http_client, endpoint, identity_header, managed_identity, resource,
):
    """Obtains token for
    `App Service <https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity?tabs=portal%2Chttp#rest-endpoint-reference>`_,
    Azure Functions, and Azure Automation.
    """
    # Prerequisite: Create your app service https://docs.microsoft.com/en-us/azure/app-service/quickstart-python
    # Assign it a managed identity https://docs.microsoft.com/en-us/azure/app-service/overview-managed-identity?tabs=portal%2Chttp
    # SSH into your container for testing https://docs.microsoft.com/en-us/azure/app-service/configure-linux-open-ssh-session
    logger.debug("Obtaining token via managed identity on Azure App Service")
    params = {
        "api-version": "2019-08-01",
        "resource": resource,
        }
    _adjust_param(params, managed_identity)
    resp = http_client.get(
        endpoint,
        params=params,
        headers={
            "X-IDENTITY-HEADER": identity_header,
            "Metadata": "true",  # Unnecessary yet harmless for App Service,
            # It will be needed by Azure Automation
            # https://docs.microsoft.com/en-us/azure/automation/enable-managed-identity-for-automation#get-access-token-for-system-assigned-managed-identity-using-http-get
            },
        )
    try:
        payload = json.loads(resp.text)
        if payload.get("access_token") and payload.get("expires_on"):
            return {  # Normalizing the payload into OAuth2 format
                "access_token": payload["access_token"],
                "expires_in": int(payload["expires_on"]) - int(time.time()),
                "resource": payload.get("resource"),
                "token_type": payload.get("token_type", "Bearer"),
                }
        return {
            "error": "invalid_scope",  # Empirically, wrong resource ends up with a vague statusCode=500
            "error_description": "{}, {}".format(
                payload.get("statusCode"), payload.get("message")),
            }
    except ValueError:
        logger.debug("IMDS emits unexpected payload: %s", resp.text)
        raise


def _obtain_token_on_service_fabric(
    http_client, endpoint, identity_header, server_thumbprint, resource,
):
    """Obtains token for
    `Service Fabric <https://learn.microsoft.com/en-us/azure/service-fabric/>`_
    """
    # Deployment https://learn.microsoft.com/en-us/azure/service-fabric/service-fabric-get-started-containers-linux
    # See also https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/tests/managed-identity-live/service-fabric/service_fabric.md
    # Protocol https://learn.microsoft.com/en-us/azure/service-fabric/how-to-managed-identity-service-fabric-app-code#acquiring-an-access-token-using-rest-api
    logger.debug("Obtaining token via managed identity on Azure Service Fabric")
    resp = http_client.get(
        endpoint,
        params={"api-version": "2019-07-01-preview", "resource": resource},
        headers={"Secret": identity_header},
        )
    try:
        payload = json.loads(resp.text)
        if payload.get("access_token") and payload.get("expires_on"):
            return {  # Normalizing the payload into OAuth2 format
                "access_token": payload["access_token"],
                "expires_in": payload["expires_on"] - int(time.time()),
                "resource": payload.get("resource"),
                "token_type": payload["token_type"],
                }
        error = payload.get("error", {})  # https://learn.microsoft.com/en-us/azure/service-fabric/how-to-managed-identity-service-fabric-app-code#error-handling
        error_mapping = {  # Map Service Fabric errors into OAuth2 errors  https://www.rfc-editor.org/rfc/rfc6749#section-5.2
            "SecretHeaderNotFound": "unauthorized_client",
            "ManagedIdentityNotFound": "invalid_client",
            "ArgumentNullOrEmpty": "invalid_scope",
            }
        return {
            "error": error_mapping.get(payload["error"]["code"], "invalid_request"),
            "error_description": resp.text,
            }
    except ValueError:
        logger.debug("IMDS emits unexpected payload: %s", resp.text)
        raise


def _obtain_token_on_arc(http_client, endpoint, resource):
    # https://learn.microsoft.com/en-us/azure/azure-arc/servers/managed-identity-authentication
    logger.debug("Obtaining token via managed identity on Azure Arc")
    resp = http_client.get(
        endpoint,
        params={"api-version": "2020-06-01", "resource": resource},
        headers={"Metadata": "true"},
        )
    www_auth = "www-authenticate"  # Header in lower case
    challenge = {
        # Normalized to lowercase, because header names are case-insensitive
        # https://datatracker.ietf.org/doc/html/rfc7230#section-3.2
        k.lower(): v for k, v in resp.headers.items() if k.lower() == www_auth
        }.get(www_auth, "").split("=")  # Output will be ["Basic realm", "content"]
    if not (  # https://datatracker.ietf.org/doc/html/rfc7617#section-2
            len(challenge) == 2 and challenge[0].lower() == "basic realm"):
        raise ValueError("Irrecognizable WWW-Authenticate header: {}".format(resp.headers))
    with open(challenge[1]) as f:
        secret = f.read()
    response = http_client.get(
        endpoint,
        params={"api-version": "2020-06-01", "resource": resource},
        headers={"Metadata": "true", "Authorization": "Basic {}".format(secret)},
        )
    try:
        payload = json.loads(response.text)
        if payload.get("access_token") and payload.get("expires_in"):
            # Example: https://learn.microsoft.com/en-us/azure/azure-arc/servers/media/managed-identity-authentication/bash-token-output-example.png
            return {
                "access_token": payload["access_token"],
                "expires_in": int(payload["expires_in"]),
                "token_type": payload.get("token_type", "Bearer"),
                "resource": payload.get("resource"),
                }
    except ValueError:  # Typically json.decoder.JSONDecodeError
        pass
    return {
        "error": "invalid_request",
        "error_description": response.text,
        }

