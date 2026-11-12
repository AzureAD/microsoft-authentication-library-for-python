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
if not (root / "msal_native_primitives" / "lib" / "native-manifest.json").is_file():
    raise RuntimeError("Stage verified candidate artifacts with tools\\build_mi_native_wheels.ps1 first.")

setup(
    name="msal-native-primitives", version="0.3.0.dev0",
    description="Local Windows x64 candidate for MSAL opaque CNG and bounded Rust mTLS",
    packages=["msal_native_primitives"],
    package_data={"msal_native_primitives": ["lib/*.dll", "lib/native-manifest.json"]},
    python_requires=">=3.9", cmdclass={"bdist_wheel": WindowsWheel},
)
