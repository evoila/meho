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
``httpx.AsyncClient`` keyed by the tenant-unique
:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
(``(tenant_id, id)``). The client is created lazily on first use and reused
across all operations against the same target. Keying on ``target.name``
alone would collide two same-named targets in different tenants — target
names are unique only per ``(tenant_id, name)`` — and since each pooled
client is host-bound via ``base_url``, the collision would route one
tenant's request to the other tenant's host and leak credentials across
the tenant boundary (evoila/meho#1682).

**Cert-bundle support:** httpx honours ``SSL_CERT_FILE`` natively; no extra
cert logic is needed here beyond not overriding the default ``verify`` flag.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import Connector

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
        # Keyed on the tenant-unique ``(tenant_id, id)`` tuple
        # (``target_cache_key``), not ``target.name``: each pooled client
        # is host-bound via ``base_url``, and two tenants may legitimately
        # own same-named targets pointing at different hosts. Name-keying
        # would route the second tenant's request to the first tenant's
        # host and leak credentials across the boundary (evoila/meho#1682).
        self._clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def _http_client(self, target: Target) -> httpx.AsyncClient:
        """Return the per-target pooled client, creating it on first use."""
        cache_key = target_cache_key(target)
        async with self._lock:
            if cache_key not in self._clients:
                self._clients[cache_key] = httpx.AsyncClient(
                    base_url=self._base_url(target),
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
                    # Follow 301/302/307/308. Vendor REST surfaces
                    # routinely canonicalise to a trailing slash with a
                    # 301 (the govmomi/vcsim legacy ``/rest`` mount does
                    # this; some real appliances do too behind a
                    # normalising reverse proxy). httpx defaults to
                    # not following — a connector that 500s on a benign
                    # 301 is needlessly fragile. Idempotent verbs are
                    # safe to replay on redirect; non-idempotent ones
                    # go through ``_post_json`` which the vendor APIs
                    # don't 301 mid-write.
                    follow_redirects=True,
                )
            return self._clients[cache_key]

    async def mount_op_path(self, target: Target, path: str, operator: Operator) -> str:
        """Map an ingested-descriptor *path* onto the wire path for *target*.

        Dispatcher hook for ``source_kind='ingested'`` ops: the G0.7
        pipeline stores spec-relative descriptor paths, and some vendor
        APIs expose those under a mount prefix the spec omits (vCenter
        REST: ``/api`` on modern, ``/rest`` on legacy/vcsim). The
        default is identity — most ingested APIs are reachable at the
        descriptor path verbatim. Vendor connectors that need a mount
        (see ``VmwareRestConnector``) override this; the override is a
        separate seam from ``_request_json`` / ``_post_json`` so their
        tenacity ``@retry`` wrapper stays intact.

        ``operator`` is threaded so an override that has to establish a
        session to learn the live mount (vCenter's modern→legacy
        fallback) authenticates under the same operator identity the
        transport call will use, rather than a credential-less stand-in.
        """
        del target, operator
        return path

    def _base_url(self, target: Target) -> str:
        scheme = "https"
        port = f":{target.port}" if target.port and target.port != 443 else ""
        return f"{scheme}://{target.host}{port}"

    async def auth_headers(self, target: Target, operator: Operator) -> dict[str, str]:
        """Return auth headers for the request.

        Vendor connectors MUST override per ``target.auth_model``. The
        full :class:`~meho_backplane.auth.operator.Operator` is threaded
        here (not just ``operator.raw_jwt``) so a connector's credential
        loader can perform an operator-context Vault read via
        ``vault_client_for_operator(operator)`` — the locked decision in
        [docs/architecture/connector-auth.md](docs/architecture/connector-auth.md).
        ``Operator`` is frozen, so passing it down carries no
        confused-deputy risk.
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
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retryable JSON request. Only idempotent verbs (GET, HEAD, OPTIONS).

        Raises :exc:`ValueError` for non-idempotent verbs so a caller that
        accidentally passes ``POST``/``PATCH`` never gets silent retry of a
        side-effecting operation. Non-idempotent callers must use
        ``_post_json`` or call the httpx client directly. ``operator`` is
        forwarded to :meth:`auth_headers` so the connector can resolve
        credentials under the operator's identity.
        """
        method = method.upper()
        if method not in _IDEMPOTENT_METHODS:
            raise ValueError(
                f"_request_json only accepts idempotent methods "
                f"{sorted(_IDEMPOTENT_METHODS)}; got {method!r}"
            )
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        resp = await client.request(method, path, params=params, json=json, headers=headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _get_json(
        self,
        target: Target,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON."""
        return await self._request_json(target, "GET", path, operator=operator, params=params)

    async def _post_json(
        self,
        target: Target,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Non-retried POST returning parsed JSON.

        Retry on non-idempotent verbs is the caller's responsibility.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        resp = await client.request("POST", path, json=json, headers=headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        """Close all pooled clients. Called by lifespan or per-target cleanup."""
        async with self._lock:
            for client in self._clients.values():
                await client.aclose()
            self._clients.clear()
