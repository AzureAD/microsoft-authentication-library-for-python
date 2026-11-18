import os
import unittest
import uuid
from unittest.mock import patch

from msal.telemetry import _get_new_correlation_id


class TestCorrelationId(unittest.TestCase):

    def test_correlation_id_should_be_a_version_4_uuid(self):
        correlation_id = _get_new_correlation_id()
        self.assertEqual(str(uuid.UUID(correlation_id)), correlation_id)
        self.assertEqual(4, uuid.UUID(correlation_id).version)

    def test_correlation_id_should_match_uuid4_from_same_random_bytes(self):
        for random_bytes in (bytes(16), b"\xff" * 16, os.urandom(16)):
            with patch("os.urandom", return_value=random_bytes):
                self.assertEqual(str(uuid.uuid4()), _get_new_correlation_id())
