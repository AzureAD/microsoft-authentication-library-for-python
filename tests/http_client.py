import requests


class MinimalHttpClient:

    def __init__(self, verify=True, proxies=None, timeout=None):
        self.session = requests.Session()
        self.session.verify = verify
        self.session.proxies = proxies
        self.timeout = timeout

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        assert not kwargs, "Our stack shouldn't leak extra kwargs: %s" % kwargs
        return MinimalResponse(requests_resp=self.session.post(
            url, params=params, data=data, headers=headers,
            timeout=self.timeout))

    def get(self, url, params=None, headers=None, **kwargs):
        assert not kwargs, "Our stack shouldn't leak extra kwargs: %s" % kwargs
        return MinimalResponse(requests_resp=self.session.get(
            url, params=params, headers=headers, timeout=self.timeout))

    def close(self):  # Not required, but we use it to avoid a warning in unit test
        self.session.close()


class MinimalResponse(object):  # Not for production use
    def __init__(self, requests_resp=None, status_code=None, text=None, headers=None):
        self.status_code = status_code or requests_resp.status_code
        self.text = text if text is not None else requests_resp.text
        self.headers = {} if headers is None else headers
        self._raw_resp = requests_resp

    def raise_for_status(self):
        if self._raw_resp is not None:  # Turns out `if requests.response` won't work
                                        # cause it would be True when 200<=status<400
            self._raw_resp.raise_for_status()
