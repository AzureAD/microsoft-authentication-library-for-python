# Note: This docstring is also used by this script's command line help.
"""A one-stop helper for desktop app to acquire an authorization code.

It starts a web server to listen redirect_uri, waiting for auth code.
It optionally opens a browser window to guide a human user to manually login.
After obtaining an auth code, the web server will automatically shut down.
"""
import webbrowser
import logging
import socket

try:  # Python 3
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs, urlencode
except ImportError:  # Fall back to Python 2
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    from urlparse import urlparse, parse_qs
    from urllib import urlencode


logger = logging.getLogger(__name__)


def obtain_auth_code(listen_port, auth_uri=None):  # For backward compatibility
    with AuthCodeReceiver(port=listen_port) as receiver:
        return receiver.get_auth_response(
            auth_uri=auth_uri,
            text="Open this link to sign in. You may use incognito window",
            ).get("code")


def _browse(auth_uri):
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


class _AuthCodeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # For flexibility, we choose to not check self.path matching redirect_uri
        #assert self.path.startswith('/THE_PATH_REGISTERED_BY_THE_APP')
        qs = parse_qs(urlparse(self.path).query)
        if qs.get('code') or qs.get("error"):  # So, it is an auth response
            # Then store it into the server instance
            self.server.auth_response = qs
            logger.debug("Got auth response: %s", qs)
            self._send_full_response(
                'Authentication complete. You can close this window.')
            # NOTE: Don't do self.server.shutdown() here. It'll halt the server.
        elif qs.get('text') and qs.get('link'):  # Then display a landing page
            self._send_full_response(
                '<a href={link}>{text}</a><hr/>{exit_hint}'.format(
                link=qs['link'][0], text=qs['text'][0],
                exit_hint=qs.get("exit_hint", [''])[0],
                ))
        else:
            self._send_full_response("This web service serves your redirect_uri")

    def _send_full_response(self, body, is_ok=True):
        self.send_response(200 if is_ok else 400)
        content_type = 'text/html' if body.startswith('<') else 'text/plain'
        self.send_header('Content-type', content_type)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


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

    def get_auth_response(self, auth_uri=None, text=None, timeout=None, state=None):
        """Wait and return the auth response, or None when timeout.

        :param str auth_uri:
            If provided, this function will try to open a local browser.
        :param str text:
            If provided (together with auth_uri),
            this function will render a landing page with ``text`` in your browser.
            This can be used to make testing more readable.
        :param int timeout: In seconds. None means wait indefinitely.
        :param str state:
            You may provide the state you used in auth_url,
            then we will use it to validate incoming response.
        :return:
            The auth response of the first leg of Auth Code flow,
            typically {"code": "...", "state": "..."} or {"error": "...", ...}
            See https://tools.ietf.org/html/rfc6749#section-4.1.2
            and https://openid.net/specs/openid-connect-core-1_0.html#AuthResponse
            Returns None when the state was mismatched, or when timeout occurred.
        """
        location = "http://localhost:{p}".format(p=self.get_port())  # For testing
        exit_hint = "Abort by visit {loc}?error=abort".format(loc=location)
        logger.debug(exit_hint)
        if auth_uri:
            page = "{loc}?{q}".format(loc=location, q=urlencode({
                "text": text,
                "link": auth_uri,
                "exit_hint": exit_hint,
                })) if text else auth_uri
            _browse(page)

        self._server.timeout = timeout  # Otherwise its handle_timeout() won't work
        self._server.auth_response = {}  # Shared with _AuthCodeHandler
        while True:
            # Derived from
            # https://docs.python.org/2/library/basehttpserver.html#more-examples
            self._server.handle_request()
            if self._server.auth_response:
                if state and state != self._server.auth_response.get("state", [None])[0]:
                    logger.debug("State mismatch. Ignoring this noise.")
                else:
                    break
        return {  # Normalize unnecessary lists into single values
            k: v[0] if isinstance(v, list) and len(v) == 1 else v
            for k, v in self._server.auth_response.items()}

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
        auth_uri = client.build_auth_request_uri(
            "code",
            scope=args.scope.split() if args.scope else None,
            redirect_uri="http://{h}:{p}".format(h=args.host, p=receiver.get_port()))
        print(json.dumps(receiver.get_auth_response(
            auth_uri=auth_uri,
            text="Open this link to sign in. You may use incognito window",
            timeout=60,
            ), indent=4))

