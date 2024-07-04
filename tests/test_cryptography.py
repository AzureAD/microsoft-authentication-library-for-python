import configparser
import os
import re
from unittest import TestCase
import warnings
import xml.etree.ElementTree as ET

import requests

from msal.application import (
    _str2bytes, _load_private_key_from_pem_str, _parse_pfx)


latest_cryptography_version = ET.fromstring(
    requests.get("https://pypi.org/rss/project/cryptography/releases.xml").text
    ).findall("./channel/item/title")[0].text


def get_current_ceiling():
    parser = configparser.ConfigParser()
    parser.read("setup.cfg")
    for line in parser["options"]["install_requires"].splitlines():
        if line.startswith("cryptography"):
            match = re.search(r"<(\d+)", line)
            if match:
                return int(match.group(1))
    raise RuntimeError("Unable to find cryptography info from setup.cfg")


def sibling(filename):
    return os.path.join(os.path.dirname(__file__), filename)


class CryptographyTestCase(TestCase):

    def test_should_be_run_with_latest_version_of_cryptography(self):
        import cryptography
        self.assertEqual(
            cryptography.__version__, latest_cryptography_version,
            "We are using cryptography {} but we should test with latest {} instead. "
            "Run 'pip install -U cryptography'.".format(
            cryptography.__version__, latest_cryptography_version))

    def test_latest_cryptography_should_support_our_usage_without_warnings(self):
        passphrase_bytes = _str2bytes("password")
        with warnings.catch_warnings(record=True) as encountered_warnings:
            with open(sibling("certificate-with-password.pem")) as f:
                _load_private_key_from_pem_str(f.read(), passphrase_bytes)
            pfx = sibling("certificate-with-password.pfx")  # Created by:
                # openssl pkcs12 -export -inkey test/certificate-with-password.pem -in tests/certificate-with-password.pem -out tests/certificate-with-password.pfx
            _parse_pfx(pfx, passphrase_bytes)
            self.assertEqual(0, len(encountered_warnings),
                "Did cryptography deprecate the functions that we used?")

    def test_ceiling_should_be_latest_cryptography_version_plus_three(self):
        expected_ceiling = int(latest_cryptography_version.split(".")[0]) + 3
        self.assertEqual(
            expected_ceiling, get_current_ceiling(),
            "Test passed with latest cryptography, so we shall bump ceiling to N+3={}, "
            "based on their latest deprecation policy "
            "https://cryptography.io/en/latest/api-stability/#deprecation".format(
            expected_ceiling))

