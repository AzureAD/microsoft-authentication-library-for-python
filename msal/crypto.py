from cryptography.hazmat.primitives.asymmetric import rsa


def _urlsafe_b64encode(n:int, bit_size:int) -> str:
    from base64 import urlsafe_b64encode
    return urlsafe_b64encode(n.to_bytes(
        length=int(bit_size/8),
        byteorder="big",
        )).decode("utf-8").rstrip("=")


def _to_jwk(public_key: rsa.RSAPublicKey) -> dict:
    """Equivalent to:

    numbers = public_key.public_numbers()
    result = {
        "kty": "RSA",
        "n": _urlsafe_b64encode(numbers.n, public_key.key_size),
        "e": _urlsafe_b64encode(numbers.e, 24),
        }
    return result
    """
    import jwt
    return jwt.get_algorithm_by_name(  # PyJWT 2.5.0 https://github.com/jpadilla/pyjwt/releases/tag/2.5.0
        "RS256"
        ).to_jwk(
            public_key,
            as_dict=True,  # PyJWT 2.7.0 https://github.com/jpadilla/pyjwt/releases/tag/2.7.0
        )

def _convert_rsa_keys(private_key: rsa.RSAPrivateKey):
    return "pairs.private_bytes()", _to_jwk(private_key.public_key())

def _generate_rsa_key() -> rsa.RSAPrivateKey:
    # https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/#cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)

