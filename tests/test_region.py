import os
import unittest
from unittest.mock import patch

from msal.region import _detect_region, _validate_region


class TestValidateRegion(unittest.TestCase):

    def test_valid_simple_regions(self):
        for region in ("eastus", "westus2", "centralindia", "northeurope"):
            self.assertEqual(_validate_region(region), region)

    def test_valid_hyphenated_regions(self):
        for region in ("east-us", "west-us-2", "south-central-us"):
            self.assertEqual(_validate_region(region), region)

    def test_rejects_dots(self):
        self.assertIsNone(_validate_region("evil.com"))

    def test_rejects_slashes(self):
        self.assertIsNone(_validate_region("evil.com/path"))

    def test_rejects_colons(self):
        self.assertIsNone(_validate_region("evil:8080"))

    def test_rejects_uppercase(self):
        self.assertIsNone(_validate_region("EastUS"))

    def test_rejects_leading_hyphen(self):
        self.assertIsNone(_validate_region("-eastus"))

    def test_rejects_leading_digit(self):
        self.assertIsNone(_validate_region("2westus"))

    def test_rejects_trailing_hyphen(self):
        self.assertIsNone(_validate_region("eastus-"))

    def test_rejects_string_longer_than_63_chars(self):
        self.assertIsNone(_validate_region("a" * 64))

    def test_accepts_string_of_exactly_63_chars(self):
        region = "a" * 63
        self.assertEqual(_validate_region(region), region)

    def test_empty_string(self):
        self.assertIsNone(_validate_region(""))

    def test_none(self):
        self.assertIsNone(_validate_region(None))


class TestDetectRegion(unittest.TestCase):

    @patch.dict(os.environ, {"REGION_NAME": "westus2"})
    def test_returns_valid_env_region(self):
        self.assertEqual(_detect_region(), "westus2")

    @patch.dict(os.environ, {"REGION_NAME": "evil.com/hijack"})
    def test_rejects_invalid_env_region(self):
        self.assertIsNone(_detect_region())

    @patch.dict(os.environ, {"REGION_NAME": ""})
    def test_empty_env_returns_none(self):
        self.assertIsNone(_detect_region())


if __name__ == "__main__":
    unittest.main()
