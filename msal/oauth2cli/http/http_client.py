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
    def __init__(self, default_headers=None, verify=None, proxy=None):
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
        :rtype: A :class:`Response <twilio.rest.http.response.Response>` object
        """
        # if timeout is not None and timeout <= 0:
        #     raise ValueError(timeout)
        #
        # kwargs = {
        #     'method': method.upper(),
        #     'url': url,
        #     'params': params,
        #     'data': data,
        #     'headers': headers,
        #     'auth': auth,
        #     'hooks': self.request_hooks
        # }
        #
        # if params:
        #     self.logger.info('{method} Request: {url}?{query}'.format(query=urlencode(params), **kwargs))
        #     self.logger.info('PARAMS: {params}'.format(**kwargs))
        # else:
        #     self.logger.info('{method} Request: {url}'.format(**kwargs))
        # if data:
        #     self.logger.info('PAYLOAD: {data}'.format(**kwargs))
        #
        # self.last_response = None
        session = self.session or Session()
        if method == "POST":
            response = session.post(headers=headers, params=params, data=data, auth=auth,
            timeout=timeout, **kwargs)
        elif method == "GET":
            response = requests.get(url= url, headers=headers, params=params, timeout=timeout)

        self.last_response = Response(int(response.status_code), response.text)

        return self.last_response