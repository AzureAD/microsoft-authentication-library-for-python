import argparse
import webbrowser
import logging

try:  # Python 3
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs, urlencode
except ImportError:  # Fall back to Python 2
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    from urlparse import urlparse, parse_qs
    from urllib import urlencode


class AuthCodeReceiver(BaseHTTPRequestHandler):
    # Note: This docstring is also used by this script's command line help.
    """A one-stop helper for desktop app to acquire an authorization code.

    It starts a web server to listen redirect_uri, waiting for auth code.
    It also opens a browser window to guide a human user to manually login.
    After obtaining an auth code, the web server will automatically shut down.
    """
    @classmethod
    def acquire(cls, auth_endpoint, redirect_port):
        """Usage: ac = AuthCodeReceiver.acquire('http://.../authorize', 8088)"""
        webbrowser.open(  # This becomes NO-OP if there is no local browser
            "http://localhost:{p}?{q}".format(p=redirect_port, q=urlencode({
                "text": """Open this link to acquire auth code.
                    If you prefer, you may want to use incognito window.""",
                "link": auth_endpoint,})))
        logging.warn(
            """Listening on http://localhost:{}, and a browser window is opened
            for you on THIS machine, and waiting for human interaction.
            This function call will hang until an auth code is received.
            """.format(redirect_port))
        server = HTTPServer(("", int(redirect_port)), cls)
        server.authcode = None
        while not server.authcode:
            # https://docs.python.org/2/library/basehttpserver.html#more-examples
            server.handle_request()
        return server.authcode

    def do_GET(self):
        # For flexibility, we choose to not check self.path matching redirect_uri
        #assert self.path.startswith('/THE_PATH_REGISTERED_BY_THE_APP')
        qs = parse_qs(urlparse(self.path).query)
        if qs.get('code'):  # Then store it into the server instance
            ac = self.server.authcode = qs['code'][0]
            self._send_full_response('Authcode:\n{}'.format(ac))
            # NOTE: Don't do self.server.shutdown() here. It'll halt the server.
        elif qs.get('text') and qs.get('link'):  # Then display a landing page
            self._send_full_response('<a href={link}>{text}</a>'.format(
                link=qs['link'][0], text=qs['text'][0]))
        else:
            self._send_full_response("This web service serves your redirect_uri")

    def _send_full_response(self, body, is_ok=True):
        self.send_response(200 if is_ok else 400)
        content_type = 'text/html' if body.startswith('<') else 'text/plain'
        self.send_header('Content-type', content_type)
        self.end_headers()
        self.wfile.write(body)

def build_auth_endpoint(endpoint, client_id):
    return "{e}?response_type=code&client_id={c}".format(e=endpoint, c=client_id)

if __name__ == '__main__':
    p = parser = argparse.ArgumentParser(
        description=AuthCodeReceiver.__doc__
        + "The auth code received will be dumped into stdout.")
    p.add_argument('endpoint',
        help="The auth endpoint for your app. For example: "
            "https://login.microsoftonline.com/your_tenant/oauth2/authorize")
    p.add_argument('client_id', help="The client_id of your web service app")
    p.add_argument('redirect_port', type=int, help="The port in redirect_uri")
    args = parser.parse_args()
    print(AuthCodeReceiver.acquire(
        build_auth_endpoint(args.endpoint, args.client_id), args.redirect_port))

