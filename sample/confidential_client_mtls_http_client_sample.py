"""A custom HTTP pipeline for MSAL's mTLS token transport.

Use the CLIENT_ID, AUTHORITY, PFX_PATH, optional PFX_PASSPHRASE, SCOPE and
optional AZURE_REGION environment variables from the default mTLS-PoP sample.
REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE and proxy environment variables retain their
Requests behavior. Run:

    python sample\\confidential_client_mtls_http_client_sample.py

The factory owns both ordinary and derived sessions. Quiesce all applications
and in-flight work before leaving its context manager. No token or private-key
material is logged.
"""
import os
import threading

import requests

import msal


class PolicyHttpClient:
    """A small example; apply your production policy in both pipeline paths."""

    def __init__(
            self, verify=True, proxies=None, timeout=30, trust_env=True,
            binding_context=None):
        self._config = {
            "verify": verify, "proxies": dict(proxies or {}),
            "timeout": timeout, "trust_env": trust_env}
        self._lock = threading.Lock()
        self._closed = False
        self._children = []
        self.response_count = 0
        self._session = requests.Session()
        self._session.verify = verify
        self._session.trust_env = trust_env
        self._session.proxies.update(self._config["proxies"])
        self._session.headers["X-Msal-Pipeline"] = "custom-policy"
        self._session.hooks["response"].append(self._record_response)
        options = {
            "max_retries": 1, "pool_connections": 4,
            "pool_maxsize": 8, "pool_block": True}
        self._session.mount("https://",
            binding_context.create_requests_adapter(**options)
            if binding_context is not None
            else requests.adapters.HTTPAdapter(**options))

    def _record_response(self, response, **kwargs):
        with self._lock:
            self.response_count += 1
        return response

    def _check_open(self):
        if self._closed:
            raise RuntimeError("The HTTP pipeline has been closed.")

    def create_mtls_client(self, binding_context):
        with self._lock:
            self._check_open()
            child = PolicyHttpClient(
                binding_context=binding_context, **self._config)
            self._children.append(child)
            return child

    def get(self, url, **kwargs):
        self._check_open()
        kwargs.setdefault("timeout", self._config["timeout"])
        return self._session.get(url, **kwargs)

    def post(self, url, **kwargs):
        self._check_open()
        kwargs.setdefault("timeout", self._config["timeout"])
        return self._session.post(url, **kwargs)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                for child in self._children:
                    child.close()
                self._session.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *args):
        self.close()


def main():
    credential = {
        "private_key_pfx_path": os.environ["PFX_PATH"],
        "passphrase": os.getenv("PFX_PASSPHRASE"),
        "public_certificate": True,
    }
    with PolicyHttpClient() as http_client:
        app = msal.ConfidentialClientApplication(
            os.environ["CLIENT_ID"],
            authority=os.environ["AUTHORITY"],
            client_credential=credential,
            http_client=http_client,
            azure_region=os.getenv("AZURE_REGION"),
        )
        result = app.acquire_token_for_client(
            os.environ["SCOPE"].split(), mtls_proof_of_possession=True)
        print("Acquired mTLS-PoP token:", result.get("token_type") == "mtls_pop")
        if "error" in result:
            print("Error code:", result["error"])


if __name__ == "__main__":
    main()
