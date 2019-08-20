import logging
import os
import json

import requests

import msal
from tests import unittest


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

def get_lab_user(query):
    # Based on https://microsoft.sharepoint-df.com/teams/MSIDLABSExtended/SitePages/LAB.aspx
    user = requests.get("https://api.msidlab.com/api/user", params=query).json()
    return {  # Mapping lab API response to our expected configuration format
        "authority": user["Authority"][0] + user["Users"]["tenantId"],
        "client_id": user["AppID"],
        "username": user["Users"]["upn"],
        "password": "TBD",  # TODO
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
        logger.info("Using lap app defined by ENV variables %s and %s",
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

def get_lab_user_secret(access_token, lab_name="msidlab4"):
    return requests.get(
        # Note: Short link won't work "https://aka.ms/GetLabUserSecret?Secret=%s"
        "https://request.msidlab.com/api/GetLabUserSecret?code=KpY5uCcoKo0aW8VOL/CUO3wnu9UF2XbSnLFGk56BDnmQiwD80MQ7HA==&Secret=%s"
            % lab_name,
        headers={"Authorization": "Bearer %s" % access_token},
        ).json()["Value"]


@unittest.skip("for now")
class E2eTestCase(unittest.TestCase):
    """
    lab_token = get_lab_app().acquire_token_for_client(
        "https://request.msidlab.com/.default"
        )  # BTW, this infrastructure tests the confidential client flow
    lab_password = get_lab_user_secret(lab_token["access_token"])
    """

    def setUp(self):
        pass
        # client_id, client_secret = get_lab_app()
        # self.lab_app = msal.ConfidentialClientApplication(client_id, client_secret)

    def test_bar(self):
        self.assertEqual("********", self.lab_password)


class BaseMixin(object):

    def skipIfNotConfigured(self, fields):
        if not all(map(self.config.get, fields)):
            self.skipTest("Configuration not sufficient")
        for field in fields:
            if not self.config.get(field):
                self.skipTest('"%s" not defined in configuration' % field)

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


class UsernamePasswordMixin(object):
    def test_username_password(self):
        self.skipIfNotConfigured([
            "authority", "client_id", "username", "password", "scope"])
        self.app = msal.PublicClientApplication(
            self.config["client_id"], authority=self.config["authority"])
        result = self.app.acquire_token_by_username_password(
            self.config["username"], self.config["password"],
            scopes=self.config.get("scope"))
        self.assertLoosely(result)
        self.assertCacheWorks(result)


DEFAULT_QUERY = {"mam": False, "mfa": False}

# Note: the following semi-parameterized testing approach is inspired from
# https://bugs.python.org/msg151444

@unittest.skip("for now")
class AadManagedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(DEFAULT_QUERY, isFederated=False))

@unittest.skip("for now")
class Adfs4FedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv4"))

@unittest.skip("for now")
class Adfs4ManagedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=False, federationProvider="ADFSv4"))

@unittest.skip("for now")  # TODO: Need to pick up the real password
class Adfs3FedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv3"))

@unittest.skip("for now")  # TODO: Need to pick up the real password
class Adfs3ManagedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=False, federationProvider="ADFSv3"))

@unittest.skip("for now")  # TODO: Need to pick up the real password
class Adfs2FedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv2"))

@unittest.skip("Lab API returns nothing. We might need to switch to beta api")
class Adfs2019FedUserPassTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        self.config = get_lab_user(dict(
            DEFAULT_QUERY, isFederated=True, federationProvider="ADFSv2019"))

CONFIG = os.path.join(os.path.dirname(__file__), "config.json")
@unittest.skipIf(not os.path.exists(CONFIG), "Optional %s not found" % CONFIG)
class FileBasedTestCase(BaseMixin, UsernamePasswordMixin, unittest.TestCase):
    def setUp(self):
        with open(CONFIG) as f:
            self.config = json.load(f)

