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

**Cert-bundle support:** httpx honours ``SSL_CERT_FILE`` natively. When a
target verifies TLS (the default, ``verify_tls=True``) the client is built
with **no** ``verify=`` argument, so the global ``SSL_CERT_FILE`` /
chart-trust-bundle path (evoila/meho#209) is in effect unchanged. When a
target opts out (``verify_tls=False``) the client is built with an explicit
insecure :class:`ssl.SSLContext` (``check_hostname`` off, ``CERT_NONE``) so
it can reach a self-signed / internal-CA appliance (evoila/meho#1774). The
opt-out is per-target, never global, and loud — see
:func:`_insecure_ssl_context` and the WARN emitted at client construction.

**Client pool key:** the pool is keyed on
:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
(``(tenant_id, id)``) **plus** :meth:`HttpConnector.extra_cache_dimensions`
(``(verify_tls,)``), i.e. ``(tenant_id, id, verify_tls)``. Appending
``verify_tls`` keeps the ``(tenant_id, id)`` tenant-isolation prefix intact
(evoila/meho#1682/#1642) while ensuring a PATCH that flips ``verify_tls`` is
not served the stale pooled client built under the previous flag.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import Connector

logger = structlog.get_logger(__name__)

# Forward declaration — replaced with `from meho_backplane.targets import Target`
# once G0.3 lands the Target model.
type Target = Any

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _build_insecure_ssl_context() -> ssl.SSLContext:
    """Return a one-shot insecure context (verification disabled).

    Reuses the in-tree idiom (``backend/tests/acceptance/_vcsim.py``):
    start from :func:`ssl.create_default_context`, then disable
    ``check_hostname`` **before** dropping ``verify_mode`` to
    :data:`ssl.CERT_NONE`. The order is load-bearing — assigning
    ``CERT_NONE`` while ``check_hostname`` is still enabled raises
    ``ValueError`` on Python 3.12 ("Cannot set verify_mode to CERT_NONE
    when check_hostname is enabled.").
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Built once and shared across every insecure dispatch in the process.
# An ``SSLContext`` is safe to share between clients; it is never mutated
# after construction, so a single cached instance avoids rebuilding the
# context (and re-reading the default trust store it starts from) on each
# verify_tls=False target.
_INSECURE_SSL_CONTEXT = _build_insecure_ssl_context()


def _insecure_ssl_context() -> ssl.SSLContext:
    """Return the process-wide cached insecure :class:`ssl.SSLContext`."""
    return _INSECURE_SSL_CONTEXT


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
        # (``target_cache_key``) plus ``extra_cache_dimensions`` (the
        # ``(verify_tls,)`` flag), not ``target.name``: each pooled client
        # is host-bound via ``base_url``, and two tenants may legitimately
        # own same-named targets pointing at different hosts. Name-keying
        # would route the second tenant's request to the first tenant's
        # host and leak credentials across the boundary (evoila/meho#1682).
        # The ``verify_tls`` suffix keeps a PATCH that flips the flag from
        # being served the stale client built under the old value.
        self._clients: dict[tuple[str, ...], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    def extra_cache_dimensions(self, target: Target) -> tuple[object, ...]:
        """Return extra pool-key dimensions appended to ``target_cache_key``.

        The base appends the resolved ``verify_tls`` flag so a target that
        toggles TLS verification (e.g. via PATCH) gets a freshly built
        client rather than the stale one cached under the previous value —
        a client's ``verify`` is fixed at construction. The append preserves
        the ``(tenant_id, id)`` prefix, so the cross-tenant isolation
        guarantee (evoila/meho#1682/#1642) is unaffected. Subclasses that
        reach into :attr:`_clients` directly MUST build their lookup key the
        same way (``target_cache_key(target) + self.extra_cache_dimensions(target)``)
        so they index the same entry the base created.
        """
        return (bool(getattr(target, "verify_tls", True)),)

    def _client_cache_key(self, target: Target) -> tuple[str, ...]:
        """Return the full pooled-client key for *target*.

        ``target_cache_key`` (``(tenant_id, id)``) plus
        :meth:`extra_cache_dimensions` (``(verify_tls,)``). Stringified into
        a flat ``tuple[str, ...]`` so the key is hashable and a subclass that
        derives it the same way produces an identical tuple.
        """
        return target_cache_key(target) + tuple(
            str(dim) for dim in self.extra_cache_dimensions(target)
        )

    async def _http_client(self, target: Target) -> httpx.AsyncClient:
        """Return the per-target pooled client, creating it on first use."""
        cache_key = self._client_cache_key(target)
        verify_tls = bool(getattr(target, "verify_tls", True))
        async with self._lock:
            if cache_key not in self._clients:
                # Pass ``verify=`` ONLY when the target opts out of
                # verification. When ``verify_tls`` is True we omit the
                # kwarg entirely so the client defaults to httpx's
                # ``verify=True``, keeping the global ``SSL_CERT_FILE`` /
                # chart-trust-bundle path (evoila/meho#209) byte-identical
                # to a connector with no TLS opt-out at all.
                verify_kwargs: dict[str, Any] = {}
                if not verify_tls:
                    # Per-target, audited last resort (evoila/meho#1774):
                    # the dispatch forwards a Vault-resolved credential over
                    # an unverified channel, so make it loud and queryable.
                    # The audit row is written on target create/update
                    # (T1 #1780); this WARN marks the actual insecure
                    # dispatch construction.
                    logger.warning(
                        "connector_tls_verification_disabled",
                        target=getattr(target, "name", None),
                        host=getattr(target, "host", None),
                    )
                    verify_kwargs["verify"] = _insecure_ssl_context()
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
                    **verify_kwargs,
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
