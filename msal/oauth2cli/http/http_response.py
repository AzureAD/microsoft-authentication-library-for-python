class Response(object):
    """

    """
    def __init__(self, status_code, text):
        self.content = text
        self.cached = False
        self.status_code = status_code
        self.ok = self.status_code < 400

    @property
    def text(self):
        return self.content
