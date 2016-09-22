class Authority(object):
    def __init__(self, authority_url, validate=True, **kwargs):
        if validate and not authority_url.lower().startswith('https'):
            raise ValueError("authority_url should start with https")
        if authority_url.endswith('/'):  # trim it
            authority_url = authority_url[:-1]
        self.authorization_endpoint = authority_url + "/oauth2/v2.0/authorize"
        self.token_endpoint = authority_url + "/oauth2/v2.0/token"

