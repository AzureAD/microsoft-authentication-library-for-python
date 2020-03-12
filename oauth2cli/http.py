"""This module documents the minimal http behaviors used by this package.

Its interface is influenced by, and similar to a subset of some popular,
real-world http libraries, such as requests, aiohttp and httpx.
"""


class HttpClient(object):
    """This describes a minimal http request interface used by this package."""

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        """HTTP post.

        params, data and headers MUST accept a dictionary.
        It returns an :class:`~Response`-like object.

        Note: In its async counterpart, this method would be defined as async.
        """
        return Response()

    def get(self, url, params=None, headers=None, **kwargs):
        """HTTP get.

        params, data and headers MUST accept a dictionary.
        It returns an :class:`~Response`-like object.

        Note: In its async counterpart, this method would be defined as async.
        """
        return Response()


class Response(object):
    """This describes a minimal http response interface used by this package.

    :var int status_code:
        The status code of this http response.

        Our async code path would also accept an alias as "status".

    :var string text:
        The body of this http response.

        Our async code path would also accept an awaitable with the same name.
    """
    status_code = 200  # Our async code path would also accept a name as "status"

    text = "body as a string"  # Our async code path would also accept an awaitable
        # We could define a json() method instead of a text property/method,
        # but a `text` would be more generic,
        # when downstream packages would potentially access some XML endpoints.

    def raise_for_status(self):
        """Raise an exception when http response status contains error"""
        raise NotImplementedError("Your implementation should provide this")


def _get_status_code(resp):
    # RFC defines and some libraries use "status_code", others use "status"
    return getattr(resp, "status_code", None) or resp.status

