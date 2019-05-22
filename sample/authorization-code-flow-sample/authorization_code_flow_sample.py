"""
The configuration file would look like this:

{
    "authority": "https://login.microsoftonline.com/organizations",
    "client_id": "your_client_id",
    "scope": ["https://graph.microsoft.com/.default"],
    "redirect_uri": "http://localhost:5000/getAToken",
        // Configure this redirect uri for this sample
        // redirect_uri should match what you've configured in here
        // https://docs.microsoft.com/en-us/azure/active-directory/develop/quickstart-configure-app-access-web-apis#add-redirect-uris-to-your-application
    "client_secret": "yoursecret"
}

You can then run this sample with a JSON configuration file:
    python sample.py parameters.json
    On the browser open http://localhost:5000/

"""

import sys  # For simplicity, we'll read config file from 1st CLI param sys.argv[1]
import json
import logging
import uuid

import flask

import msal

app = flask.Flask(__name__)
app.debug = True
app.secret_key = 'development'


# Optional logging
# logging.basicConfig(level=logging.DEBUG)

config = json.load(open(sys.argv[1]))

application = msal.ConfidentialClientApplication(
        config["client_id"], authority=config["authority"],
        client_credential=config["client_secret"],
        # token_cache=...  # Default cache is in memory only.
        # You can learn how to use SerializableTokenCache from
        # https://msal-python.rtfd.io/en/latest/#msal.SerializableTokenCache
    )


@app.route("/")
def main():
    resp = flask.Response(status=307)
    resp.headers['location'] = '/login'
    return resp


@app.route("/login")
def login():
    auth_state = str(uuid.uuid4())
    flask.session['state'] = auth_state
    authorization_url = application.get_authorization_request_url(config['scope'], state=auth_state,
                                                                  redirect_uri=config['redirect_uri'])
    resp = flask.Response(status=307)
    resp.headers['location'] = authorization_url
    return resp


@app.route("/getAToken")
def main_logic():
    code = flask.request.args['code']
    state = flask.request.args['state']
    if state != flask.session['state']:
        raise ValueError("State does not match")

    result = application.acquire_token_by_authorization_code(code, scopes=config["scope"],
                                                                 redirect_uri=config['redirect_uri'])
    return flask.render_template('display.html', auth_result=result)


if __name__ == "__main__":
    app.run()
