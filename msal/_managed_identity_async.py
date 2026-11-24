# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Bounded asyncio adapters for blocking managed identity v2 operations."""

import asyncio
import logging
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from typing import Optional

from ._managed_identity_v2 import _Deadline, _V2Error, _acquisition_cancellation
from .managed_identity import ManagedIdentityError


logger = logging.getLogger(__name__)


class _AsyncWorker:
    _closed_error_code = "session_closed"

    def __init__(self, concurrency):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
            raise _V2Error("Async concurrency must be an integer in [1, 32].",
                           stage="binding", error_code="unsupported_transport_option")
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="msal-mi-v2")
        self._slots = asyncio.Semaphore(concurrency)
        self._loop, self._closed, self._inflight = None, False, set()

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise _V2Error("An async managed identity session must stay on its original event loop.",
                           stage="binding", error_code="unsupported_transport_option")
        self._loop = loop
        return loop

    def _require_open(self):
        if self._closed:
            raise _V2Error("The async managed identity session is closed.",
                           stage="binding", error_code=self._closed_error_code)

    async def _run(self, operation):
        loop = self._check_loop()
        self._require_open()
        await self._slots.acquire()
        try:
            self._require_open()
            future = loop.run_in_executor(self._executor, operation)
        except BaseException:
            self._slots.release()
            raise
        self._inflight.add(future)
        future.add_done_callback(self._completed)
        try:
            # Cancelling a waiter must not release capacity still owned by native work.
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(self._abandoned)
            raise

    def _completed(self, future):
        self._inflight.discard(future)
        self._slots.release()
        if not future.cancelled():
            future.exception()

    @staticmethod
    def _abandoned(future):
        if (not future.cancelled() and future.exception() is not None
                and not isinstance(future.exception(), (CancelledError, asyncio.CancelledError))):
            logger.warning("Cancelled managed identity work failed in the background (%s).",
                           type(future.exception()).__name__)

    async def aclose(self):
        self._check_loop()
        self._closed = True
        self._executor.shutdown(wait=False)
        if self._inflight:
            await asyncio.shield(asyncio.gather(*self._inflight, return_exceptions=True))

    async def __aenter__(self):
        self._check_loop()
        self._require_open()
        return self

    async def __aexit__(self, *_exception):
        await self.aclose()


class _AsyncManagedIdentityClient(_AsyncWorker):
    def __init__(self, client, concurrency):
        super().__init__(concurrency)
        self._client = client

    async def acquire_token_for_client(
        self, *, resource: str, claims_challenge: Optional[str] = None,
        mtls_proof_of_possession: bool = False, request_over_mtls: bool = False,
        with_attestation_support: bool = False, minimum_binding_strength: Optional[int] = None,
        force_refresh: bool = False,
    ):
        """Acquire an explicit v2 token using the owning client's cache and binding policy."""
        if not mtls_proof_of_possession and not request_over_mtls:
            raise ManagedIdentityError("Async acquisition requires an explicit mTLS token mode.")
        cancellation = threading.Event()

        def acquire():
            with _acquisition_cancellation(cancellation):
                return self._client.acquire_token_for_client(
                    resource=resource, claims_challenge=claims_challenge,
                    mtls_proof_of_possession=mtls_proof_of_possession, request_over_mtls=request_over_mtls,
                    with_attestation_support=with_attestation_support, minimum_binding_strength=minimum_binding_strength,
                    force_refresh=force_refresh)

        try:
            return await self._run(acquire)
        except asyncio.CancelledError:
            cancellation.set()
            raise

    async def get_managed_identity_capabilities(self, *, timeout=None):
        """Discover capabilities without blocking the loop; the budget includes worker admission."""
        budget = _Deadline(90 if timeout is None else timeout)

        def discover():
            return self._client.get_managed_identity_capabilities(timeout=budget.remaining())

        try:
            return await asyncio.wait_for(self._run(discover), timeout=budget.remaining())
        except asyncio.TimeoutError:
            raise _V2Error("The managed identity discovery deadline expired.",
                           stage="discovery", error_code="timeout") from None
