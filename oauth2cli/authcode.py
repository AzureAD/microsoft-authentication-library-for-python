# Note: This docstring is also used by this script's command line help.
"""A one-stop helper for desktop app to acquire an authorization code.

It starts a web server to listen redirect_uri, waiting for auth code.
It optionally opens a browser window to guide a human user to manually login.
After obtaining an auth code, the web server will automatically shut down.
"""
import webbrowser
import logging
from contextlib import contextmanager

try:  # Python 3
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs, urlencode
except ImportError:  # Fall back to Python 2
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    from urlparse import urlparse, parse_qs
    from urllib import urlencode


logger = logging.getLogger(__name__)


def obtain_auth_code(listen_port, **kwargs):  # For backward compatibility
    with AuthCodeReceiver(listen_port) as receiver:
        return receiver.get_auth_code(**kwargs)


@contextmanager
def AuthCodeReceiver(port=0):
    """This function will return a web server listening on http://localhost:port
    Such server will automatically be shut down at the end.

    :param port:
        The local web server will listen at http://localhost:<port>
        You need to use the same port when you register with your app.
        If your authorization server supports dynamic port, you can use port=0 here.
        Port 0 means to select an arbitrary unused port, per this official example:
        https://docs.python.org/2.7/library/socketserver.html#asynchronous-mixins

    :return:
        An instance of the web server.  You can then call ``server.get_auth_code()``
        which will hang until it receives and then return the auth code,
        or None when timeout.
        You need to open a browser on this device and visit your auth_uri.
    """
    server = TimedHttpServer(("", port), AuthCodeHandler)
    server.redirect_uri = "http://localhost:%d" % server.current_port()
    try:
        yield server  # Caller can then use its redirect_uri and get_auth_code()
    finally:  # Ensure the server will be closed
        server.server_close()


def browse(auth_uri):
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

class AuthCodeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # For flexibility, we choose to not check self.path matching redirect_uri
        #assert self.path.startswith('/THE_PATH_REGISTERED_BY_THE_APP')
        qs = parse_qs(urlparse(self.path).query)
        if qs.get('code'):  # Then store it into the server instance
            if self.server.state != qs.get("state", [None])[0]:
                self._send_full_response("State mismatch", is_ok=False)
            else:
                ac = self.server.authcode = qs['code'][0]
                logger.debug("Got auth code: %s", ac)
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

class AuthCodeTimeoutError(RuntimeError):
    pass

class TimedHttpServer(HTTPServer):
    """handle_timeout() will be triggered when no request comes in self.timeout seconds.
    See https://docs.python.org/3/library/socketserver.html#socketserver.BaseServer.handle_timeout
    """
    def handle_timeout(self):
        # We choose to not call self.server_close() here,
        # because it would cause a socket.error exception in handle_request(),
        # and likely end up the server being server_close() twice, which smells.
        raise AuthCodeTimeoutError()

    def current_port(self):
        # https://docs.python.org/2.7/library/socketserver.html#SocketServer.BaseServer.server_address
        return self.server_address[1]

    def get_auth_code(self, auth_uri=None, text=None, timeout=None, state=None):
        """Wait and return the auth code, or None when timeout.

        :param auth_uri: If provided, this function will try to open a local browser.
        :param text: If provided (together with auth_uri),
            this function will render a landing page with ``text``, for testing purpose.
        :param timeout: In seconds. None means wait indefinitely.
        :param state: If provided, we will ignore incoming requests with mismatched state.
            You need to make sure your auth_uri would also use the same state.
        """
        listen_port = self.current_port()
        exit_hint = "Visit http://localhost:{p}?code=exit to abort".format(p=listen_port)
        logger.debug(exit_hint)
        if auth_uri:
            page = "http://localhost:{p}?{q}".format(p=listen_port, q=urlencode({
                "text": text,
                "link": auth_uri,
                "exit_hint": exit_hint,
                })) if text else auth_uri
            browse(page)

        self.timeout = timeout
        self.state = state
        self.authcode = None
        try:
            while not self.authcode:
                # Derived from
                # https://docs.python.org/2/library/basehttpserver.html#more-examples
                self.handle_request()
            return self.authcode
        except AuthCodeTimeoutError:
            logger.info("No auth code received in last %d second(s)", self.timeout)



# Note: Manually use or test this module by:
#       python -m path.to.this.file -h
if __name__ == '__main__':
    import argparse
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
    p.add_argument('--scope', default=None, help="The scope list")
    args = parser.parse_args()
    client = Client({"authorization_endpoint": args.endpoint}, args.client_id)

    ## This pattern is for backward compatibility, only supports pre-defined port
    # print(obtain_auth_code(args.port, auth_uri=client.build_auth_request_uri(
    #     "code", scope=args.scope, redirect_uri="http://localhost:%d" % args.port)))

    # This is the recommended pattern, which supports dynamic port
    with AuthCodeReceiver(port=args.port) as receiver:
        auth_uri = client.build_auth_request_uri(
            "code", scope=args.scope, redirect_uri=receiver.redirect_uri)
        print(receiver.get_auth_code(
            auth_uri=auth_uri,
            text="Open this link to sign in. You may use incognito window",
            ))

