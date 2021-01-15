#!/usr/bin/env python
from setuptools import setup
import re, io

# setup.py shall not import its target package
__version__ = re.search(
    r'__version__\s*=\s*[\'"]([^\'"]*)[\'"]',  # It excludes inline comment too
    io.open('oauth2cli/__init__.py', encoding='utf_8_sig').read()
    ).group(1)

setup(
    name='oauth2cli',
    version=__version__,
    description=('The generic, spec-compliant OAuth2 client-side library, '
        'with callbacks for token storage.'),
    license='MIT',
    author='Ray Luo',
    author_email='rayluo.mba@gmail.com',
    url='https://github.com/rayluo/oauth2cli',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'License :: OSI Approved :: MIT License',
    ],
    packages=['oauth2cli'],
    install_requires=[
        'requests>=2.0.0,<3',
        'PyJWT>=1.0.0,<3',
    ]
)
