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
        """Test that HTML in error response is properly escaped"""
        with AuthCodeReceiver() as receiver:
            receiver._scheduled_actions = [(  # Injection happens here when the port is known
                1,  # Delay it until the receiver is activated by get_auth_response()
                lambda: self.assertEqual(
                    "<html>&lt;script&gt;alert(&#x27;xss&#x27;);&lt;/script&gt;</html>",
                    requests.post(
                        "http://localhost:{}".format(receiver.get_port()),
                        data={"error": "<script>alert('xss');</script>"},
                    ).text,
            ))]
            receiver.get_auth_response(  # Starts server and hang until timeout
                timeout=3,
                error_template="<html>$error</html>",
            )

    def test_get_request_with_auth_code_is_rejected(self):
        """Test that GET request with auth code is rejected for security"""
        with AuthCodeReceiver() as receiver:
            test_state = "test_state_67890"
            receiver._scheduled_actions = [(
                1,
                lambda: self.assertEqual(400, requests.get(
                    "http://localhost:{}".format(receiver.get_port()), params={
                        "code": "test_auth_code_12345",
                        "state": test_state
                    }
                ).status_code)
            )]
            result = receiver.get_auth_response(timeout=3, state=test_state)
            self.assertIsNone(result, "Should not receive auth response via GET")

    def test_post_request_with_auth_code(self):
        """Test that POST request with auth code is handled correctly (form_post response mode)"""
        with AuthCodeReceiver() as receiver:
            test_code = "test_auth_code_12345"
            test_state = "test_state_67890"
            receiver._scheduled_actions = [(
                1,
                lambda: requests.post(
                    "http://localhost:{}".format(receiver.get_port()),
                    data={"code": test_code, "state": test_state},
                )
            )]
            result = receiver.get_auth_response(timeout=3, state=test_state)
            self.assertIsNotNone(result, "Should receive auth response via POST")
            self.assertEqual(result.get("code"), test_code)
            self.assertEqual(result.get("state"), test_state)

    def test_post_request_with_error(self):
        """Test that POST request with error is handled correctly"""
        with AuthCodeReceiver() as receiver:
            test_error = "access_denied"
            test_error_description = "User denied access"
            receiver._scheduled_actions = [(
                1,
                lambda: requests.post(
                    "http://localhost:{}".format(receiver.get_port()),
                    data={"error": test_error, "error_description": test_error_description},
                )
            )]
            result = receiver.get_auth_response(timeout=3)
            self.assertIsNotNone(result, "Should receive auth response via POST")
            self.assertEqual(result.get("error"), test_error)
            self.assertEqual(result.get("error_description"), test_error_description)

    def test_post_request_state_mismatch(self):
        """Test that POST request with mismatched state is rejected"""
        with AuthCodeReceiver() as receiver:
            receiver._scheduled_actions = [(
                1,
                lambda: requests.post(
                    "http://localhost:{}".format(receiver.get_port()),
                    data={"code": "test_code", "state": "wrong_state"},
                )
            )]
            result = receiver.get_auth_response(timeout=3, state="expected_state")
            self.assertIsNone(result, "Should not receive auth response due to state mismatch")
