import time
import binascii
import base64
import uuid
import logging

import jwt


logger = logging.getLogger(__name__)

class Signer(object):
    def sign_assertion(
            self, audience, issuer, subject, expires_at,
            issued_at=None, assertion_id=None, **kwargs):
        # Names are defined in https://tools.ietf.org/html/rfc7521#section-5
        raise NotImplementedError("Will be implemented by sub-class")


class JwtSigner(Signer):
    def __init__(self, key, algorithm, sha1_thumbprint=None, headers=None):
        """Create a signer.

        Args:

            key (str): The key for signing, e.g. a base64 encoded private key.
            algorithm (str):
                "RS256", etc.. See https://pyjwt.readthedocs.io/en/latest/algorithms.html
                RSA and ECDSA algorithms require "pip install cryptography".
            sha1_thumbprint (str): The x5t aka X.509 certificate SHA-1 thumbprint.
            headers (dict): Additional headers, e.g. "kid" or "x5c" etc.
        """
        self.key = key
        self.algorithm = algorithm
        self.headers = headers or {}
        if sha1_thumbprint:  # https://tools.ietf.org/html/rfc7515#section-4.1.7
            self.headers["x5t"] = base64.urlsafe_b64encode(
                binascii.a2b_hex(sha1_thumbprint)).decode()

    def sign_assertion(
            self, audience, issuer, subject=None, expires_at=None,
            issued_at=None, assertion_id=None, not_before=None,
            additional_claims=None, **kwargs):
        """Sign a JWT Assertion.

        Parameters are defined in https://tools.ietf.org/html/rfc7523#section-3
        Key-value pairs in additional_claims will be added into payload as-is.
        """
        now = time.time()
        payload = {
            'aud': audience,
            'iss': issuer,
            'sub': subject or issuer,
            'exp': expires_at or (now + 10*60),  # 10 minutes
            'iat': issued_at or now,
            'jti': assertion_id or str(uuid.uuid4()),
            }
        if not_before:
            payload['nbf'] = not_before
        payload.update(additional_claims or {})
        try:
            return jwt.encode(
                payload, self.key, algorithm=self.algorithm, headers=self.headers)
        except:
            if self.algorithm.startswith("RS") or self.algorithm.starswith("ES"):
                logger.exception(
                    'Some algorithms requires "pip install cryptography". '
                    'See https://pyjwt.readthedocs.io/en/latest/installation.html#cryptographic-dependencies-optional')
            raise

