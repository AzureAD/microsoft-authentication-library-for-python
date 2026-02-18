import os
import unittest

from tests.test_e2e import (
    E2eTestCase,
    LabBasedTestCase,
    PublicCloudScenariosTestCase,
    DeviceFlowTestCase,
    WorldWideTestCase,
    CiamTestCase,
    CiamCudTestCase,
    ArlingtonCloudTestCase,
    PopTestCase,
)


_MANUAL_E2E_ENABLED = bool(os.getenv("RUN_MANUAL_E2E"))
_MANUAL_SKIP_REASON = "Manual E2E tests. Set RUN_MANUAL_E2E=1 to run."


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class PublicCloudScenariosManualTestCase(PublicCloudScenariosTestCase):
    def test_auth_code(self):
        self.manual_test_auth_code()

    def test_auth_code_with_matching_nonce(self):
        self.manual_test_auth_code_with_matching_nonce()

    def test_auth_code_with_mismatching_nonce(self):
        self.manual_test_auth_code_with_mismatching_nonce()


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class DeviceFlowManualTestCase(DeviceFlowTestCase):
    def test_device_flow(self):
        self.manual_test_device_flow()


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class WorldWideManualTestCase(WorldWideTestCase):
    def test_cloud_acquire_token_interactive(self):
        self.manual_test_cloud_acquire_token_interactive()

    def test_msa_pt_app_signin_via_organizations_authority_without_login_hint(self):
        self.manual_test_msa_pt_app_signin_via_organizations_authority_without_login_hint()

    def test_b2c_acquire_token_by_auth_code(self):
        self.manual_test_b2c_acquire_token_by_auth_code()

    def test_b2c_acquire_token_by_auth_code_flow(self):
        self.manual_test_b2c_acquire_token_by_auth_code_flow()


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class CiamManualTestCase(CiamTestCase):
    def test_ciam_acquire_token_interactive(self):
        self.manual_test_ciam_acquire_token_interactive()

    def test_ciam_device_flow(self):
        self.manual_test_ciam_device_flow()


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class CiamCudManualTestCase(CiamCudTestCase):
    def test_ciam_acquire_token_interactive(self):
        self.manual_test_ciam_acquire_token_interactive()


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class ArlingtonManualTestCase(ArlingtonCloudTestCase):
    def test_acquire_token_device_flow(self):
        self.manual_test_acquire_token_device_flow()


@unittest.skipUnless(_MANUAL_E2E_ENABLED, _MANUAL_SKIP_REASON)
class PopManualTestCase(PopTestCase):
    def test_at_pop_should_contain_pop_scheme_content(self):
        self.manual_test_at_pop_should_contain_pop_scheme_content()

    def test_at_pop_via_testingsts_service(self):
        self.manual_test_at_pop_via_testingsts_service()

    def test_at_pop_calling_pattern(self):
        self.manual_test_at_pop_calling_pattern()
