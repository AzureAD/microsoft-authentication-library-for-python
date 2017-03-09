import argparse
import json
import logging
import getpass

from msal.oauth2 import Client
from msal.authority import Authority
from msal.application import ConfidentialClientApplication


parser = argparse.ArgumentParser()
parser.add_argument("client_id")
parser.add_argument("--credential", help="If not given in CLI, you will be prompted")
parser.add_argument("--authority", default="https://login.microsoftonline.com/common")
parser.add_argument("--scope")
parser.add_argument("--verbose", action="store_true")
args = parser.parse_args()

if args.verbose:
    logging.basicConfig(level=logging.DEBUG)

credential = args.credential or getpass.getpass()

c = Client(
    args.client_id,
    client_secret=credential,
    token_endpoint=Authority(args.authority).token_endpoint)
token = c.acquire_token_with_client_credentials(args.scope)
print(json.dumps(token, indent=2))

app = ConfidentialClientApplication(
    args.client_id, credential, authority=args.authority)
token = app.acquire_token_for_client(args.scope)
print(json.dumps(token, indent=2))

