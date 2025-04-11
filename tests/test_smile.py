#!/usr/bin/env python3
"""
Test case that uses the MSAL Feature Test Runner (smile.py)
Uses the run_testcases() function with a remote URL to execute test cases
"""
import os
import unittest

import requests
try:
    from dotenv import load_dotenv  # Use this only in local dev machine
    load_dotenv()  # take environment variables from .env.
except:
    pass

from tests.smile import run_testcases


TESTCASES_URL = os.environ.get(
    'SMILE_TESTCASES_URL', "http://localhost:5000/testcases.json")

class SmileRemoteTestCase(unittest.TestCase):
    """Test case that runs test cases from a remote URL using smile.py"""

    def test_remote_testcases(self):
        try:
            # First try to reach the server to see if it's accessible
            response = requests.head(TESTCASES_URL, timeout=5)
            response.raise_for_status()
        except (requests.RequestException, requests.ConnectionError) as e:
            self.skipTest(f"Test server is unreachable: {e}")
        result = run_testcases(TESTCASES_URL)
        self.assertTrue(result, "All remote test cases should pass")


if __name__ == "__main__":
    unittest.main()