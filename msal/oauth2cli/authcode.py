# Note: This docstring is also used by this script's command line help.
"""A one-stop helper for desktop app to acquire an authorization code.

It starts a web server to listen redirect_uri, waiting for auth code.
It optionally opens a browser window to guide a human user to manually login.
After obtaining an auth code, the web server will automatically shut down.
"""
import logging
import socket
from string import Template

try:  # Python 3
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs, urlencode
except ImportError:  # Fall back to Python 2
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    from urlparse import urlparse, parse_qs
    from urllib import urlencode


logger = logging.getLogger(__name__)


def obtain_auth_code(listen_port, auth_uri=None):  # Historically only used in testing
    with AuthCodeReceiver(port=listen_port) as receiver:
        return receiver.get_auth_response(
            auth_uri=auth_uri,
            welcome_template="""<html><body>
                Open this link to <a href='$auth_uri'>Sign In</a>
                (You may want to use incognito window)
                <hr><a href='$abort_uri'>Abort</a>
                </body></html>""",
            ).get("code")


def _browse(auth_uri):
    import webbrowser  # Lazy import. Some distro may not have this.
    controller = webbrowser.get()  # Get a default controller
    # Some Linux Distro does not setup default browser properly,
    # so we try to explicitly use some popular browser, if we found any.
    for browser in ["chrome", "firefox", "safari", "windows-default"]:
        try:
            controller = webbrowser.get(browser)
            break
        except webbrowser.Error:
            pass  # This browser is not installed. Try next one.
    logger.info("Please open a browser on THIS device to visit: %s" % auth_uri)
    controller.open(auth_uri)


def _qs2kv(qs):
    """Flatten parse_qs()'s single-item lists into the item itself"""
    return {k: v[0] if isinstance(v, list) and len(v) == 1 else v
        for k, v in qs.items()}


class _AuthCodeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # For flexibility, we choose to not check self.path matching redirect_uri
        #assert self.path.startswith('/THE_PATH_REGISTERED_BY_THE_APP')
        qs = parse_qs(urlparse(self.path).query)
        if qs.get('code') or qs.get("error"):  # So, it is an auth response
            self.server.auth_response = _qs2kv(qs)
            logger.debug("Got auth response: %s", self.server.auth_response)
            template = (self.server.success_template
                if "code" in qs else self.server.error_template)
            self._send_full_response(
                template.safe_substitute(**self.server.auth_response))
            # NOTE: Don't do self.server.shutdown() here. It'll halt the server.
        else:
            self._send_full_response(self.server.welcome_page)

    def _send_full_response(self, body, is_ok=True):
        self.send_response(200 if is_ok else 400)
        content_type = 'text/html' if body.startswith('<') else 'text/plain'
        self.send_header('Content-type', content_type)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        logger.debug(format, *args)  # To override the default log-to-stderr behavior


class _AuthCodeHttpServer(HTTPServer):
    def handle_timeout(self):
        # It will be triggered when no request comes in self.timeout seconds.
        # See https://docs.python.org/3/library/socketserver.html#socketserver.BaseServer.handle_timeout
        raise RuntimeError("Timeout. No auth response arrived.")  # Terminates this server
            # We choose to not call self.server_close() here,
            # because it would cause a socket.error exception in handle_request(),
            # and likely end up the server being server_close() twice.


class _AuthCodeHttpServer6(_AuthCodeHttpServer):
    address_family = socket.AF_INET6


class AuthCodeReceiver(object):
    # This class has (rather than is) an _AuthCodeHttpServer, so it does not leak API
    def __init__(self, port=None):
        """Create a Receiver waiting for incoming auth response.

        :param port:
            The local web server will listen at http://...:<port>
            You need to use the same port when you register with your app.
            If your Identity Provider supports dynamic port, you can use port=0 here.
            Port 0 means to use an arbitrary unused port, per this official example:
            https://docs.python.org/2.7/library/socketserver.html#asynchronous-mixins
        """
        address = "127.0.0.1"  # Hardcode, for now, Not sure what to expose, yet.
            # Per RFC 8252 (https://tools.ietf.org/html/rfc8252#section-8.3):
            #   * Clients should listen on the loopback network interface only.
            #     (It is not recommended to use "" shortcut to bind all addr.)
            #   * the use of localhost is NOT RECOMMENDED.
            #     (Use) the loopback IP literal
            #     rather than localhost avoids inadvertently listening on network
            #     interfaces other than the loopback interface.
            # Note:
            #   When this server physically listens to a specific IP (as it should),
            #   you will still be able to specify your redirect_uri using either
            #   IP (e.g. 127.0.0.1) or localhost, whichever matches your registration.
        Server = _AuthCodeHttpServer6 if ":" in address else _AuthCodeHttpServer
            # TODO: But, it would treat "localhost" or "" as IPv4.
            # If pressed, we might just expose a family parameter to caller.
        self._server = Server((address, port or 0), _AuthCodeHandler)

    def get_port(self):
        """The port this server actually listening to"""
        # https://docs.python.org/2.7/library/socketserver.html#SocketServer.BaseServer.server_address
        return self._server.server_address[1]

    def get_auth_response(self, auth_uri=None, timeout=None, state=None,
            welcome_template=None, success_template=None, error_template=None):
        """Wait and return the auth response, or None when timeout.

        :param str auth_uri:
            If provided, this function will try to open a local browser.
        :param int timeout: In seconds. None means wait indefinitely.
        :param str state:
            You may provide the state you used in auth_url,
            then we will use it to validate incoming response.
        :param str welcome_template:
            If provided, your end user will see it instead of the auth_uri.
            When present, it shall be a plaintext or html template following
            `Python Template string syntax <https://docs.python.org/3/library/string.html#template-strings>`_,
            and include some of these placeholders: $auth_uri and $abort_uri.
        :param str success_template:
            The page will be displayed when authentication was largely successful.
            Placeholders can be any of these:
            https://tools.ietf.org/html/rfc6749#section-5.1
        :param str error_template:
            The page will be displayed when authentication encountered error.
            Placeholders can be any of these:
            https://tools.ietf.org/html/rfc6749#section-5.2
        :return:
            The auth response of the first leg of Auth Code flow,
            typically {"code": "...", "state": "..."} or {"error": "...", ...}
            See https://tools.ietf.org/html/rfc6749#section-4.1.2
            and https://openid.net/specs/openid-connect-core-1_0.html#AuthResponse
            Returns None when the state was mismatched, or when timeout occurred.
        """
        welcome_uri = "http://localhost:{p}".format(p=self.get_port())
        abort_uri = "{loc}?error=abort".format(loc=welcome_uri)
        logger.debug("Abort by visit %s", abort_uri)
        self._server.welcome_page = Template(welcome_template or "").safe_substitute(
            auth_uri=auth_uri, abort_uri=abort_uri)
        if auth_uri:
            _browse(welcome_uri if welcome_template else auth_uri)
        self._server.success_template = Template(success_template or
            "Authentication completed. You can close this window now.")
        self._server.error_template = Template(error_template or
            "Authentication failed. $error: $error_description. ($error_uri)")

        self._server.timeout = timeout  # Otherwise its handle_timeout() won't work
        self._server.auth_response = {}  # Shared with _AuthCodeHandler
        while True:
            # Derived from
            # https://docs.python.org/2/library/basehttpserver.html#more-examples
            self._server.handle_request()
            if self._server.auth_response:
                if state and state != self._server.auth_response.get("state"):
                    logger.debug("State mismatch. Ignoring this noise.")
                else:
                    break
        return self._server.auth_response

    def close(self):
        """Either call this eventually; or use the entire class as context manager"""
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

# Note: Manually use or test this module by:
#       python -m path.to.this.file -h
if __name__ == '__main__':
    import argparse, json
    from .oauth2 import Client
    logging.basicConfig(level=logging.INFO)
    p = parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=__doc__ + "The auth code received will be shown at stdout.")
    p.add_argument(
        '--endpoint', help="The auth endpoint for your app.",
        default="https://login.microsoftonline.com/common/oauth2/v2.0/authorize")
    p.add_argument('client_id', help="The client_id of your application")
    p.add_argument('--port', type=int, default=0, help="The port in redirect_uri")
    p.add_argument('--host', default="127.0.0.1", help="The host of redirect_uri")
    p.add_argument('--scope', default=None, help="The scope list")
    args = parser.parse_args()
    client = Client({"authorization_endpoint": args.endpoint}, args.client_id)
    with AuthCodeReceiver(port=args.port) as receiver:
        flow = client.initiate_auth_code_flow(
            scope=args.scope.split() if args.scope else None,
            redirect_uri="http://{h}:{p}".format(h=args.host, p=receiver.get_port()),
            )
        print(json.dumps(receiver.get_auth_response(
            auth_uri=flow["auth_uri"],
            welcome_template=
                "<a href='$auth_uri'>Sign In</a>, or <a href='$abort_uri'>Abort</a",
            error_template="Oh no. $error",
            success_template="Oh yeah. Got $code",
            timeout=60,
            state=flow["state"],  # Optional
            ), indent=4))

