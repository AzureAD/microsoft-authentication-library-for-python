import httpx

from .oauth2cli import HttpClient, Response


class DefaultHttpClient(HttpClient):
    """
    Default HTTP Client
    """
    def __init__(self):
        """
        Constructor for the DefaultHttpClient

        verify=True,  # type: Union[str, True, False, None]
        proxies=None,  # type: Optional[dict]
        """
        self.session = httpx.Client()

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        if params is None:
            params = {}
        params.update(**kwargs)

        response = self.session.post(url=url, params=params, headers=headers, data=data)
        return Response(response.status_code, response.text, response)

    def get(self, url, params=None, headers=None, **kwargs):
        if params is None:
            params = {}
        params.update(kwargs)
        response = self.session.get(url=url, params=params, headers=headers)
        return Response(response.status_code, response.text, response)


class Response(Response):

    def __init__(self, status_code, text, response):
        """Constructor for DefaultResponseObject
            status,  # type: int
            text,  # type: str response in string format
            response,  # type: Raw response from requests
        """
        self.status_code = status_code
        self.text = text
        self.response = response


    def raise_for_status(self):
        self.response.raise_for_status()
