import requests

from .http import HttpClient, Response


class DefaultHttpClient(HttpClient):
    """
    Default HTTP Client
    """
    def __init__(self, verify=True, proxies=None):
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

    def post(self, url, params=None, data=None, headers=None, **kwargs):

        response = self.session.post(url=url, params=params, headers=headers, data=data, **kwargs)
        return Response(response)

    def get(self, url, params=None, headers=None, **kwargs):
        response = self.session.get(url=url, params=params, headers=headers, **kwargs)
        return Response(response)


class Response(Response):

    def __init__(self, response):
        """Constructor for DefaultResponseObject
            response: Raw http response from requests
        """
        self.status_code = response.status_code
        self.text = response.text
        self.response = response

    def raise_for_status(self):
        self.response.raise_for_status()
