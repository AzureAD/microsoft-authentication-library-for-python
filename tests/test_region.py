import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from msal.region import (
    _detect_region, _detect_region_of_azure_vm, _validate_region)

from tests.http_client import MinimalResponse


class _StubHttpClient(object):
    """Records the requested URL/headers and returns a preconfigured response.

    If *response* is an exception instance, it is raised from ``get`` to
    simulate a network failure (e.g. not running in an Azure VM)."""

    def __init__(self, response):
        self._response = response
        self.url = None
        self.headers = None

    def get(self, url, params=None, headers=None, **kwargs):
        self.url = url
        self.headers = headers
        if isinstance(self._response, Exception):
            raise self._response
        return self._response



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
        self.assertIsNone(_validate_region("westus-"))

    def test_rejects_label_longer_than_63_characters(self):
        self.assertIsNone(_validate_region("a" * 64))

    def test_rejects_adversarial_dns_labels(self):
        for region in (
                "EastUS", "east us", " eastus", "eastus ", "eastus\n",
                "westus2.login.microsoft.com", "https://westus2",
                "westus2:443", "westus2/path", "wéstus"):
            with self.subTest(region=region):
                self.assertIsNone(_validate_region(region))

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

    def test_rejects_unnormalized_env_regions(self):
        for region in ("EastUS", "East Us 2", "westus-", "a" * 64):
            with self.subTest(region=region), patch.dict(
                    os.environ, {"REGION_NAME": region}):
                self.assertIsNone(_detect_region())

    @patch.dict(os.environ, {"REGION_NAME": ""})
    def test_empty_env_returns_none(self):
        self.assertIsNone(_detect_region())


class TestDetectRegionOfAzureVm(unittest.TestCase):

    def test_valid_location_is_returned(self):
        client = _StubHttpClient(
            MinimalResponse(status_code=200, text='{"location": "westus2"}'))
        self.assertEqual(_detect_region_of_azure_vm(client), "westus2")

    def test_request_uses_compute_json_endpoint(self):
        client = _StubHttpClient(
            MinimalResponse(status_code=200, text='{"location": "westus2"}'))
        _detect_region_of_azure_vm(client)
        self.assertEqual(
            client.url,
            "http://169.254.169.254/metadata/instance/compute"
            "?api-version=2021-02-01")
        self.assertNotIn("/location", client.url)
        self.assertNotIn("format=text", client.url)
        self.assertEqual(client.headers, {"Metadata": "true"})

    def test_missing_location_returns_none(self):
        client = _StubHttpClient(MinimalResponse(status_code=200, text="{}"))
        self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_null_location_returns_none(self):
        client = _StubHttpClient(
            MinimalResponse(status_code=200, text='{"location": null}'))
        self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_malformed_json_returns_none(self):
        client = _StubHttpClient(
            MinimalResponse(status_code=200, text="not json"))
        self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_invalid_location_value_returns_none(self):
        client = _StubHttpClient(
            MinimalResponse(status_code=200, text='{"location": "evil.com/hijack"}'))
        self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_invalid_dns_label_location_returns_none(self):
        for region in ("westus-", "a" * 64, "westus2:443", "wéstus"):
            with self.subTest(region=region):
                client = _StubHttpClient(MinimalResponse(
                    status_code=200, text='{"location": "%s"}' % region))
                self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_non_string_location_returns_none(self):
        client = _StubHttpClient(
            MinimalResponse(status_code=200, text='{"location": 123}'))
        self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_non_string_response_text_returns_none(self):
        # A custom http_client could yield a non-string resp.text; json.loads
        # would raise TypeError, which must be treated as a malformed response.
        client = _StubHttpClient(SimpleNamespace(status_code=200, text=None))
        self.assertIsNone(_detect_region_of_azure_vm(client))

    def test_network_failure_returns_none(self):
        client = _StubHttpClient(IOError("IMDS unreachable"))
        self.assertIsNone(_detect_region_of_azure_vm(client))


if __name__ == "__main__":
    unittest.main()
