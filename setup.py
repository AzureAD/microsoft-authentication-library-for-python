#!/usr/bin/env python
#------------------------------------------------------------------------------
#
# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files(the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and / or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions :
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
#------------------------------------------------------------------------------

from setuptools import setup, find_packages
import re, io

# setup.py shall not import main package
__version__ = re.search(
    r'__version__\s*=\s*[\'"]([^\'"]*)[\'"]',  # It excludes inline comment too
    io.open('msal/application.py', encoding='utf_8_sig').read()
    ).group(1)

long_description = open('README.md').read()

setup(
    name='msal',
    version=__version__,
    description=' '.join(
        """The Microsoft Authentication Library (MSAL) for Python library
        enables your app to access the Microsoft Cloud
        by supporting authentication of users with
        Microsoft Azure Active Directory accounts (AAD) and Microsoft Accounts (MSA)
        using industry standard OAuth2 and OpenID Connect.""".split()),
    long_description=long_description,
    long_description_content_type="text/markdown",
    license='MIT',
    author='Microsoft Corporation',
    author_email='nugetaad@microsoft.com',
    url='https://github.com/AzureAD/microsoft-authentication-library-for-python',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    packages=find_packages(exclude=["tests"]),
    package_data={'': ['LICENSE']},  # Do not use data_files=[...],
        # which would cause the LICENSE being copied to /usr/local,
        # and tend to fail because of insufficient permission.
        # See https://stackoverflow.com/a/14211600/728675 for more detail
    install_requires=[
        'requests>=2.0.0,<3',
        'PyJWT[crypto]>=1.0.0,<3',  # MSAL does not use jwt.decode(), therefore is insusceptible to CVE-2022-29217 so no need to bump to PyJWT 2.4+

        'cryptography>=0.6,<44',
            # load_pem_private_key() is available since 0.6
            # https://github.com/pyca/cryptography/blob/master/CHANGELOG.rst#06---2014-09-29
            #
            # And we will use the cryptography (X+3).0.0 as the upper bound,
            # based on their latest deprecation policy
            # https://cryptography.io/en/latest/api-stability/#deprecation

        "mock;python_version<'3.3'",
        ],
    extras_require={  # It does not seem to work if being defined inside setup.cfg
        "broker": [
            # The broker is defined as optional dependency,
            # so that downstream apps can opt in. The opt-in is needed, partially because
            # most existing MSAL Python apps do not have the redirect_uri needed by broker.
            # MSAL Python uses a subset of API from PyMsalRuntime 0.11.2+,
            # but we still bump the lower bound to 0.13.2+ for its important bugfix (https://github.com/AzureAD/microsoft-authentication-library-for-cpp/pull/3244)
            "pymsalruntime>=0.13.2,<0.14;python_version>='3.6' and platform_system=='Windows'",
            ],
        },
)

