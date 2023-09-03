import unittest
import socket
import sys

import requests

from msal.oauth2cli.authcode import AuthCodeReceiver


class TestAuthCodeReceiver(unittest.TestCase):
    def test_setup_at_a_given_port_and_teardown(self):
        port = 12345  # Assuming this port is available
        with AuthCodeReceiver(port=port) as receiver:
            self.assertEqual(port, receiver.get_port())

    def test_setup_at_a_ephemeral_port_and_teardown(self):
        port = 0
        with AuthCodeReceiver(port=port) as receiver:
            self.assertNotEqual(port, receiver.get_port())

    def test_no_two_concurrent_receivers_can_listen_on_same_port(self):
        with AuthCodeReceiver() as receiver:
            expected_error = OSError if sys.version_info[0] > 2 else socket.error
            with self.assertRaises(expected_error):
                with AuthCodeReceiver(port=receiver.get_port()):
                    pass

    def test_template_should_escape_input(self):
        with AuthCodeReceiver() as receiver:
            receiver._scheduled_actions = [(  # Injection happens here when the port is known
                1,  # Delay it until the receiver is activated by get_auth_response()
                lambda: self.assertEqual(
                    "<html>&lt;tag&gt;foo&lt;/tag&gt;</html>",
                    requests.get("http://localhost:{}?error=<tag>foo</tag>".format(
                        receiver.get_port())).text,
                    "Unsafe data in HTML should be escaped",
            ))]
            receiver.get_auth_response(  # Starts server and hang until timeout
                timeout=3,
                error_template="<html>$error</html>",
            )

