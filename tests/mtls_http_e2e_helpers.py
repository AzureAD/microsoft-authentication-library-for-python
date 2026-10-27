"""Custom-pipeline recording for the mTLS HTTP factory E2E tests."""

from contextlib import contextmanager

import requests

from sample.confidential_client_mtls_http_client_sample import PolicyHttpClient
from tests.mtls_e2e_helpers import token_request_summary


class _CustomTokenTransportError(AssertionError):
    pass


@contextmanager
def record_custom_transport_failures():
    """Drop upper OAuth frames only for this recorder's transport failures."""
    __tracebackhide__ = True
    try:
        yield
    except _CustomTokenTransportError:
        raise AssertionError(
            "The custom token request transport failed before receiving a response."
        ) from None


class RecordingPolicyHttpClient(PolicyHttpClient):
    """Add safe recording to the existing sample's real certificate pipeline."""

    def __init__(self, binding_context=None, expected_binding=None,
                 expected_assertion=None, **kwargs):
        __tracebackhide__ = True
        self.binding_context = binding_context
        self.expected_binding = expected_binding or binding_context
        self.expected_assertion = expected_assertion
        self.token_requests = []
        super().__init__(binding_context=binding_context, **kwargs)

    def create_mtls_client(self, binding_context):
        with self._lock:
            self._check_open()
            child = RecordingPolicyHttpClient(
                binding_context=binding_context,
                expected_binding=self.expected_binding,
                expected_assertion=self.expected_assertion, **self._config)
            self._children.append(child)
            return child

    def post(self, url, **kwargs):
        __tracebackhide__ = True
        try:
            response = super().post(url, **kwargs)
        except requests.exceptions.RequestException:
            raise _CustomTokenTransportError(
                "The custom token request transport failed before receiving a response."
            ) from None
        summary = token_request_summary(
            response, self.expected_binding,
            self._session.get_adapter(response.request.url),
            expected_assertion=self.expected_assertion)
        if summary is not None:
            self.token_requests.append(summary)
        return response
