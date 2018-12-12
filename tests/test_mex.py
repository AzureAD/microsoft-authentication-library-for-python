import os

from tests import unittest
from msal.mex import *


THIS_FOLDER = os.path.dirname(__file__)

class TestMex(unittest.TestCase):

    def _test_parser(self, sample, expected_endpoint):
        with open(os.path.join(THIS_FOLDER, sample)) as sample_file:
            endpoint = Mex(mex_document=sample_file.read()
                ).get_wstrust_username_password_endpoint()["address"]
            self.assertEqual(expected_endpoint, endpoint)

    def test_happy_path_1(self):
        self._test_parser("microsoft.mex.xml",
            'https://corp.sts.microsoft.com/adfs/services/trust/13/usernamemixed')

    def test_happy_path_2(self):
        self._test_parser('arupela.mex.xml',
            'https://fs.arupela.com/adfs/services/trust/13/usernamemixed')

    def test_happy_path_3(self):
        self._test_parser('archan.us.mex.xml',
            'https://arvmserver2012.archan.us/adfs/services/trust/13/usernamemixed')

