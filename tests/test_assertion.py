import json

from msal.oauth2cli import JwtAssertionCreator
from msal.oauth2cli.oidc import decode_part

from tests import unittest


class AssertionTestCase(unittest.TestCase):
    def test_extra_claims(self):
        assertion = JwtAssertionCreator(key=None, algorithm="none").sign_assertion(
            "audience", "issuer", additional_claims={"client_ip": "1.2.3.4"})
        payload = json.loads(decode_part(assertion.split(b'.')[1].decode('utf-8')))
        self.assertEqual("1.2.3.4", payload.get("client_ip"))

