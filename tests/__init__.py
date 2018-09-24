import sys
if sys.version_info[:2] < (2, 7):
    # The unittest module got a significant overhaul in Python 2.7,
    # so if we're in 2.6 we can use the backported version unittest2.
    import unittest2 as unittest
else:
    import unittest

