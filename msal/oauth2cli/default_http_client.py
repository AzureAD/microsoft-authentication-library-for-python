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
        return self.session.post(url=url, params=params, headers=headers, data=data, **kwargs)

    def get(self, url, params=None, headers=None, **kwargs):
        return self.session.get(url=url, params=params, headers=headers, **kwargs)

