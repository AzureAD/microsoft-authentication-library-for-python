from tests import unittest

import msal
from msal import oauth2cli


class TestIdToken(unittest.TestCase):
    EXPIRED_ID_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJpc3N1ZXIiLCJpYXQiOjE3MDY1NzA3MzIsImV4cCI6MTY3NDk0ODMzMiwiYXVkIjoiZm9vIiwic3ViIjoic3ViamVjdCJ9.wyWNFxnE35SMP6FpxnWZmWQAy4KD0No_Q1rUy5bNnLs"

    def test_id_token_should_tolerate_time_error(self):
        self.assertEqual(oauth2cli.oidc.decode_id_token(self.EXPIRED_ID_TOKEN), {
            "iss": "issuer",
            "iat": 1706570732,
            "exp": 1674948332,  # 2023-1-28
            "aud": "foo",
            "sub": "subject",
            }, "id_token is decoded correctly, without raising exception")

    def test_id_token_should_error_out_on_client_id_error(self):
        with self.assertRaises(msal.IdTokenError):
            oauth2cli.oidc.decode_id_token(self.EXPIRED_ID_TOKEN, client_id="not foo")

