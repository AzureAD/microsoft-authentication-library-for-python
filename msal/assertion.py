import time
import binascii
import base64
import uuid

import jwt


def create_jwt_assertion(
        private_pem, thumbprint, audience, issuer,
        subject=None,  # If None is specified, the value of issuer will be used
        not_valid_before=None,  # If None, the current time will be used
        jwt_id=None):  # If None is specified, a UUID will be generated
    assert '-----BEGIN PRIVATE KEY-----' in private_pem, "Need a standard PEM"
    nbf = time.time() if not_valid_before is None else not_valid_before
    payload = {  # key names are all from JWT standard names
        'aud': audience,
        'iss': issuer,
        'sub': subject or issuer,
        'nbf': nbf,
        'exp': nbf + 10*60,  # 10 minutes
        'jti': str(uuid.uuid4()) if jwt_id is None else jwt_id,
        }
    # Per http://self-issued.info/docs/draft-jones-json-web-token-01.html
    h = {'x5t': base64.urlsafe_b64encode(binascii.a2b_hex(thumbprint)).decode()}
    return jwt.encode(payload, private_pem, algorithm='RS256', headers=h)

