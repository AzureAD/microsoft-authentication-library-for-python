from requests import Request, Session
from .http_response import Response

import requests

class HttpClient(object):
    """
    An abstract class representing an HTTP client.
    """
    def request(self, method, url, params=None, data=None, headers=None, auth=None,
                timeout=None, allow_redirects=False):
        """
        Make an HTTP request.
        """

class DefaultHttpClient(HttpClient):
    """
    General purpose HTTP Client for interacting with the Twilio API
    """
    def __init__(self, default_headers={}, verify=None, proxy=None):
        """
        Constructor for the TwilioHttpClient

        :param bool pool_connections
        :param request_hooks
        :param int timeout: Timeout for the requests.
                            Timeout should never be zero (0) or less.
        :param logger
        :param dict proxy: Http proxy for the requests session
        """
        self.session = Session()
        self.session.headers.update(default_headers or {})
        self.session.verify = verify
        self.session.proxy = proxy

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None,
                allow_redirects=False, **kwargs):
        """
        Make an HTTP Request with parameters provided.

        :param str method: The HTTP method to use
        :param str url: The URL to request
        :param dict params: Query parameters to append to the URL
        :param dict data: Parameters to go in the body of the HTTP request
        :param dict headers: HTTP Headers to send with the request
        :param tuple auth: Basic Auth arguments
        :param float timeout: Socket/Read timeout for the request
        :param boolean allow_redirects: Whether or not to allow redirects
        See the requests documentation for explanation of all these parameters

        :return: An http response
        :rtype: A :class:`Response <http.response.Response>` object
        """
        content = None
        if method == "POST":
            response = self.session.post(url=url, headers=headers, params=params, data=data, auth=auth,
                                         timeout=timeout, **kwargs)
            if response.status_code >=500:
                response.raise_for_status()
            try:
                # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
                # even an error response will be a valid json structure,
                # so we simply return it here, without needing to invent an exception.
                content = response.json()
            except ValueError:
                self.logger.exception(
                    "Token response is not in json format: %s", response.text)
                raise
        elif method == "GET":
            response = self.session.get(url=url, headers=headers, params=params, timeout=timeout, data=data, auth=auth)
            response.raise_for_status()
            content = response.json()
        response = Response(int(response.status_code), content)
        return response
