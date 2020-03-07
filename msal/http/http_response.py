class Response(object):

    def __init__(self, status_code, content):
        """HTTP Response object
            :param int status_code: Status code from HTTP response
            :param str text: HTTP response in string format
        """
        self.status_code = status_code
        self.content = content
