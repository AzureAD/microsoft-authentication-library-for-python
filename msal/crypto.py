from base64 import urlsafe_b64encode

from cryptography.hazmat.primitives.asymmetric import rsa


def _urlsafe_b64encode(n:int, bit_size:int) -> str:
    return urlsafe_b64encode(n.to_bytes(length=int(bit_size/8))).decode("utf-8")


def _to_jwk(public_key: rsa.RSAPublicKey) -> dict:
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "n": _urlsafe_b64encode(numbers.n, public_key.key_size),
        "e": _urlsafe_b64encode(numbers.e, 24),  # TODO: TBD
        }

def _convert_rsa_keys(private_key: rsa.RSAPrivateKey):
    return "pairs.private_bytes()", _to_jwk(private_key.public_key())

def _generate_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)

