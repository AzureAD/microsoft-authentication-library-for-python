import unittest
from msal.cloudshell import _scope_to_resource

class TestScopeToResource(unittest.TestCase):

    def test_expected_behaviors(self):
        for scope, expected_resource in {
            "https://analysis.windows.net/powerbi/api/foo":
                "https://analysis.windows.net/powerbi/api",  # A special case
            "https://pas.windows.net/CheckMyAccess/Linux/.default":
                "https://pas.windows.net/CheckMyAccess/Linux/.default",  # Special case
            "https://double-slash.com//scope": "https://double-slash.com/",
            "https://single-slash.com/scope": "https://single-slash.com",
            "guid/some/scope": "guid",
            "797f4846-ba00-4fd7-ba43-dac1f8f63013/.default":  # Realistic GUID
                "797f4846-ba00-4fd7-ba43-dac1f8f63013"
        }.items():
            self.assertEqual(_scope_to_resource(scope), expected_resource)

if __name__ == '__main__':
    unittest.main()
