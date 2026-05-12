# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Abstract HTTP-API connector with shared transport plumbing.

Every HTTP-API connector (vSphere, NSX, Harbor, Hetzner Robot, etc.)
inherits :class:`HttpConnector` and overrides ``auth_headers()`` plus the
three ABC methods (``fingerprint``, ``probe``, ``execute``).

**Retry policy:** Three retries on idempotent verbs (GET, HEAD, OPTIONS)
with exponential backoff (0.5 s → 1 s → 2 s). The ``_request_json`` helper
carries the retry decorator; non-idempotent callers must bypass it.
``_retryable`` allows retries only on connection errors and 5xx responses —
4xx responses represent caller/auth errors that retrying would not fix.

**Client pooling:** Each :class:`HttpConnector` instance owns a dict of
``httpx.AsyncClient`` keyed by ``target.name``. The client is created lazily
on first use and reused across all operations against the same target.

**Cert-bundle support:** httpx honours ``SSL_CERT_FILE`` natively; no extra
cert logic is needed here beyond not overriding the default ``verify`` flag.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.connectors.base import Connector

logger = structlog.get_logger()

# Forward declaration — replaced with `from meho_backplane.targets import Target`
# once G0.3 lands the Target model.
type Target = Any

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _retryable(exc: BaseException) -> bool:
    """Retry on connection errors and 5xx; never on 4xx."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


class HttpConnector(Connector):
    """Abstract HTTP-API connector with retry/timeout/cert-bundle support.

    Subclasses MUST override ``auth_headers()`` and the three ABC methods
    (``fingerprint``, ``probe``, ``execute``).
    """

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def _http_client(self, target: Target) -> httpx.AsyncClient:
        """Return the per-target pooled client, creating it on first use."""
        async with self._lock:
            if target.name not in self._clients:
                self._clients[target.name] = httpx.AsyncClient(
                    base_url=self._base_url(target),
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
                )
            return self._clients[target.name]

    def _base_url(self, target: Target) -> str:
        scheme = "https"
        port = f":{target.port}" if target.port and target.port != 443 else ""
        return f"{scheme}://{target.host}{port}"

    async def auth_headers(self, target: Target, raw_jwt: str) -> dict[str, str]:
        """Return auth headers for the request.

        Vendor connectors MUST override per ``target.auth_model``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override auth_headers() — "
            f"target {target.name!r} uses {target.auth_model}"
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
        retry=retry_if_exception(_retryable),
        reraise=True,
    )
    async def _request_json(
        self,
        target: Target,
        method: str,
        path: str,
        *,
        raw_jwt: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retryable JSON request. Caller decides if retry is safe via method.

        Use for idempotent verbs (GET, HEAD, OPTIONS).  Non-idempotent callers
        should call the httpx client directly to avoid unintended side effects.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, raw_jwt)
        resp = await client.request(method, path, params=params, json=json, headers=headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _get_json(
        self,
        target: Target,
        path: str,
        *,
        raw_jwt: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON."""
        return await self._request_json(target, "GET", path, raw_jwt=raw_jwt, params=params)

    async def _post_json(
        self,
        target: Target,
        path: str,
        *,
        raw_jwt: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Non-retried POST returning parsed JSON.

        Retry on non-idempotent verbs is the caller's responsibility.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, raw_jwt)
        resp = await client.request("POST", path, json=json, headers=headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        """Close all pooled clients. Called by lifespan or per-target cleanup."""
        async with self._lock:
            for client in self._clients.values():
                await client.aclose()
            self._clients.clear()
