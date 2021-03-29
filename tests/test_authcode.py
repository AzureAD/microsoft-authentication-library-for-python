from unittest.mock import patch

from msal.oauth2cli.authcode import _browse
from tests import unittest


class TestUtil(unittest.TestCase):

    def test_browse(self):
        auth_uri = "https://example.com/"

        with patch("webbrowser.open", return_value=True):
            self.assertTrue(_browse(auth_uri))

        with patch("webbrowser.open", return_value=False):
            with patch("msal.oauth2cli.authcode.is_wsl", return_value=False):
                self.assertFalse(_browse(auth_uri))

            with patch("msal.oauth2cli.authcode.is_wsl", return_value=True):
                with patch("subprocess.call", return_value=0) as call_mock:
                    self.assertTrue(_browse(auth_uri))
                    call_mock.assert_called_with(
                        ['powershell.exe', '-NoProfile', '-Command', 'Start-Process "https://example.com/"'])

                with patch("subprocess.call", return_value=1):
                    self.assertFalse(_browse(auth_uri))
