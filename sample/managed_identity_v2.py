"""Public, installed-candidate samples. Never print tokens or resource values."""

import argparse
import asyncio

import msal
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pop", "async-pop", "bearer"))
    parser.add_argument("--resource", required=True, help="OAuth audience, e.g. https://vault.azure.net")
    parser.add_argument("--url", required=True, help="Authorized resource URL for the selected mode")
    parser.add_argument("--client-id", help="Optional user-assigned managed identity client GUID")
    args = parser.parse_args()
    identity = (msal.UserAssignedManagedIdentity(client_id=args.client_id) if args.client_id else
                msal.SystemAssignedManagedIdentity())
    with requests.Session() as metadata:
        client = msal.ManagedIdentityClient(identity, http_client=metadata)
        result = client.acquire_token_for_client(
            resource=args.resource, with_attestation_support=True,
            **({"request_over_mtls": True} if args.mode == "bearer" else
               {"mtls_proof_of_possession": True}))
        headers = {"Authorization": result["token_type"] + " " + result["access_token"]}
        if args.mode != "bearer":
            headers["x-ms-tokenboundauth"] = "true"
        if args.mode == "bearer":
            with requests.Session() as ordinary:
                response = ordinary.get(args.url, headers=headers, timeout=30, allow_redirects=False)
        elif args.mode == "pop":
            with result["binding_certificate"].open_session(resource_origin=args.url) as session:
                response = session.get(args.url, headers=headers, timeout=30)
        else:
            async def send():
                async with result["binding_certificate"].open_async_session(
                        resource_origin=args.url, max_concurrency=4) as session:
                    return await session.get(args.url, headers=headers, timeout=30)
            response = asyncio.run(send())
        if not 200 <= response.status_code < 300:
            raise RuntimeError("The resource operation failed (HTTP {}).".format(response.status_code))
        print("Resource request succeeded using " + args.mode + ".")


if __name__ == "__main__":
    main()
