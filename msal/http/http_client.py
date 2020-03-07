import logging

from requests import Session

from .http_response import Response
logger = logging.getLogger(__name__)


class HttpClient(object):
    """
    An abstract class representing an HTTP client.
    """
    def request(self, method, url, params=None, data=None, headers=None, auth=None,
                timeout=None, allow_redirects=False):
        """
        Makes an HTTP Request with parameters provided.

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
        :rtype: A :class:`Response <http.http_response.Response>` object
        """


class DefaultHttpClient(HttpClient):
    """
    Default HTTP Client
    """
    def __init__(self, verify=True, proxies=None):
        """
        Constructor for the DefaultHttpClient

        :param verify: (optional)
            It will be passed to the
            `verify parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#ssl-cert-verification>`_
        :param proxies: (optional)
            It will be passed to the
            `proxies parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#proxies>`_
        """
        self.session = Session()
        self.session.verify = verify
        self.session.proxies = proxies

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None,
                allow_redirects=False, **kwargs):

        if method == "POST":
            response = self.session.post(url=url, headers=headers, params=params, data=data, auth=auth,
                                         timeout=timeout, **kwargs)
        elif method == "GET":
            response = self.session.get(url=url, headers=headers, params=params, timeout=timeout, data=data, auth=auth)

        content = response.text
        response = Response(int(response.status_code), content)
        return response
