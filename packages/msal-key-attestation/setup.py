from pathlib import Path

from setuptools import setup
from wheel.bdist_wheel import bdist_wheel


class WindowsWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        return "py3", "none", "win_amd64"


root = Path(__file__).parent
if not (root / "msal_key_attestation" / "lib" / "attestation-manifest.json").is_file():
    raise RuntimeError("Stage verified candidate artifacts with tools\\build_mi_native_wheels.ps1 first.")

setup(
    name="msal-key-attestation", version="1.1.8.dev0",
    description="Local independently serviceable Windows KeyGuard attestation candidate",
    packages=["msal_key_attestation"],
    package_data={"msal_key_attestation": ["lib/*.dll", "lib/attestation-manifest.json"]},
    python_requires=">=3.9", cmdclass={"bdist_wheel": WindowsWheel},
)
