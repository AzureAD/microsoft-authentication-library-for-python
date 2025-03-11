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
            "6dae42f8-4368-4678-94ff-3960e28e3630/.default":
                # The real guid of AKS resource
                # https://learn.microsoft.com/en-us/azure/aks/kubelogin-authentication#how-to-use-kubelogin-with-aks
                "6dae42f8-4368-4678-94ff-3960e28e3630",
        }.items():
            self.assertEqual(_scope_to_resource(scope), expected_resource)

if __name__ == '__main__':
    unittest.main()
