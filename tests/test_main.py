import os
import stat
import sys
import tempfile
from unittest.mock import patch

import msal
from tests import unittest
from tests.test_token_cache import build_response


@unittest.skipIf(sys.platform == "win32", "File mode bits are POSIX-specific")
class TestTesterTokenCacheFile(unittest.TestCase):
    """The ``python -m msal`` tester persists refresh tokens into its cwd"""

    @classmethod
    def setUpClass(cls):
        with patch("builtins.input", return_value=""):  # Its DEBUG log prompt
            from msal import __main__ as tester
        cls.tester = tester

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)
        cache = msal.SerializableTokenCache()
        cache.add({
            "client_id": "my_app",
            "scope": ["s1"],
            "token_endpoint": "https://login.example.com/contoso/oauth2/v2.0/token",
            "response": build_response(
                uid="uid", utid="utid", access_token="an AT",
                refresh_token="a RT"),
            })
        patcher = patch.object(self.tester, "global_cache", cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertCacheFileIsOwnerOnly(self):
        self.tester._save_cache()
        filename = self.tester._token_cache_filename
        self.assertEqual(0o600, stat.S_IMODE(os.stat(filename).st_mode))
        with open(filename) as f:
            self.assertIn("a RT", f.read())

    def test_new_cache_file_should_be_owner_only(self):
        self.addCleanup(os.umask, os.umask(0o022))  # A common default umask
        self.assertCacheFileIsOwnerOnly()

    def test_existing_cache_file_should_become_owner_only(self):
        open(self.tester._token_cache_filename, "w").close()
        os.chmod(self.tester._token_cache_filename, 0o644)  # Left by older versions
        self.assertCacheFileIsOwnerOnly()
