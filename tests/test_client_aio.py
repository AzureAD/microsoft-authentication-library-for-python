import os
import logging
import asyncio

import aiohttp
#import httpx

from oauth2cli.aio.oidc import Client
from tests import unittest
from .test_client import load_conf
from .test_client import TestClient as SyncTestClient


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__file__)

CONFIG_FILENAME = "config.json"
THIS_FOLDER = os.path.dirname(__file__)
CONFIG = load_conf(os.path.join(THIS_FOLDER, CONFIG_FILENAME)) or {}


# Since the OAuth2 specs uses snake_case, this test config also uses snake_case
@unittest.skipUnless("client_id" in CONFIG, "client_id missing")
class TestClient(SyncTestClient):
    """We inherit all test_foo() in base class, and _run() them in async way"""

    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.get_event_loop()  # Create loop before coroutines,
            # to avoid error in here https://stackoverflow.com/questions/60066383

        cls.session = aiohttp.ClientSession()  # TODO: initialize in async method?
        #cls.session = httpx.AsyncClient()

        cls.client = cls.create_client(Client, cls.session)

    @classmethod
    def tearDownClass(cls):
        _close = getattr(cls.session, "close",  # when using aiohttp
            getattr(cls.session, "aclose", None))  # when using httpx
        if _close:
            cls._run(_close())

    @classmethod
    def _run(cls, coro):
        return cls.loop.run_until_complete(coro)

    async def _sleep(self, n):
        await asyncio.sleep(n)

