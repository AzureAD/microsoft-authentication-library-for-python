import requests


class MinimalHttpClient:

    def __init__(self, verify=True, proxies=None, timeout=None):
        self.session = requests.Session()
        self.session.verify = verify
        self.session.proxies = proxies
        self.timeout = timeout

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        return self.session.post(
            url, params=params, data=data, headers=headers,
            timeout=self.timeout)

    def get(self, url, params=None, headers=None, **kwargs):
        return self.session.get(
            url, params=params, headers=headers, timeout=self.timeout)
