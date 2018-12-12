import sys
import logging

if sys.version_info[:2] < (2, 7):
    # The unittest module got a significant overhaul in Python 2.7,
    # so if we're in 2.6 we can use the backported version unittest2.
    import unittest2 as unittest
else:
    import unittest


class Oauth2TestCase(unittest.TestCase):

    logger = logging.getLogger(__file__)

    def assertLoosely(self, response, assertion=None,
            skippable_errors=("invalid_grant", "interaction_required")):
        if response.get("error") in skippable_errors:
            self.logger.debug("Response = %s", response)
            # Some of these errors are configuration issues, not library issues
            raise unittest.SkipTest(response.get("error_description"))
        else:
            if assertion is None:
                assertion = lambda: self.assertIn("access_token", response)
            assertion()

