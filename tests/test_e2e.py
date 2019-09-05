import logging
import os
import json
import time

import requests

import msal
from tests import unittest


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


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
        self.assertNotEqual(
            result_from_wire['access_token'], result_from_cache['access_token'],
            "We should get a fresh AT (via RT)")

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
        self.app = msal.PublicClientApplication(client_id, authority=authority)
        result = self.app.acquire_token_by_username_password(
            username, password, scopes=scope)
        self.assertLoosely(result)
        # self.assertEqual(None, result.get("error"), str(result))
        self.assertCacheWorksForUser(result, scope, username=username)


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

    def test_auth_code(self):
        self.skipUnlessWithConfig(["client_id", "scope"])
        from msal.oauth2cli.authcode import obtain_auth_code
        self.app = msal.ClientApplication(
            self.config["client_id"],
            client_credential=self.config.get("client_secret"),
            authority=self.config.get("authority"))
        port = self.config.get("listen_port", 44331)
        redirect_uri = "http://localhost:%s" % port
        auth_request_uri = self.app.get_authorization_request_url(
            self.config["scope"], redirect_uri=redirect_uri)
        ac = obtain_auth_code(port, auth_uri=auth_request_uri)
        self.assertNotEqual(ac, None)

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
            # Note: Short link won't work "https://aka.ms/GetLabUserSecret?Secret=%s"
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
    def test_acquire_token_obo(self):  # It hardcodes many pre-defined resources
        obo_client_id = "23c64cd8-21e4-41dd-9756-ab9e2c23f58c"
        obo_scopes = ["https://graph.microsoft.com/User.Read"]
        config = get_lab_user(isFederated=False)
        pca = msal.PublicClientApplication(
            "be9b0186-7dfd-448a-a944-f771029105bf", authority=config.get("authority"))
        pca_result = pca.acquire_token_by_username_password(
            config["username"],
            self.get_lab_user_secret(config["lab"]["labname"]),
            scopes=["%s/access_as_user" % obo_client_id],  # Need setup beforehand
            )
        self.assertNotEqual(None, pca_result.get("access_token"), "PCA should work")

        cca = msal.ConfidentialClientApplication(
            obo_client_id,
            client_credential=os.getenv("OBO_CLIENT_SECRET"),
            authority=config.get("authority"))
        cca_result = cca.acquire_token_on_behalf_of(
            pca_result['access_token'], obo_scopes)
        self.assertNotEqual(None, cca_result.get("access_token"), str(cca_result))

        # Cache would also work, with the one-cache-per-user caveat.
        if len(cca.get_accounts()) == 1:
            account = cca.get_accounts()[0]  # This test involves only 1 account
            result = cca.acquire_token_silent(obo_scopes, account)
            self.assertEqual(cca_result["access_token"], result["access_token"])

