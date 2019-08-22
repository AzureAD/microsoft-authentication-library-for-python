import logging
import os
import json

import requests

import msal
from tests import unittest


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class E2eTestCase(unittest.TestCase):
    config = {}

    def skipIfNotConfigured(self, fields):
        for field in fields:
            if not self.config.get(field):
                self.skipTest('"%s" not found in configuration' % field)

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

    def assertCacheWorks(self, result_from_wire):
        result = result_from_wire
        # You can filter by predefined username, or let end user to choose one
        accounts = self.app.get_accounts(username=self.config.get("username"))
        self.assertNotEqual(0, len(accounts))
        account = accounts[0]
        # Going to test acquire_token_silent(...) to locate an AT from cache
        result_from_cache = self.app.acquire_token_silent(
                self.config["scope"], account=account)
        self.assertIsNotNone(result_from_cache)
        self.assertEqual(result['access_token'], result_from_cache['access_token'],
                "We should get a cached AT")

        # Going to test acquire_token_silent(...) to obtain an AT by a RT from cache
        self.app.token_cache._cache["AccessToken"] = {}  # A hacky way to clear ATs
        result_from_cache = self.app.acquire_token_silent(
                self.config["scope"], account=account)
        self.assertIsNotNone(result_from_cache,
                "We should get a result from acquire_token_silent(...) call")
        self.assertNotEqual(result['access_token'], result_from_cache['access_token'],
                "We should get a fresh AT (via RT)")

    def test_username_password(self):
        self.skipIfNotConfigured([
            "authority", "client_id", "username", "password", "scope"])
        self.app = msal.PublicClientApplication(
            self.config["client_id"], authority=self.config["authority"])
        result = self.app.acquire_token_by_username_password(
            self.config["username"], self.config["password"],
            scopes=self.config.get("scope"))
        self.assertLoosely(result)
        # self.assertEqual(None, result.get("error"), str(result))
        self.assertCacheWorks(result)


CONFIG = os.path.join(os.path.dirname(__file__), "config.json")
@unittest.skipIf(not os.path.exists(CONFIG), "Optional %s not found" % CONFIG)
class FileBasedTestCase(E2eTestCase):
    def setUp(self):
        with open(CONFIG) as f:
            self.config = json.load(f)


def get_lab_user(query):  # This API requires no authorization
    # Based on https://microsoft.sharepoint-df.com/teams/MSIDLABSExtended/SitePages/LAB.aspx
    user = requests.get("https://api.msidlab.com/api/user", params=query).json()
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
        raise NotImplementedError("MSI-based mechanism has not been implemented yet")
    return msal.ConfidentialClientApplication(client_id, client_secret,
            authority="https://login.microsoftonline.com/"
                "72f988bf-86f1-41af-91ab-2d7cd011db47",  # Microsoft tenant ID
            )

def get_session(lab_app):  # BTW, this infrastructure tests the confidential client flow
    logger.info("Creating session")
    lab_token = lab_app.acquire_token_for_client("https://request.msidlab.com/.default")
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer %s" % lab_token["access_token"]})
    session.hooks["response"].append(lambda r, *args, **kwargs: r.raise_for_status())
    return session


class LabBasedTestCase(E2eTestCase):
    session = get_session(get_lab_app())  # It will run even all test cases are skipped
    _secrets = {}

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
    def get_lab_user(cls, query):  # The query format is in lab team's Aug 9 email
        resp = cls.session.get("https://user.msidlab.com/api/user", params=query)
        result = resp.json()[0]
        return {  # Mapping lab API response to our simplified configuration format
            "authority": result["lab"]["authority"] + result["lab"]["tenantid"],
            "client_id": result["app"]["objectid"],
            "username": result["user"]["upn"],
            "lab": result["lab"],
            "scope": ["https://graph.microsoft.com/.default"],
            }

DEFAULT_QUERY = {"mam": False, "mfa": False}

class AadManagedUserTestCase(LabBasedTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(DEFAULT_QUERY,
            isFederated=False,  # Supposed to find a pure managed user,
                # but lab still gives us a idlab@msidlab4.onmicrosoft.com
            ))
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

class Adfs4FedUserTestCase(LabBasedTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv4"))
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

class Adfs4ManagedUserTestCase(LabBasedTestCase):  # a.k.a. the hybrid
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=False, federationProvider="ADFSv4"))
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

class Adfs3FedUserTestCase(LabBasedTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv3"))
        #cls.config = cls.get_lab_user({
        #    "MFA": "none", "UserType": "federated", "FederationProvider": "adfsv3"})
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

class Adfs3ManagedUserTestCase(LabBasedTestCase):  # a.k.a. the hybrid
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=False, federationProvider="ADFSv3"))
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

class Adfs2FedUserTestCase(LabBasedTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv2"))
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

@unittest.skip("Lab API returns nothing. We might need to switch to beta api")
class Adfs2019FedUserTestCase(LabBasedTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv2019"))
        cls.config["password"] = cls.get_lab_user_secret(cls.config["lab"]["labname"])

