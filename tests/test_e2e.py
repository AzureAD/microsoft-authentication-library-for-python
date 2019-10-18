import logging
import os
import json
import time

import requests

import msal
from tests import unittest


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _get_app_and_auth_code(
        client_id,
        client_secret=None,
        authority="https://login.microsoftonline.com/common",
        port=44331,
        scopes=["https://graph.microsoft.com/.default"],  # Microsoft Graph
        ):
    from msal.oauth2cli.authcode import obtain_auth_code
    app = msal.ClientApplication(client_id, client_secret, authority=authority)
    redirect_uri = "http://localhost:%d" % port
    ac = obtain_auth_code(port, auth_uri=app.get_authorization_request_url(
        scopes, redirect_uri=redirect_uri))
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
        # Going to test acquire_token_silent(...) to locate an AT from cache
        result_from_cache = self.app.acquire_token_silent(scope, account=account)
        self.assertIsNotNone(result_from_cache)
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
            authority=None, client_id=None, username=None, password=None, scope=None, trust_framework_policy=None
            **ignored):
        assert authority and client_id and username and password and scope
        self.app = msal.PublicClientApplication(client_id, authority=authority, trust_framework_policy= trust_framework_policy)
        result = self.app.acquire_token_by_username_password(
            username, password, scopes=scope)
        self.assertLoosely(result)
        # self.assertEqual(None, result.get("error"), str(result))
        self.assertCacheWorksForUser(
            result, scope,
            username=username if ".b2clogin.com" not in authority else None,
            )


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

    def _get_app_and_auth_code(self):
        return _get_app_and_auth_code(
            self.config["client_id"],
            client_secret=self.config.get("client_secret"),
            authority=self.config.get("authority"),
            port=self.config.get("listen_port", 44331),
            scopes=self.config["scope"],
            )

    def test_auth_code(self):
        self.skipUnlessWithConfig(["client_id", "scope"])
        (self.app, ac, redirect_uri) = self._get_app_and_auth_code()
        result = self.app.acquire_token_by_authorization_code(
            ac, self.config["scope"], redirect_uri=redirect_uri)
        logger.debug("%s.cache = %s",
            self.id(), json.dumps(self.app.token_cache._cache, indent=4))
        self.assertIn(
            "access_token", result,
            "{error}: {error_description}".format(
                # Note: No interpolation here, cause error won't always present
                error=result.get("error"),
                error_description=result.get("error_description")))
        self.assertCacheWorksForUser(result, self.config["scope"], username=None)


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
            authority=self.config.get("authority"))
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
            {"private_key": private_key, "thumbprint": client_cert["thumbprint"]})
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
                })
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
        scopes = self.config["scope"]
        self.app = msal.PublicClientApplication(
            self.config['client_id'], authority=self.config["authority"])
        flow = self.app.initiate_device_flow(scopes=scopes)
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
        self.assertCacheWorksForUser(result, scopes, username=None)
        result["access_token"] = result["refresh_token"] = "************"
        logger.info(
            "%s obtained tokens: %s", self.id(), json.dumps(result, indent=4))


def get_lab_user(mam=False, mfa=False, isFederated=False, federationProvider=None):
    # Based on https://microsoft.sharepoint-df.com/teams/MSIDLABSExtended/SitePages/LAB.aspx
    user = requests.get("https://api.msidlab.com/api/user", params=dict(  # Publicly available
        mam=mam, mfa=mfa, isFederated=isFederated, federationProvider=federationProvider,
        )).json()
    return {  # Mapping lab API response to our simplified configuration format
        "authority": user["Authority"][0] + user["Users"]["tenantId"],
        "client_id": user["AppID"],
        "username": user["Users"]["upn"],
        "lab": {"labname": user["Users"]["upn"].split('@')[1].split('.')[0]},  # :(
        "scope": ["https://graph.microsoft.com/.default"],
        }

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
        # https://microsoft.sharepoint-df.com/teams/MSIDLABSExtended/SitePages/Rese.aspx#programmatic-access-info-for-lab-request-api
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
            )

def get_session(lab_app, scopes):  # BTW, this infrastructure tests the confidential client flow
    logger.info("Creating session")
    lab_token = lab_app.acquire_token_for_client(scopes)
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer %s" % lab_token["access_token"]})
    session.hooks["response"].append(lambda r, *args, **kwargs: r.raise_for_status())
    return session


class LabBasedTestCase(E2eTestCase):
    _secrets = {}

    @classmethod
    def setUpClass(cls):
        cls.session = get_session(get_lab_app(), [
            "https://request.msidlab.com/.default",  # Existing user & password API
            # "https://user.msidlab.com/.default",  # New user API
            ])

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    @classmethod
    def get_lab_user_secret(cls, lab_name="msidlab4"):
        lab_name = lab_name.lower()
        if lab_name not in cls._secrets:
            logger.info("Querying lab user password for %s", lab_name)
            # Short link only works in browser "https://aka.ms/GetLabUserSecret?Secret=%s"
            # So we use the official link written in here
            # https://microsoft.sharepoint-df.com/teams/MSIDLABSExtended/SitePages/Programmatically-accessing-LAB-API%27s.aspx
            url = ("https://request.msidlab.com/api/GetLabUserSecret?code=KpY5uCcoKo0aW8VOL/CUO3wnu9UF2XbSnLFGk56BDnmQiwD80MQ7HA==&Secret=%s"
                % lab_name)
            resp = cls.session.get(url)
            cls._secrets[lab_name] = resp.json()["Value"]
        return cls._secrets[lab_name]

    @classmethod
    def get_lab_user(cls, query):  # Experimental: The query format is in lab team's Aug 9 email
        resp = cls.session.get("https://user.msidlab.com/api/user", params=query)
        result = resp.json()[0]
        return {  # Mapping lab API response to our simplified configuration format
            "authority": result["lab"]["authority"] + result["lab"]["tenantid"],
            "client_id": result["app"]["objectid"],
            "username": result["user"]["upn"],
            "lab": result["lab"],
            "scope": ["https://graph.microsoft.com/.default"],
            }

    def test_aad_managed_user(self):  # Pure cloud or hybrid
        config = get_lab_user(isFederated=False)
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    def test_adfs4_fed_user(self):
        config = get_lab_user(isFederated=True, federationProvider="ADFSv4")
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    def test_adfs4_managed_user(self):  # Conceptually the hybrid
        config = get_lab_user(isFederated=False, federationProvider="ADFSv4")
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    def test_adfs3_fed_user(self):
        config = get_lab_user(isFederated=True, federationProvider="ADFSv3")
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    def test_adfs3_managed_user(self):
        config = get_lab_user(isFederated=False, federationProvider="ADFSv3")
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    def test_adfs2_fed_user(self):
        config = get_lab_user(isFederated=True, federationProvider="ADFSv2")
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    @unittest.skip("Old Lab API returns nothing. We will switch to new api later")
    def test_adfs2019_fed_user(self):
        config = get_lab_user(isFederated=True, federationProvider="ADFSv2019")
        self._test_username_password(
            password=self.get_lab_user_secret(config["lab"]["labname"]), **config)

    @unittest.skipUnless(
        os.getenv("OBO_CLIENT_SECRET"),
        "Need OBO_CLIENT_SECRET from https://buildautomation.vault.azure.net/secrets/IdentityDivisionDotNetOBOServiceSecret")
    def test_acquire_token_obo(self):
        # Some hardcoded, pre-defined settings
        obo_client_id = "23c64cd8-21e4-41dd-9756-ab9e2c23f58c"
        downstream_scopes = ["https://graph.microsoft.com/User.Read"]
        config = get_lab_user(isFederated=False)

        # 1. An app obtains a token representing a user, for our mid-tier service
        pca = msal.PublicClientApplication(
            "be9b0186-7dfd-448a-a944-f771029105bf", authority=config.get("authority"))
        pca_result = pca.acquire_token_by_username_password(
            config["username"],
            self.get_lab_user_secret(config["lab"]["labname"]),
            scopes=[  # The OBO app's scope. Yours might be different.
                "%s/access_as_user" % obo_client_id],
            )
        self.assertIsNotNone(pca_result.get("access_token"), "PCA should work")

        # 2. Our mid-tier service uses OBO to obtain a token for downstream service
        cca = msal.ConfidentialClientApplication(
            obo_client_id,
            client_credential=os.getenv("OBO_CLIENT_SECRET"),
            authority=config.get("authority"),
            # token_cache= ...,  # Default token cache is all-tokens-store-in-memory.
                # That's fine if OBO app uses short-lived msal instance per session.
                # Otherwise, the OBO app need to implement a one-cache-per-user setup.
            )
        cca_result = cca.acquire_token_on_behalf_of(
            pca_result['access_token'], downstream_scopes)
        self.assertNotEqual(None, cca_result.get("access_token"), str(cca_result))

        # 3. Now the OBO app can simply store downstream token(s) in same session.
        #    Alternatively, if you want to persist the downstream AT, and possibly
        #    the RT (if any) for prolonged access even after your own AT expires,
        #    now it is the time to persist current cache state for current user.
        #    Assuming you already did that (which is not shown in this test case),
        #    the following part shows one of the ways to obtain an AT from cache.
        username = cca_result.get("id_token_claims", {}).get("preferred_username")
        self.assertEqual(config["username"], username)
        if username:  # A precaution so that we won't use other user's token
            account = cca.get_accounts(username=username)[0]
            result = cca.acquire_token_silent(downstream_scopes, account)
            self.assertEqual(cca_result["access_token"], result["access_token"])

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
        scopes = ["https://msidlabb2c.onmicrosoft.com/msaapp/user_impersonation"]
        (self.app, ac, redirect_uri) = _get_app_and_auth_code(
            "b876a048-55a5-4fc5-9403-f5d90cb1c852",
            client_secret=self.get_lab_user_secret("MSIDLABB2C-MSAapp-AppSecret"),
            authority=self._build_b2c_authority("B2C_1_SignInPolicy"),
            port=3843,  # Lab defines 4 of them: [3843, 4584, 4843, 60000]
            scopes=scopes,
            )
        result = self.app.acquire_token_by_authorization_code(
            ac, scopes, redirect_uri=redirect_uri)
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
        self.assertCacheWorksForUser(result, scopes, username=None)

    def test_b2c_acquire_token_by_ropc(self):
        self._test_username_password(
            authority = "https://msidlabb2c.b2clogin.com/msidlabb2c.onmicrosoft.com",
            trust_framework_policy = "B2C_1_ROPC_Auth",
            client_id="e3b9ad76-9763-4827-b088-80c7a7888f79",
            username="b2clocal@msidlabb2c.onmicrosoft.com",
            password=self.get_lab_user_secret("msidlabb2c"),
            scope=["https://msidlabb2c.onmicrosoft.com/msidlabb2capi/read"],
            )

