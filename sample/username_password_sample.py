import argparse
import json
import logging
import getpass

from msal.oauth2 import Client
from msal.authority import Authority


parser = argparse.ArgumentParser()
parser.add_argument("client_id")
parser.add_argument("username")
parser.add_argument("--password", help="If not given in CLI, you will be prompted")
parser.add_argument("--authority", default="https://login.microsoftonline.com/common")
parser.add_argument("--resource", help="Provide this to mimic the ADAL behavior")
parser.add_argument("--scope", default='openid')
parser.add_argument("--verbose", action="store_true")
args = parser.parse_args()

if args.verbose:
    logging.basicConfig(level=logging.DEBUG)

c = Client(args.client_id, token_endpoint=Authority(args.authority).token_endpoint)
token = c.acquire_token_with_username_password(
    args.username, args.password or getpass.getpass(), args.scope,
    resource=args.resource,  # Mimic the ADAL behavior
    )

print(json.dumps(token, indent=2))

