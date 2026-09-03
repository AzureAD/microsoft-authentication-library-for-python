"""Manual smoke test for the Intel-Mac broker-disable gate.

This script is intentionally credential-free and runs in a few seconds.
It exercises the production decision logic in ``ClientApplication._decide_broker``
on real hardware (no mocks), which is the one thing the unit tests in
``test_application.py::TestBrokerDisabledOnIntelMac`` cannot do — CI runs on
``ubuntu-latest`` only, so ``sys.platform`` is patched there.

How to run::

    pip install --force-reinstall "msal[broker]"  # or your local checkout
    python tests/intel_mac_broker_smoke_test.py

Expected outcomes:

* Apple Silicon Mac (``arm64``) with ``pymsalruntime`` installed:
  ``_enable_broker`` is ``True``.
* Intel Mac (``x86_64`` / ``i386``), or any other non-``arm64`` machine
  (including an ``x86_64`` Python running under Rosetta):
  ``_enable_broker`` is ``False`` even though the opt-in was passed and even
  if a broker is installed on the device.
* Non-Mac (Windows, Linux):
  ``enable_broker_on_mac`` is ignored — ``_enable_broker`` is ``False``.

The script asserts the expected outcome for the host it runs on and exits
non-zero if the gate misbehaves.
"""
import platform
import sys

import msal


_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Azure CLI, public
_AUTHORITY = "https://login.microsoftonline.com/organizations"


def _expected_broker_state():
    if sys.platform != "darwin":
        return False, "non-Mac platform — enable_broker_on_mac is a no-op"
    if platform.machine() != "arm64":
        return False, "not Apple Silicon — broker disabled by product policy"
    return True, "Apple Silicon Mac — broker should be enabled"


def main():
    print(f"sys.platform       = {sys.platform!r}")
    print(f"platform.machine() = {platform.machine()!r}")

    expected, why = _expected_broker_state()
    print(f"Expected _enable_broker = {expected}  ({why})")

    app = msal.PublicClientApplication(
        _CLIENT_ID,
        authority=_AUTHORITY,
        enable_broker_on_mac=True,  # Only opt in on Mac; this script validates the Mac gate.
        )
    actual = bool(app._enable_broker)
    print(f"Actual   _enable_broker = {actual}")

    if actual != expected:
        print(
            "FAIL: Intel-Mac gate misbehaved. "
            "See ClientApplication._decide_broker in msal/application.py.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
