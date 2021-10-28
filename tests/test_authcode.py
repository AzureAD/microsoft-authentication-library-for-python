import unittest
import socket
import sys

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
        port = 12345  # Assuming this port is available
        with AuthCodeReceiver(port=port) as receiver:
            expected_error = OSError if sys.version_info[0] > 2 else socket.error
            with self.assertRaises(expected_error):
                with AuthCodeReceiver(port=port) as receiver2:
                    pass

