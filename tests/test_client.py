import os
import json
import logging

from oauth2cli.oauth2 import Client
from tests import unittest

from .authcode import AuthCodeReceiver


THIS_FOLDER = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(THIS_FOLDER, 'config.json')


class TestClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(CONFIG_FILE) as f:
            cls.conf = json.load(f)

    def test_client_credentials(self):
        client = Client(self.conf['clientId'], self.conf['clientSecret'],
                token_endpoint=self.conf["token_endpoint"])
        result = client.obtain_token_with_client_credentials(self.conf['scope'])
        self.assertIn('access_token', result)

