import logging
import os
import json
import time
import unittest

import requests

import msal
from tests.http_client import MinimalHttpClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _get_app_and_auth_code(
        client_id,
        client_secret=None,
        authority="https://login.microsoftonline.com/common",
        port=44331,
        scopes=["https://graph.microsoft.com/.default"],  # Microsoft Graph
        **kwargs):
    from msal.oauth2cli.authcode import obtain_auth_code
    app = msal.ClientApplication(
        client_id, client_secret, authority=authority, http_client=MinimalHttpClient())
    redirect_uri = "http://localhost:%d" % port
    ac = obtain_auth_code(port, auth_uri=app.get_authorization_request_url(
        scopes, redirect_uri=redirect_uri, **kwargs))
    assert ac is not None
    return (app, ac, redirect_uri)

@unittest.skipIf(os.getenv("TRAVIS_TAG"), "Skip e2e tests during tagged release")
class E2eTestCase(unittest.TestCase):

    def assertLoosely(self, response, assertion=None,
            skippable_errors=("invalid_grant", "interaction_required")):
        if response.get("error") in skippable_errors:
            logger.debug("Response = %s", response)
            # Some of these errors are configuration issues, not library issues
            raise unittest.SkipTest(response.get("error_description"))
        else:
            if assertion is None:
                assertion = lambda: self.assertIn(
                    "access_token", response,
                    "{error}: {error_description}".format(
                        # Do explicit response.get(...) rather than **response
                        error=response.get("error"),
                        error_description=response.get("error_description")))
            assertion()

    def assertCacheWorksForUser(self, result_from_wire, scope, username=None):
        # You can filter by predefined username, or let end user to choose one
        accounts = self.app.get_accounts(username=username)
        self.assertNotEqual(0, len(accounts))
        account = accounts[0]
        if ("scope" not in result_from_wire  # This is the usual case
                or  # Authority server could reject some scopes
                set(scope) <= set(result_from_wire["scope"].split(" "))
                ):
            # Going to test acquire_token_silent(...) to locate an AT from cache
            result_from_cache = self.app.acquire_token_silent(scope, account=account)
            self.assertIsNotNone(result_from_cache)
            self.assertIsNone(
                result_from_cache.get("refresh_token"), "A cache hit returns no RT")
            self.assertEqual(
                result_from_wire['access_token'], result_from_cache['access_token'],
                "We should get a cached AT")

        # Going to test acquire_token_silent(...) to obtain an AT by a RT from cache
        self.app.token_cache._cache["AccessToken"] = {}  # A hacky way to clear ATs
        result_from_cache = self.app.acquire_token_silent(scope, account=account)
        self.assertIsNotNone(result_from_cache,
                "We should get a result from acquire_token_silent(...) call")
        self.assertIsNotNone(
            # We used to assert it this way:
            #   result_from_wire['access_token'] != result_from_cache['access_token']
            # but ROPC in B2C tends to return the same AT we obtained seconds ago.
            # Now looking back, "refresh_token grant would return a brand new AT"
            # was just an empirical observation but never a committment in specs,
            # so we adjust our way to assert here.
            (result_from_cache or {}).get("access_token"),
            "We should get an AT from acquire_token_silent(...) call")

    def assertCacheWorksForApp(self, result_from_wire, scope):
        # Going to test acquire_token_silent(...) to locate an AT from cache
        result_from_cache = self.app.acquire_token_silent(scope, account=None)
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(
            result_from_wire['access_token'], result_from_cache['access_token'],
            "We should get a cached AT")

    def _test_username_password(self,
            authority=None, client_id=None, username=None, password=None, scope=None,
            **ignored):
        assert authority and client_id and username and password and scope
        self.app = msal.PublicClientApplication(
            client_id, authority=authority, http_client=MinimalHttpClient())
        result = self.app.acquire_token_by_username_password(
            username, password, scopes=scope)
        self.assertLoosely(result)
        self.assertEqual(None, result.get("error"), str(result))
        self.assertCacheWorksForUser(
            result, scope,
            username=username if ".b2clogin.com" not in authority else None,
            )

    def _test_device_flow(
            self, client_id=None, authority=None, scope=None, **ignored):
        assert client_id and authority and scope
        self.app = msal.PublicClientApplication(
            client_id, authority=authority, http_client=MinimalHttpClient())
        flow = self.app.initiate_device_flow(scopes=scope)
        assert "user_code" in flow, "DF does not seem to be provisioned: %s".format(
            json.dumps(flow, indent=4))
        logger.info(flow["message"])

        duration = 60
        logger.info("We will wait up to %d seconds for you to sign in" % duration)
        flow["expires_at"] = min(  # Shorten the time for quick test
            flow["expires_at"], time.time() + duration)
        result = self.app.acquire_token_by_device_flow(flow)
        self.assertLoosely(  # It will skip this test if there is no user interaction
            result,
            assertion=lambda: self.assertIn('access_token', result),
            skippable_errors=self.app.client.DEVICE_FLOW_RETRIABLE_ERRORS)
        if "access_token" not in result:
            self.skip("End user did not complete Device Flow in time")
        self.assertCacheWorksForUser(result, scope, username=None)
        result["access_token"] = result["refresh_token"] = "************"
        logger.info(
            "%s obtained tokens: %s", self.id(), json.dumps(result, indent=4))


THIS_FOLDER = os.path.dirname(__file__)
CONFIG = os.path.join(THIS_FOLDER, "config.json")
@unittest.skipUnless(os.path.exists(CONFIG), "Optional %s not found" % CONFIG)
class FileBasedTestCase(E2eTestCase):
    # This covers scenarios that are not currently available for test automation.
    # So they mean to be run on maintainer's machine for semi-automated tests.

    @classmethod
    def setUpClass(cls):
        with open(CONFIG) as f:
            cls.config = json.load(f)

    def skipUnlessWithConfig(self, fields):
        for field in fields:
            if field not in self.config:
                self.skipTest('Skipping due to lack of configuration "%s"' % field)

    def test_username_password(self):
        self.skipUnlessWithConfig(["client_id", "username", "password", "scope"])
        self._test_username_password(**self.config)

    def _get_app_and_auth_code(self, **kwargs):
        return _get_app_and_auth_code(
            self.config["client_id"],
            client_secret=self.config.get("client_secret"),
            authority=self.config.get("authority"),
            port=self.config.get("listen_port", 44331),
            scopes=self.config["scope"],
            **kwargs)

    def _test_auth_code(self, auth_kwargs, token_kwargs):
        self.skipUnlessWithConfig(["client_id", "scope"])
        (self.app, ac, redirect_uri) = self._get_app_and_auth_code(**auth_kwargs)
        result = self.app.acquire_token_by_authorization_code(
            ac, self.config["scope"], redirect_uri=redirect_uri, **token_kwargs)
        logger.debug("%s.cache = %s",
            self.id(), json.dumps(self.app.token_cache._cache, indent=4))
        self.assertIn(
            "access_token", result,
            "{error}: {error_description}".format(
                # Note: No interpolation here, cause error won't always present
                error=result.get("error"),
                error_description=result.get("error_description")))
        self.assertCacheWorksForUser(result, self.config["scope"], username=None)

    def test_auth_code(self):
        self._test_auth_code({}, {})

    def test_auth_code_with_matching_nonce(self):
        self._test_auth_code({"nonce": "foo"}, {"nonce": "foo"})

    def test_auth_code_with_mismatching_nonce(self):
        self.skipUnlessWithConfig(["client_id", "scope"])
        (self.app, ac, redirect_uri) = self._get_app_and_auth_code(nonce="foo")
        with self.assertRaises(ValueError):
            self.app.acquire_token_by_authorization_code(
                ac, self.config["scope"], redirect_uri=redirect_uri, nonce="bar")

    def test_ssh_cert(self):
        self.skipUnlessWithConfig(["client_id", "scope"])

        JWK1 = """{"kty":"RSA", "n":"2tNr73xwcj6lH7bqRZrFzgSLj7OeLfbn8216uOMDHuaZ6TEUBDN8Uz0ve8jAlKsP9CQFCSVoSNovdE-fs7c15MxEGHjDcNKLWonznximj8pDGZQjVdfK-7mG6P6z-lgVcLuYu5JcWU_PeEqIKg5llOaz-qeQ4LEDS4T1D2qWRGpAra4rJX1-kmrWmX_XIamq30C9EIO0gGuT4rc2hJBWQ-4-FnE1NXmy125wfT3NdotAJGq5lMIfhjfglDbJCwhc8Oe17ORjO3FsB5CLuBRpYmP7Nzn66lRY3Fe11Xz8AEBl3anKFSJcTvlMnFtu3EpD-eiaHfTgRBU7CztGQqVbiQ", "e":"AQAB"}"""
        JWK2 = """{"kty":"RSA", "n":"72u07mew8rw-ssw3tUs9clKstGO2lvD7ZNxJU7OPNKz5PGYx3gjkhUmtNah4I4FP0DuF1ogb_qSS5eD86w10Wb1ftjWcoY8zjNO9V3ph-Q2tMQWdDW5kLdeU3-EDzc0HQeou9E0udqmfQoPbuXFQcOkdcbh3eeYejs8sWn3TQprXRwGh_TRYi-CAurXXLxQ8rp-pltUVRIr1B63fXmXhMeCAGwCPEFX9FRRs-YHUszUJl9F9-E0nmdOitiAkKfCC9LhwB9_xKtjmHUM9VaEC9jWOcdvXZutwEoW2XPMOg0Ky-s197F9rfpgHle2gBrXsbvVMvS0D-wXg6vsq6BAHzQ", "e":"AQAB"}"""
        data1 = {"token_type": "ssh-cert", "key_id": "key1", "req_cnf": JWK1}
        ssh_test_slice = {
            "dc": "prod-wst-test1",
            "slice": "test",
            "sshcrt": "true",
            }

        (self.app, ac, redirect_uri) = self._get_app_and_auth_code()

        result = self.app.acquire_token_by_authorization_code(
            ac, self.config["scope"], redirect_uri=redirect_uri, data=data1,
            params=ssh_test_slice)
        self.assertEqual("ssh-cert", result["token_type"])
        logger.debug("%s.cache = %s",
            self.id(), json.dumps(self.app.token_cache._cache, indent=4))

        # acquire_token_silent() needs to be passed the same key to work
        account = self.app.get_accounts()[0]
        result_from_cache = self.app.acquire_token_silent(
            self.config["scope"], account=account, data=data1)
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(
            result['access_token'], result_from_cache['access_token'],
            "We should get the cached SSH-cert")

        # refresh_token grant can fetch an ssh-cert bound to a different key
        refreshed_ssh_cert = self.app.acquire_token_silent(
            self.config["scope"], account=account, params=ssh_test_slice,
            data={"token_type": "ssh-cert", "key_id": "key2", "req_cnf": JWK2})
        self.assertIsNotNone(refreshed_ssh_cert)
        self.assertEqual(refreshed_ssh_cert["token_type"], "ssh-cert")
        self.assertNotEqual(result["access_token"], refreshed_ssh_cert['access_token'])

    def test_client_secret(self):
        self.skipUnlessWithConfig(["client_id", "client_secret"])
        self.app = msal.ConfidentialClientApplication(
            self.config["client_id"],
            client_credential=self.config.get("client_secret"),
            authority=self.config.get("authority"),
            http_client=MinimalHttpClient())
        scope = self.config.get("scope", [])
        result = self.app.acquire_token_for_client(scope)
        self.assertIn('access_token', result)
        self.assertCacheWorksForApp(result, scope)

    def test_client_certificate(self):
        self.skipUnlessWithConfig(["client_id", "client_certificate"])
        client_cert = self.config["client_certificate"]
        assert "private_key_path" in client_cert and "thumbprint" in client_cert
        with open(os.path.join(THIS_FOLDER, client_cert['private_key_path'])) as f:
            private_key = f.read()  # Should be in PEM format
        self.app = msal.ConfidentialClientApplication(
            self.config['client_id'],
            {"private_key": private_key, "thumbprint": client_cert["thumbprint"]},
            http_client=MinimalHttpClient())
        scope = self.config.get("scope", [])
        result = self.app.acquire_token_for_client(scope)
        self.assertIn('access_token', result)
        self.assertCacheWorksForApp(result, scope)

    def test_subject_name_issuer_authentication(self):
        self.skipUnlessWithConfig(["client_id", "client_certificate"])
        client_cert = self.config["client_certificate"]
        assert "private_key_path" in client_cert and "thumbprint" in client_cert
        if not "public_certificate" in client_cert:
            self.skipTest("Skipping SNI test due to lack of public_certificate")
        with open(os.path.join(THIS_FOLDER, client_cert['private_key_path'])) as f:
            private_key = f.read()  # Should be in PEM format
        with open(os.path.join(THIS_FOLDER, client_cert['public_certificate'])) as f:
            public_certificate = f.read()
        self.app = msal.ConfidentialClientApplication(
            self.config['client_id'], authority=self.config["authority"],
            client_credential={
                "private_key": private_key,
                "thumbprint": self.config["thumbprint"],
                "public_certificate": public_certificate,
                },
            http_client=MinimalHttpClient())
        scope = self.config.get("scope", [])
        result = self.app.acquire_token_for_client(scope)
        self.assertIn('access_token', result)
        self.assertCacheWorksForApp(result, scope)


@unittest.skipUnless(os.path.exists(CONFIG), "Optional %s not found" % CONFIG)
class DeviceFlowTestCase(E2eTestCase):  # A leaf class so it will be run only once
    @classmethod
    def setUpClass(cls):
        with open(CONFIG) as f:
            cls.config = json.load(f)

    def test_device_flow(self):
        self._test_device_flow(**self.config)


def get_lab_app(
        env_client_id="LAB_APP_CLIENT_ID",
        env_client_secret="LAB_APP_CLIENT_SECRET",
        ):
    """Returns the lab app as an MSAL confidential client.

    Get it from environment variables if defined, otherwise fall back to use MSI.
    """
    if os.getenv(env_client_id) and os.getenv(env_client_secret):
        # A shortcut mainly for running tests on developer's local development machine
        # or it could be setup on Travis CI
        #   https://docs.travis-ci.com/user/environment-variables/#defining-variables-in-repository-settings
        # Data came from here
        # https://docs.msidlab.com/accounts/confidentialclient.html
        logger.info("Using lab app defined by ENV variables %s and %s",
                env_client_id, env_client_secret)
        client_id = os.getenv(env_client_id)
        client_secret = os.getenv(env_client_secret)
    else:
        logger.info("ENV variables %s and/or %s are not defined. Fall back to MSI.",
                env_client_id, env_client_secret)
        # See also https://microsoft.sharepoint-df.com/teams/MSIDLABSExtended/SitePages/Programmatically-accessing-LAB-API's.aspx
        raise unittest.SkipTest("MSI-based mechanism has not been implemented yet")
    return msal.ConfidentialClientApplication(client_id, client_secret,
            authority="https://login.microsoftonline.com/"
                "72f988bf-86f1-41af-91ab-2d7cd011db47",  # Microsoft tenant ID
            http_client=MinimalHttpClient())

def get_session(lab_app, scopes):  # BTW, this infrastructure tests the confidential client flow
    logger.info("Creating session")
    lab_token = lab_app.acquire_token_for_client(scopes)
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer %s" % lab_token["access_token"]})
    session.hooks["response"].append(lambda r, *args, **kwargs: r.raise_for_status())
    return session


class LabBasedTestCase(E2eTestCase):
    _secrets = {}
    adfs2019_scopes = ["placeholder"]  # Need this to satisfy MSAL API surface.
        # Internally, MSAL will also append more scopes like "openid" etc..
        # ADFS 2019 will issue tokens for valid scope only, by default "openid".
        # https://docs.microsoft.com/en-us/windows-server/identity/ad-fs/overview/ad-fs-faq#what-permitted-scopes-are-supported-by-ad-fs

    @classmethod
    def setUpClass(cls):
        # https://docs.msidlab.com/accounts/apiaccess.html#code-snippet
        cls.session = get_session(get_lab_app(), ["https://msidlab.com/.default"])

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    @classmethod
    def get_lab_app_object(cls, **query):  # https://msidlab.com/swagger/index.html
        url = "https://msidlab.com/api/app"
        resp = cls.session.get(url, params=query)
        return resp.json()[0]

    @classmethod
    def get_lab_user_secret(cls, lab_name="msidlab4"):
        lab_name = lab_name.lower()
        if lab_name not in cls._secrets:
            logger.info("Querying lab user password for %s", lab_name)
            url = "https://msidlab.com/api/LabUserSecret?secret=%s" % lab_name
            resp = cls.session.get(url)
            cls._secrets[lab_name] = resp.json()["value"]
        return cls._secrets[lab_name]

    @classmethod
    def get_lab_user(cls, **query):  # https://docs.msidlab.com/labapi/userapi.html
        resp = cls.session.get("https://msidlab.com/api/user", params=query)
        result = resp.json()[0]
        _env = query.get("azureenvironment", "").lower()
        authority_base = {
            "azureusgovernment": "https://login.microsoftonline.us/"
            }.get(_env, "https://login.microsoftonline.com/")
        scope = {
            "azureusgovernment": ["https://graph.microsoft.us/.default"],
            }.get(_env, ["https://graph.microsoft.com/.default"])
        return {  # Mapping lab API response to our simplified configuration format
            "authority": authority_base + result["tenantID"],
            "client_id": result["appId"],
            "username": result["upn"],
            "lab_name": result["labName"],
            "scope": scope,
            }

    def _test_acquire_token_by_auth_code(
            self, client_id=None, authority=None, port=None, scope=None,
            **ignored):
        assert client_id and authority and port and scope
        (self.app, ac, redirect_uri) = _get_app_and_auth_code(
            client_id, authority=authority, port=port, scopes=scope)
        result = self.app.acquire_token_by_authorization_code(
            ac, scope, redirect_uri=redirect_uri)
        logger.debug(
            "%s: cache = %s, id_token_claims = %s",
            self.id(),
            json.dumps(self.app.token_cache._cache, indent=4),
            json.dumps(result.get("id_token_claims"), indent=4),
            )
        self.assertIn(
            "access_token", result,
            "{error}: {error_description}".format(
                # Note: No interpolation here, cause error won't always present
                error=result.get("error"),
                error_description=result.get("error_description")))
        self.assertCacheWorksForUser(result, scope, username=None)

    def _test_acquire_token_obo(self, config_pca, config_cca):
        # 1. An app obtains a token representing a user, for our mid-tier service
        pca = msal.PublicClientApplication(
            config_pca["client_id"], authority=config_pca["authority"],
            http_client=MinimalHttpClient())
        pca_result = pca.acquire_token_by_username_password(
            config_pca["username"],
            config_pca["password"],
            scopes=config_pca["scope"],
            )
        self.assertIsNotNone(
            pca_result.get("access_token"),
            "PCA failed to get AT because %s" % json.dumps(pca_result, indent=2))

        # 2. Our mid-tier service uses OBO to obtain a token for downstream service
        cca = msal.ConfidentialClientApplication(
            config_cca["client_id"],
            client_credential=config_cca["client_secret"],
            authority=config_cca["authority"],
            http_client=MinimalHttpClient(),
            # token_cache= ...,  # Default token cache is all-tokens-store-in-memory.
                # That's fine if OBO app uses short-lived msal instance per session.
                # Otherwise, the OBO app need to implement a one-cache-per-user setup.
            )
        cca_result = cca.acquire_token_on_behalf_of(
            pca_result['access_token'], config_cca["scope"])
        self.assertNotEqual(None, cca_result.get("access_token"), str(cca_result))

        # 3. Now the OBO app can simply store downstream token(s) in same session.
        #    Alternatively, if you want to persist the downstream AT, and possibly
        #    the RT (if any) for prolonged access even after your own AT expires,
        #    now it is the time to persist current cache state for current user.
        #    Assuming you already did that (which is not shown in this test case),
        #    the following part shows one of the ways to obtain an AT from cache.
        username = cca_result.get("id_token_claims", {}).get("preferred_username")
        self.assertEqual(config_cca["username"], username)
        if username:  # A precaution so that we won't use other user's token
            account = cca.get_accounts(username=username)[0]
            result = cca.acquire_token_silent(config_cca["scope"], account)
            self.assertEqual(cca_result["access_token"], result["access_token"])

    def _test_acquire_token_by_client_secret(
            self, client_id=None, client_secret=None, authority=None, scope=None,
            **ignored):
        assert client_id and client_secret and authority and scope
        app = msal.ConfidentialClientApplication(
            client_id, client_credential=client_secret, authority=authority,
            http_client=MinimalHttpClient())
        result = app.acquire_token_for_client(scope)
        self.assertIsNotNone(result.get("access_token"), "Got %s instead" % result)


class WorldWideTestCase(LabBasedTestCase):

    def test_aad_managed_user(self):  # Pure cloud
        config = self.get_lab_user(usertype="cloud")
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    def test_adfs4_fed_user(self):
        config = self.get_lab_user(usertype="federated", federationProvider="ADFSv4")
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    def test_adfs3_fed_user(self):
        config = self.get_lab_user(usertype="federated", federationProvider="ADFSv3")
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    def test_adfs2_fed_user(self):
        config = self.get_lab_user(usertype="federated", federationProvider="ADFSv2")
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    def test_adfs2019_fed_user(self):
        config = self.get_lab_user(usertype="federated", federationProvider="ADFSv2019")
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    def test_ropc_adfs2019_onprem(self):
        # Configuration is derived from https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/4.7.0/tests/Microsoft.Identity.Test.Common/TestConstants.cs#L250-L259
        config = self.get_lab_user(usertype="onprem", federationProvider="ADFSv2019")
        config["authority"] = "https://fs.%s.com/adfs" % config["lab_name"]
        config["scope"] = self.adfs2019_scopes
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    @unittest.skipIf(os.getenv("TRAVIS"), "Browser automation is not yet implemented")
    def test_adfs2019_onprem_acquire_token_by_auth_code(self):
        """When prompted, you can manually login using this account:

        # https://msidlab.com/api/user?usertype=onprem&federationprovider=ADFSv2019
        username = "..."  # The upn from the link above
        password="***"  # From https://aka.ms/GetLabUserSecret?Secret=msidlabXYZ
        """
        config = self.get_lab_user(usertype="onprem", federationProvider="ADFSv2019")
        config["authority"] = "https://fs.%s.com/adfs" % config["lab_name"]
        config["scope"] = self.adfs2019_scopes
        config["port"] = 8080
        self._test_acquire_token_by_auth_code(**config)

    @unittest.skipUnless(
        os.getenv("LAB_OBO_CLIENT_SECRET"),
        "Need LAB_OBO_CLIENT SECRET from https://msidlabs.vault.azure.net/secrets/TodoListServiceV2-OBO/c58ba97c34ca4464886943a847d1db56")
    @unittest.skipUnless(
        os.getenv("LAB_OBO_CONFIDENTIAL_CLIENT_ID"),
        "Confidential client id can be found here https://docs.msidlab.com/flows/onbehalfofflow.html")
    @unittest.skipUnless(
        os.getenv("LAB_OBO_PUBLIC_CLIENT_ID"),
        "Public client id can be found here https://docs.msidlab.com/flows/onbehalfofflow.html")
    def test_acquire_token_obo(self):
        config = self.get_lab_user(usertype="cloud")

        config_cca = {}
        config_cca.update(config)
        config_cca["client_id"] = os.getenv("LAB_OBO_CONFIDENTIAL_CLIENT_ID")
        config_cca["scope"] = ["https://graph.microsoft.com/.default"]
        config_cca["client_secret"] = os.getenv("LAB_OBO_CLIENT_SECRET")

        config_pca = {}
        config_pca.update(config)
        config_pca["client_id"] = os.getenv("LAB_OBO_PUBLIC_CLIENT_ID")
        config_pca["password"] = self.get_lab_user_secret(config_pca["lab_name"])
        config_pca["scope"] = ["api://%s/read" % config_cca["client_id"]]

        self._test_acquire_token_obo(config_pca, config_cca)

    def _build_b2c_authority(self, policy):
        base = "https://msidlabb2c.b2clogin.com/msidlabb2c.onmicrosoft.com"
        return base + "/" + policy  # We do not support base + "?p=" + policy

    @unittest.skipIf(os.getenv("TRAVIS"), "Browser automation is not yet implemented")
    def test_b2c_acquire_token_by_auth_code(self):
        """
        When prompted, you can manually login using this account:

            username="b2clocal@msidlabb2c.onmicrosoft.com"
                # This won't work https://msidlab.com/api/user?usertype=b2c
            password="***"  # From https://aka.ms/GetLabUserSecret?Secret=msidlabb2c
        """
        config = self.get_lab_app_object(azureenvironment="azureb2ccloud")
        self._test_acquire_token_by_auth_code(
            authority=self._build_b2c_authority("B2C_1_SignInPolicy"),
            client_id=config["appId"],
            port=3843,  # Lab defines 4 of them: [3843, 4584, 4843, 60000]
            scope=config["defaultScopes"].split(','),
            )

    def test_b2c_acquire_token_by_ropc(self):
        config = self.get_lab_app_object(azureenvironment="azureb2ccloud")
        self._test_username_password(
            authority=self._build_b2c_authority("B2C_1_ROPC_Auth"),
            client_id=config["appId"],
            username="b2clocal@msidlabb2c.onmicrosoft.com",
            password=self.get_lab_user_secret("msidlabb2c"),
            scope=config["defaultScopes"].split(','),
            )


class ArlingtonCloudTestCase(LabBasedTestCase):
    environment = "azureusgovernment"

    def test_acquire_token_by_ropc(self):
        config = self.get_lab_user(azureenvironment=self.environment)
        config["password"] = self.get_lab_user_secret(config["lab_name"])
        self._test_username_password(**config)

    def test_acquire_token_by_client_secret(self):
        config = self.get_lab_user(usertype="cloud", azureenvironment=self.environment, publicClient="no")
        config["client_secret"] = self.get_lab_user_secret("ARLMSIDLAB1-IDLASBS-App-CC-Secret")
        self._test_acquire_token_by_client_secret(**config)

    def test_acquire_token_obo(self):
        config_cca = self.get_lab_user(
            usertype="cloud", azureenvironment=self.environment, publicClient="no")
        config_cca["scope"] = ["https://graph.microsoft.us/.default"]
        config_cca["client_secret"] = self.get_lab_user_secret("ARLMSIDLAB1-IDLASBS-App-CC-Secret")

        config_pca = self.get_lab_user(usertype="cloud", azureenvironment=self.environment, publicClient="yes")
        obo_app_object = self.get_lab_app_object(
            usertype="cloud", azureenvironment=self.environment, publicClient="no")
        config_pca["password"] = self.get_lab_user_secret(config_pca["lab_name"])
        config_pca["scope"] = ["{app_uri}/files.read".format(app_uri=obo_app_object.get("identifierUris"))]

        self._test_acquire_token_obo(config_pca, config_cca)

    def test_acquire_token_device_flow(self):
        config = self.get_lab_user(usertype="cloud", azureenvironment=self.environment, publicClient="yes")
        config["scope"] = ["user.read"]
        self._test_device_flow(**config)

    def test_acquire_token_silent_with_an_empty_cache_should_return_none(self):
        config = self.get_lab_user(
            usertype="cloud", azureenvironment=self.environment, publicClient="no")
        app = msal.ConfidentialClientApplication(
            config['client_id'], authority=config['authority'],
            http_client=MinimalHttpClient())
        result = app.acquire_token_silent(scopes=config['scope'], account=None)
        self.assertEqual(result, None)
        # Note: An alias in this region is no longer accepting HTTPS traffic.
        #       If this test case passes without exception,
        #       it means MSAL Python is not affected by that.

if __name__ == "__main__":
    unittest.main()

