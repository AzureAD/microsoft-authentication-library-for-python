import requests

from .http import HttpClient, Response


class DefaultHttpClient(HttpClient):
    """
    Default HTTP Client
    """
    def __init__(self, verify=True, proxies=None, timeout=None):
        """
        Constructor for the DefaultHttpClient

        verify=True,  # type: Union[str, True, False, None]
        proxies=None,  # type: Optional[dict]
        """
        self.session = requests.Session()
        if verify:
            self.session.verify = verify
        if proxies:
            self.session.proxies = proxies
        if timeout:
            self.session.timeout = timeout

    def post(self, url, params=None, data=None, headers=None, **kwargs):

        response = self.session.post(url=url, params=params, headers=headers, data=data, **kwargs)
        return Response(response.status_code, response.text)

    def get(self, url, params=None, headers=None, **kwargs):
        response = self.session.get(url=url, params=params, headers=headers, **kwargs)
        return Response(response.status_code, response.text)


class Response(Response):

    def __init__(self, status_code, text):
        """HTTP Response object
            :param int status_code: Status code from HTTP response
            :param str text: HTTP response in string format
        """
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        self.text.raise_for_status()