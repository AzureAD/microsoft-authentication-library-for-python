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
        if headers:
            # Early versions of MSAL did not require http response to contain headers.
            # As of April 2025, some Azure Identity code paths still yield response without headers.
            # Here we mimic the behavior of header-less response by default,
            # so that test cases can cover header-less response scenarios.
            self.headers = headers
        self._raw_resp = requests_resp

    def raise_for_status(self):
        if self._raw_resp is not None:  # Turns out `if requests.response` won't work
                                        # cause it would be True when 200<=status<400
            self._raw_resp.raise_for_status()


class RecordingHttpClient(object):
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self._get_routes = []
        self._post_routes = []

    def add_get_route(self, matcher, responder):
        self._get_routes.append((matcher, responder))

    def add_post_route(self, matcher, responder):
        self._post_routes.append((matcher, responder))

    def get(self, url, params=None, headers=None, **kwargs):
        call = {
            "url": url,
            "params": params,
            "headers": headers,
            "kwargs": kwargs,
        }
        self.get_calls.append(call)
        for matcher, responder in self._get_routes:
            if matcher(call):
                return responder(call)
        return MinimalResponse(status_code=404, text="")

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        call = {
            "url": url,
            "params": params,
            "data": data,
            "headers": headers,
            "kwargs": kwargs,
        }
        self.post_calls.append(call)
        for matcher, responder in self._post_routes:
            if matcher(call):
                return responder(call)
        return MinimalResponse(status_code=404, text="")
